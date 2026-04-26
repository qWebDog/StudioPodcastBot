import asyncio
from datetime import datetime
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from aiogram.utils.keyboard import InlineKeyboardBuilder
from database import async_session, User, Slot, Service, Booking, get_user, get_booking_details
from keyboards import admin_kb, slot_list_kb, slot_action_kb, booking_action_kb
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

@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    await m.answer("👑 Панель администратора:", reply_markup=admin_kb())

# --- СОЗДАНИЕ СЛОТА НА ДЕНЬ ---
@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.slot_date)
    await cb.message.answer("📅 Дата (ГГГГ-ММ-ДД):")
    await cb.answer()

@router.message(AdminFSM.slot_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text.strip())
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
    await m.answer("💵 Цена слота за весь день (только цифры):")

@router.message(AdminFSM.slot_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: await m.answer("❌ Некорректная цена."); return
    
    d = await state.get_data()
    async with async_session() as s:
        s.add(Slot(date=d["date"], start_time=d["start_time"], end_time=d["end_time"], price=price))
        await s.commit()
    await m.answer(f"✅ Слот создан: {d['date']} | {d['start_time']}-{d['end_time']} | 💰 {int(price)}₽")
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

# --- ОСТАЛЬНОЕ (Списки, Отмена, Перенос, Рассылка, Фильтры) ---
# (Оставьте без изменений код из предыдущих версий для этих функций)
# Для экономии места показаны только ключевые изменения.
# Убедитесь, что `slot_list_kb` и `slot_action_kb` импортированы из keyboards.py
