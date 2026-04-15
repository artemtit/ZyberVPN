from aiogram import Dispatcher

from .keys import router as keys_router
from .connect import router as connect_router
from .payments import router as payments_router
from .profile import router as profile_router
from .purchase import router as purchase_router
from .start import router as start_router
from .support import router as support_router


def setup_routers(dp: Dispatcher) -> None:
    dp.include_router(start_router)
    dp.include_router(keys_router)
    dp.include_router(connect_router)
    dp.include_router(profile_router)
    dp.include_router(support_router)
    dp.include_router(purchase_router)
    dp.include_router(payments_router)
