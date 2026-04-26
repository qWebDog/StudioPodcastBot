import asyncio
import json
import re
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone
from keyboards import welcome_kb, dates_kb, slots_kb, services_kb, confirm_kb
from config import ADMIN_IDS

router = Router()

class BookFSM(StatesGroup):
    phone = State()
    date = State()
    time = State()
    services = State()

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    user = await get_user(m.from_user.id)
    if user and user.phone:
        await state.update_data(phone=user.phone)
        await m.answer(f"🎙️ Привет! Номер `{user.phone}` уже сохранён.\nНачать бронирование?", reply_markup=welcome_kb(), parse_mode="Markdown")
    else:
        await m.answer("🎙️ Добро пожаловать! Укажите номер телефона для связи:", reply_markup=welcome_kb())

@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("phone"):
        await _show_dates(cb, state, is_callback=True)
        await cb.answer()
        return
    await state.set_state(BookFSM.phone)
    await cb.message.answer("📞 Введите номер телефона:")
    await cb.answer()

async def _show_dates(event, state: FSMContext, is_callback: bool = False):
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked).distinct())
        dates = [r[0] for r in res]

    if not dates:
        text = "❌ Нет свободных дат. Попробуйте позже."
        if is_callback: await event.message.answer(text)
        else: await event.answer(text)
        await state.clear()
        return

    await state.set_state(BookFSM.date)
    text = "📆 Выберите дату:"
    if is_callback: await event.message.answer(text, reply_markup=dates_kb(dates))
    else: await event.answer(text, reply_markup=dates_kb(dates))

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    if not validate_phone(m.text.strip()):
        await m.answer("⚠️ Некорректный номер. Введите только цифры и символы +, -, пробелы (мин. 7 цифр).")
        return
    await state.update_data(phone=m.text.strip())
    async with async_session() as s:
        user = await get_user(m.from_user.id)
        if not user:
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=m.text.strip()))
        else: user.phone = m.text.strip()
        await s.commit()
    await _show_dates(m, state, is_callback=False)

@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    await state.update_data(date=cb.data.split(":")[1])
    async with async_session() as s:
        date = (await state.get_data())["date"]
        res = await s.execute(select(Slot).where(Slot.date == date, Slot.is_active, ~Slot.is_booked))
        slots = res.scalars().all()
    
    if not slots:
        await cb.message.answer("❌ На эту дату нет свободных слотов.")
        await cb.answer(); return

    await state.set_state(BookFSM.time)
    await cb.message.answer("⏰ Выберите время работы:", reply_markup=slots_kb(slots))
    await cb.answer()

@router.callback_query(F.data.startswith("book_time:"))
async def select_time(cb: CallbackQuery, state: FSMContext):
    slot_id = int(cb.data.split(":")[1])
    async with async_session() as s:
        slot = await s.get(Slot, slot_id)
        if not slot or slot.is_booked:
            await cb.answer("⛔ Слот только что забронирован", show_alert=True)
            return
        await state.update_data(
            slot_id=slot.id, 
            date=slot.date, 
            time=f"{slot.start_time}-{slot.end_time}", 
            slot_price=slot.price
        )
    
    await state.set_state(BookFSM.services)
    await state.update_data(selected_services=[])
    async with async_session() as s:
        svcs = (await s.execute(select(Service).where(Service.is_active))).scalars().all()
    await cb.message.answer("🛠 Доп. услуги (или 'Завершить'):", reply_markup=services_kb(svcs))
    await cb.answer()

@router.callback_query(F.data.startswith("book_svc:") | (F.data == "book_svcs_done"))
async def manage_services(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if cb.data == "book_svcs_done":
        svc_total = sum(s["price"] for s in data.get("selected_services", []))
        total = data.get("slot_price", 0) + svc_total
        await cb.message.answer(
            f"📋 *Итог:*\n📅 {data['date']} ⏰ {data['time']}\n📞 {data['phone']}\n🎙️ Слот: {int(data.get('slot_price', 0))}₽\n💰 Услуги: {int(svc_total)}₽\n💵 *Всего: {int(total)}₽", 
            reply_markup=confirm_kb(), parse_mode="Markdown"
        )
        await cb.answer(); return
        
    sid = int(cb.data.split(":")[1])
    async with async_session() as s: svc = await s.get(Service, sid)
    sel = data.get("selected_services", [])
    if not any(x["id"] == sid for x in sel): sel.append({"id": svc.id, "name": svc.name, "price": svc.price})
    await state.update_data(selected_services=sel)
    await cb.answer(f"✅ Добавлено: {svc.name}")

@router.callback_query(F.data == "book_confirm")
async def confirm_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("slot_id") or not data.get("phone"):
        await cb.message.answer("⏳ Данные бронирования утеряны. Начните заново.")
        await state.clear(); return

    async with async_session() as s:
        slot = await s.get(Slot, data["slot_id"])
        if not slot or slot.is_booked or not slot.is_active:
            await cb.message.answer("❌ Этот слот уже занят или отменён админом.")
            await state.clear(); return
            
        slot.is_booked = True
        
        svc_total = sum(x["price"] for x in data.get("selected_services", []))
        total_price = slot.price + svc_total
        
        s.add(Booking(
            user_tg_id=cb.from_user.id, 
            slot_id=slot.id, 
            services=json.dumps([x["id"] for x in data.get("selected_services", [])]), 
            total_price=total_price
        ))
        await s.commit()
        
    await cb.message.answer(f"✅ Бронь создана! Сумма: {int(total_price)}₽. За 2 часа пришлём напоминание.")
    await state.clear()
    await cb.answer()

# (Остальные функции: my_bookings, my_cancel - оставьте без изменений)
