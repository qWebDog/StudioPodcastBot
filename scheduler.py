import logging, json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select, join
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from database import async_session, Booking, Slot, User, get_user
from config import TIMEZONE, ADMIN_IDS

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()
tz = ZoneInfo(TIMEZONE)

def init_scheduler(bot: Bot):
    scheduler.add_job(check_reminders, "interval", minutes=5, args=[bot], replace_existing=True)
    scheduler.start()
    logger.info("🕒 Scheduler started")

async def check_reminders(bot: Bot):
    now = datetime.now(tz)
    target = now + timedelta(hours=2, minutes=10)
    
    async with async_session() as s:
        res = await s.execute(select(Booking).where(Booking.status == "confirmed", ~Booking.reminder_sent))
        bookings = res.scalars().all()

    for b in bookings:
        try:
            slot_ids = json.loads(b.slot_ids)
            if not slot_ids: continue
            sl = await s.get(Slot, slot_ids[0])
            if not sl: continue
            
            start = datetime.strptime(f"{sl.date} {sl.start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            if now <= start <= target:
                kb = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"rem_confirm:{b.id}")],
                    [InlineKeyboardButton(text="❌ Отменить", callback_data=f"rem_cancel:{b.id}")]
                ])
                await bot.send_message(b.user_tg_id, f"🎙️ *Напоминание:*\nЗапись через ~2 часа (`{sl.date} {sl.start_time}`).\nПодтвердите или отмените.", reply_markup=kb, parse_mode="Markdown")
                b.reminder_sent = True
                await s.commit()
        except Exception as e:
            logger.error(f"Reminder error #{b.id}: {e}")
