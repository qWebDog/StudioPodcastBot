import asyncio
import json
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, func
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database import async_session, User, Slot, Service, Booking, get_user, get_booking_details
from keyboards import parse_admin_date, format_date_display, admin_kb, slot_list_kb, slot_action_kb, booking_action_kb
from config import ADMIN_IDS


router = Router()

class AdminFSM(StatesGroup):
    slot_date = State()
    slot_start = State()
    slot_end = State()
    slot_price = State()
    edit_price = State()
    svc_name = State()
    svc_price = State()
    broadcast = State()
    move_date = State()
    move_start = State()
    move_end = State()
    filter_date = State()
    search_phone = State()
    auto_days = State()
    auto_start = State()
    auto_end = State()
    auto_price = State()

@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    await m.answer("👑 Панель администратора:", reply_markup=admin_kb())

# --- СОЗДАНИЕ ПОЧАСОВЫХ СЛОТОВ ---
@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.slot_date)
    await cb.message.answer("📅 Введите дату (ДД-ММ, например 15-05):")
    await cb.answer()

@router.message(AdminFSM.slot_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_date(m: Message, state: FSMContext):
    try:
        date_iso = parse_admin_date(m.text.strip())
    except ValueError:
        return await m.answer("❌ Ошибка. Введите дату в формате ДД-ММ (например 20-05):")
        
    await state.update_data(date=date_iso)
    await state.set_state(AdminFSM.slot_start)
    await m.answer("⏰ Начало рабочего дня (например, 10:00):")

@router.message(AdminFSM.slot_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_start(m: Message, state: FSMContext):
    await state.update_data(start_time=m.text.strip())
    await state.set_state(AdminFSM.slot_end)
    await m.answer("⏱️ Конец рабочего дня (например, 20:00):")

@router.message(AdminFSM.slot_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_end(m: Message, state: FSMContext):
    await state.update_data(end_time=m.text.strip())
    await state.set_state(AdminFSM.slot_price)
    await m.answer("💵 Цена за 1 час (только цифры):")

@router.message(AdminFSM.slot_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: await m.answer("❌ Некорректная цена."); return

    d = await state.get_data()
    start_dt = datetime.strptime(d["start_time"], "%H:%M")
    end_dt = datetime.strptime(d["end_time"], "%H:%M")
    if start_dt >= end_dt: await m.answer("❌ Конец дня должен быть позже начала."); return

    new_slots = []
    curr = start_dt
    while (curr + timedelta(hours=1)) <= end_dt:
        s_t = curr.strftime("%H:%M")
        e_t = (curr + timedelta(hours=1)).strftime("%H:%M")
        new_slots.append(Slot(date=d["date"], start_time=s_t, end_time=e_t, price=price))
        curr += timedelta(hours=1)

    async with async_session() as s:
        s.add_all(new_slots)
        await s.commit()
    await m.answer(f"✅ Создано {len(new_slots)} слотов на {d['date']}\n🕒 {d['start_time']}-{d['end_time']} | 💰 {int(price)}₽/час")
    await state.clear()

# --- РЕДАКТИРОВАНИЕ ЦЕНЫ ---
@router.callback_query(F.data.startswith("slot_edit_price:"), F.from_user.id.in_(ADMIN_IDS))
async def slot_edit_price(cb: CallbackQuery, state: FSMContext):
    await state.update_data(edit_slot_id=int(cb.data.split(":")[1]))
    await state.set_state(AdminFSM.edit_price)
    await cb.message.answer("💵 Введите новую цену слота:")
    await cb.answer()

@router.message(AdminFSM.edit_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_edit_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: await m.answer("❌ Некорректная цена."); return
    data = await state.get_data()
    async with async_session() as s:
        slot = await s.get(Slot, data["edit_slot_id"])
        if slot:
            slot.price = price
            await s.commit()
            await m.answer(f"✅ Цена слота #{slot.id} обновлена: {int(price)}₽")
        else: await m.answer("❌ Слот не найден")
    await state.clear()

# --- СПИСОК СЛОТОВ (без изменений) ---
@router.callback_query(F.data == "admin_slots_list", F.from_user.id.in_(ADMIN_IDS))
async def list_slots(cb: CallbackQuery, state: FSMContext):
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.is_active).order_by(Slot.date, Slot.start_time).limit(30))
        slots = res.scalars().all()
    if not slots: return await cb.message.answer("Нет активных слотов.")
    await cb.message.answer("📋 Список слотов:", reply_markup=slot_list_kb(slots))
    await cb.answer()

@router.callback_query(F.data.startswith("slot_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def manage_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        slot = await s.get(Slot, sid)
    if not slot: return await cb.answer("Слот не найден", show_alert=True)
    await cb.message.answer(f"Слот: {slot.date} {slot.start_time}-{slot.end_time}\nЗабронирован: {'Да' if slot.is_booked else 'Нет'}", reply_markup=slot_action_kb(sid))
    await cb.answer()

@router.callback_query(F.data.startswith("slot_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def cancel_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        slot = await s.get(Slot, sid)
        if not slot: return
        slot.is_active = False
        if slot.is_booked:
            b = (await s.execute(select(Booking).where(Booking.slot_ids.contains(str(sid)), Booking.status == "confirmed"))).scalar_one_or_none()
            if b:
                b.status = "cancelled_by_admin"
                user = await get_user(b.user_tg_id)
                try: await cb.bot.send_message(b.user_tg_id, f"❌ Админ отменил слот `{slot.date} {slot.start_time}`. Свяжитесь для переноса.", parse_mode="Markdown")
                except: pass
        await s.commit()
    await cb.message.edit_text(f"❌ Слот отменен.", reply_markup=None)
    await cb.answer("Успешно")

# --- БРОНИ (адаптировано под мульти-слоты) ---
@router.callback_query(F.data == "admin_bookings_list", F.from_user.id.in_(ADMIN_IDS))
async def list_bookings(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(15))
        bookings = res.scalars().all()
    if not bookings: return await cb.message.answer("📭 Броней пока нет.")

    msg = "📖 *Последние брони:*\n"
    kb = InlineKeyboardBuilder()
    for b in bookings:
        _, slots, user = await get_booking_details(b.id)
        times = ", ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots]) if slots else "N/A"
        status_emoji = "🟢" if b.status == "confirmed" else "🔴"
        msg += f"\n{status_emoji} #{b.id} | 📅 {slots[0].date if slots else '?'} ⏰ {times}\n👤 {user.username or 'Нет'} | 💰 {int(b.total_price)}₽"
        kb.button(text=f"#{b.id}", callback_data=f"adm_manage:{b.id}")
    kb.adjust(2)
    kb.button(text="🔙 В меню", callback_data="admin_menu")

    await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("adm_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_manage(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b, slots, user = await get_booking_details(bid)
    if not b: return await cb.answer("❌ Не найдено", show_alert=True)

    times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots]) if slots else "N/A"
    date = slots[0].date if slots else "?"
    status_map = {"confirmed": "🟢 Активна", "cancelled": "❌ Отменена", "cancelled_by_admin": "⛔ Отм. админом"}
    st = status_map.get(b.status, b.status)

    txt = f"🆔 Бронь #{b.id}\n👤 Клиент: @{user.username or 'Нет'}\n📞 Телефон: `{user.phone or 'Нет'}`\n📅 Дата: {date}\n⏰ Часы: {times}\n💰 Сумма: {int(b.total_price)}₽\n📊 Статус: {st}"

    await cb.message.edit_text(txt, parse_mode="Markdown", reply_markup=booking_action_kb(b.id, b.status))
    await cb.answer()

# --- АВТОПРОДЛЕНИЕ РАСПИСАНИЯ ---
@router.callback_query(F.data == "admin_auto_extend", F.from_user.id.in_(ADMIN_IDS))
async def start_auto_extend(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.auto_days)
    await cb.message.answer("📅 Введите количество дней для продления (например, 7 или 30):")
    await cb.answer()

@router.message(AdminFSM.auto_days, F.from_user.id.in_(ADMIN_IDS))
async def proc_auto_days(m: Message, state: FSMContext):
    try: days = int(m.text.strip())
    except: return await m.answer("❌ Введите целое число.")
    if days <= 0 or days > 365: return await m.answer("❌ От 1 до 365 дней.")
    await state.update_data(days=days)
    await state.set_state(AdminFSM.auto_start)
    await m.answer("⏰ Начало рабочего дня (например, 10:00):")

@router.message(AdminFSM.auto_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_auto_start(m: Message, state: FSMContext):
    await state.update_data(start_time=m.text.strip())
    await state.set_state(AdminFSM.auto_end)
    await m.answer("⏱️ Конец рабочего дня (например, 20:00):")

@router.message(AdminFSM.auto_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_auto_end(m: Message, state: FSMContext):
    await state.update_data(end_time=m.text.strip())
    await state.set_state(AdminFSM.auto_price)
    await m.answer("💵 Цена за 1 час (только цифры):")

@router.message(AdminFSM.auto_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_auto_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: return await m.answer("❌ Некорректная цена.")
    
    data = await state.get_data()
    
    # 📅 Определяем дату старта (следующий день после последнего слота в БД)
    async with async_session() as s:
        res = await s.execute(select(func.max(Slot.date)))
        max_date_str = res.scalar()
        
        if max_date_str:
            start_date = datetime.strptime(max_date_str, "%Y-%m-%d").date() + timedelta(days=1)
        else:
            from datetime import date
            start_date = date.today() + timedelta(days=1)
            
        # Валидация времени
        start_dt = datetime.strptime(data["start_time"], "%H:%M")
        end_dt = datetime.strptime(data["end_time"], "%H:%M")
        if start_dt >= end_dt:
            return await m.answer("❌ Конец дня должен быть позже начала.")
            
        new_slots = []
        skipped = 0
        
        for i in range(data["days"]):
            d = start_date + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            
            # Пропускаем даты, где уже есть слоты
            check = await s.execute(select(Slot.id).where(Slot.date == date_str).limit(1))
            if check.scalar():
                skipped += 1
                continue
                
            curr = start_dt
            while (curr + timedelta(hours=1)) <= end_dt:
                new_slots.append(Slot(
                    date=date_str,
                    start_time=curr.strftime("%H:%M"),
                    end_time=(curr + timedelta(hours=1)).strftime("%H:%M"),
                    price=price
                ))
                curr += timedelta(hours=1)
                
        if not new_slots:
            return await m.answer(f"⚠️ На все {data['days']} дней слоты уже существуют.")
            
        s.add_all(new_slots)
        await s.commit()
        
    await m.answer(
        f"✅ Расписание продлено!\n"
        f"📅 Период: {start_date} → {start_date + timedelta(days=data['days']-1)}\n"
        f"🕒 Создано слотов: `{len(new_slots)}`\n"
        f"⏭️ Пропущено дат: `{skipped}`\n"
        f"💰 Цена/час: `{int(price)}₽`"
    )
    await state.clear()

@router.callback_query(F.data == "admin_menu")
async def go_back_to_admin(cb: CallbackQuery):
    await cb.message.answer("👑 Панель администратора:", reply_markup=admin_kb())
    await cb.answer()
