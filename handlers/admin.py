import asyncio
from datetime import datetime
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select
from database import async_session, User, Slot, Service, get_user
from keyboards import admin_kb, slot_list_kb, slot_action_kb
from config import ADMIN_IDS

router = Router()

class AdminFSM(StatesGroup):
    slot_date = State()
    slot_start = State()
    slot_end = State()
    svc_name = State()
    svc_price = State()
    broadcast = State()
    move_date = State()
    move_start = State()
    move_end = State()

@router.message(Command("admin"))
async def cmd_admin(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    await m.answer("👑 Панель администратора:", reply_markup=admin_kb())

@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def admin_add_slot(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.slot_date)
    await cb.message.answer("📅 Дата (YYYY-MM-DD):")
    await cb.answer()

@router.message(AdminFSM.slot_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_date(m: Message, state: FSMContext):
    await state.update_data(date=m.text.strip())
    await state.set_state(AdminFSM.slot_start)
    await m.answer("⏰ Начало (10:00):")

@router.message(AdminFSM.slot_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_start(m: Message, state: FSMContext):
    await state.update_data(start=m.text.strip())
    await state.set_state(AdminFSM.slot_end)
    await m.answer("⏱️ Окончание (11:00):")

@router.message(AdminFSM.slot_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_slot_end(m: Message, state: FSMContext):
    d = await state.get_data()
    async with async_session() as s:
        s.add(Slot(date=d["date"], start_time=d["start"], end_time=d["end"]))
        await s.commit()
    await m.answer(f"✅ Слот добавлен: {d['date']} {d['start']}-{d['end']}")
    await state.clear()

# --- Управление слотами ---
@router.callback_query(F.data == "admin_slots_list", F.from_user.id.in_(ADMIN_IDS))
async def list_slots(cb: CallbackQuery, state: FSMContext):
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.is_active).order_by(Slot.date, Slot.start_time).limit(20))
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
            b = (await s.execute(select(Booking).where(Booking.slot_id == sid, Booking.status == "confirmed"))).scalar_one_or_none()
            if b:
                b.status = "cancelled_by_admin"
                user = await get_user(b.user_tg_id)
                try:
                    await cb.bot.send_message(b.user_tg_id, f"❌ Администратор отменил ваш слот `{slot.date} {slot.start_time}`.\nСвяжитесь для переноса.", parse_mode="Markdown")
                except: pass
        await s.commit()
    await cb.message.edit_text(f"❌ Слот отменен.", reply_markup=None)
    await cb.answer("Успешно")

@router.callback_query(F.data.startswith("slot_move:"), F.from_user.id.in_(ADMIN_IDS))
async def start_move_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1])
    await state.update_data(move_slot_id=sid)
    await state.set_state(AdminFSM.move_date)
    await cb.message.answer("📅 Новая дата (YYYY-MM-DD):")
    await cb.answer()

@router.message(AdminFSM.move_date, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_date(m: Message, state: FSMContext):
    await state.update_data(move_date=m.text.strip())
    await state.set_state(AdminFSM.move_start)
    await m.answer("⏰ Новое начало (10:00):")

@router.message(AdminFSM.move_start, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_start(m: Message, state: FSMContext):
    await state.update_data(move_start=m.text.strip())
    await state.set_state(AdminFSM.move_end)
    await m.answer("⏱️ Новое окончание (11:00):")

@router.message(AdminFSM.move_end, F.from_user.id.in_(ADMIN_IDS))
async def proc_move_end(m: Message, state: FSMContext):
    data = await state.get_data()
    sid = data["move_slot_id"]
    async with async_session() as s:
        slot = await s.get(Slot, sid)
        if not slot: return await m.answer("❌ Ошибка")
        old_d, old_t = slot.date, slot.start_time
        slot.date, slot.start_time, slot.end_time = data["move_date"], data["move_start"], data["move_end"]
        if slot.is_booked:
            b = (await s.execute(select(Booking).where(Booking.slot_id == sid, Booking.status == "confirmed"))).scalar_one_or_none()
            if b:
                user = await get_user(b.user_tg_id)
                try:
                    await m.bot.send_message(b.user_tg_id, f"🔄 Админ перенёс ваш слот.\n📅 Было: `{old_d} {old_t}`\n📅 Стало: `{slot.date} {slot.start_time}`", parse_mode="Markdown")
                except: pass
        await s.commit()
    await m.answer(f"✅ Слот перенесён: {slot.date} {slot.start_time}-{slot.end_time}")
    await state.clear()

# --- Услуги и рассылка (без изменений) ---
@router.callback_query(F.data == "admin_services", F.from_user.id.in_(ADMIN_IDS))
async def admin_svc(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.svc_name); await cb.message.answer("💼 Название услуги:"); await cb.answer()

@router.message(AdminFSM.svc_name, F.from_user.id.in_(ADMIN_IDS))
async def proc_svc_name(m: Message, state: FSMContext):
    await state.update_data(name=m.text.strip()); await state.set_state(AdminFSM.svc_price); await m.answer("💵 Цена:")

@router.message(AdminFSM.svc_price, F.from_user.id.in_(ADMIN_IDS))
async def proc_svc_price(m: Message, state: FSMContext):
    try: price = float(m.text.replace(",", "."))
    except: await m.answer("❌"); return
    d = await state.get_data(); async with async_session() as s: s.add(Service(name=d["name"], price=price)); await s.commit()
    await m.answer(f"✅ Услуга добавлена"); await state.clear()

@router.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.broadcast); await cb.message.answer("📤 Текст рассылки:"); await cb.answer()

@router.message(AdminFSM.broadcast, F.from_user.id.in_(ADMIN_IDS))
async def proc_broadcast(m: Message, state: FSMContext):
    await m.answer("🔄 Отправка..."); async with async_session() as s: ids = (await s.execute(select(User.tg_id))).scalars().all()
    ok = 0
    for uid in ids:
        try: await m.bot.send_message(uid, m.text); ok += 1
        except: pass
        await asyncio.sleep(0.3)
    await m.answer(f"✅ Готово. Доставлено: {ok}"); await state.clear()

@router.callback_query(F.data == "admin_bookings_list", F.from_user.id.in_(ADMIN_IDS))
async def list_bookings(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(15))
        bookings = res.scalars().all()
    if not bookings: return await cb.message.answer("📭 Броней пока нет.")

    msg = "📖 *Последние брони:*\n"
    kb = InlineKeyboardBuilder()
    for b in bookings:
        slot = await s.get(Slot, b.slot_id)
        user = await get_user(b.user_tg_id)
        status_emoji = "🟢" if b.status == "confirmed" else "🔴"
        msg += f"\n{status_emoji} #{b.id} | {slot.date} {slot.start_time}\n👤 {user.username or 'Нет'} | 📞 {user.phone or 'Нет'}"
        kb.button(text=f"#{b.id}", callback_data=f"adm_manage:{b.id}")
    kb.adjust(3)

    await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("adm_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_manage(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    b, slot, user = await get_booking_details(bid)
    if not b: return await cb.answer("❌ Не найдено", show_alert=True)

    status_map = {"confirmed": "🟢 Активна", "cancelled": "❌ Отменена", "cancelled_by_admin": "⛔ Отм. админом"}
    st = status_map.get(b.status, b.status)

    txt = f"🆔 Бронь #{b.id}\n👤 Клиент: @{user.username or 'Нет'}\n📞 Телефон: `{user.phone or 'Нет'}`\n📅 Дата: {slot.date}\n⏰ Время: {slot.start_time}-{slot.end_time}\n💰 Сумма: {int(b.total_price)}₽\n📊 Статус: {st}"

    await cb.message.edit_text(txt, parse_mode="Markdown", reply_markup=booking_action_kb(b.id, b.status))
    await cb.answer()


@router.callback_query(F.data.startswith("adm_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.status != "confirmed": return await cb.answer("⛔", show_alert=True)
        b.status = "cancelled_by_admin"
        slot = await s.get(Slot, b.slot_id)
        if slot: slot.is_booked = False
        await s.commit()

    user = await get_user(b.user_tg_id)
    try:
        await cb.bot.send_message(b.user_tg_id, f"❌ Администратор отменил вашу бронь #{bid}. Свяжитесь для переноса.")
    except:
        pass

    await cb.message.edit_text(f"❌ Бронь #{bid} отменена.", reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад",
                                                                                                        callback_data="admin_bookings_list").as_markup())
    await cb.answer("Успешно")


@router.callback_query(F.data.startswith("adm_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_confirm(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b: return await cb.answer("⛔", show_alert=True)
        b.status = "confirmed"
        await s.commit()
    await cb.answer("✅ Подтверждено")
    await cb.message.edit_text(f"✅ Бронь #{bid} подтверждена.",
                               reply_markup=InlineKeyboardBuilder().button(text="🔙 Назад",
                                                                           callback_data="admin_bookings_list").as_markup())


# --- НОВЫЕ ФИЛЬТРЫ И ПОИСК ---
@router.callback_query(F.data == "adm_filter_date", F.from_user.id.in_(ADMIN_IDS))
async def start_date_filter(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.filter_date)
    await cb.message.answer("📅 Введите дату для поиска (ГГГГ-ММ-ДД):")
    await cb.answer()


@router.message(AdminFSM.filter_date, F.from_user.id.in_(ADMIN_IDS))
async def process_date_filter(m: Message, state: FSMContext):
    date = m.text.strip()
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return await m.answer("❌ Неверный формат. Используйте ГГГГ-ММ-ДД")

    async with async_session() as s:
        stmt = (
            select(Booking, Slot.start_time, Slot.end_time, Slot.date)
                .join(Slot, Booking.slot_id == Slot.id)
                .where(Slot.date == date)
                .order_by(Booking.created_at.desc())
        )
        rows = (await s.execute(stmt)).all()

    if not rows:
        return await m.answer(
            f"🔍 Броней на `{date}` не найдено.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu").as_markup()
        )

    msg = f"📅 *Брони на {date}:*\n"
    kb = InlineKeyboardBuilder()
    for b, start_t, end_t, d in rows:
        user = await get_user(b.user_tg_id)
        status = "🟢" if b.status == "confirmed" else "🔴"
        msg += f"\n{status} #{b.id} | {start_t}-{end_t}\n👤 {user.username or 'Нет'} | 📞 {user.phone or 'Нет'} | 💰 {int(b.total_price)}₽"
        kb.button(text=f"#{b.id}", callback_data=f"adm_manage:{b.id}")

    kb.button(text="🔙 В меню", callback_data="admin_menu")
    kb.adjust(2)
    await m.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    await state.clear()


@router.callback_query(F.data == "adm_search_phone", F.from_user.id.in_(ADMIN_IDS))
async def start_phone_search(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.search_phone)
    await cb.message.answer("📱 Введите номер телефона (полный или часть):")
    await cb.answer()


@router.message(AdminFSM.search_phone, F.from_user.id.in_(ADMIN_IDS))
async def process_phone_search(m: Message, state: FSMContext):
    phone_query = m.text.strip().replace("+", "").replace(" ", "").replace("-", "")
    if len(phone_query) < 3:
        return await m.answer("❌ Введите минимум 3 цифры для поиска.")

    async with async_session() as s:
        # Ищем пользователей по номеру
        user_stmt = select(User).where(User.phone.isnot(None), User.phone.like(f"%{phone_query}%"))
        users = (await s.execute(user_stmt)).scalars().all()

        if not users:
            return await m.answer(
                f"🔍 Пользователей с номером `...{phone_query}` не найдено.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu").as_markup()
            )

        user_ids = [u.tg_id for u in users]
        # Ищем брони этих пользователей
        b_stmt = select(Booking).where(Booking.user_tg_id.in_(user_ids)).order_by(Booking.created_at.desc()).limit(30)
        bookings = (await s.execute(b_stmt)).scalars().all()

    msg = f"📱 *Найдено броней по номеру:* `{phone_query}`\n"
    kb = InlineKeyboardBuilder()
    for b in bookings:
        slot = await s.get(Slot, b.slot_id)
        user = next((u for u in users if u.tg_id == b.user_tg_id), None)
        status = "🟢" if b.status == "confirmed" else "🔴"
        msg += f"\n{status} #{b.id} | 📅 {slot.date} {slot.start_time}\n👤 {user.username or 'Нет'} | 💰 {int(b.total_price)}₽"
        kb.button(text=f"#{b.id}", callback_data=f"adm_manage:{b.id}")

    kb.button(text="🔙 В меню", callback_data="admin_menu")
    kb.adjust(2)
    await m.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    await state.clear()

# Кнопка возврата в главное меню админки
@router.callback_query(F.data == "admin_menu")
async def go_back_to_admin(cb: CallbackQuery):
    await cb.message.answer("👑 Панель администратора:", reply_markup=admin_kb())
    await cb.answer()