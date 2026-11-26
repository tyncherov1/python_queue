from __future__ import annotations

import asyncio
import csv
import datetime as dt
import html
import io
import logging
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.config import Settings
from app.database import Database
from app.models import ActionResult, QueueEntry, Student
from app.ws_manager import WebSocketManager

logger = logging.getLogger(__name__)

NotifierFn = Callable[[int, str], Awaitable[None]]


def format_duration_ru(seconds: int) -> str:
    seconds = max(0, int(seconds))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours} ч {mins} мин"
    if mins > 0 and secs > 0:
        return f"{mins} мин {secs} сек"
    if mins > 0:
        return f"{mins} мин"
    return f"{secs} сек"


class QueueService:
    """Thread/async-safe core service shared by the Telegram bot and FastAPI whiteboard."""

    def __init__(
        self,
        db: Database,
        settings: Settings,
        ws_manager: WebSocketManager,
    ) -> None:
        self.db = db
        self.settings = settings
        self.ws_manager = ws_manager
        self._lock = asyncio.Lock()
        self._notifier: NotifierFn | None = None
        self._bot_username: str = settings.bot_username

    def set_notifier(self, notifier: NotifierFn | None) -> None:
        self._notifier = notifier

    def set_bot_username(self, username: str) -> None:
        self._bot_username = username.strip().lstrip("@")

    @property
    def bot_username(self) -> str:
        return self._bot_username

    async def _notify(self, telegram_id: int | None, text: str) -> None:
        if telegram_id is None or self._notifier is None:
            return
        try:
            await self._notifier(telegram_id, text)
        except Exception as exc:
            logger.debug("Failed to notify telegram_id=%s: %s", telegram_id, exc)

    async def _broadcast_state(self) -> dict[str, Any]:
        snapshot = await self.get_board_snapshot()
        await self.ws_manager.broadcast({"type": "state", "data": snapshot})
        return snapshot

    # ------------------------------------------------------------------
    # State Snapshot (for Whiteboard + Telegram)
    # ------------------------------------------------------------------
    async def get_board_snapshot(self) -> dict[str, Any]:
        state = await self.db.get_all_state()
        active = await self.db.get_active_entry()
        waiting = await self.db.get_waiting_entries()
        stats = await self.db.get_stats()

        now = time.time()
        timer_duration = max(10, int(float(state.get("timer_duration_sec", self.settings.default_timer_seconds))))
        timer_status = state.get("timer_status", "idle")
        stored_remaining = float(state.get("timer_remaining_sec", timer_duration))
        started_at_raw = state.get("timer_started_at", "")
        timer_started_at = float(started_at_raw) if started_at_raw else None
        timer_epoch = int(state.get("timer_epoch", "1"))

        if timer_status == "running" and timer_started_at is not None:
            effective_remaining = stored_remaining - (now - timer_started_at)
        else:
            effective_remaining = stored_remaining

        return {
            "server_now": now,
            "seminar_title": state.get("seminar_title", self.settings.seminar_title),
            "bot_username": self._bot_username,
            "bot_connected": self._notifier is not None,
            "pin_required": bool(self.settings.board_pin),
            "queue_open": state.get("queue_open", "1") == "1",
            "auto_start_timer": state.get("auto_start_timer", "0") == "1",
            "rejoin_cooldown_minutes": int(state.get("rejoin_cooldown_minutes", self.settings.rejoin_cooldown_minutes)),
            "cancel_cooldown_minutes": int(state.get("cancel_cooldown_minutes", self.settings.cancel_cooldown_minutes)),
            "max_active_slots": int(state.get("max_active_slots", self.settings.max_active_slots)),
            "board_visible_queue": int(state.get("board_visible_queue", self.settings.board_visible_queue)),
            "current": active.to_dict() if active else None,
            "queue": [entry.to_dict() for entry in waiting],
            "queue_length": len(waiting),
            "timer": {
                "status": timer_status,
                "duration_sec": timer_duration,
                "remaining_sec": round(effective_remaining, 3),
                "stored_remaining_sec": stored_remaining,
                "started_at": timer_started_at,
                "epoch": timer_epoch,
            },
            "stats": stats,
        }

    # ------------------------------------------------------------------
    # Student Queue Operations
    # ------------------------------------------------------------------
    async def check_join_eligibility(self, student: Student) -> ActionResult:
        """Validate whether a student can join right now (signup-to-signup cooldown)."""
        fresh_student = await self.db.get_student_by_id(student.id) or student
        state = await self.db.get_all_state()
        queue_open = state.get("queue_open", "1") == "1"
        if not queue_open:
            return ActionResult(
                ok=False,
                message="🔒 Запись в очередь сейчас приостановлена преподавателем.",
            )

        now = time.time()
        rejoin_cd_min = int(state.get("rejoin_cooldown_minutes", self.settings.rejoin_cooldown_minutes))
        cancel_cd_min = int(state.get("cancel_cooldown_minutes", self.settings.cancel_cooldown_minutes))
        max_slots = int(state.get("max_active_slots", self.settings.max_active_slots))

        open_entries = await self.db.get_student_open_entries(fresh_student.id)
        if max_slots > 0 and len(open_entries) >= max_slots:
            pos_parts = [
                "сейчас отвечаете" if e.status == "active" else f"#{e.position}"
                for e in open_entries
            ]
            return ActionResult(
                ok=False,
                message=(
                    f"🚫 Вы уже занимаете максимум мест в очереди (<b>{len(open_entries)} из {max_slots}</b>: "
                    f"<b>{', '.join(pos_parts)}</b>).\n"
                    "Когда подойдёт ваша очередь или вы отмените одну из записей, можно будет записаться снова."
                ),
            )

        # Check signup-to-signup cooldown (allows standing in queue twice once cooldown passes, e.g. #1 and #7)
        if rejoin_cd_min > 0 and fresh_student.last_joined_at is not None:
            elapsed = now - fresh_student.last_joined_at
            required = rejoin_cd_min * 60
            if elapsed < required:
                remaining = math.ceil(required - elapsed)
                pos_hint = ""
                if open_entries:
                    pos_parts = [
                        "сейчас отвечаете" if e.status == "active" else f"#{e.position}"
                        for e in open_entries
                    ]
                    pos_hint = f"\n📍 Ваши текущие места: <b>{', '.join(pos_parts)}</b>"
                return ActionResult(
                    ok=False,
                    message=(
                        f"⏳ Записываться можно раз в <b>{rejoin_cd_min} мин</b>.\n"
                        f"Следующая запись откроется через <b>{format_duration_ru(remaining)}</b>."
                        f"{pos_hint}"
                    ),
                    cooldown_remaining_sec=remaining,
                )

        # Check short anti-spam cooldown after voluntary cancellation
        if cancel_cd_min > 0 and fresh_student.last_left_at is not None:
            elapsed = now - fresh_student.last_left_at
            required = cancel_cd_min * 60
            if elapsed < required:
                remaining = math.ceil(required - elapsed)
                return ActionResult(
                    ok=False,
                    message=(
                        "⏳ Вы только что отказались от очереди.\n"
                        f"Повторно записаться можно через <b>{format_duration_ru(remaining)}</b>."
                    ),
                    cooldown_remaining_sec=remaining,
                )

        waiting = await self.db.get_waiting_entries()
        my_positions = [
            "сейчас отвечаете" if e.status == "active" else f"#{e.position}"
            for e in open_entries
        ]
        return ActionResult(
            ok=True,
            message="Можно записаться",
            extra={
                "waiting_count": len(waiting),
                "existing_positions": my_positions,
            },
        )

    async def _find_predecessor(self, position: int) -> tuple[str | None, bool]:
        """Return (predecessor_name, is_currently_active) for a waiting student at 1-based position."""
        waiting = await self.db.get_waiting_entries()
        if position > 1:
            for item in waiting:
                if item.position == position - 1:
                    return item.display_name, False
        active = await self.db.get_active_entry()
        if active is not None:
            return active.display_name, True
        return None, False

    async def join_queue(self, student: Student) -> ActionResult:
        async with self._lock:
            fresh_student = await self.db.get_student_by_id(student.id) or student
            check = await self.check_join_eligibility(fresh_student)
            if not check.ok:
                return check

            waiting = await self.db.get_waiting_entries()
            new_pos = len(waiting) + 1
            now = time.time()

            cur = await self.db.conn.execute(
                """
                INSERT INTO queue_entries (student_id, display_name, telegram_id, status, position, joined_at, added_by_admin)
                VALUES (?, ?, ?, 'waiting', ?, ?, 0)
                """,
                (
                    fresh_student.id,
                    fresh_student.full_name,
                    fresh_student.telegram_id,
                    new_pos,
                    now,
                ),
            )
            entry_id = int(cur.lastrowid)
            await self.db.conn.execute(
                "UPDATE students SET last_joined_at = ? WHERE id = ?",
                (now, fresh_student.id),
            )
            await self.db.conn.commit()

            entry = await self.db.get_entry_by_id(entry_id)
            after_name, after_is_active = await self._find_predecessor(new_pos)
            my_open = await self.db.get_student_open_entries(fresh_student.id)
            timer_duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            est_wait_min = max(0, round(((new_pos - 1) * timer_duration) / 60))

        await self._broadcast_state()

        if after_name:
            suffix = " (сейчас отвечает)" if after_is_active else ""
            after_line = f"👤 Вы идёте после: <b>{html.escape(after_name)}</b>{suffix}"
        else:
            after_line = "🥇 Вы первый(ая) в очереди! Приготовьтесь выходить."

        wait_line = f"\n⏱ Примерное ожидание: ~{est_wait_min} мин" if new_pos > 1 else ""
        multi_line = ""
        if len(my_open) > 1:
            all_pos = ", ".join(
                "сейчас отвечаете" if e.status == "active" else f"#{e.position}"
                for e in my_open
            )
            multi_line = f"\n📌 Все ваши места в очереди: <b>{all_pos}</b>"
        return ActionResult(
            ok=True,
            message=(
                f"✅ <b>Вы записаны в очередь!</b>\n\n"
                f"📍 Ваша позиция: <b>#{new_pos}</b>\n"
                f"{after_line}"
                f"{wait_line}"
                f"{multi_line}"
            ),
            entry=entry,
            position=new_pos,
            after_name=after_name,
            after_is_active=after_is_active,
        )

    async def leave_queue(
        self,
        student: Student,
        *,
        entry_id: int | None = None,
        leave_all: bool = False,
    ) -> ActionResult:
        async with self._lock:
            waiting_entries = await self.db.get_student_waiting_entries(student.id)
            if not waiting_entries:
                active_entries = await self.db.get_student_open_entries(student.id)
                if active_entries and active_entries[0].status == "active":
                    return ActionResult(
                        ok=False,
                        message="⚠️ Сейчас уже идёт ваш ответ у преподавателя. Попросите преподавателя нажать «Следующий человек».",
                    )
                return ActionResult(ok=False, message="ℹ️ Вас сейчас нет в очереди.")

            if leave_all:
                targets = waiting_entries
            elif entry_id is not None:
                target = next((e for e in waiting_entries if e.id == entry_id), None)
                if target is None:
                    return ActionResult(ok=False, message="ℹ️ Эта запись уже неактуальна.")
                targets = [target]
            else:
                # Default: cancel the latest waiting entry
                targets = [waiting_entries[-1]]

            now = time.time()
            removed_positions = [t.position for t in targets]
            for target in targets:
                await self.db.conn.execute(
                    "UPDATE queue_entries SET status = 'left', position = NULL, finished_at = ? WHERE id = ?",
                    (now, target.id),
                )

            # Recompute last_joined_at from remaining non-canceled signups so canceling a misclick doesn't lock out for 15 min
            prev_self_join = await self.db.get_last_self_joined_at(student.id)
            await self.db.conn.execute(
                "UPDATE students SET last_left_at = ?, last_joined_at = ? WHERE id = ?",
                (now, prev_self_join, student.id),
            )
            await self.db.renumber_waiting()
            await self.db.conn.commit()

        await self._broadcast_state()
        if len(targets) > 1:
            return ActionResult(
                ok=True,
                message=f"✅ Все ваши записи ({len(targets)} шт.) удалены из очереди.",
            )
        pos_str = f" (#{removed_positions[0]})" if removed_positions[0] else ""
        return ActionResult(
            ok=True,
            message=f"✅ Вы отказались от записи{pos_str} и были удалены из очереди.",
        )

    async def get_student_status(self, student: Student) -> ActionResult:
        fresh = await self.db.get_student_by_id(student.id) or student
        open_entries = await self.db.get_student_open_entries(fresh.id)
        if not open_entries:
            check = await self.check_join_eligibility(fresh)
            extra_hint = (
                f"\n\n{check.message}"
                if not check.ok and check.cooldown_remaining_sec > 0
                else "\n\nНажмите «📝 Записаться в очередь», чтобы встать в список."
            )
            return ActionResult(
                ok=True,
                message=f"📭 Вы сейчас не стоите в очереди.{extra_hint}",
            )

        timer_duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))

        # Single-entry clean format
        if len(open_entries) == 1:
            entry = open_entries[0]
            if entry.status == "active":
                return ActionResult(
                    ok=True,
                    message="🟢 <b>Сейчас ваша очередь!</b> Выходите к преподавателю.",
                    entry=entry,
                    position=0,
                )

            pos = entry.position or 1
            after_name, after_is_active = await self._find_predecessor(pos)
            est_wait_min = max(0, round(((pos - 1) * timer_duration) / 60))

            if after_name:
                suffix = " (сейчас отвечает)" if after_is_active else ""
                after_line = f"👤 Вы идёте после: <b>{html.escape(after_name)}</b>{suffix}"
            else:
                after_line = "🥇 Вы первый(ая) в очереди!"

            before_count = pos - 1
            lines = [
                f"📍 Ваша позиция в очереди: <b>#{pos}</b>",
                after_line,
                f"👥 Перед вами в ожидании: <b>{before_count} чел.</b>",
            ]
            if before_count > 0:
                lines.append(f"⏱ Примерное ожидание: ~{est_wait_min} мин")

            return ActionResult(
                ok=True,
                message="\n".join(lines),
                entry=entry,
                position=pos,
                after_name=after_name,
                after_is_active=after_is_active,
            )

        # Multi-entry format (student stands in queue multiple times, e.g. #1 and #7)
        lines = [f"📍 <b>Ваши места в очереди ({len(open_entries)}):</b>", ""]
        first_waiting_pos: int | None = None
        first_after_name: str | None = None
        first_after_active = False

        for entry in open_entries:
            if entry.status == "active":
                lines.append("• 🟢 <b>Сейчас ваша очередь!</b> Выходите к преподавателю.")
            else:
                pos = entry.position or 1
                after_name, after_is_active = await self._find_predecessor(pos)
                if first_waiting_pos is None:
                    first_waiting_pos = pos
                    first_after_name = after_name
                    first_after_active = after_is_active
                est_wait_min = max(0, round(((pos - 1) * timer_duration) / 60))
                if after_name:
                    suffix = " (сейчас отвечает)" if after_is_active else ""
                    after_txt = f"после: <b>{html.escape(after_name)}</b>{suffix}"
                else:
                    after_txt = "🥇 первый(ая) в очереди"
                eta_txt = f" (~{est_wait_min} мин)" if pos > 1 else ""
                lines.append(f"• <b>#{pos}</b> — {after_txt}{eta_txt}")

        return ActionResult(
            ok=True,
            message="\n".join(lines),
            entry=open_entries[0],
            position=first_waiting_pos if first_waiting_pos is not None else 0,
            after_name=first_after_name,
            after_is_active=first_after_active,
        )

    # ------------------------------------------------------------------
    # Admin Queue Manipulation (Insert Anywhere, Move, Swap, Remove)
    # ------------------------------------------------------------------
    async def admin_insert_student(
        self,
        *,
        student_id: int | None = None,
        custom_name: str | None = None,
        position: int | None = None,
    ) -> ActionResult:
        notify_target: tuple[int | None, str] | None = None

        async with self._lock:
            student: Student | None = None
            if student_id is not None:
                student = await self.db.get_student_by_id(student_id)
                if student is None:
                    return ActionResult(ok=False, message="❌ Ученик не найден в базе.")
                display_name = student.full_name
                tg_id = student.telegram_id
            else:
                cleaned = (custom_name or "").strip()
                if not cleaned:
                    return ActionResult(ok=False, message="❌ Укажите имя ученика.")
                display_name = cleaned
                tg_id = None

            waiting = await self.db.get_waiting_entries()
            ordered_ids = [item.id for item in waiting]

            if position is None:
                target_idx = len(ordered_ids)
            else:
                target_idx = max(0, min(position - 1, len(ordered_ids)))

            now = time.time()
            cur = await self.db.conn.execute(
                """
                INSERT INTO queue_entries (student_id, display_name, telegram_id, status, position, joined_at, added_by_admin)
                VALUES (?, ?, ?, 'waiting', ?, ?, 1)
                """,
                (
                    student.id if student else None,
                    display_name,
                    tg_id,
                    target_idx + 1,
                    now,
                ),
            )
            new_entry_id = int(cur.lastrowid)
            ordered_ids.insert(target_idx, new_entry_id)
            await self.db.renumber_waiting(ordered_ids)
            await self.db.conn.commit()

            entry = await self.db.get_entry_by_id(new_entry_id)
            final_pos = target_idx + 1
            after_name, after_is_active = await self._find_predecessor(final_pos)

            if tg_id:
                after_txt = (
                    f"\n👤 Вы идёте после: <b>{html.escape(after_name)}</b>" + (" (сейчас отвечает)" if after_is_active else "")
                    if after_name
                    else "\n🥇 Вы первый(ая) в очереди!"
                )
                notify_target = (
                    tg_id,
                    f"📌 Администратор добавил вас в очередь на позицию <b>#{final_pos}</b>.{after_txt}",
                )

        await self._broadcast_state()
        if notify_target:
            await self._notify(*notify_target)

        return ActionResult(
            ok=True,
            message=f"✅ <b>{html.escape(display_name)}</b> добавлен(а) на позицию <b>#{final_pos}</b>.",
            entry=entry,
            position=final_pos,
        )

    async def admin_move_entry(self, entry_id: int, new_position: int) -> ActionResult:
        notify_target: tuple[int | None, str] | None = None

        async with self._lock:
            waiting = await self.db.get_waiting_entries()
            ordered_ids = [item.id for item in waiting]
            if entry_id not in ordered_ids:
                return ActionResult(ok=False, message="❌ Запись не найдена в ожидающей очереди.")

            entry = next(item for item in waiting if item.id == entry_id)
            old_pos = ordered_ids.index(entry_id) + 1
            ordered_ids.remove(entry_id)
            clamped_idx = max(0, min(new_position - 1, len(ordered_ids)))
            ordered_ids.insert(clamped_idx, entry_id)
            final_pos = clamped_idx + 1

            await self.db.renumber_waiting(ordered_ids)
            await self.db.conn.commit()

            if entry.telegram_id and final_pos != old_pos:
                after_name, after_is_active = await self._find_predecessor(final_pos)
                after_txt = (
                    f"\n👤 Теперь вы после: <b>{html.escape(after_name)}</b>" + (" (сейчас отвечает)" if after_is_active else "")
                    if after_name
                    else "\n🥇 Теперь вы первый(ая) в очереди!"
                )
                notify_target = (
                    entry.telegram_id,
                    f"↕️ Администратор изменил вашу позицию: <b>#{old_pos} → #{final_pos}</b>.{after_txt}",
                )

        await self._broadcast_state()
        if notify_target:
            await self._notify(*notify_target)

        return ActionResult(
            ok=True,
            message=f"✅ <b>{html.escape(entry.display_name)}</b> перемещён(а): #{old_pos} → #{final_pos}.",
            position=final_pos,
        )

    async def admin_swap_positions(self, pos_a: int, pos_b: int) -> ActionResult:
        notifications: list[tuple[int | None, str]] = []

        async with self._lock:
            waiting = await self.db.get_waiting_entries()
            if pos_a == pos_b or not (1 <= pos_a <= len(waiting)) or not (1 <= pos_b <= len(waiting)):
                return ActionResult(
                    ok=False,
                    message=f"❌ Некорректные позиции. Доступны позиции от 1 до {len(waiting)}.",
                )

            ordered = list(waiting)
            idx_a, idx_b = pos_a - 1, pos_b - 1
            ordered[idx_a], ordered[idx_b] = ordered[idx_b], ordered[idx_a]
            await self.db.renumber_waiting([item.id for item in ordered])
            await self.db.conn.commit()

            first = ordered[idx_a]
            second = ordered[idx_b]
            if first.telegram_id:
                notifications.append((first.telegram_id, f"🔄 Администратор поменял вас местами в очереди. Ваша новая позиция: <b>#{pos_a}</b>."))
            if second.telegram_id:
                notifications.append((second.telegram_id, f"🔄 Администратор поменял вас местами в очереди. Ваша новая позиция: <b>#{pos_b}</b>."))

        await self._broadcast_state()
        for tg_id, text in notifications:
            await self._notify(tg_id, text)

        return ActionResult(
            ok=True,
            message=f"✅ Поменяли местами: <b>{html.escape(second.display_name)}</b> (#{pos_b}) ⇄ <b>{html.escape(first.display_name)}</b> (#{pos_a}).",
        )

    async def admin_remove_entry(self, entry_id: int) -> ActionResult:
        notify_target: tuple[int | None, str] | None = None

        async with self._lock:
            entry = await self.db.get_entry_by_id(entry_id)
            if entry is None or entry.status != "waiting":
                return ActionResult(ok=False, message="❌ Запись не найдена в ожидающей очереди.")

            now = time.time()
            await self.db.conn.execute(
                "UPDATE queue_entries SET status = 'removed', position = NULL, finished_at = ? WHERE id = ?",
                (now, entry_id),
            )
            await self.db.renumber_waiting()
            await self.db.conn.commit()

            if entry.telegram_id:
                notify_target = (
                    entry.telegram_id,
                    "🗑 Администратор удалил вас из текущей очереди.",
                )

        await self._broadcast_state()
        if notify_target:
            await self._notify(*notify_target)

        return ActionResult(
            ok=True,
            message=f"✅ <b>{html.escape(entry.display_name)}</b> удалён(а) из очереди.",
        )

    async def clear_queue(self, reset_cooldowns: bool = False) -> ActionResult:
        async with self._lock:
            now = time.time()
            await self.db.conn.execute(
                """
                UPDATE queue_entries
                SET status = 'removed', position = NULL, finished_at = ?
                WHERE status IN ('waiting', 'active')
                """,
                (now,),
            )
            if reset_cooldowns:
                await self.db.conn.execute(
                    "UPDATE students SET last_joined_at = NULL, last_served_at = NULL, last_left_at = NULL"
                )
            duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            epoch = int(await self.db.get_state("timer_epoch", "1")) + 1
            await self.db.set_state(
                {
                    "timer_status": "idle",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": "",
                    "timer_epoch": epoch,
                },
                commit=False,
            )
            await self.db.conn.commit()

        await self._broadcast_state()
        suffix = " Кулдауны сброшены." if reset_cooldowns else ""
        return ActionResult(ok=True, message=f"🧹 Очередь полностью очищена.{suffix}")

    # ------------------------------------------------------------------
    # Seminar Flow: Call Next, Skip, Finish, Timer Controls
    # ------------------------------------------------------------------
    async def call_next(self, mark_current_as: str = "done") -> ActionResult:
        """Finish/skip current student and pull #1 waiting student onto the stage."""
        notifications: list[tuple[int | None, str]] = []

        async with self._lock:
            now = time.time()
            active = await self.db.get_active_entry()
            if active is not None:
                final_status = "skipped" if mark_current_as == "skipped" else "done"
                await self.db.conn.execute(
                    "UPDATE queue_entries SET status = ?, finished_at = ? WHERE id = ?",
                    (final_status, now, active.id),
                )
                if active.student_id is not None:
                    if final_status == "done":
                        await self.db.conn.execute(
                            """
                            UPDATE students
                            SET last_served_at = ?, visits_count = visits_count + 1
                            WHERE id = ?
                            """,
                            (now, active.student_id),
                        )
                    else:
                        # Skipped student gets short cancel cooldown instead of full answer cooldown
                        await self.db.conn.execute(
                            "UPDATE students SET last_left_at = ? WHERE id = ?",
                            (now, active.student_id),
                        )
                        if active.telegram_id:
                            notifications.append(
                                (
                                    active.telegram_id,
                                    "⏩ Преподаватель пропустил вашу очередь (вы не подошли). Вы сможете записаться снова позже.",
                                )
                            )

            waiting = await self.db.get_waiting_entries()
            next_entry: QueueEntry | None = None
            upcoming_entry: QueueEntry | None = None

            if waiting:
                next_entry = waiting[0]
                await self.db.conn.execute(
                    """
                    UPDATE queue_entries
                    SET status = 'active', position = NULL, started_at = ?
                    WHERE id = ?
                    """,
                    (now, next_entry.id),
                )
                remaining_ids = [item.id for item in waiting[1:]]
                await self.db.renumber_waiting(remaining_ids)
                if len(waiting) > 1:
                    upcoming_entry = waiting[1]

            # Reset timer for the new student (start only if auto_start_timer is enabled)
            duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            auto_start = await self.db.get_state("auto_start_timer", "0") == "1"
            epoch = int(await self.db.get_state("timer_epoch", "1")) + 1

            if next_entry is not None and auto_start:
                timer_updates = {
                    "timer_status": "running",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": now,
                    "timer_epoch": epoch,
                }
            else:
                timer_updates = {
                    "timer_status": "idle",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": "",
                    "timer_epoch": epoch,
                }

            await self.db.set_state(timer_updates, commit=False)
            await self.db.conn.commit()

            if next_entry and next_entry.telegram_id:
                notifications.append(
                    (
                        next_entry.telegram_id,
                        "🔔 <b>Ваша очередь!</b>\nВыходите к преподавателю.",
                    )
                )
            if upcoming_entry and upcoming_entry.telegram_id and next_entry:
                notifications.append(
                    (
                        upcoming_entry.telegram_id,
                        f"⏳ <b>Приготовьтесь!</b> Вы следующий(ая) в очереди (сразу после: <b>{html.escape(next_entry.display_name)}</b>).",
                    )
                )

        await self._broadcast_state()
        for tg_id, text in notifications:
            await self._notify(tg_id, text)

        if next_entry is not None:
            return ActionResult(
                ok=True,
                message=f"⏭ Вызван(а): <b>{html.escape(next_entry.display_name)}</b>.",
                entry=next_entry,
            )
        if active is not None:
            return ActionResult(
                ok=True,
                message=f"✅ Ответ <b>{html.escape(active.display_name)}</b> завершён. В очереди больше никого нет.",
            )
        return ActionResult(ok=False, message="📭 Очередь пуста — вызывать пока некого.")

    async def finish_current(self) -> ActionResult:
        """Finish current student without immediately calling the next one."""
        async with self._lock:
            active = await self.db.get_active_entry()
            if active is None:
                return ActionResult(ok=False, message="ℹ️ Сейчас никто не отвечает.")
            now = time.time()
            await self.db.conn.execute(
                "UPDATE queue_entries SET status = 'done', finished_at = ? WHERE id = ?",
                (now, active.id),
            )
            if active.student_id is not None:
                await self.db.conn.execute(
                    """
                    UPDATE students
                    SET last_served_at = ?, visits_count = visits_count + 1
                    WHERE id = ?
                    """,
                    (now, active.student_id),
                )
            duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            epoch = int(await self.db.get_state("timer_epoch", "1")) + 1
            await self.db.set_state(
                {
                    "timer_status": "idle",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": "",
                    "timer_epoch": epoch,
                },
                commit=False,
            )
            await self.db.conn.commit()

        await self._broadcast_state()
        return ActionResult(
            ok=True,
            message=f"✅ Ответ <b>{html.escape(active.display_name)}</b> завершён.",
        )

    # ------------------------------------------------------------------
    # Timer Controls
    # ------------------------------------------------------------------
    async def timer_start(self) -> dict[str, Any]:
        async with self._lock:
            active = await self.db.get_active_entry()
            if active is None:
                raise ValueError("Нельзя запустить таймер: сейчас никто не вызван из очереди")
            state = await self.db.get_all_state()
            status = state.get("timer_status", "idle")
            if status != "running":
                duration = float(state.get("timer_duration_sec", self.settings.default_timer_seconds))
                remaining = float(state.get("timer_remaining_sec", duration))
                if status == "idle":
                    remaining = duration
                epoch = int(state.get("timer_epoch", "1")) + (1 if status == "idle" else 0)
                await self.db.set_state(
                    {
                        "timer_status": "running",
                        "timer_remaining_sec": remaining,
                        "timer_started_at": time.time(),
                        "timer_epoch": epoch,
                    }
                )
        return await self._broadcast_state()

    async def timer_pause(self) -> dict[str, Any]:
        async with self._lock:
            state = await self.db.get_all_state()
            if state.get("timer_status") == "running":
                now = time.time()
                started_at = float(state.get("timer_started_at") or now)
                stored_rem = float(state.get("timer_remaining_sec", self.settings.default_timer_seconds))
                new_remaining = stored_rem - (now - started_at)
                await self.db.set_state(
                    {
                        "timer_status": "paused",
                        "timer_remaining_sec": new_remaining,
                        "timer_started_at": "",
                    }
                )
        return await self._broadcast_state()

    async def timer_reset(self) -> dict[str, Any]:
        async with self._lock:
            duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            epoch = int(await self.db.get_state("timer_epoch", "1")) + 1
            await self.db.set_state(
                {
                    "timer_status": "idle",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": "",
                    "timer_epoch": epoch,
                }
            )
        return await self._broadcast_state()

    async def timer_add_seconds(self, delta_sec: int = 60) -> dict[str, Any]:
        async with self._lock:
            state = await self.db.get_all_state()
            status = state.get("timer_status", "idle")
            now = time.time()
            stored_rem = float(state.get("timer_remaining_sec", self.settings.default_timer_seconds))
            epoch = int(state.get("timer_epoch", "1"))

            if status == "running":
                started_at = float(state.get("timer_started_at") or now)
                current_rem = stored_rem - (now - started_at)
                # If already in overtime, add delta from 0 so "+1 мин" gives a clean full minute
                new_rem = (delta_sec if current_rem < 0 and delta_sec > 0 else current_rem + delta_sec)
                if current_rem <= 0 < new_rem:
                    epoch += 1
                await self.db.set_state(
                    {
                        "timer_remaining_sec": max(0.0, new_rem),
                        "timer_started_at": now,
                        "timer_epoch": epoch,
                    }
                )
            else:
                new_rem = max(0.0, (delta_sec if stored_rem < 0 and delta_sec > 0 else stored_rem + delta_sec))
                if stored_rem <= 0 < new_rem:
                    epoch += 1
                await self.db.set_state(
                    {
                        "timer_remaining_sec": new_rem,
                        "timer_epoch": epoch,
                    }
                )
        return await self._broadcast_state()

    # ------------------------------------------------------------------
    # Dynamic Settings Updates (Admin Bot + Whiteboard Settings Modal)
    # ------------------------------------------------------------------
    async def update_settings(
        self,
        *,
        seminar_title: str | None = None,
        timer_duration_sec: int | None = None,
        rejoin_cooldown_minutes: int | None = None,
        cancel_cooldown_minutes: int | None = None,
        max_active_slots: int | None = None,
        queue_open: bool | None = None,
        auto_start_timer: bool | None = None,
        board_visible_queue: int | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            state = await self.db.get_all_state()
            updates: dict[str, Any] = {}

            if seminar_title is not None and seminar_title.strip():
                updates["seminar_title"] = seminar_title.strip()[:120]

            if timer_duration_sec is not None:
                clamped = max(10, min(7200, int(timer_duration_sec)))
                updates["timer_duration_sec"] = clamped
                # If timer is idle, immediately show the updated duration on the board
                if state.get("timer_status", "idle") == "idle":
                    updates["timer_remaining_sec"] = float(clamped)
                    updates["timer_epoch"] = int(state.get("timer_epoch", "1")) + 1

            if rejoin_cooldown_minutes is not None:
                updates["rejoin_cooldown_minutes"] = max(0, min(1440, int(rejoin_cooldown_minutes)))

            if cancel_cooldown_minutes is not None:
                updates["cancel_cooldown_minutes"] = max(0, min(120, int(cancel_cooldown_minutes)))

            if max_active_slots is not None:
                updates["max_active_slots"] = max(0, min(20, int(max_active_slots)))

            if queue_open is not None:
                updates["queue_open"] = "1" if queue_open else "0"

            if auto_start_timer is not None:
                updates["auto_start_timer"] = "1" if auto_start_timer else "0"

            if board_visible_queue is not None:
                updates["board_visible_queue"] = max(1, min(32, int(board_visible_queue)))

            if updates:
                await self.db.set_state(updates)

        return await self._broadcast_state()

    async def undo_last_call(self) -> ActionResult:
        """Undo an accidental 'Next' tap: put current speaker back at #1 and restore previous speaker."""
        async with self._lock:
            active = await self.db.get_active_entry()
            last_done = await self.db.get_last_done_entry()

            if active is None and last_done is None:
                return ActionResult(ok=False, message="ℹ️ Нечего отменять.")

            waiting = await self.db.get_waiting_entries()
            ordered_ids = [item.id for item in waiting]

            # Move current active back to the front of waiting (#1)
            if active is not None:
                await self.db.conn.execute(
                    "UPDATE queue_entries SET status = 'waiting', position = 1, started_at = NULL WHERE id = ?",
                    (active.id,),
                )
                ordered_ids.insert(0, active.id)
                await self.db.renumber_waiting(ordered_ids)

            restored_name: str | None = None
            if last_done is not None:
                was_done = last_done.status == "done"
                await self.db.conn.execute(
                    "UPDATE queue_entries SET status = 'active', position = NULL, finished_at = NULL WHERE id = ?",
                    (last_done.id,),
                )
                if was_done and last_done.student_id is not None:
                    await self.db.conn.execute(
                        "UPDATE students SET visits_count = MAX(0, visits_count - 1) WHERE id = ?",
                        (last_done.student_id,),
                    )
                restored_name = last_done.display_name

            duration = int(float(await self.db.get_state("timer_duration_sec", str(self.settings.default_timer_seconds))))
            epoch = int(await self.db.get_state("timer_epoch", "1")) + 1
            await self.db.set_state(
                {
                    "timer_status": "idle",
                    "timer_remaining_sec": float(duration),
                    "timer_started_at": "",
                    "timer_epoch": epoch,
                },
                commit=False,
            )
            await self.db.conn.commit()

        await self._broadcast_state()
        if restored_name:
            return ActionResult(
                ok=True,
                message=f"↩️ Возвращён предыдущий ученик: <b>{html.escape(restored_name)}</b>.",
            )
        return ActionResult(
            ok=True,
            message="↩️ Вызов отменён — ученик возвращён на позицию #1.",
        )

    async def export_history_csv(self) -> bytes:
        """Generate an Excel-friendly UTF-8-BOM semicolon CSV report of the seminar queue."""
        entries = await self.db.get_history_entries()
        status_labels = {
            "done": "Ответил(а)",
            "active": "Сейчас отвечает",
            "waiting": "В очереди",
            "left": "Вышел(а) сам(а)",
            "skipped": "Пропущен(а)",
            "removed": "Удалён(а)",
        }

        def _fmt_ts(ts: float | None) -> str:
            if ts is None:
                return ""
            return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")

        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=";")
        writer.writerow(
            ["№", "Ученик", "Статус", "Записался", "Начал отвечать", "Завершил", "Длительность (сек)"]
        )
        for idx, e in enumerate(entries, start=1):
            dur = (
                round(e.finished_at - e.started_at)
                if e.started_at is not None and e.finished_at is not None
                else ""
            )
            writer.writerow(
                [
                    idx,
                    e.display_name,
                    status_labels.get(e.status, e.status),
                    _fmt_ts(e.joined_at),
                    _fmt_ts(e.started_at),
                    _fmt_ts(e.finished_at),
                    dur,
                ]
            )
        return ("\ufeff" + buf.getvalue()).encode("utf-8")

    async def broadcast_announcement(self, text: str, scope: str = "queue") -> int:
        """Send an admin announcement via Telegram to everyone in queue or the full roster."""
        cleaned = text.strip()
        if not cleaned or self._notifier is None:
            return 0
        recipients = await self.db.list_notifiable_telegram_ids(scope=scope)
        payload = f"📢 <b>Объявление преподавателя:</b>\n\n{html.escape(cleaned)}"
        sent = 0
        for tg_id in recipients:
            try:
                await self._notifier(tg_id, payload)
                sent += 1
            except Exception as exc:
                logger.debug("Broadcast failed for tg_id=%s: %s", tg_id, exc)
        return sent

    async def notify_admins(self, text: str) -> None:
        """Send a security or system alert to all configured admins via Telegram."""
        if self._notifier is None:
            return
        admin_tg_ids: set[int] = set(self.settings.admin_ids)
        try:
            cur = await self.db.conn.execute(
                "SELECT telegram_id FROM students WHERE is_admin = 1 AND active_in_roster = 1 AND telegram_id IS NOT NULL"
            )
            for row in await cur.fetchall():
                admin_tg_ids.add(int(row["telegram_id"]))
        except Exception as exc:
            logger.debug("Failed to query admin IDs: %s", exc)

        for tg_id in admin_tg_ids:
            await self._notify(tg_id, text)

    async def reload_roster(self) -> int:
        async with self._lock:
            count = await self.db.sync_students_from_csv()
        await self._broadcast_state()
        return count
