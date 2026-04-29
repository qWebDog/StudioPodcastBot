import asyncio
import json
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone, get_booking_details
from keyboards import client_main_kb, back_to_menu_kb, months_kb, dates_kb, time_slots_kb, services_kb, confirm_kb, format_date_display, back_cancel_kb, MONTH_NAMES
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)
STUDIO_TZ = ZoneInfo("Europe/Moscow")  # 🌍 Часовой пояс студии

class BookFSM(StatesGroup):
    month = State()
    date = State()
    slots = State()
    name = State()
    phone = State()
    services = State()

# 🔄 Единый переключатель меню
async def switch_view(cb: CallbackQuery, view: str):
    text = ""
    kb = back_to_menu_kb()

    if view == "main":
        text = "🎙️ Добро пожаловать в студию подкастов! Выберите нужное действие:"
        kb = client_main_kb()
    elif view == "price":
        text = (
            "💰 **Прайс-лист:**\n\n"
            "🎙️ **Звукозапись**\n   └ 1500₽/час\n\n"
            "📹 **Видеосъёмка:**\n"
            "   • 1 камера — 500₽/час\n"
            "   • 2 камеры — 1000₽/час\n"
            "   • 3 камеры — 1500₽/час\n\n"
            "🎬 **Монтаж**\n   └ 5000₽"
        )
    elif view == "contact":
        text = (
            "📞 **Связь с администратором:**\n"
            "👤 Telegram: `@ваш_ник_админа`\n"
            "📱 Телефон: `+7 (999) 123-45-67`\n"
            "🕒 График: 10:00 – 22:00 (МСК)\n"
            "Отвечаем в течение 15 минут."
        )
    elif view == "bookings":
        today = datetime.now().date().strftime("%Y-%m-%d")
        async with async_session() as s:
            res = await s.execute(select(Booking).where(Booking.user_tg_id == cb.from_user.id).order_by(Booking.created_at.desc()).limit(20))
            all_bookings = res.scalars().all()

        active = []
        for b in all_bookings:
            _, slots, _ = await get_booking_details(b.id)
            if not slots: continue
            if slots[0].date >= today and b.status in ["confirmed", "confirmed_reminder"]:
                active.append((b, slots))

        if not active:
            text = "📭 У вас нет активных записей на сегодня или в будущем."
        else:
            text = "📋 **Ваши активные записи:**\n"
            kb = InlineKeyboardBuilder()
            for b, slots in active:
                times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
                text += f"\n🟢 #{b.id} | {format_date_display(slots[0].date)} ⏰ {times}\n💰 {int(b.total_price)}₽"
                kb.button(text=f"Отменить #{b.id}", callback_data=f"my_cancel:{b.id}")
            kb.adjust(1)
            kb.button(text="⬅️ В главное меню", callback_data="view_main")
            kb = kb.as_markup()

    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎙️ Добро пожаловать! Выберите действие:", reply_markup=client_main_kb())

# 🖥 Обработчики меню
@router.callback_query(F.data == "view_main")
async def go_main(cb: CallbackQuery): 
    await switch_view(cb, "main")
    await cb.answer()

@router.callback_query(F.data == "view_price")
async def go_price(cb: CallbackQuery): 
    await switch_view(cb, "price")
    await cb.answer()

@router.callback_query(F.data == "view_contact")
async def go_contact(cb: CallbackQuery): 
    await switch_view(cb, "contact")
    await cb.answer()

@router.callback_query(F.data == "view_bookings")
async def go_bookings(cb: CallbackQuery): 
    await switch_view(cb, "bookings")
    await cb.answer()

# 📅 Бронирование: Шаг 1/6 (Месяцы)
@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    await _show_months(cb, state, is_callback=True)
    await cb.answer()

async def _show_months(event, state: FSMContext, is_callback: bool = False):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= today).distinct())
        dates = [r[0] for r in res]
    
    months_dict = {}
    for d in dates: months_dict.setdefault(d[:7], []).append(d)
    months = sorted(months_dict.keys())

    if not months:
        txt = "❌ Свободных дат пока нет. Попробуйте позже или свяжитесь с админом."
        if is_callback: await event.message.answer(txt)
        else: await event.answer(txt)
        await state.clear()
        return

    await state.set_state(BookFSM.month)
    txt = "📅 **Шаг 1/6:** Выберите месяц:"
    kb = InlineKeyboardBuilder()
    for ym in months:
        year, month = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[month]} {year}", callback_data=f"book_month:{ym}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="view_main"))
    
    if is_callback: await event.message.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    else: await event.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")

@router.callback_query(F.data.startswith("book_month:"))
async def select_month(cb: CallbackQuery, state: FSMContext):
    ym = cb.data.split(":")[1]
    await state.update_data(year_month=ym)
    await state.set_state(BookFSM.date)
    
    async with async_session() as s:
        res = await s.execute(
            select(Slot.date).where(
                Slot.is_active, ~Slot.is_booked, 
                func.strftime('%Y-%m', Slot.date) == ym, 
                Slot.date >= datetime.now().date().strftime("%Y-%m-%d")
            ).distinct().order_by(Slot.date)
        )
        dates = [r[0] for r in res]

    if not dates:
        kb = InlineKeyboardBuilder().button(text="⬅️ Назад к месяцам", callback_data="back_to_months")
        await cb.message.answer("❌ В этом месяце нет свободных дней.", reply_markup=kb.as_markup())
        await cb.answer()
        return

    await cb.message.answer("📆 **Шаг 2/6:** Выберите дату:", reply_markup=dates_kb(dates), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "back_to_months")
async def back_to_months(cb: CallbackQuery, state: FSMContext): 
    await _show_months(cb, state, is_callback=True)
    await cb.answer()

# ⏰ Шаг 3/6 (Время с фильтром 1.5ч)
@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    date_iso = cb.data.split(":")[1]
    await state.update_data(date=date_iso)
    
    now = datetime.now(STUDIO_TZ)
    threshold = now + timedelta(hours=1, minutes=30)
    
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_iso, Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        all_slots = res.scalars().all()
    
    slots = []
    if date_iso == now.strftime("%Y-%m-%d"):
        for sl in all_slots:
            slot_dt = datetime.strptime(f"{date_iso} {sl.start_time}", "%Y-%m-%d %
