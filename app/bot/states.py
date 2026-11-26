from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class AdminStates(StatesGroup):
    waiting_custom_name = State()
    waiting_insert_position = State()
    waiting_move_position = State()
    waiting_timer_minutes = State()
    waiting_cooldown_minutes = State()
    waiting_broadcast_text = State()
