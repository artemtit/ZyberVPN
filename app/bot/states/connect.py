from aiogram.fsm.state import State, StatesGroup


class ConnectFlowState(StatesGroup):
    choosing_device = State()
    choosing_app = State()
    done = State()