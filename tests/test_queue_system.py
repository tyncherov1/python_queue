from __future__ import annotations

import time
from pathlib import Path

import pytest
from aiogram.types import Chat, Message, User
from fastapi.testclient import TestClient

from app.bot.middleware import WhitelistMiddleware
from app.config import BASE_DIR, Settings
from app.database import Database
from app.main import create_app
from app.queue_service import QueueService
from app.ws_manager import WebSocketManager


def _write_test_csv(tmp_path: Path) -> Path:
    csv_path = tmp_path / "test_students.csv"
    lines = ["id,full_name,telegram_id,username,role"]
    sample_names = [
        "Абрамов Артём Сергеевич",
        "Алексеева Дарья Игоревна",
        "Белов Максим Андреевич",
        "Богданова Анна Дмитриевна",
    ]
    for i in range(1, 33):
        name = sample_names[i - 1] if i <= len(sample_names) else f"Ученик Тестовый {i:02d}"
        lines.append(f"{i},{name},,student_{i:02d},student")
    csv_path.write_bytes(("\ufeff" + "\n".join(lines) + "\n").encode("utf-8"))
    return csv_path


def _make_settings(tmp_path: Path, **overrides) -> Settings:
    csv_path = overrides.pop("students_csv", None) or _write_test_csv(tmp_path)
    defaults = dict(
        bot_token="",
        bot_username="test_seminar_bot",
        admin_ids={999001},
        default_timer_minutes=7.0,
        rejoin_cooldown_minutes=15,
        cancel_cooldown_minutes=2,
        board_pin="",
        students_csv=csv_path,
        db_path=tmp_path / "test_queue.db",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.mark.asyncio
async def test_csv_loads_32_students_and_supports_semicolon_excel(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    db = Database(cfg)
    await db.connect()
    try:
        count = await db.sync_students_from_csv()
        assert count == 32
        roster = await db.list_roster_students()
        assert len(roster) == 32

        # Also verify Russian Excel semicolon CSV with BOM
        semi_csv = tmp_path / "excel_students.csv"
        semi_csv.write_bytes(
            "\ufeffФИО;telegram_id;username;role\n"
            "Тестов Тест Тестович;;test_user;student\n"
            "Преподаватель Главный;555777;;admin\n".encode("utf-8")
        )
        count_semi = await db.sync_students_from_csv(semi_csv)
        assert count_semi == 2
        resolved = await db.resolve_telegram_user(123456, "TEST_USER")
        assert resolved is not None
        assert resolved.full_name == "Тестов Тест Тестович"
        assert resolved.telegram_id == 123456  # auto-bound!
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_strict_silent_whitelist_middleware(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    db = Database(cfg)
    await db.connect()
    await db.sync_students_from_csv()
    svc = QueueService(db=db, settings=cfg, ws_manager=WebSocketManager())
    middleware = WhitelistMiddleware(svc)

    called_with: list[dict] = []

    async def dummy_handler(event, data):
        called_with.append(data)
        return "HANDLED"

    try:
        # 1. Stranger NOT in students.csv and NOT in ADMIN_IDS -> completely ignored!
        stranger_msg = Message(
            message_id=1,
            date=int(time.time()),
            chat=Chat(id=777777, type="private"),
            from_user=User(id=777777, is_bot=False, first_name="Stranger", username="random_guy"),
            text="/start",
        )
        result = await middleware(dummy_handler, stranger_msg, {})
        assert result is None
        assert len(called_with) == 0

        # 2. Whitelisted student matched by @username -> allowed & bound!
        student_msg = Message(
            message_id=2,
            date=int(time.time()),
            chat=Chat(id=100001, type="private"),
            from_user=User(id=100001, is_bot=False, first_name="Artyom", username="student_01"),
            text="/start",
        )
        result_ok = await middleware(dummy_handler, student_msg, {})
        assert result_ok == "HANDLED"
        assert len(called_with) == 1
        assert called_with[0]["student"].telegram_id == 100001

        # 3. Admin from ADMIN_IDS (not in CSV) -> allowed as admin!
        admin_msg = Message(
            message_id=3,
            date=int(time.time()),
            chat=Chat(id=999001, type="private"),
            from_user=User(id=999001, is_bot=False, first_name="Prof", username="prof_admin"),
            text="/admin",
        )
        result_adm = await middleware(dummy_handler, admin_msg, {})
        assert result_adm == "HANDLED"
        assert called_with[1]["student"].is_admin is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_queue_join_leave_predecessor_and_signup_to_signup_cooldown(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path, rejoin_cooldown_minutes=15, cancel_cooldown_minutes=2)
    db = Database(cfg)
    await db.connect()
    await db.sync_students_from_csv()
    svc = QueueService(db=db, settings=cfg, ws_manager=WebSocketManager())

    sent_notifications: list[tuple[int, str]] = []

    async def fake_notifier(tg_id: int, text: str) -> None:
        sent_notifications.append((tg_id, text))

    svc.set_notifier(fake_notifier)

    try:
        st1 = await db.resolve_telegram_user(101, "student_01")
        st2 = await db.resolve_telegram_user(102, "student_02")
        st3 = await db.resolve_telegram_user(103, "student_03")
        assert st1 and st2 and st3

        # Student 1 joins -> position #1
        r1 = await svc.join_queue(st1)
        assert r1.ok and r1.position == 1
        assert r1.after_name is None

        # Immediate second join is blocked by 15-min signup-to-signup cooldown
        r1_too_soon = await svc.join_queue(st1)
        assert not r1_too_soon.ok
        assert r1_too_soon.cooldown_remaining_sec > 800

        # Student 2 & 3 join -> positions #2, #3
        r2 = await svc.join_queue(st2)
        assert r2.ok and r2.position == 2
        assert r2.after_name == st1.full_name
        assert st1.full_name in r2.message

        r3 = await svc.join_queue(st3)
        assert r3.ok and r3.position == 3
        assert r3.after_name == st2.full_name

        # Simulate 16 minutes passing since st1's FIRST signup while st1 is STILL waiting at #1!
        await db.conn.execute(
            "UPDATE students SET last_joined_at = ? WHERE id = ?",
            (time.time() - 16 * 60, st1.id),
        )
        await db.conn.commit()

        # Student 1 CAN now stand in the queue TWICE (#1 and #4)!
        r1_second_slot = await svc.join_queue(st1)
        assert r1_second_slot.ok
        assert r1_second_slot.position == 4
        assert r1_second_slot.after_name == st3.full_name

        # Status shows BOTH positions (#1 and #4)
        st1_status = await svc.get_student_status(st1)
        assert "#1" in st1_status.message
        assert "#4" in st1_status.message

        # Student 1 cancels ONLY the second slot (#4), keeping #1!
        waiting_st1 = await db.get_student_waiting_entries(st1.id)
        assert len(waiting_st1) == 2
        leave_second = await svc.leave_queue(st1, entry_id=waiting_st1[1].id)
        assert leave_second.ok
        waiting_st1_after = await db.get_student_waiting_entries(st1.id)
        assert len(waiting_st1_after) == 1
        assert waiting_st1_after[0].position == 1

        # Call next -> st1 becomes active, st2 becomes #1 (notifies BOTH!)
        next_res = await svc.call_next()
        assert next_res.ok
        assert next_res.entry.student_id == st1.id
        notified_ids = [tg_id for tg_id, _ in sent_notifications]
        assert 101 in notified_ids
        assert 102 in notified_ids

        # Status for st2 reports they follow st1 (currently answering)
        st2_status = await svc.get_student_status(st2)
        assert st2_status.position == 1
        assert st2_status.after_name == st1.full_name
        assert st2_status.after_is_active is True

        # Student 3 leaves voluntarily -> gets short cancel cooldown
        leave_res = await svc.leave_queue(st3)
        assert leave_res.ok
        rejoin_after_leave = await svc.join_queue(st3)
        assert not rejoin_after_leave.ok
        assert rejoin_after_leave.cooldown_remaining_sec > 0

        # Verify cooldown is signup-to-signup, NOT from when teacher finishes serving!
        # st1's signup was 16 min ago; teacher finishes st1 right now -> st1 can rejoin immediately (after cancel_cd=0)
        await db.conn.execute(
            "UPDATE students SET last_joined_at = ?, last_left_at = NULL WHERE id = ?",
            (time.time() - 16 * 60, st1.id),
        )
        await db.conn.commit()
        next_res2 = await svc.call_next()
        assert next_res2.ok and next_res2.entry.student_id == st2.id

        # Even though st1 was JUST served 0 seconds ago, 16 min passed since last signup -> allowed!
        r1_rejoin_ok = await svc.join_queue(st1)
        assert r1_rejoin_ok.ok and r1_rejoin_ok.position == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_admin_insert_middle_move_swap_remove(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path)
    db = Database(cfg)
    await db.connect()
    await db.sync_students_from_csv()
    svc = QueueService(db=db, settings=cfg, ws_manager=WebSocketManager())

    try:
        roster = await db.list_roster_students()
        s1, s2, s3, s4 = roster[0], roster[1], roster[2], roster[3]

        await svc.join_queue(s1)
        await svc.join_queue(s2)
        await svc.join_queue(s3)

        # Admin inserts s4 right in the middle at position #2
        ins = await svc.admin_insert_student(student_id=s4.id, position=2)
        assert ins.ok and ins.position == 2

        waiting = await db.get_waiting_entries()
        assert [w.student_id for w in waiting] == [s1.id, s4.id, s2.id, s3.id]

        # Swap position 1 and 4
        sw = await svc.admin_swap_positions(1, 4)
        assert sw.ok
        waiting = await db.get_waiting_entries()
        assert [w.student_id for w in waiting] == [s3.id, s4.id, s2.id, s1.id]

        # Move s1 (currently #4) to #2
        mv = await svc.admin_move_entry(waiting[3].id, 2)
        assert mv.ok and mv.position == 2
        waiting = await db.get_waiting_entries()
        assert [w.student_id for w in waiting] == [s3.id, s1.id, s4.id, s2.id]

        # Remove #2 (s1)
        rm = await svc.admin_remove_entry(waiting[1].id)
        assert rm.ok
        waiting = await db.get_waiting_entries()
        assert [w.student_id for w in waiting] == [s3.id, s4.id, s2.id]
        assert [w.position for w in waiting] == [1, 2, 3]
    finally:
        await db.close()


def test_fastapi_whiteboard_api_pin_qr_and_websocket(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path, board_pin="4321", bot_username="demo_queue_bot")
    app = create_app(cfg, start_bot=False)

    with TestClient(app) as client:
        # Homepage & health (and verify Skip/Finish buttons are removed)
        home_resp = client.get("/")
        assert home_resp.status_code == 200
        assert "btnSkip" not in home_resp.text
        assert "btnFinishOnly" not in home_resp.text
        health = client.get("/health").json()
        assert health["status"] == "ok"

        # QR SVG works
        qr_resp = client.get("/api/qr.svg")
        assert qr_resp.status_code == 200
        assert "image/svg+xml" in qr_resp.headers["content-type"]

        # Protected mutation fails without PIN
        assert client.post("/api/timer/start").status_code == 403

        # PIN verification
        assert client.post("/api/pin/verify", json={"pin": "0000"}).json()["valid"] is False
        assert client.post("/api/pin/verify", json={"pin": "4321"}).json()["valid"] is True

        headers = {"X-Board-Pin": "4321"}

        # Cannot start timer when queue is empty / nobody is active!
        assert client.post("/api/timer/start", headers=headers).status_code == 400

        # Insert student -> still cannot start timer until student is called!
        r_ins = client.post(
            "/api/queue/insert",
            json={"custom_name": "Иванов Иван"},
            headers=headers,
        )
        assert r_ins.status_code == 200
        assert r_ins.json()["state"]["queue_length"] == 1
        assert "added_by_admin" not in r_ins.json()["state"]["queue"][0]
        assert client.post("/api/timer/start", headers=headers).status_code == 400

        # WebSocket receives live state immediately on connect & on mutation
        with client.websocket_connect("/ws") as ws:
            initial = ws.receive_json()
            assert initial["type"] == "state"
            assert initial["data"]["queue_length"] == 1

            r_next = client.post("/api/queue/next", headers=headers)
            assert r_next.status_code == 200
            pushed = ws.receive_json()
            assert pushed["type"] == "state"
            assert pushed["data"]["current"]["display_name"] == "Иванов Иван"
            assert pushed["data"]["timer"]["status"] == "idle"

            r_start = client.post("/api/timer/start", headers=headers)
            assert r_start.status_code == 200
            pushed_timer = ws.receive_json()
            assert pushed_timer["data"]["timer"]["status"] == "running"

            r_add = client.post("/api/timer/add", json={"seconds": 60}, headers=headers)
            assert r_add.status_code == 200
            pushed_add = ws.receive_json()
            assert pushed_add["data"]["timer"]["remaining_sec"] > 450

        # Static whiteboard assets load cleanly
        assert client.get("/static/styles.css").status_code == 200
        assert client.get("/static/app.js").status_code == 200


@pytest.mark.asyncio
async def test_bot_student_and_admin_handlers_end_to_end(tmp_path: Path) -> None:
    """Exercise student join->confirm inline flow, queue view, and admin /insert & /swap handlers."""
    from unittest.mock import AsyncMock

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from app.bot.handlers_admin import cmd_insert, cmd_swap
    from app.bot.handlers_student import (
        cb_join_confirm,
        cb_leave_confirm,
        handle_full_queue,
        handle_join_request,
    )

    cfg = _make_settings(tmp_path)
    db = Database(cfg)
    await db.connect()
    await db.sync_students_from_csv()
    svc = QueueService(db=db, settings=cfg, ws_manager=WebSocketManager())
    storage = MemoryStorage()

    try:
        st1 = await db.resolve_telegram_user(201, "student_01")
        st2 = await db.resolve_telegram_user(202, "student_02")
        admin = await db.resolve_telegram_user(999001, "prof_admin", "Профессор")
        assert st1 and st2 and admin

        fsm1 = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=201, user_id=201))

        # 1. Student presses "Записаться" -> receives inline confirmation message
        msg_join = AsyncMock()
        await handle_join_request(msg_join, st1, svc, fsm1)
        msg_join.answer.assert_awaited_once()
        join_text = msg_join.answer.call_args.args[0]
        join_kb = msg_join.answer.call_args.kwargs["reply_markup"]
        assert "Подтверждение записи" in join_text
        assert join_kb.inline_keyboard[0][0].callback_data == "join:confirm"
        assert join_kb.inline_keyboard[0][1].callback_data == "join:cancel"

        # 2. Student clicks "✅ Подтвердить" -> added to queue at #1
        cb = AsyncMock()
        cb.message = AsyncMock()
        await cb_join_confirm(cb, st1, svc)
        cb.message.edit_text.assert_awaited_once()
        assert "#1" in cb.message.edit_text.call_args.args[0]

        # 3. Student 2 joins directly & checks full queue command
        await svc.join_queue(st2)
        msg_q = AsyncMock()
        fsm2 = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=202, user_id=202))
        await handle_full_queue(msg_q, st2, svc, fsm2)
        q_text = msg_q.answer.call_args.args[0]
        assert st1.full_name in q_text
        assert st2.full_name in q_text
        assert "Вы" in q_text

        # 4. Admin uses /insert 2 Белов -> inserts Белов Максим right in the middle (#2)
        msg_ins = AsyncMock()
        msg_ins.text = "/insert 2 Белов"
        await cmd_insert(msg_ins, admin, svc)
        waiting = await db.get_waiting_entries()
        assert len(waiting) == 3
        assert "Белов" in waiting[1].display_name

        # 5. Admin swaps #1 and #3
        msg_sw = AsyncMock()
        msg_sw.text = "/swap 1 3"
        await cmd_swap(msg_sw, admin, svc)
        waiting = await db.get_waiting_entries()
        assert waiting[0].student_id == st2.id
        assert waiting[2].student_id == st1.id

        # 6. Student 1 confirms leaving
        cb_leave = AsyncMock()
        cb_leave.data = "leave:confirm"
        cb_leave.message = AsyncMock()
        await cb_leave_confirm(cb_leave, st1, svc)
        waiting = await db.get_waiting_entries()
        assert len(waiting) == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_legacy_db_migrates_last_joined_at_column(tmp_path: Path) -> None:
    """Verify an existing SQLite DB created before last_joined_at upgrades cleanly on startup."""
    import sqlite3

    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE students (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            full_name TEXT NOT NULL,
            telegram_id INTEGER UNIQUE,
            username TEXT,
            is_admin INTEGER NOT NULL DEFAULT 0,
            last_served_at REAL,
            last_left_at REAL,
            visits_count INTEGER NOT NULL DEFAULT 0,
            active_in_roster INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    conn.commit()
    conn.close()

    cfg = _make_settings(tmp_path, db_path=db_file)
    db = Database(cfg)
    await db.connect()
    try:
        count = await db.sync_students_from_csv()
        assert count == 32
        st = await db.resolve_telegram_user(301, "student_01")
        assert st is not None
        assert st.last_joined_at is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_max_active_slots_undo_csv_export_and_broadcast(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        rejoin_cooldown_minutes=0,
        cancel_cooldown_minutes=0,
        max_active_slots=2,
    )
    db = Database(cfg)
    await db.connect()
    await db.sync_students_from_csv()
    svc = QueueService(db=db, settings=cfg, ws_manager=WebSocketManager())

    sent_messages: list[tuple[int, str]] = []

    async def fake_notifier(tg_id: int, text: str) -> None:
        sent_messages.append((tg_id, text))

    svc.set_notifier(fake_notifier)

    try:
        st1 = await db.resolve_telegram_user(401, "student_01")
        st2 = await db.resolve_telegram_user(402, "student_02")
        st3 = await db.resolve_telegram_user(403, "student_03")
        assert st1 and st2 and st3

        # 1. Student 1 can occupy up to MAX_ACTIVE_SLOTS = 2 spots simultaneously
        assert (await svc.join_queue(st1)).ok
        assert (await svc.join_queue(st2)).ok
        assert (await svc.join_queue(st1)).ok  # 2nd spot (#3)

        # 3rd simultaneous spot is blocked!
        third = await svc.join_queue(st1)
        assert not third.ok
        assert "максимум мест" in third.message

        # Admin using regular join button has the EXACT same restrictions as normal users:
        # 1) blocked when queue is closed; 2) capped at MAX_ACTIVE_SLOTS;
        # while admin_insert_student (admin panel) still works!
        admin = await db.resolve_telegram_user(999001, "prof_admin", "Профессор")
        assert admin and admin.is_admin
        await svc.update_settings(queue_open=False)
        admin_closed = await svc.join_queue(admin)
        assert not admin_closed.ok
        assert "приостановлена" in admin_closed.message

        # Admin panel insert works even while queue is closed and beyond MAX_ACTIVE_SLOTS
        panel_ins = await svc.admin_insert_student(student_id=st1.id, position=1)
        assert panel_ins.ok
        # Remove that extra admin-inserted slot before continuing checks
        assert (await svc.admin_remove_entry(panel_ins.entry.id)).ok
        await svc.update_settings(queue_open=True)

        assert (await svc.join_queue(admin)).ok
        assert (await svc.join_queue(admin)).ok
        admin_third = await svc.join_queue(admin)
        assert not admin_third.ok
        assert "максимум мест" in admin_third.message
        assert (await svc.leave_queue(admin, leave_all=True)).ok

        # 2. Broadcast announcement to queue vs full roster
        sent_messages.clear()
        n_queue = await svc.broadcast_announcement("Перерыв 5 минут", scope="queue")
        assert n_queue == 2  # st1 and st2 are in queue (deduplicated!)
        n_all = await svc.broadcast_announcement("Всем привет", scope="all")
        assert n_all == 4  # st1, st2, st3 + admin all bound telegram_id

        # 3. Call next twice (st1 -> active, then st1 -> done & st2 -> active)
        assert (await svc.call_next()).ok
        assert (await svc.call_next()).ok
        # Now st1 only has 1 open spot left, so they CAN join again!
        assert (await svc.join_queue(st1)).ok

        # 4. Undo accidental 'Next' -> st2 returns to #1 waiting, st1 returns to active!
        undo_res = await svc.undo_last_call()
        assert undo_res.ok
        active_after_undo = await db.get_active_entry()
        waiting_after_undo = await db.get_waiting_entries()
        assert active_after_undo is not None and active_after_undo.student_id == st1.id
        assert waiting_after_undo[0].student_id == st2.id

        # 5. CSV export includes UTF-8 BOM & student names
        csv_bytes = await svc.export_history_csv()
        assert csv_bytes.startswith(b"\xef\xbb\xbf")
        csv_text = csv_bytes.decode("utf-8-sig")
        assert st1.full_name in csv_text
        assert st2.full_name in csv_text
    finally:
        await db.close()


def test_web_export_csv_and_undo_endpoints(tmp_path: Path) -> None:
    cfg = _make_settings(tmp_path, board_pin="9999")
    app = create_app(cfg, start_bot=False)
    headers = {"X-Board-Pin": "9999"}

    with TestClient(app) as client:
        assert client.get("/api/export.csv").status_code == 403
        client.post("/api/queue/insert", json={"custom_name": "Первый Ученик"}, headers=headers)
        client.post("/api/queue/insert", json={"custom_name": "Второй Ученик"}, headers=headers)
        client.post("/api/queue/next", headers=headers)
        client.post("/api/queue/next", headers=headers)

        undo_r = client.post("/api/queue/undo", headers=headers)
        assert undo_r.status_code == 200
        assert undo_r.json()["state"]["current"]["display_name"] == "Первый Ученик"
        assert undo_r.json()["state"]["queue"][0]["display_name"] == "Второй Ученик"

        csv_r = client.get("/api/export.csv", headers=headers)
        assert csv_r.status_code == 200
        assert "text/csv" in csv_r.headers["content-type"]
        assert "Первый Ученик" in csv_r.content.decode("utf-8-sig")


def test_security_bruteforce_lockout_hmac_token_headers_and_alert(tmp_path: Path) -> None:
    cfg = _make_settings(
        tmp_path,
        board_pin="SuperSecret2026!",
        pin_max_attempts=3,
        pin_lockout_minutes=15,
    )
    app = create_app(cfg, start_bot=False)

    alerts: list[tuple[int, str]] = []

    async def fake_notifier(tg_id: int, text: str) -> None:
        alerts.append((tg_id, text))

    with TestClient(app) as client:
        # Speed up artificial sleep during unit tests while keeping real guard logic intact
        app.state.pin_guard.failure_delay_step = 0.0
        app.state.queue_service.set_notifier(fake_notifier)

        # 1. Security headers present, /docs hidden when BOARD_PIN is set, eye toggle in HTML
        home = client.get("/")
        assert home.status_code == 200
        assert "btnTogglePinVis" in home.text
        assert home.headers["x-frame-options"] == "DENY"
        assert home.headers["x-content-type-options"] == "nosniff"
        assert home.headers["referrer-policy"] == "no-referrer"
        assert "content-security-policy" in home.headers
        assert client.get("/docs").status_code == 404

        # 2. Attacker IP makes 3 wrong password guesses -> locked out for 15 minutes (HTTP 429)
        attacker_hdrs = {"CF-Connecting-IP": "203.0.113.55"}
        r1 = client.post("/api/pin/verify", json={"pin": "wrong1"}, headers=attacker_hdrs)
        assert r1.status_code == 200
        assert r1.json()["valid"] is False
        assert r1.json()["attempts_left"] == 2

        r2 = client.post("/api/pin/verify", json={"pin": "wrong2"}, headers=attacker_hdrs)
        assert r2.status_code == 200
        assert r2.json()["attempts_left"] == 1

        r3 = client.post("/api/pin/verify", json={"pin": "wrong3"}, headers=attacker_hdrs)
        assert r3.status_code == 429
        assert r3.json()["locked"] is True
        assert r3.json()["retry_after_sec"] >= 895
        assert int(r3.headers["retry-after"]) >= 895

        # Admin got a Telegram security alert!
        assert any(tg_id == 999001 and "203.0.113.55" in txt for tg_id, txt in alerts)

        # Even if attacker tries the RIGHT password during lockout -> still 429!
        r4 = client.post("/api/pin/verify", json={"pin": "SuperSecret2026!"}, headers=attacker_hdrs)
        assert r4.status_code == 429

        # Direct API bypass attempt from locked IP -> also 429!
        r_direct = client.post(
            "/api/queue/insert",
            json={"custom_name": "Hacker"},
            headers={**attacker_hdrs, "X-Board-Pin": "SuperSecret2026!"},
        )
        assert r_direct.status_code == 429

        # Lockout status endpoint reports locked for attacker
        st_att = client.get("/api/pin/status", headers=attacker_hdrs).json()
        assert st_att["locked"] is True and st_att["retry_after_sec"] > 0

        # 3. Legitimate teacher from another IP logs in -> gets signed HMAC token (not raw PIN!)
        teacher_hdrs = {"CF-Connecting-IP": "198.51.100.10"}
        ok_login = client.post(
            "/api/pin/verify",
            json={"pin": "SuperSecret2026!"},
            headers=teacher_hdrs,
        )
        assert ok_login.status_code == 200
        body = ok_login.json()
        assert body["valid"] is True
        token = body["token"]
        assert token and "SuperSecret2026!" not in token

        # Signed X-Board-Token authenticates protected endpoints cleanly!
        ins_ok = client.post(
            "/api/queue/insert",
            json={"custom_name": "Иванов Иван"},
            headers={**teacher_hdrs, "X-Board-Token": token},
        )
        assert ins_ok.status_code == 200

        # Tampered token is rejected with 403
        ins_bad = client.post(
            "/api/queue/insert",
            json={"custom_name": "Fake"},
            headers={**teacher_hdrs, "X-Board-Token": token[:-4] + "0000"},
        )
        assert ins_bad.status_code == 403

