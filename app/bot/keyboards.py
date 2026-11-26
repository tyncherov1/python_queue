from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.models import QueueEntry, Student

BTN_JOIN = "📝 Записаться в очередь"
BTN_LEAVE = "❌ Выйти из очереди"
BTN_STATUS = "📍 Моё место"
BTN_QUEUE = "📋 Вся очередь"
BTN_ADMIN = "👑 Админ-панель"


def main_reply_keyboard(is_admin: bool = False) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text=BTN_JOIN), KeyboardButton(text=BTN_LEAVE)],
        [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_QUEUE)],
    ]
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        input_field_placeholder="Выберите действие...",
    )


def confirm_join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Подтвердить", callback_data="join:confirm"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="join:cancel"),
            ]
        ]
    )


def confirm_leave_keyboard(entries: list[QueueEntry] | None = None) -> InlineKeyboardMarkup:
    if not entries or len(entries) == 1:
        cb = f"leave:confirm:{entries[0].id}" if entries else "leave:confirm"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Да, выйти", callback_data=cb),
                    InlineKeyboardButton(text="↩️ Остаться", callback_data="leave:cancel"),
                ]
            ]
        )

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"❌ Отменить позицию #{e.position}",
                callback_data=f"leave:confirm:{e.id}",
            )
        ]
        for e in entries
    ]
    rows.append(
        [
            InlineKeyboardButton(text="🗑 Отменить все записи", callback_data="leave:confirm_all"),
            InlineKeyboardButton(text="↩️ Остаться", callback_data="leave:cancel"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def queue_refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить очередь", callback_data="queue:refresh")]
        ]
    )


def status_refresh_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Обновить статус", callback_data="status:refresh")]
        ]
    )


def admin_main_keyboard(
    *,
    queue_open: bool,
    timer_minutes: float,
    cooldown_minutes: int,
    max_active_slots: int = 2,
) -> InlineKeyboardMarkup:
    open_label = "🔒 Закрыть запись" if queue_open else "🔓 Открыть запись"
    timer_str = f"{timer_minutes:g}"
    slots_str = "∞" if max_active_slots == 0 else str(max_active_slots)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⏭ Следующий", callback_data="adm:next"),
                InlineKeyboardButton(text="↩️ Вернуть назад", callback_data="adm:undo"),
            ],
            [
                InlineKeyboardButton(text="➕ Добавить ученика", callback_data="adm:add_list:0"),
                InlineKeyboardButton(text="↕️ Управлять очередью", callback_data="adm:manage"),
            ],
            [
                InlineKeyboardButton(text=open_label, callback_data="adm:toggle_open"),
                InlineKeyboardButton(text="📢 Объявление", callback_data="adm:bc_menu"),
            ],
            [
                InlineKeyboardButton(text=f"⏱ Таймер ({timer_str} мин)", callback_data="adm:timer_menu"),
                InlineKeyboardButton(text=f"⏳ Кулдаун ({cooldown_minutes} мин)", callback_data="adm:cd_menu"),
            ],
            [
                InlineKeyboardButton(text=f"🎟 Лимит мест ({slots_str})", callback_data="adm:slots_menu"),
                InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats"),
            ],
            [
                InlineKeyboardButton(text="📥 Отчёт CSV", callback_data="adm:export_csv"),
                InlineKeyboardButton(text="📄 Перечитать CSV", callback_data="adm:reload_csv"),
            ],
            [
                InlineKeyboardButton(text="🔄 Обновить", callback_data="adm:home"),
                InlineKeyboardButton(text="🧹 Очистить очередь", callback_data="adm:clear_prompt"),
            ],
        ]
    )


def admin_students_keyboard(
    students: list[Student],
    queued_counts: dict[int, int],
    page: int = 0,
    page_size: int = 8,
) -> InlineKeyboardMarkup:
    total_pages = max(1, (len(students) + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    chunk = students[page * page_size : (page + 1) * page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for st in chunk:
        cnt = queued_counts.get(st.id, 0)
        marker = f"✓×{cnt} " if cnt > 1 else ("✓ " if cnt == 1 else "")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{marker}{st.full_name}",
                    callback_data=f"adm:pick_student:{st.id}",
                )
            ]
        )

    nav_row: list[InlineKeyboardButton] = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"adm:add_list:{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="adm:noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"adm:add_list:{page + 1}"))
    rows.append(nav_row)

    rows.append(
        [
            InlineKeyboardButton(text="✏️ Ввести имя вручную", callback_data="adm:add_custom"),
            InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_insert_position_keyboard(queue_length: int) -> InlineKeyboardMarkup:
    max_pos = queue_length + 1
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text="⏫ В начало (#1)", callback_data="adm:ins_pos:1"),
            InlineKeyboardButton(text=f"⏬ В конец (#{max_pos})", callback_data=f"adm:ins_pos:{max_pos}"),
        ]
    ]

    # Quick middle-position buttons (up to 8 positions)
    quick_positions = [p for p in range(2, min(max_pos, 10))]
    for i in range(0, len(quick_positions), 4):
        rows.append(
            [
                InlineKeyboardButton(text=f"На #{p}", callback_data=f"adm:ins_pos:{p}")
                for p in quick_positions[i : i + 4]
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="🔢 Ввести номер позиции", callback_data="adm:ins_pos_custom"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="adm:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_manage_queue_keyboard(waiting: list[QueueEntry]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in waiting[:20]:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"#{entry.position}  {entry.display_name}",
                    callback_data=f"adm:entry:{entry.id}",
                )
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="➕ Добавить", callback_data="adm:add_list:0"),
            InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_entry_actions_keyboard(entry: QueueEntry, total_waiting: int) -> InlineKeyboardMarkup:
    pos = entry.position or 1
    move_row: list[InlineKeyboardButton] = []
    if pos > 1:
        move_row.append(InlineKeyboardButton(text="⬆️ Выше", callback_data=f"adm:mv:{entry.id}:{pos - 1}"))
    if pos < total_waiting:
        move_row.append(InlineKeyboardButton(text="⬇️ Ниже", callback_data=f"adm:mv:{entry.id}:{pos + 1}"))

    edge_row: list[InlineKeyboardButton] = []
    if pos > 1:
        edge_row.append(InlineKeyboardButton(text="⏫ В начало (#1)", callback_data=f"adm:mv:{entry.id}:1"))
    if pos < total_waiting:
        edge_row.append(InlineKeyboardButton(text=f"⏬ В конец (#{total_waiting})", callback_data=f"adm:mv:{entry.id}:{total_waiting}"))

    rows: list[list[InlineKeyboardButton]] = []
    if move_row:
        rows.append(move_row)
    if edge_row:
        rows.append(edge_row)
    if total_waiting > 1:
        rows.append(
            [
                InlineKeyboardButton(text="🔢 На позицию #...", callback_data=f"adm:mv_custom:{entry.id}"),
                InlineKeyboardButton(text="🔄 Поменять с...", callback_data=f"adm:swap_pick:{pos}"),
            ]
        )
    rows.append(
        [
            InlineKeyboardButton(text="🗑 Удалить из очереди", callback_data=f"adm:del:{entry.id}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(text="◀️ К списку очереди", callback_data="adm:manage"),
            InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_swap_target_keyboard(waiting: list[QueueEntry], source_pos: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for entry in waiting[:20]:
        if entry.position == source_pos:
            continue
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"⇄ #{entry.position} {entry.display_name}",
                    callback_data=f"adm:swap_do:{source_pos}:{entry.position}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="adm:manage")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_timer_presets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="3 мин", callback_data="adm:set_timer:180"),
                InlineKeyboardButton(text="5 мин", callback_data="adm:set_timer:300"),
                InlineKeyboardButton(text="7 мин", callback_data="adm:set_timer:420"),
            ],
            [
                InlineKeyboardButton(text="10 мин", callback_data="adm:set_timer:600"),
                InlineKeyboardButton(text="15 мин", callback_data="adm:set_timer:900"),
                InlineKeyboardButton(text="✏️ Своё время", callback_data="adm:timer_custom"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home")],
        ]
    )


def admin_cooldown_presets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="0 мин (выкл)", callback_data="adm:set_cd:0"),
                InlineKeyboardButton(text="5 мин", callback_data="adm:set_cd:5"),
                InlineKeyboardButton(text="10 мин", callback_data="adm:set_cd:10"),
            ],
            [
                InlineKeyboardButton(text="15 мин", callback_data="adm:set_cd:15"),
                InlineKeyboardButton(text="20 мин", callback_data="adm:set_cd:20"),
                InlineKeyboardButton(text="30 мин", callback_data="adm:set_cd:30"),
            ],
            [
                InlineKeyboardButton(text="✏️ Своё значение", callback_data="adm:cd_custom"),
                InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home"),
            ],
        ]
    )


def admin_slots_presets_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1 место", callback_data="adm:set_slots:1"),
                InlineKeyboardButton(text="2 места", callback_data="adm:set_slots:2"),
                InlineKeyboardButton(text="3 места", callback_data="adm:set_slots:3"),
            ],
            [
                InlineKeyboardButton(text="∞ Без ограничений", callback_data="adm:set_slots:0"),
            ],
            [InlineKeyboardButton(text="↩️ В меню", callback_data="adm:home")],
        ]
    )


def admin_broadcast_scope_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📋 Тем, кто в очереди", callback_data="adm:bc_scope:queue"),
            ],
            [
                InlineKeyboardButton(text="👥 Всем ученикам группы", callback_data="adm:bc_scope:all"),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:home")],
        ]
    )


def admin_clear_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🧹 Очистить очередь", callback_data="adm:clear_do:0"),
            ],
            [
                InlineKeyboardButton(
                    text="🧹 Очистить + сбросить кулдауны",
                    callback_data="adm:clear_do:1",
                ),
            ],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm:home")],
        ]
    )
