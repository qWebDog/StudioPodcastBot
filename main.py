import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties  # 🆕 Новый импорт
from config import BOT_TOKEN
from database import init_db
from handlers import client, admin
from scheduler import init_scheduler
from middleware.antiflood import AntiFloodMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

async def main():
    await init_db()
    
    # ✅ Исправленная инициализация бота для aiogram 3.7+
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode="Markdown")
    )
    
    dp = Dispatcher(storage=MemoryStorage())
    
    dp.include_router(client.router)
    dp.include_router(admin.router)
    
    # 🛡️ Антифлуд
    dp.message.middleware(AntiFloodMiddleware(cooldown=1.0))
    dp.callback_query.middleware(AntiFloodMiddleware(cooldown=0.5))
    
    init_scheduler(bot)
    logging.info("🚀 Bot started successfully")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
