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
from keyboards import welcome_kb, dates_kb, times_kb, services_kb, confirm_kb
from config import ADMIN_IDS

router = Router()

class BookFSM(StatesGroup):
    phone = State()
    date = State()
    time = State()
    services = State()

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear() # Очищаем старые сессии
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

# 🛠 УНИВЕРСАЛЬНАЯ ФУНКЦИЯ
async def _show_dates(event, state: FSMContext, is_callback: bool = False):
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked).distinct())
        dates = [r[0] for r in res]

    if not dates:
        text = "❌ Нет свободных дат. Попробуйте позже."
        if is_callback:
            await event.message.answer(text)
        else:
            await event.answer(text)
        await state.clear()
        return

    await state.set_state(BookFSM.date)
    text = "📆 Выберите дату:"
    if is_callback:
        await event.message.answer(text, reply_markup=dates_kb(dates))
    else:
        await event.answer(text, reply_markup=dates_kb(dates))

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
        else:
            user.phone = m.text.strip()
        await s.commit()
    await _show_dates(m, state, is_callback=False)

@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    await state.update_data(date=cb.data.split(":")[1])
    async with async_session() as s:
        # Берем дату из состояния
        data = await state.get_data()
        res = await s.execute(select(Slot.start_time).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked))
        times = [r[0] for r in res]
    await state.set_state(BookFSM.time)
    await cb.message.answer("⏰ Выберите время:", reply_markup=times_kb(times))
    await cb.answer()

@router.callback_query(F.data.startswith("book_time:"))
async def select_time(cb: CallbackQuery, state: FSMContext):
    await state.update_data(time=cb.data.split(":")[1])
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
        total = sum(s["price"] for s in data.get("selected_services", []))
        await cb.message.answer(f"📋 *Итог:*\n📅 {data['date']} ⏰ {data['time']}\n📞 {data['phone']}\n💰 {int(total)}₽", reply_markup=confirm_kb(), parse_mode="Markdown")
        await cb.answer()
        return
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        svc = await s.get(Service, sid)
    sel = data.get("selected_services", [])
    if not any(x["id"] == sid for x in sel):
        sel.append({"id": svc.id, "name": svc.name, "price": svc.price})
    await state.update_data(selected_services=sel)
    await cb.answer(f"✅ Добавлено: {svc.name}")

@router.callback_query(F.data == "book_confirm")
async def confirm_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    
    # 🛡️ ЗАЩИТА: Проверка наличия данных (если бот перезагрузился в процессе)
    if not data.get("date") or not data.get("time") or not data.get("phone"):
        await cb.message.answer("⏳ Сессия бронирования истекла. Пожалуйста, начните заново.")
        await state.clear()
        return

    async with async_session() as s:
        # Ищем слот
        res = await s.execute(select(Slot).where(
            Slot.date == data["date"], 
            Slot.start_time == data["time"], 
            ~Slot.is_booked, 
            Slot.is_active
        ))
        slot = res.scalar_one_or_none()
        
        if not slot:
            await cb.message.answer("❌ Этот слот только что забронировали. Выберите другое время.")
            await state.clear()
            return
            
        # Бронируем слот
        slot.is_booked = True
        total = sum(x["price"] for x in data.get("selected_services", []))
        s.add(Booking(
            user_tg_id=cb.from_user.id, 
            slot_id=slot.id, 
            services=json.dumps([x["id"] for x in data.get("selected_services", [])]), 
            total_price=total
        ))
        await s.commit()
        
    await cb.message.answer("✅ Бронь создана! За 2 часа пришлём напоминание.")
    await state.clear()
    await cb.answer()

@router.callback_query(F.data == "my_bookings")
async def show_my_bookings(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).where(Booking.user_tg_id == cb.from_user.id).order_by(Booking.created_at.desc()).limit(10))
        bookings = res.scalars().all()

    if not bookings:
        await cb.message.answer("📭 У вас пока нет записей.")
        await cb.answer()
        return

    msg = "📋 *Ваши записи:*\n"
    kb = InlineKeyboardBuilder()
    
    for b in bookings:
        slot = await s.get(Slot, b.slot_id)
        status_map = {"confirmed": "🟢", "cancelled": "❌", "cancelled_by_admin": "⛔", "completed": "✅"}
        st_icon = status_map.get(b.status, "❓")
        msg += f"\n{st_icon} #{b.id} | {slot.date} {slot.start_time} | 💰 {int(b.total_price)}₽"
        if b.status == "confirmed":
            kb.button(text=f"Отменить #{b.id}", callback_data=f"my_cancel:{b.id}")
            
    if kb.buttons:
        kb.adjust(1)
        await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    else:
        await cb.message.answer(msg, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("my_cancel:"))
async def my_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status != "confirmed":
            return await cb.answer("⛔ Нельзя отменить эту запись", show_alert=True)
        
        b.status = "cancelled"
        slot = await s.get(Slot, b.slot_id)
        if slot: slot.is_booked = False
        await s.commit()

    await cb.message.edit_text("❌ Вы отменили запись. Слот освобождён.")
    await cb.answer()
    
    user = await get_user(cb.from_user.id)
    tag = f"@{user.username}" if user and user.username else f"ID:{cb.from_user.id}"
    for aid in ADMIN_IDS:
        try: await cb.bot.send_message(aid, f"🔔 Клиент {tag} отменил бронь #{bid}")
        except: pass
        await asyncio.sleep(0.3)
