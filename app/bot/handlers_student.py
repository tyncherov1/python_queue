from __future__ import annotations

import html
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.keyboards import (
    BTN_JOIN,
    BTN_LEAVE,
    BTN_QUEUE,
    BTN_STATUS,
    confirm_join_keyboard,
    confirm_leave_keyboard,
    main_reply_keyboard,
    queue_refresh_keyboard,
    status_refresh_keyboard,
)
from app.models import Student
from app.queue_service import QueueService

router = Router(name="student")


def _format_timer_mmss(remaining_sec: float) -> str:
    if remaining_sec < 0:
        over = int(abs(remaining_sec))
        m, s = divmod(over, 60)
        return f"+{m:02d}:{s:02d} (время вышло)"
    rem = int(remaining_sec)
    m, s = divmod(rem, 60)
    return f"{m:02d}:{s:02d}"


def format_full_queue_text(snapshot: dict[str, Any], viewer: Student) -> str:
    title = html.escape(snapshot["seminar_title"])
    open_badge = "🟢 Запись открыта" if snapshot["queue_open"] else "🔒 Запись приостановлена"
    lines = [f"<b>📋 {title}</b>", open_badge, ""]

    current = snapshot.get("current")
    timer = snapshot.get("timer", {})
    if current:
        curr_name = html.escape(current["display_name"])
        you_mark = "  👈 <i>Вы</i>" if current.get("student_id") == viewer.id else ""
        timer_info = ""
        if timer.get("status") == "running":
            timer_info = f"  ⏱ <code>{_format_timer_mmss(timer['remaining_sec'])}</code>"
        elif timer.get("status") == "paused":
            timer_info = f"  ⏸ <code>{_format_timer_mmss(timer['remaining_sec'])}</code>"
        lines.append(f"🎙 <b>Сейчас отвечает:</b>\n   {curr_name}{you_mark}{timer_info}")
        lines.append("")
    else:
        lines.append("🎙 <b>Сейчас отвечает:</b> <i>никого нет</i>")
        lines.append("")

    waiting: list[dict[str, Any]] = snapshot.get("queue", [])
    if not waiting:
        lines.append("📭 <i>Очередь ожидания пока пуста.</i>")
    else:
        lines.append(f"<b>Ожидают ({len(waiting)} чел.):</b>")
        for item in waiting:
            pos = item["position"]
            name = html.escape(item["display_name"])
            mark = "  👈 <b>Вы</b>" if item.get("student_id") == viewer.id else ""
            lines.append(f"<b>{pos}.</b> {name}{mark}")

    return "\n".join(lines)


@router.message(Command("start"))
async def cmd_start(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    await state.clear()
    status_res = await queue_service.get_student_status(student)
    role_note = "\n👑 <i>У вас есть права администратора (/admin).</i>" if student.is_admin else ""
    text = (
        f"👋 Здравствуйте, <b>{html.escape(student.full_name)}</b>!{role_note}\n\n"
        "Этот бот управляет очередью к преподавателю на семинаре.\n"
        "Используйте кнопки ниже или команды:\n"
        "• /join — записаться в очередь\n"
        "• /leave — отказаться от очереди\n"
        "• /status — узнать своё место и после кого вы идёте\n"
        "• /queue — посмотреть всю очередь\n\n"
        f"{status_res.message}"
    )
    await message.answer(
        text,
        reply_markup=main_reply_keyboard(is_admin=student.is_admin),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, student: Student) -> None:
    lines = [
        "<b>ℹ️ Справка по боту</b>",
        "",
        "<b>Команды для учеников:</b>",
        "• /join — записаться в очередь (с подтверждением)",
        "• /leave — отказаться от очереди",
        "• /status — ваше место и после кого вы идёте",
        "• /queue — вся текущая очередь",
    ]
    if student.is_admin:
        lines.extend(
            [
                "",
                "<b>Команды для администраторов:</b>",
                "• /admin — интерактивная панель управления",
                "• /next — вызвать следующего ученика",
                "• /insert <code>&lt;позиция&gt; &lt;имя/фамилия&gt;</code> — вставить в любое место",
                "• /move <code>&lt;откуда&gt; &lt;куда&gt;</code> — переместить позицию",
                "• /swap <code>&lt;поз1&gt; &lt;поз2&gt;</code> — поменять двоих местами",
                "• /remove <code>&lt;позиция&gt;</code> — удалить из очереди",
                "• /reload — перечитать файл students.csv",
            ]
        )
    await message.answer(
        "\n".join(lines),
        reply_markup=main_reply_keyboard(is_admin=student.is_admin),
    )


@router.message(Command("join"))
@router.message(F.text == BTN_JOIN)
async def handle_join_request(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    await state.clear()
    check = await queue_service.check_join_eligibility(student)
    if not check.ok:
        await message.answer(check.message)
        return

    waiting_count = check.extra.get("waiting_count", 0)
    existing_positions: list[str] = check.extra.get("existing_positions", [])
    next_pos = waiting_count + 1
    existing_line = (
        f"Уже есть места: <b>{', '.join(existing_positions)}</b>\n"
        if existing_positions
        else ""
    )
    await message.answer(
        f"📝 <b>Подтверждение записи</b>\n\n"
        f"Ученик: <b>{html.escape(student.full_name)}</b>\n"
        f"{existing_line}"
        f"Сейчас в очереди: <b>{waiting_count} чел.</b>\n"
        f"Вы встанете на позицию: <b>#{next_pos}</b>\n\n"
        f"Подтвердить запись?",
        reply_markup=confirm_join_keyboard(),
    )


@router.callback_query(F.data == "join:confirm")
async def cb_join_confirm(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    res = await queue_service.join_queue(student)
    if callback.message:
        try:
            await callback.message.edit_text(res.message)
        except TelegramBadRequest:
            pass
    await callback.answer("Вы записаны!" if res.ok else "Не удалось записаться")


@router.callback_query(F.data == "join:cancel")
async def cb_join_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text("❌ Запись в очередь отменена.")
        except TelegramBadRequest:
            pass
    await callback.answer("Отменено")


@router.message(Command("leave"))
@router.message(F.text == BTN_LEAVE)
async def handle_leave_request(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    await state.clear()
    waiting_entries = await queue_service.db.get_student_waiting_entries(student.id)
    if not waiting_entries:
        open_entries = await queue_service.db.get_student_open_entries(student.id)
        if open_entries and open_entries[0].status == "active":
            await message.answer(
                "⚠️ Сейчас уже идёт ваш ответ у преподавателя. Попросите преподавателя нажать «Следующий человек»."
            )
            return
        await message.answer("ℹ️ Вас сейчас нет в очереди.")
        return

    if len(waiting_entries) == 1:
        await message.answer(
            f"❓ Вы действительно хотите отказаться от очереди?\n"
            f"Ваша текущая позиция: <b>#{waiting_entries[0].position}</b>.",
            reply_markup=confirm_leave_keyboard(waiting_entries),
        )
    else:
        pos_list = ", ".join(f"#{e.position}" for e in waiting_entries)
        await message.answer(
            f"❓ У вас несколько записей в очереди (<b>{pos_list}</b>).\n"
            "Выберите, какую запись отменить:",
            reply_markup=confirm_leave_keyboard(waiting_entries),
        )


@router.callback_query(F.data == "leave:confirm")
@router.callback_query(F.data.startswith("leave:confirm:"))
@router.callback_query(F.data == "leave:confirm_all")
async def cb_leave_confirm(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    data = callback.data or "leave:confirm"
    if data == "leave:confirm_all":
        res = await queue_service.leave_queue(student, leave_all=True)
    elif data.startswith("leave:confirm:"):
        entry_id = int(data.split(":")[2])
        res = await queue_service.leave_queue(student, entry_id=entry_id)
    else:
        res = await queue_service.leave_queue(student)

    if callback.message:
        try:
            await callback.message.edit_text(res.message)
        except TelegramBadRequest:
            pass
    await callback.answer("Запись отменена" if res.ok else "Ошибка")


@router.callback_query(F.data == "leave:cancel")
async def cb_leave_cancel(callback: CallbackQuery) -> None:
    if callback.message:
        try:
            await callback.message.edit_text("✅ Вы остались на своём месте в очереди.")
        except TelegramBadRequest:
            pass
    await callback.answer("Вы остались в очереди")


@router.message(Command("status"))
@router.message(F.text == BTN_STATUS)
async def handle_status(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    await state.clear()
    res = await queue_service.get_student_status(student)
    await message.answer(res.message, reply_markup=status_refresh_keyboard())


@router.callback_query(F.data == "status:refresh")
async def cb_status_refresh(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    res = await queue_service.get_student_status(student)
    if callback.message:
        try:
            await callback.message.edit_text(res.message, reply_markup=status_refresh_keyboard())
        except TelegramBadRequest:
            await callback.answer("Статус не изменился")
            return
    await callback.answer("Обновлено")


@router.message(Command("queue"))
@router.message(F.text == BTN_QUEUE)
async def handle_full_queue(
    message: Message,
    student: Student,
    queue_service: QueueService,
    state: FSMContext,
) -> None:
    await state.clear()
    snapshot = await queue_service.get_board_snapshot()
    text = format_full_queue_text(snapshot, student)
    await message.answer(text, reply_markup=queue_refresh_keyboard())


@router.callback_query(F.data == "queue:refresh")
async def cb_queue_refresh(
    callback: CallbackQuery,
    student: Student,
    queue_service: QueueService,
) -> None:
    snapshot = await queue_service.get_board_snapshot()
    text = format_full_queue_text(snapshot, student)
    if callback.message:
        try:
            await callback.message.edit_text(text, reply_markup=queue_refresh_keyboard())
        except TelegramBadRequest:
            await callback.answer("Очередь не изменилась")
            return
    await callback.answer("Очередь обновлена")
