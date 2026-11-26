from __future__ import annotations

import asyncio
import hashlib
import hmac
import math
import secrets
import time
from collections import deque
from dataclasses import dataclass, field

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_client_ip(request: Request) -> str:
    """
    Extract real client IP safely.
    Only trusts proxy headers (CF-Connecting-IP / X-Forwarded-For) when the direct
    peer is loopback (e.g. local cloudflared / nginx reverse proxy), preventing
    LAN attackers from spoofing headers to bypass lockouts.
    """
    peer = request.client.host if request.client else "unknown"
    if peer in LOOPBACK_HOSTS:
        cf_ip = request.headers.get("cf-connecting-ip", "").strip()
        if cf_ip:
            return cf_ip[:64]
        xff = request.headers.get("x-forwarded-for", "").strip()
        if xff:
            return xff.split(",")[0].strip()[:64]
    return peer[:64]


@dataclass
class _AttemptRecord:
    failures: deque[float] = field(default_factory=deque)
    locked_until: float = 0.0
    lockout_count: int = 0


class PinBruteForceGuard:
    """
    Protects board authentication against password guessing / brute-force attacks:
    - Constant-time PIN comparison (prevents timing attacks)
    - Progressive artificial delay on each wrong password
    - Per-IP 15-minute lockout after N failed attempts (default 5)
    - Global distributed-attack shield (if many IPs guess simultaneously)
    - Signed HMAC-SHA256 session tokens so the plaintext password is not stored in browser storage
    """

    def __init__(
        self,
        *,
        max_attempts: int = 5,
        lockout_minutes: int = 15,
        attempt_window_seconds: int = 900,
        global_max_failures: int = 25,
        global_window_seconds: int = 300,
        token_ttl_seconds: int = 12 * 3600,
        failure_delay_step: float = 0.4,
    ) -> None:
        self.max_attempts = max(1, max_attempts)
        self.lockout_seconds = max(60, lockout_minutes * 60)
        self.attempt_window_seconds = max(60, attempt_window_seconds)
        self.global_max_failures = max(10, global_max_failures)
        self.global_window_seconds = max(60, global_window_seconds)
        self.token_ttl_seconds = token_ttl_seconds
        self.failure_delay_step = max(0.0, failure_delay_step)

        self._signing_secret = secrets.token_bytes(32)
        self._records: dict[str, _AttemptRecord] = {}
        self._global_failures: deque[float] = deque()
        self._global_locked_until: float = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def verify_pin_constant_time(candidate: str, expected: str) -> bool:
        if not expected:
            return True
        return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))

    def _pin_fingerprint(self, expected_pin: str) -> str:
        return hashlib.sha256(expected_pin.encode("utf-8")).hexdigest()[:16]

    def create_session_token(self, expected_pin: str) -> str:
        """Create a signed token bound to the current PIN and expiration timestamp."""
        exp = int(time.time()) + self.token_ttl_seconds
        fp = self._pin_fingerprint(expected_pin)
        nonce = secrets.token_hex(8)
        payload = f"{exp}.{fp}.{nonce}"
        sig = hmac.new(self._signing_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{sig}"

    def verify_session_token(self, token: str, expected_pin: str) -> bool:
        if not expected_pin:
            return True
        if not token:
            return False
        parts = token.strip().split(".")
        if len(parts) != 4:
            return False
        exp_str, fp, nonce, sig = parts
        if not exp_str.isdigit():
            return False
        if int(exp_str) < int(time.time()):
            return False
        if not hmac.compare_digest(fp, self._pin_fingerprint(expected_pin)):
            return False
        payload = f"{exp_str}.{fp}.{nonce}"
        expected_sig = hmac.new(
            self._signing_secret, payload.encode("utf-8"), hashlib.sha256
        ).hexdigest()[:32]
        return hmac.compare_digest(sig, expected_sig)

    async def check_lockout(self, ip: str) -> int:
        """Return remaining lockout seconds for this IP (or global shield), or 0 if not locked."""
        async with self._lock:
            now = time.time()
            rem = 0.0
            if self._global_locked_until > now:
                rem = max(rem, self._global_locked_until - now)
            rec = self._records.get(ip)
            if rec is not None and rec.locked_until > now:
                rem = max(rem, rec.locked_until - now)
            return math.ceil(rem) if rem > 0 else 0

    async def record_failure(self, ip: str) -> tuple[bool, int, int]:
        """
        Record a failed PIN attempt for IP.
        Returns (just_locked, attempts_left, retry_after_sec).
        Also applies progressive artificial delay to throttle scripts.
        """
        delay = 0.0
        just_locked = False
        attempts_left = 0
        retry_after_sec = 0

        async with self._lock:
            now = time.time()
            rec = self._records.setdefault(ip, _AttemptRecord())

            # Prune expired failure timestamps
            cutoff = now - self.attempt_window_seconds
            while rec.failures and rec.failures[0] < cutoff:
                rec.failures.popleft()

            global_cutoff = now - self.global_window_seconds
            while self._global_failures and self._global_failures[0] < global_cutoff:
                self._global_failures.popleft()

            rec.failures.append(now)
            self._global_failures.append(now)

            fail_count = len(rec.failures)
            delay = min(2.5, fail_count * self.failure_delay_step)

            if fail_count >= self.max_attempts:
                rec.lockout_count += 1
                rec.failures.clear()
                rec.locked_until = now + self.lockout_seconds
                just_locked = True
                attempts_left = 0
                retry_after_sec = self.lockout_seconds
            else:
                attempts_left = self.max_attempts - fail_count

            # Global distributed brute-force shield
            if len(self._global_failures) >= self.global_max_failures:
                self._global_failures.clear()
                self._global_locked_until = max(self._global_locked_until, now + self.lockout_seconds)
                just_locked = True
                attempts_left = 0
                retry_after_sec = max(retry_after_sec, self.lockout_seconds)

            # Prevent unbounded memory growth from random IPs
            if len(self._records) > 2000:
                stale = [k for k, v in self._records.items() if v.locked_until <= now and not v.failures]
                for k in stale[:1000]:
                    self._records.pop(k, None)

        if delay > 0:
            await asyncio.sleep(delay)

        return just_locked, attempts_left, retry_after_sec

    async def record_success(self, ip: str) -> None:
        async with self._lock:
            self._records.pop(ip, None)


class SlidingWindowRateLimiter:
    """Lightweight per-IP rate limiter protecting API endpoints against request floods."""

    def __init__(self, max_requests: int = 180, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = asyncio.Lock()

    async def allow(self, ip: str) -> tuple[bool, int]:
        async with self._lock:
            now = time.time()
            q = self._hits.setdefault(ip, deque())
            cutoff = now - self.window_seconds
            while q and q[0] < cutoff:
                q.popleft()

            if len(q) >= self.max_requests:
                retry_after = max(1, math.ceil(q[0] + self.window_seconds - now))
                return False, retry_after

            q.append(now)

            if len(self._hits) > 2000:
                stale = [k for k, v in self._hits.items() if not v]
                for k in stale[:1000]:
                    self._hits.pop(k, None)

            return True, 0


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Adds defensive HTTP security headers (XSS, clickjacking, MIME sniffing, Referer leak)."""

    CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss:; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )

    async def dispatch(self, request: Request, call_next) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response
