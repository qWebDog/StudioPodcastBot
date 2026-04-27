import asyncio, json, logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, update, func
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database import async_session, User, Slot, Service, Booking, get_user, get_booking_details
from keyboards import admin_kb, slot_list_kb, slot_action_kb, booking_action_kb, format_date_display
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

class AdminFSM(StatesGroup):
    slot_date = State(); slot_start = State(); slot_end = State(); slot_price = State()
    edit_price = State(); svc_name = State(); svc_price = State(); broadcast = State()
    move_date = State(); move_start = State(); move_end = State()
    filter_date = State(); search_phone = State()
    auto_days = State(); auto_start = State(); auto_end = State(); auto_price = State()

@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    await m.answer("👑 Панель администратора:", reply_markup=admin_kb())

@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.slot_date); await cb.message.answer("📅 Введите дату (ДД-ММ, например 15-05):"); await cb.answer()

@router.message(AdminFSM.slot_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_date(m: Message, state: FSMContext):
    from keyboards import parse_admin_date
    try: date_iso = parse_admin_date(m.text.strip())
    except ValueError: return await m.answer("❌ Ошибка. Введите ДД-ММ:")
    await state.update_data(date=date_iso); await state.set_state(AdminFSM.slot_start); await m.answer("⏰ Начало рабочего дня (10:00):")

@router.message(AdminFSM.slot_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_start(m: Message, state: FSMContext):
    await state.update_data(start_time=m.text.strip()); await state.set_state(AdminFSM.slot_end); await m.answer("⏱️ Конец рабочего дня (20:00):")

@router.message(AdminFSM.slot_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_end(m: Message, state: FSMContext):
    await state.update_data(end_time=m.text.strip()); await state.set_state(AdminFSM.slot_price); await m.answer("💵 Цена за 1 час:")

@router.message(AdminFSM.slot_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: return await m.answer("❌ Некорректная цена.")
    d = await state.get_data()
    start_dt = datetime.strptime(d["start_time"], "%H:%M"); end_dt = datetime.strptime(d["end_time"], "%H:%M")
    if start_dt >= end_dt: return await m.answer("❌ Конец позже начала.")
    new_slots = []
    curr = start_dt
    while (curr + timedelta(hours=1)) <= end_dt:
        new_slots.append(Slot(date=d["date"], start_time=curr.strftime("%H:%M"), end_time=(curr + timedelta(hours=1)).strftime("%H:%M"), price=price))
        curr += timedelta(hours=1)
    async with async_session() as s: s.add_all(new_slots); await s.commit()
    await m.answer(f"✅ Создано {len(new_slots)} слотов на {d['date']}\n💰 {int(price)}₽/час"); await state.clear()

@router.callback_query(F.data == "admin_slots_list", F.from_user.id.in_(ADMIN_IDS))
async def list_slots(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.is_active).order_by(Slot.date, Slot.start_time).limit(30))
        slots = res.scalars().all()
    if not slots: return await cb.message.answer("Нет активных слотов.")
    await cb.message.answer("📋 Список слотов:", reply_markup=slot_list_kb(slots)); await cb.answer()

@router.callback_query(F.data.startswith("slot_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def manage_slot(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s: slot = await s.get(Slot, sid)
    if not slot: return await cb.answer("Слот не найден", show_alert=True)
    await cb.message.answer(f"Слот: {slot.date} {slot.start_time}-{slot.end_time}\nЗабронирован: {'Да' if slot.is_booked else 'Нет'}", reply_markup=slot_action_kb(sid)); await cb.answer()

@router.callback_query(F.data.startswith("slot_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def cancel_slot(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        slot = await s.get(Slot, sid)
        if not slot: return
        slot.is_active = False
        if slot.is_booked:
            b = (await s.execute(select(Booking).where(Booking.slot_ids.contains(str(sid)), Booking.status == "confirmed"))).scalar_one_or_none()
            if b: b.status = "cancelled_by_admin"; await s.commit()
            user = await get_user(b.user_tg_id)
            try: await cb.bot.send_message(b.user_tg_id, f"❌ Админ отменил слот `{slot.date} {slot.start_time}`. Свяжитесь для переноса.", parse_mode="Markdown")
            except: pass
        await s.commit()
    await cb.message.edit_text(f"❌ Слот отменен.", reply_markup=None); await cb.answer("Успешно")

@router.callback_query(F.data.startswith("slot_edit_price:"), F.from_user.id.in_(ADMIN_IDS))
async def slot_edit_price(cb: CallbackQuery, state: FSMContext):
    await state.update_data(edit_slot_id=int(cb.data.split(":")[1])); await state.set_state(AdminFSM.edit_price)
    await cb.message.answer("💵 Введите новую цену слота:"); await cb.answer()

@router.message(AdminFSM.edit_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_edit_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: return await m.answer("❌ Некорректная цена.")
    data = await state.get_data()
    async with async_session() as s:
        slot = await s.get(Slot, data["edit_slot_id"])
        if slot: slot.price = price; await s.commit(); await m.answer(f"✅ Цена обновлена: {int(price)}₽")
        else: await m.answer("❌ Слот не найден")
    await state.clear()

@router.callback_query(F.data.startswith("slot_move:"), F.from_user.id.in_(ADMIN_IDS))
async def start_move_slot(cb: CallbackQuery, state: FSMContext):
    await state.update_data(move_slot_id=int(cb.data.split(":")[1])); await state.set_state(AdminFSM.move_date)
    await cb.message.answer("📅 Новая дата (ДД-ММ):"); await cb.answer()

@router.message(AdminFSM.move_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_date(m: Message, state: FSMContext):
    from keyboards import parse_admin_date
    try: date_iso = parse_admin_date(m.text.strip())
    except: return await m.answer("❌ Формат ДД-ММ:")
    await state.update_data(move_date=date_iso); await state.set_state(AdminFSM.move_start); await m.answer("⏰ Новое начало (10:00):")

@router.message(AdminFSM.move_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_start(m: Message, state: FSMContext):
    await state.update_data(move_start=m.text.strip()); await state.set_state(AdminFSM.move_end); await m.answer("⏱️ Новое окончание (11:00):")

@router.message(AdminFSM.move_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_end(m: Message, state: FSMContext):
    await state.update_data(move_end=m.text.strip()); data = await state.get_data()
    async with async_session() as s:
        slot = await s.get(Slot, data["move_slot_id"])
        if not slot: return await m.answer("❌ Ошибка")
        old_d, old_t = slot.date, slot.start_time
        slot.date, slot.start_time, slot.end_time = data["move_date"], data["move_start"], data["move_end"]
        if slot.is_booked:
            b = (await s.execute(select(Booking).where(Booking.slot_ids.contains(str(slot.id)), Booking.status == "confirmed"))).scalar_one_or_none()
            if b:
                user = await get_user(b.user_tg_id)
                try: await m.bot.send_message(b.user_tg_id, f"🔄 Админ перенёс ваш слот.\n📅 Было: `{old_d} {old_t}`\n📅 Стало: `{slot.date} {slot.start_time}`", parse_mode="Markdown")
                except: pass
        await s.commit()
    await m.answer(f"✅ Слот перенесён: {slot.date} {slot.start_time}-{slot.end_time}"); await state.clear()

@router.callback_query(F.data == "admin_bookings_list", F.from_user.id.in_(ADMIN_IDS))
async def list_bookings(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(15)); bookings = res.scalars().all()
    if not bookings: return await cb.message.answer("📭 Броней пока нет.")
    msg = "📖 **Последние брони:**\n"; kb = InlineKeyboardBuilder()
    for b in bookings:
        _, slots, user = await get_booking_details(b.id)
        if not slots: continue
        times = ", ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
        st = "🟢" if b.status == "confirmed" else "🔴"
        msg += f"\n{st} #{b.id} | 📅 {format_date_display(slots[0].date)} ⏰ {times}\n👤 {user.username or 'Нет'} | 💰 {int(b.total_price)}₽"
        kb.button(text=f"#{b.id}", callback_data=f"adm_manage:{b.id}")
    kb.adjust(2); kb.button(text="🔙 В меню", callback_data="admin_menu")
    await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup()); await cb.answer()

@router.callback_query(F.data.startswith("adm_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_manage(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b, slots, user = await get_booking_details(bid)
    if not b: return await cb.answer("❌ Не найдено", show_alert=True)
    times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots]) if slots else "N/A"
    st_map = {"confirmed": "🟢 Активна", "cancelled": "❌ Отменена", "cancelled_by_admin": "⛔ Отм. админом", "confirmed_reminder": "🟢 Подтв."}
    txt = f"🆔 Бронь #{b.id}\n👤 Клиент: @{user.username or 'Нет'}\n📞 Телефон: `{user.phone or 'Нет'}`\n📅 Дата: {format_date_display(slots[0].date) if slots else '?'}\n⏰ Часы: {times}\n💰 Сумма: {int(b.total_price)}₽\n📊 Статус: {st_map.get(b.status, b.status)}"
    await cb.message.edit_text(txt, parse_mode="Markdown", reply_markup=booking_action_kb(b.id, b.status)); await cb.answer()

@router.callback_query(F.data.startswith("adm_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.status != "confirmed": return await cb.answer("⛔", show_alert=True)
        b.status = "cancelled_by_admin"
        for sid in json.loads(b.slot_ids):
            sl = await s.get(Slot, sid)
            if sl: sl.is_booked = False
        await s.commit()
    user = await get_user(b.user_tg_id)
    try: await cb.bot.send_message(b.user_tg_id, f"❌ Администратор отменил вашу бронь #{bid}. Свяжитесь для переноса.")
    except: pass
    await cb.message.edit_text(f"❌ Бронь #{bid} отменена.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_bookings_list").as_markup())
    await cb.answer("Успешно")

@router.callback_query(F.data.startswith("adm_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_confirm(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b: return await cb.answer("⛔", show_alert=True)
        b.status = "confirmed"; await s.commit()
    await cb.answer("✅ Подтверждено")
    await cb.message.edit_text(f"✅ Бронь #{bid} подтверждена.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад", callback_data="admin_bookings_list").as_markup())

@router.callback_query(F.data == "admin_menu")
async def go_back_to_admin(cb: CallbackQuery):
    await cb.message.answer("👑 Панель администратора:", reply_markup=admin_kb()); await cb.answer()
