import asyncio
import json
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone
from keyboards import welcome_kb, dates_kb, multi_slots_kb, services_kb, confirm_kb
from config import ADMIN_IDS

router = Router()

class BookFSM(StatesGroup):
    phone = State()
    date = State()
    slots = State()
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
        txt = "❌ Нет свободных дат. Попробуйте позже."
        if is_callback: await event.message.answer(txt)
        else: await event.answer(txt)
        await state.clear(); return
    await state.set_state(BookFSM.date)
    txt = "📆 Выберите дату:"
    if is_callback: await event.message.answer(txt, reply_markup=dates_kb(dates))
    else: await event.answer(txt, reply_markup=dates_kb(dates))

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    if not validate_phone(m.text.strip()):
        await m.answer("⚠️ Некорректный номер. Введите только цифры и символы +, -, пробелы (мин. 7 цифр).")
        return
    await state.update_data(phone=m.text.strip())
    async with async_session() as s:
        user = await get_user(m.from_user.id)
        if not user: s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=m.text.strip()))
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
        await cb.message.answer("❌ На эту дату нет свободных часов.")
        await cb.answer(); return
    await state.set_state(BookFSM.slots)
    await state.update_data(selected_slots=[])
    await cb.message.answer("⏰ Выберите часы (можно несколько):", reply_markup=multi_slots_kb(slots, []))
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
        res = await s.execute(select(Slot).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked))
        slots = res.scalars().all()
    try:
        await cb.message.edit_text("⏰ Выберите часы (можно несколько):", reply_markup=multi_slots_kb(slots, sel))
    except: pass
    await cb.answer()

@router.callback_query(F.data == "slots_done")
async def finish_slots(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_slots"):
        await cb.answer("⚠️ Выберите хотя бы один час!", show_alert=True)
        return
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
            f"📋 *Итог:*\n📅 {data['date']}\n⏰ {', '.join(times_str)}\n📞 {data['phone']}\n🎙️ Часы: {int(slot_total)}₽\n💰 Услуги: {int(svc_total)}₽\n💵 *Всего: {int(total)}₽",
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
    if not data.get("selected_slots") or not data.get("phone"):
        await cb.message.answer("⏳ Данные утеряны. Начните заново."); await state.clear(); return

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                await cb.message.answer(f"❌ Слот {sl.start_time}-{sl.end_time if sl else 'N/A'} уже занят.")
                await state.clear(); return
            slots.append(sl)
            
        for sl in slots: sl.is_booked = True
        svc_total = sum(x["price"] for x in data.get("selected_services", []))
        slot_total = sum(sl.price for sl in slots)
        s.add(Booking(user_tg_id=cb.from_user.id, slot_ids=json.dumps(data["selected_slots"]), services=json.dumps([x["id"] for x in data.get("selected_services", [])]), total_price=slot_total+svc_total))
        await s.commit()
    await cb.message.answer(f"✅ Бронь создана! Сумма: {int(slot_total+svc_total)}₽. За 2 часа пришлём напоминание.")
    await state.clear()
    await cb.answer()

# (Функции my_bookings и my_cancel остаются без изменений, они совместимы)
