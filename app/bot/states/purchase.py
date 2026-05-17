from aiogram.fsm.state import State, StatesGroup


class PurchaseState(StatesGroup):
    waiting_email = State()
    waiting_payment = State()
    waiting_promo_code = State()  # discount promo entered during checkout


class ProfileState(StatesGroup):
    waiting_topup_amount = State()
    waiting_topup_input = State()
