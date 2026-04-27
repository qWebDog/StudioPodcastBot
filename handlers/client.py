import asyncio
import json
import logging
from datetime import datetime
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone, get_booking_details
from keyboards import welcome_kb, dates_kb, time_slots_kb, services_kb, confirm_kb, format_date_display
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

class BookFSM(StatesGroup):
    date = State()
    slots = State()
    name = State()
    phone = State()
    services = State()

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎙️ Добро пожаловать! Начнём бронирование.", reply_markup=welcome_kb())

@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    await _show_dates(cb, state, is_callback=True)
    await cb.answer()

async def _show_dates(event, state: FSMContext, is_callback: bool = False):
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked).distinct())
        dates = [r[0] for r in res]
    if not dates:
        txt = "❌ Свободных дат пока нет. Попробуйте позже или свяжитесь с админом."
        if is_callback: await event.message.answer(txt)
        else: await event.answer(txt)
        await state.clear(); return
    
    await state.set_state(BookFSM.date)
    txt = "📅 **Шаг 1/5:** Выберите удобную дату:"
    if is_callback: await event.message.answer(txt, reply_markup=dates_kb(dates), parse_mode="Markdown")
    else: await event.answer(txt, reply_markup=dates_kb(dates), parse_mode="Markdown")

@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    date_iso = cb.data.split(":")[1]
    await state.update_data(date=date_iso)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_iso, Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        slots = res.scalars().all()
    if not slots:
        await cb.message.answer("❌ На эту дату все часы заняты. Выберите другую.")
        await cb.answer(); return
    
    # Цена берётся из первого слота (для дня она одинаковая)
    price_per_hour = int(slots[0].price)
    await state.set_state(BookFSM.slots)
    await state.update_data(selected_slots=[])
    
    await cb.message.answer(
        f"📆 {format_date_display(date_iso)} | 💰 **{price_per_hour}₽/час**\n"
        f"⏰ **Шаг 2/5:** Выберите нужные часы (можно несколько):",
        reply_markup=time_slots_kb(slots, []),
        parse_mode="Markdown"
    )
    await cb.answer()

@router.callback_query(F.data.startswith("slot_toggle:"))
async def toggle_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    data = await state.get_data()
    sel = data.get("selected_slots", [])
    if sid in sel: sel.remove(sid)
    else: sel.append(sid)
    await state.update_data(selected_slots=sel)
    
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        slots = res.scalars().all()
    try:
        await cb.message.edit_reply_markup(reply_markup=time_slots_kb(slots, sel))
    except Exception: pass
    await cb.answer()

@router.callback_query(F.data == "slots_done")
async def finish_slots(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_slots"):
        await cb.answer("⚠️ Выберите хотя бы один час!", show_alert=True)
        return
    await state.set_state(BookFSM.name)
    
    # Проверяем, есть ли сохранённое имя
    user = await get_user(cb.from_user.id)
    saved_name = f" (Сохранённое: `{user.client_name}`)" if user and user.client_name else ""
    await cb.message.answer(f"👤 **Шаг 3/5:** Введите ваше имя{saved_name}:")
    await cb.answer()

@router.message(BookFSM.name)
async def save_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2:
        return await m.answer("⚠️ Имя должно содержать минимум 2 символа.")
    await state.update_data(client_name=name)
    async with async_session() as s:
        user = await get_user(m.from_user.id)
        if not user:
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, client_name=name))
        else:
            user.client_name = name
        await s.commit()
        
    await state.set_state(BookFSM.phone)
    user = await get_user(m.from_user.id)
    saved_phone = f" (Сохранённый: `{user.phone}`)" if user and user.phone else ""
    await m.answer(f"📞 **Шаг 4/5:** Введите номер телефона{saved_phone}:")

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    if not validate_phone(m.text.strip()):
        return await m.answer("⚠️ Некорректный номер. Введите только цифры и символы +, -, пробелы (мин. 7 цифр).")
    await state.update_data(phone=m.text.strip())
    async with async_session() as s:
        user = await get_user(m.from_user.id)
        if not user: s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=m.text.strip()))
        else: user.phone = m.text.strip()
        await s.commit()
        
    await state.set_state(BookFSM.services)
    await state.update_data(selected_services=[])
    async with async_session() as s:
        svcs = (await s.execute(select(Service).where(Service.is_active))).scalars().all()
    await m.answer("🛠 **Шаг 5/5:** Выберите доп. услуги (или 'Завершить'):", reply_markup=services_kb(svcs), parse_mode="Markdown")

@router.callback_query(F.data.startswith("book_svc:") | (F.data == "book_svcs_done"))
async def manage_services(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if cb.data == "book_svcs_done":
        svc_total = sum(s["price"] for s in data.get("selected_services", []))
        slot_total = 0
        times_str = []
        async with async_session() as s:
            for sid in data["selected_slots"]:
                sl = await s.get(Slot, sid)
                if sl: 
                    slot_total += sl.price
                    times_str.append(f"{sl.start_time}-{sl.end_time}")
        total = slot_total + svc_total
        await cb.message.answer(
            f"📋 **Итог бронирования:**\n"
            f"👤 {data['client_name']}\n"
            f"📞 {data['phone']}\n"
            f"📅 {format_date_display(data['date'])}\n"
            f"⏰ {', '.join(times_str)}\n"
            f"🎙️ Часы: {int(slot_total)}₽\n"
            f"💰 Услуги: {int(svc_total)}₽\n"
            f"💵 **Всего: {int(total)}₽**",
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
    if not data.get("selected_slots") or not data.get("client_name") or not data.get("phone"):
        await cb.message.answer("⏳ Данные утеряны. Начните заново по команде /start")
        await state.clear(); return

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                await cb.message.answer(f"❌ Слот {sl.start_time if sl else 'N/A'}-{sl.end_time if sl else ''} только что забронировали.")
                await state.clear(); return
            slots.append(sl)
            
        for sl in slots: sl.is_booked = True
        svc_total = sum(x["price"] for x in data.get("selected_services", []))
        slot_total = sum(sl.price for sl in slots)
        s.add(Booking(
            user_tg_id=cb.from_user.id, 
            slot_ids=json.dumps(data["selected_slots"]), 
            services=json.dumps([x["id"] for x in data.get("selected_services", [])]), 
            total_price=slot_total+svc_total
        ))
        await s.commit()
    await cb.message.answer(f"✅ Бронь на {format_date_display(data['date'])} создана! Сумма: {int(slot_total+svc_total)}₽. За 2 часа пришлём напоминание.")
    await state.clear()
    await cb.answer()

# (my_bookings, my_cancel, _notify_admins остаются из прошлой версии без изменений)
