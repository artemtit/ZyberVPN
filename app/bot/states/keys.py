from aiogram.fsm.state import State, StatesGroup


class KeyCommentState(StatesGroup):
    waiting_for_comment = State()
