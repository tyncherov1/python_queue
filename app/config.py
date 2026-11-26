from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent


def _parse_int_set(raw: str) -> set[int]:
    result: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        cleaned = part.strip()
        if cleaned and cleaned.lstrip("-").isdigit():
            result.add(int(cleaned))
    return result


def _parse_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def _parse_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return max(minimum, float(raw))
    except ValueError:
        return default


@dataclass
class Settings:
    bot_token: str = ""
    bot_username: str = ""
    bot_mode: str = "polling"  # "polling" or "webhook"
    public_url: str = ""
    webhook_secret: str = ""
    admin_ids: set[int] = field(default_factory=set)

    seminar_title: str = "Семинар • Очередь к преподавателю"
    default_timer_minutes: float = 7.0
    rejoin_cooldown_minutes: int = 15
    cancel_cooldown_minutes: int = 2
    board_visible_queue: int = 7
    board_pin: str = ""

    students_csv: Path = field(default_factory=lambda: BASE_DIR / "students.csv")
    db_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "queue.db")
    host: str = "0.0.0.0"
    port: int = 8000
    timezone: str = "Europe/Moscow"

    @property
    def default_timer_seconds(self) -> int:
        return max(10, int(round(self.default_timer_minutes * 60)))

    @property
    def resolved_webhook_secret(self) -> str:
        if self.webhook_secret:
            return self.webhook_secret
        if not self.bot_token:
            return "seminar-queue-secret"
        return hashlib.sha256(f"wh:{self.bot_token}".encode("utf-8")).hexdigest()[:32]

    @property
    def webhook_url(self) -> str:
        base = self.public_url.rstrip("/")
        return f"{base}/webhook/telegram" if base else ""


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(env_path or (BASE_DIR / ".env"), override=False)

    csv_raw = os.getenv("STUDENTS_CSV", "students.csv").strip() or "students.csv"
    csv_path = Path(csv_raw)
    if not csv_path.is_absolute():
        csv_path = BASE_DIR / csv_path

    db_raw = os.getenv("DB_PATH", "data/queue.db").strip() or "data/queue.db"
    db_path = Path(db_raw)
    if not db_path.is_absolute():
        db_path = BASE_DIR / db_path

    # Render / Koyeb / Railway / HF Spaces automatically pass PORT env var
    port = _parse_int("PORT", 8000, minimum=1)

    bot_mode = os.getenv("BOT_MODE", "polling").strip().lower()
    if bot_mode not in {"polling", "webhook"}:
        bot_mode = "polling"

    # Render sets RENDER_EXTERNAL_URL automatically if PUBLIC_URL isn't set
    public_url = (
        os.getenv("PUBLIC_URL", "").strip()
        or os.getenv("RENDER_EXTERNAL_URL", "").strip()
    )

    return Settings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        bot_username=os.getenv("BOT_USERNAME", "").strip().lstrip("@"),
        bot_mode=bot_mode,
        public_url=public_url,
        webhook_secret=os.getenv("WEBHOOK_SECRET", "").strip(),
        admin_ids=_parse_int_set(os.getenv("ADMIN_IDS", "")),
        seminar_title=(
            os.getenv("SEMINAR_TITLE", "").strip()
            or "Семинар • Очередь к преподавателю"
        ),
        default_timer_minutes=_parse_float("DEFAULT_TIMER_MINUTES", 7.0, minimum=0.5),
        rejoin_cooldown_minutes=_parse_int("REJOIN_COOLDOWN_MINUTES", 15, minimum=0),
        cancel_cooldown_minutes=_parse_int("CANCEL_COOLDOWN_MINUTES", 2, minimum=0),
        board_visible_queue=_parse_int("BOARD_VISIBLE_QUEUE", 7, minimum=1),
        board_pin=os.getenv("BOARD_PIN", "").strip(),
        students_csv=csv_path,
        db_path=db_path,
        host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=port,
        timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip() or "Europe/Moscow",
    )
