from aiogram.fsm.state import State, StatesGroup


class PurchaseState(StatesGroup):
    waiting_email = State()
    waiting_payment = State()


class ProfileState(StatesGroup):
    waiting_topup_amount = State()
