from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    BTN_ADMIN,
    BTN_JOIN,
    BTN_LEAVE,
    BTN_QUEUE,
    BTN_STATUS,
    admin_clear_confirm_keyboard,
    admin_cooldown_presets_keyboard,
    admin_entry_actions_keyboard,
    admin_insert_position_keyboard,
    admin_main_keyboard,
    admin_manage_queue_keyboard,
    admin_students_keyboard,
    admin_swap_target_keyboard,
    admin_timer_presets_keyboard,
)
from app.bot.states import AdminStates
from app.models import Student
from app.queue_service import QueueService, format_duration_ru

router = Router(name="admin")


async def _render_admin_home(queue_service: QueueService) -> tuple[str, Any]:
    snap = await queue_service.get_board_snapshot()
    current = snap.get("current")
    curr_text = html.escape(current["display_name"]) if current else "<i>никого</i>"
    waiting = snap.get("queue", [])
    timer_min = snap["timer"]["duration_sec"] / 60.0
    cd_min = snap["rejoin_cooldown_minutes"]
    open_str = "🟢 Открыта" if snap["queue_open"] else "🔒 Закрыта"

    preview_lines = []
    for item in waiting[:5]:
        preview_lines.append(f"  {item['position']}. {html.escape(item['display_name'])}")
    if len(waiting) > 5:
        preview_lines.append(f"  <i>... и ещё {len(waiting) - 5} чел.</i>")
    preview = "\n".join(preview_lines) if preview_lines else "  <i>пусто</i>"

    text = (
        "👑 <b>Панель администратора</b>\n\n"
        f"🎙 Сейчас отвечает: <b>{curr_text}</b>\n"
        f"📋 В очереди: <b>{len(waiting)} чел.</b> ({open_str})\n"
        f"{preview}\n\n"
        f"⏱ Таймер: <b>{timer_min:g} мин</b>  |  ⏳ Кулдаун: <b>{cd_min} мин</b>"
    )
    kb = admin_main_keyboard(
        queue_open=snap["queue_open"],
        timer_minutes=timer_min,
        cooldown_minutes=cd_min,
    )
    return text, kb


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup: Any = None) -> None:
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=reply_markup)
        except TelegramBadRequest:
            pass


def _is_admin(student: Student) -> bool:
    return bool(student.is_admin)


# ----------------------------------------------------------------------
# Admin Entry Point & Quick Commands
# ----------------------------------------------------------------------
@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN)
async def cmd_admin(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    await state.clear()
    text, kb = await _render_admin_home(queue_service)
    await message.answer(text, reply_markup=kb)


@router.message(Command("next"))
async def cmd_next(message: Message, student: Student, queue_service: QueueService) -> None:
    if not _is_admin(student):
        return
    res = await queue_service.call_next()
    await message.answer(res.message)


@router.message(Command("reload"))
async def cmd_reload(message: Message, student: Student, queue_service: QueueService) -> None:
    if not _is_admin(student):
        return
    count = await queue_service.reload_roster()
    await message.answer(f"🔄 Список учеников перезагружен из CSV: <b>{count} чел.</b>")


@router.message(Command("insert"))
async def cmd_insert(message: Message, student: Student, queue_service: QueueService) -> None:
    """Usage: /insert <position> <student query or custom name>"""
    if not _is_admin(student):
        return
    parts = (message.text or "").split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer(
            "Использование: <code>/insert &lt;позиция&gt; &lt;фамилия или имя&gt;</code>\n"
            "Пример: <code>/insert 2 Иванов</code>"
        )
        return

    pos = int(parts[1])
    query = parts[2].strip()
    matches = await queue_service.db.find_students_by_query(query)
    if len(matches) == 1:
        res = await queue_service.admin_insert_student(student_id=matches[0].id, position=pos)
    elif len(matches) > 1:
        names = "\n".join(f"• {html.escape(m.full_name)}" for m in matches[:6])
        await message.answer(
            f"Найдено несколько учеников по запросу «{html.escape(query)}»:\n{names}\n\n"
            "Уточните фамилию или выберите через /admin → «➕ Добавить ученика»."
        )
        return
    else:
        res = await queue_service.admin_insert_student(custom_name=query, position=pos)
    await message.answer(res.message)


@router.message(Command("move"))
async def cmd_move(message: Message, student: Student, queue_service: QueueService) -> None:
    """Usage: /move <from_pos> <to_pos>"""
    if not _is_admin(student):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await message.answer("Использование: <code>/move &lt;откуда&gt; &lt;куда&gt;</code>\nПример: <code>/move 5 2</code>")
        return

    from_pos, to_pos = int(parts[1]), int(parts[2])
    waiting = await queue_service.db.get_waiting_entries()
    target = next((e for e in waiting if e.position == from_pos), None)
    if target is None:
        await message.answer(f"❌ На позиции #{from_pos} никого нет.")
        return
    res = await queue_service.admin_move_entry(target.id, to_pos)
    await message.answer(res.message)


@router.message(Command("swap"))
async def cmd_swap(message: Message, student: Student, queue_service: QueueService) -> None:
    """Usage: /swap <pos1> <pos2>"""
    if not _is_admin(student):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not (parts[1].isdigit() and parts[2].isdigit()):
        await message.answer("Использование: <code>/swap &lt;поз1&gt; &lt;поз2&gt;</code>\nПример: <code>/swap 1 3</code>")
        return
    res = await queue_service.admin_swap_positions(int(parts[1]), int(parts[2]))
    await message.answer(res.message)


@router.message(Command("remove"))
async def cmd_remove(message: Message, student: Student, queue_service: QueueService) -> None:
    """Usage: /remove <pos>"""
    if not _is_admin(student):
        return
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/remove &lt;позиция&gt;</code>\nПример: <code>/remove 2</code>")
        return
    pos = int(parts[1])
    waiting = await queue_service.db.get_waiting_entries()
    target = next((e for e in waiting if e.position == pos), None)
    if target is None:
        await message.answer(f"❌ На позиции #{pos} никого нет.")
        return
    res = await queue_service.admin_remove_entry(target.id)
    await message.answer(res.message)


# ----------------------------------------------------------------------
# Admin Inline Callbacks
# ----------------------------------------------------------------------
@router.callback_query(F.data == "adm:noop")
async def cb_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "adm:home")
async def cb_admin_home(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.clear()
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "adm:next")
async def cb_admin_next(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    res = await queue_service.call_next()
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"{res.message}\n\n{text}", kb)
    await callback.answer("Готово" if res.ok else "Очередь пуста")


@router.callback_query(F.data == "adm:skip")
async def cb_admin_skip(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    res = await queue_service.call_next(mark_current_as="skipped")
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"⏩ Текущий пропущен. {res.message}\n\n{text}", kb)
    await callback.answer("Пропущено")


@router.callback_query(F.data == "adm:toggle_open")
async def cb_admin_toggle_open(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    snap = await queue_service.get_board_snapshot()
    new_open = not snap["queue_open"]
    await queue_service.update_settings(queue_open=new_open)
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, text, kb)
    await callback.answer("Запись открыта" if new_open else "Запись закрыта")


# --- Add Student to Any Position ---
@router.callback_query(F.data.startswith("adm:add_list:"))
async def cb_admin_add_list(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    page = int(callback.data.split(":")[2])
    students = await queue_service.db.list_roster_students()
    waiting = await queue_service.db.get_waiting_entries()
    active = await queue_service.db.get_active_entry()
    queued_counts: dict[int, int] = {}
    for e in waiting:
        if e.student_id is not None:
            queued_counts[e.student_id] = queued_counts.get(e.student_id, 0) + 1
    if active and active.student_id is not None:
        queued_counts[active.student_id] = queued_counts.get(active.student_id, 0) + 1

    kb = admin_students_keyboard(students, queued_counts, page=page)
    await _safe_edit(
        callback,
        "➕ <b>Выберите ученика для добавления в очередь</b>\n"
        "<i>(✓ — уже стоит в очереди; можно добавить повторно на любую позицию)</i>",
        kb,
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:pick_student:"))
async def cb_admin_pick_student(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    picked_id = int(callback.data.split(":")[2])
    picked = await queue_service.db.get_student_by_id(picked_id)
    if picked is None:
        await callback.answer("Ученик не найден", show_alert=True)
        return

    waiting = await queue_service.db.get_waiting_entries()
    await state.update_data(insert_student_id=picked.id, insert_custom_name=None)
    await _safe_edit(
        callback,
        f"📍 Куда вставить <b>{html.escape(picked.full_name)}</b>?\n"
        f"Сейчас в очереди: <b>{len(waiting)} чел.</b>",
        admin_insert_position_keyboard(len(waiting)),
    )
    await callback.answer()


@router.callback_query(F.data == "adm:add_custom")
async def cb_admin_add_custom(
    callback: CallbackQuery,
    student: Student,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_custom_name)
    await _safe_edit(
        callback,
        "✏️ Введите имя и фамилию ученика, которого нужно добавить в очередь (отправьте сообщением):",
    )
    await callback.answer()


@router.message(AdminStates.waiting_custom_name)
async def fsm_custom_name(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    name = (message.text or "").strip()
    if not name or name.startswith("/") or name in {BTN_JOIN, BTN_LEAVE, BTN_STATUS, BTN_QUEUE, BTN_ADMIN}:
        await state.clear()
        await message.answer("❌ Ввод отменён.")
        return
    waiting = await queue_service.db.get_waiting_entries()
    await state.set_state(None)
    await state.update_data(insert_student_id=None, insert_custom_name=name)
    await message.answer(
        f"📍 Куда вставить <b>{html.escape(name)}</b>?\n"
        f"Сейчас в очереди: <b>{len(waiting)} чел.</b>",
        reply_markup=admin_insert_position_keyboard(len(waiting)),
    )


@router.callback_query(F.data.startswith("adm:ins_pos:"))
async def cb_admin_insert_at_pos(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    pos = int(callback.data.split(":")[2])
    data = await state.get_data()
    st_id = data.get("insert_student_id")
    custom_name = data.get("insert_custom_name")
    await state.clear()

    if st_id is None and not custom_name:
        await callback.answer("Сессия выбора истекла, начните заново.", show_alert=True)
        return

    res = await queue_service.admin_insert_student(
        student_id=st_id,
        custom_name=custom_name,
        position=pos,
    )
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"{res.message}\n\n{text}", kb)
    await callback.answer("Добавлено!" if res.ok else "Ошибка")


@router.callback_query(F.data == "adm:ins_pos_custom")
async def cb_admin_insert_pos_custom(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    waiting = await queue_service.db.get_waiting_entries()
    await state.set_state(AdminStates.waiting_insert_position)
    await _safe_edit(
        callback,
        f"🔢 Введите номер позиции от <b>1</b> до <b>{len(waiting) + 1}</b> (отправьте числом):",
    )
    await callback.answer()


@router.message(AdminStates.waiting_insert_position)
async def fsm_insert_position(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("Введите положительное целое число (например <code>2</code>).")
        return
    pos = int(raw)
    data = await state.get_data()
    await state.clear()
    res = await queue_service.admin_insert_student(
        student_id=data.get("insert_student_id"),
        custom_name=data.get("insert_custom_name"),
        position=pos,
    )
    text, kb = await _render_admin_home(queue_service)
    await message.answer(f"{res.message}\n\n{text}", reply_markup=kb)


# --- Reorder / Move / Swap / Delete ---
@router.callback_query(F.data == "adm:manage")
async def cb_admin_manage(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    waiting = await queue_service.db.get_waiting_entries()
    if not waiting:
        await _safe_edit(
            callback,
            "📭 Очередь ожидания сейчас пуста.",
            admin_manage_queue_keyboard([]),
        )
        await callback.answer()
        return

    await _safe_edit(
        callback,
        "↕️ <b>Управление очередью</b>\nНажмите на ученика, чтобы переместить его, поменять местами или удалить:",
        admin_manage_queue_keyboard(waiting),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:entry:"))
async def cb_admin_entry(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    entry_id = int(callback.data.split(":")[2])
    waiting = await queue_service.db.get_waiting_entries()
    entry = next((e for e in waiting if e.id == entry_id), None)
    if entry is None:
        await callback.answer("Эта запись уже изменилась", show_alert=True)
        await cb_admin_manage(callback, student, queue_service)
        return

    await _safe_edit(
        callback,
        f"👤 <b>{html.escape(entry.display_name)}</b>\nТекущая позиция: <b>#{entry.position}</b> из {len(waiting)}",
        admin_entry_actions_keyboard(entry, len(waiting)),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:mv:"))
async def cb_admin_move(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, entry_id_s, new_pos_s = callback.data.split(":")
    entry_id, new_pos = int(entry_id_s), int(new_pos_s)
    res = await queue_service.admin_move_entry(entry_id, new_pos)
    waiting = await queue_service.db.get_waiting_entries()
    entry = next((e for e in waiting if e.id == entry_id), None)
    if entry is not None:
        await _safe_edit(
            callback,
            f"{res.message}\n\n👤 <b>{html.escape(entry.display_name)}</b>\nТекущая позиция: <b>#{entry.position}</b> из {len(waiting)}",
            admin_entry_actions_keyboard(entry, len(waiting)),
        )
    else:
        await _safe_edit(callback, res.message, admin_manage_queue_keyboard(waiting))
    await callback.answer("Перемещено" if res.ok else "Ошибка")


@router.callback_query(F.data.startswith("adm:mv_custom:"))
async def cb_admin_move_custom(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    entry_id = int(callback.data.split(":")[2])
    waiting = await queue_service.db.get_waiting_entries()
    await state.set_state(AdminStates.waiting_move_position)
    await state.update_data(move_entry_id=entry_id)
    await _safe_edit(
        callback,
        f"🔢 Введите новую позицию (число от <b>1</b> до <b>{len(waiting)}</b>):",
    )
    await callback.answer()


@router.message(AdminStates.waiting_move_position)
async def fsm_move_position(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) < 1:
        await message.answer("Введите целое число >= 1.")
        return
    data = await state.get_data()
    entry_id = data.get("move_entry_id")
    await state.clear()
    if not entry_id:
        return
    res = await queue_service.admin_move_entry(int(entry_id), int(raw))
    text, kb = await _render_admin_home(queue_service)
    await message.answer(f"{res.message}\n\n{text}", reply_markup=kb)


@router.callback_query(F.data.startswith("adm:swap_pick:"))
async def cb_admin_swap_pick(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    source_pos = int(callback.data.split(":")[2])
    waiting = await queue_service.db.get_waiting_entries()
    await _safe_edit(
        callback,
        f"🔄 С кем поменять местами позицию <b>#{source_pos}</b>?",
        admin_swap_target_keyboard(waiting, source_pos),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:swap_do:"))
async def cb_admin_swap_do(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    _, _, pos_a_s, pos_b_s = callback.data.split(":")
    res = await queue_service.admin_swap_positions(int(pos_a_s), int(pos_b_s))
    waiting = await queue_service.db.get_waiting_entries()
    await _safe_edit(callback, res.message, admin_manage_queue_keyboard(waiting))
    await callback.answer("Поменяли местами" if res.ok else "Ошибка")


@router.callback_query(F.data.startswith("adm:del:"))
async def cb_admin_delete(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    entry_id = int(callback.data.split(":")[2])
    res = await queue_service.admin_remove_entry(entry_id)
    waiting = await queue_service.db.get_waiting_entries()
    await _safe_edit(callback, res.message, admin_manage_queue_keyboard(waiting))
    await callback.answer("Удалено" if res.ok else "Ошибка")


# --- Timer & Cooldown Settings ---
@router.callback_query(F.data == "adm:timer_menu")
async def cb_admin_timer_menu(callback: CallbackQuery, student: Student) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _safe_edit(
        callback,
        "⏱ <b>Настройка длительности таймера на доске</b>\nВыберите время для одного ученика:",
        admin_timer_presets_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:set_timer:"))
async def cb_admin_set_timer(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    sec = int(callback.data.split(":")[2])
    await queue_service.update_settings(timer_duration_sec=sec)
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"⏱ Таймер установлен на <b>{sec // 60} мин</b>.\n\n{text}", kb)
    await callback.answer("Таймер обновлён")


@router.callback_query(F.data == "adm:timer_custom")
async def cb_admin_timer_custom(
    callback: CallbackQuery,
    student: Student,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_timer_minutes)
    await _safe_edit(callback, "⏱ Введите время таймера в минутах (например <code>7</code> или <code>5.5</code>):")
    await callback.answer()


@router.message(AdminStates.waiting_timer_minutes)
async def fsm_timer_minutes(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    raw = (message.text or "").strip().replace(",", ".")
    try:
        minutes = float(raw)
        if not (0.5 <= minutes <= 120):
            raise ValueError
    except ValueError:
        await message.answer("Введите число минут от 0.5 до 120 (например <code>7</code>).")
        return
    await state.clear()
    await queue_service.update_settings(timer_duration_sec=int(round(minutes * 60)))
    text, kb = await _render_admin_home(queue_service)
    await message.answer(f"⏱ Таймер установлен на <b>{minutes:g} мин</b>.\n\n{text}", reply_markup=kb)


@router.callback_query(F.data == "adm:cd_menu")
async def cb_admin_cd_menu(callback: CallbackQuery, student: Student) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _safe_edit(
        callback,
        "⏳ <b>Кулдаун между записями</b>\n"
        "Через сколько минут после записи ученик сможет записаться в очередь ещё раз:",
        admin_cooldown_presets_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:set_cd:"))
async def cb_admin_set_cd(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    minutes = int(callback.data.split(":")[2])
    await queue_service.update_settings(rejoin_cooldown_minutes=minutes)
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"⏳ Кулдаун повторной записи: <b>{minutes} мин</b>.\n\n{text}", kb)
    await callback.answer("Кулдаун обновлён")


@router.callback_query(F.data == "adm:cd_custom")
async def cb_admin_cd_custom(
    callback: CallbackQuery,
    student: Student,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await state.set_state(AdminStates.waiting_cooldown_minutes)
    await _safe_edit(callback, "⏳ Введите время кулдауна в минутах (целое число, <code>0</code> — отключить):")
    await callback.answer()


@router.message(AdminStates.waiting_cooldown_minutes)
async def fsm_cooldown_minutes(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    if not _is_admin(student):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("Введите целое число минут (например <code>15</code> или <code>0</code>).")
        return
    minutes = int(raw)
    await state.clear()
    await queue_service.update_settings(rejoin_cooldown_minutes=minutes)
    text, kb = await _render_admin_home(queue_service)
    await message.answer(f"⏳ Кулдаун установлен: <b>{minutes} мин</b>.\n\n{text}", reply_markup=kb)


# --- Stats, Reload CSV, Clear Queue ---
@router.callback_query(F.data == "adm:stats")
async def cb_admin_stats(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    snap = await queue_service.get_board_snapshot()
    stats = snap["stats"]
    avg_str = format_duration_ru(stats["avg_duration_sec"]) if stats["avg_duration_sec"] > 0 else "—"
    recent_lines = []
    for item in stats.get("recent_done", []):
        dur = f" ({format_duration_ru(item['duration_sec'])})" if item.get("duration_sec") is not None else ""
        recent_lines.append(f"• {html.escape(item['display_name'])}{dur}")
    recent_block = "\n".join(recent_lines) if recent_lines else "<i>пока никто не завершил ответ</i>"

    text = (
        "📊 <b>Статистика семинара</b>\n\n"
        f"👥 Учеников в списке (CSV): <b>{stats['roster_count']}</b>\n"
        f"✅ Уже ответили: <b>{stats['served_count']}</b>\n"
        f"📋 Сейчас ожидают: <b>{snap['queue_length']}</b>\n"
        f"⏱ Среднее время ответа: <b>{avg_str}</b>\n\n"
        f"<b>Последние ответившие:</b>\n{recent_block}"
    )
    _, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, text, kb)
    await callback.answer()


@router.callback_query(F.data == "adm:reload_csv")
async def cb_admin_reload_csv(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    count = await queue_service.reload_roster()
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"📄 Список обновлён из CSV: <b>{count} чел.</b>\n\n{text}", kb)
    await callback.answer(f"Загружено: {count}")


@router.callback_query(F.data == "adm:clear_prompt")
async def cb_admin_clear_prompt(callback: CallbackQuery, student: Student) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    await _safe_edit(
        callback,
        "🧹 <b>Очистка очереди</b>\nВыберите режим очистки (удобно перед началом нового семинара):",
        admin_clear_confirm_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("adm:clear_do:"))
async def cb_admin_clear_do(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    if not _is_admin(student):
        await callback.answer("Недостаточно прав", show_alert=True)
        return
    reset_cd = callback.data.split(":")[2] == "1"
    res = await queue_service.clear_queue(reset_cooldowns=reset_cd)
    text, kb = await _render_admin_home(queue_service)
    await _safe_edit(callback, f"{res.message}\n\n{text}", kb)
    await callback.answer("Очередь очищена")
