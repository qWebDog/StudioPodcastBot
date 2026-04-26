import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from config import BOT_TOKEN
from database import init_db
from handlers import client, admin
from scheduler import init_scheduler
from middleware.antiflood import AntiFloodMiddleware  # 🆕

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


async def main():
    await init_db()
    bot = Bot(token=BOT_TOKEN, parse_mode="Markdown")
    dp = Dispatcher(storage=MemoryStorage())

    dp.include_router(client.router)
    dp.include_router(admin.router)

    # 🛡️ Подключаем антифлуд
    dp.message.middleware(AntiFloodMiddleware(cooldown=1.0))  # Текст: 1 сек
    dp.callback_query.middleware(AntiFloodMiddleware(cooldown=0.5))  # Кнопки: 0.5 сек

    init_scheduler(bot)
    logging.info("🚀 Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())