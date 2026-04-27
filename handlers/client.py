import asyncio, json, logging
from datetime import datetime
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, update
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone, get_booking_details
from keyboards import welcome_kb, dates_kb, multi_slots_kb, services_kb, confirm_kb, format_date_display
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)

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
        await _go_to_dates(cb, state, is_callback=True)
        await cb.answer(); return
    await state.set_state(BookFSM.phone)
    await cb.message.answer("📞 Введите номер телефона:"); await cb.answer()

async def _go_to_dates(event, state: FSMContext, is_callback: bool = False):
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked).distinct())
        dates = [r[0] for r in res]
    if not dates:
        txt = "❌ Свободных дат пока нет. Попробуйте позже или свяжитесь с админом."
        if is_callback: await event.message.answer(txt)
        else: await event.answer(txt)
        await state.clear(); return
    await state.set_state(BookFSM.date)
    txt = "📅 **Шаг 1/3:** Выберите удобную дату:"
    if is_callback: await event.message.answer(txt, reply_markup=dates_kb(dates), parse_mode="Markdown")
    else: await event.answer(txt, reply_markup=dates_kb(dates), parse_mode="Markdown")

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
    await _go_to_dates(m, state, is_callback=False)

@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    date_iso = cb.data.split(":")[1]
    await state.update_data(date=date_iso)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_iso, Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        slots = res.scalars().all()
    if not slots: await cb.message.answer("❌ На эту дату все часы заняты. Выберите другую."); await cb.answer(); return
    await state.set_state(BookFSM.slots)
    await state.update_data(selected_slots=[])
    await cb.message.answer(f"📆 {format_date_display(date_iso)}\n⏰ **Шаг 2/3:** Выберите нужные часы (можно несколько):", reply_markup=multi_slots_kb(slots, []), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("slot_toggle:"))
async def toggle_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1]); data = await state.get_data()
    sel = data.get("selected_slots", [])
    if sid in sel: sel.remove(sid)
    else: sel.append(sid)
    await state.update_data(selected_slots=sel)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        slots = res.scalars().all()
    try: await cb.message.edit_reply_markup(reply_markup=multi_slots_kb(slots, sel))
    except Exception: pass
    await cb.answer()

@router.callback_query(F.data == "slots_done")
async def finish_slots(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_slots"): await cb.answer("⚠️ Выберите хотя бы один час!", show_alert=True); return
    await state.set_state(BookFSM.services)
    await state.update_data(selected_services=[])
    async with async_session() as s:
        svcs = (await s.execute(select(Service).where(Service.is_active))).scalars().all()
    await cb.message.answer("🛠 **Шаг 3/3:** Выберите доп. услуги (или 'Завершить'):", reply_markup=services_kb(svcs), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("book_svc:") | (F.data == "book_svcs_done"))
async def manage_services(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if cb.data == "book_svcs_done":
        svc_total = sum(s["price"] for s in data.get("selected_services", []))
        slot_total = 0; times_str = []
        async with async_session() as s:
            for sid in data["selected_slots"]:
                sl = await s.get(Slot, sid)
                if sl: slot_total += sl.price; times_str.append(f"{sl.start_time}-{sl.end_time}")
        total = slot_total + svc_total
        await cb.message.answer(f"📋 **Итог бронирования:**\n📅 {format_date_display(data['date'])}\n⏰ {', '.join(times_str)}\n📞 {data['phone']}\n🎙️ Часы: {int(slot_total)}₽\n💰 Услуги: {int(svc_total)}₽\n💵 **Всего: {int(total)}₽**", reply_markup=confirm_kb(), parse_mode="Markdown")
        await cb.answer(); return
    sid = int(cb.data.split(":")[1])
    async with async_session() as s: svc = await s.get(Service, sid)
    sel = data.get("selected_services", [])
    if not any(x["id"] == sid for x in sel): sel.append({"id": svc.id, "name": svc.name, "price": svc.price})
    await state.update_data(selected_services=sel); await cb.answer(f"✅ Добавлено: {svc.name}")

@router.callback_query(F.data == "book_confirm")
async def confirm_booking(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_slots") or not data.get("phone"): await cb.message.answer("⏳ Данные утеряны. Начните заново по команде /start"); await state.clear(); return
    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active: await cb.message.answer(f"❌ Слот {sl.start_time if sl else 'N/A'}-{sl.end_time if sl else ''} только что забронировали."); await state.clear(); return
            slots.append(sl)
        for sl in slots: sl.is_booked = True
        svc_total = sum(x["price"] for x in data.get("selected_services", []))
        slot_total = sum(sl.price for sl in slots)
        s.add(Booking(user_tg_id=cb.from_user.id, slot_ids=json.dumps(data["selected_slots"]), services=json.dumps([x["id"] for x in data.get("selected_services", [])]), total_price=slot_total+svc_total))
        await s.commit()
    await cb.message.answer(f"✅ Бронь на {format_date_display(data['date'])} создана! Сумма: {int(slot_total+svc_total)}₽. За 2 часа пришлём напоминание.")
    await state.clear(); await cb.answer()

@router.callback_query(F.data.startswith("rem_confirm:") | F.data.startswith("rem_cancel:"))
async def handle_reminder(cb: CallbackQuery):
    action, bid_str = cb.data.split(":")
    bid = int(bid_str)
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.status != "confirmed": return await cb.answer("Статус уже изменён.", show_alert=True)
        if cb.from_user.id != b.user_tg_id and cb.from_user.id not in ADMIN_IDS: return await cb.answer("⛔ Не ваша бронь", show_alert=True)
        
        new_status = "confirmed_reminder" if action == "rem_confirm" else "cancelled"
        b.status = new_status
        if new_status == "cancelled":
            for sid in json.loads(b.slot_ids):
                sl = await s.get(Slot, sid)
                if sl: sl.is_booked = False
        await s.commit()
    await cb.answer("✅" if action == "rem_confirm" else "❌")
    await cb.message.edit_text(f"{'✅ Подтверждено' if action == 'rem_confirm' else '❌ Отменено'} по напоминанию.", parse_mode="Markdown")
    await _notify_admins(cb.bot, b, "confirmed" if action == "rem_confirm" else "cancelled")

async def _notify_admins(bot, booking, action):
    user = await get_user(booking.user_tg_id)
    tag = f"@{user.username}" if user and user.username else f"ID:{booking.user_tg_id}"
    msg = f"{'✅' if action == 'confirmed' else '❌'} Клиент {tag} ответил на напоминание.\n🆔 Бронь #{booking.id}"
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg)
        except Exception as e: logger.error(f"Admin notify fail {aid}: {e}")
        await asyncio.sleep(0.3)

@router.callback_query(F.data == "my_bookings")
async def show_my_bookings(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).where(Booking.user_tg_id == cb.from_user.id).order_by(Booking.created_at.desc()).limit(10))
        bookings = res.scalars().all()
    if not bookings: await cb.message.answer("📭 У вас пока нет записей."); await cb.answer(); return
    msg = "📋 **Ваши записи:**\n"; kb = InlineKeyboardBuilder()
    for b in bookings:
        _, slots, _ = await get_booking_details(b.id)
        if not slots: continue
        times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
        st = "🟢" if b.status == "confirmed" else "❌"
        msg += f"\n{st} #{b.id} | {format_date_display(slots[0].date)} ⏰ {times}\n💰 {int(b.total_price)}₽"
        if b.status == "confirmed": kb.button(text=f"Отменить #{b.id}", callback_data=f"my_cancel:{b.id}")
    if kb.buttons: kb.adjust(1); await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    else: await cb.message.answer(msg, parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("my_cancel:"))
async def my_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status != "confirmed": return await cb.answer("⛔ Нельзя отменить", show_alert=True)
        b.status = "cancelled"
        for sid in json.loads(b.slot_ids):
            sl = await s.get(Slot, sid)
            if sl: sl.is_booked = False
        await s.commit()
    await cb.message.edit_text("❌ Вы отменили запись. Слоты освобождены."); await cb.answer()
    await _notify_admins(cb.bot, b, "cancelled")
