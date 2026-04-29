import asyncio
import json
import logging
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone, get_booking_details
from keyboards import welcome_kb, dates_kb, months_kb, time_slots_kb, services_kb, confirm_kb, format_date_display, back_cancel_kb
from config import ADMIN_IDS
from zoneinfo import ZoneInfo

router = Router()
logger = logging.getLogger(__name__)

STUDIO_TZ = ZoneInfo("Europe/Moscow")

class BookFSM(StatesGroup):
    month = State()  
    date = State()
    slots = State()
    name = State()
    phone = State()
    services = State()

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎙️ Добро пожаловать в студию подкастов! Выберите нужное действие:", reply_markup=welcome_kb())

@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    await _show_months(cb, state, is_callback=True)
    await cb.answer()

@router.callback_query(F.data == "contact_admin")
async def contact_admin(cb: CallbackQuery):
    msg = (
        "📞 **Связь с администратором:**\n"
        "👤 Telegram: `@ваш_ник_админа`\n"
        "📱 Телефон: `+7 (999) 123-45-67`\n"
        "🕒 График: 10:00 – 22:00 (МСК)\n"
        "Отвечаем в течение 15 минут."
    )
    kb = InlineKeyboardBuilder().button(text="⬅️ В главное меню", callback_data="main_menu")
    await cb.message.answer(msg, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()

async def _show_months(event, state: FSMContext, is_callback: bool = False):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(
            select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= today).distinct()
        )
        dates = [r[0] for r in res]

    # Группируем даты по месяцам (YYYY-MM)
    months_dict = {}
    for d in dates:
        ym = d[:7]
        months_dict.setdefault(ym, []).append(d)
    months = sorted(months_dict.keys())

    if not months:
        txt = "❌ Свободных дат пока нет. Попробуйте позже или свяжитесь с админом."
        if is_callback: await event.message.answer(txt)
        else: await event.answer(txt)
        await state.clear(); return

    await state.set_state(BookFSM.month)
    txt = "📅 **Шаг 1/6:** Выберите месяц:"
    if is_callback: await event.message.answer(txt, reply_markup=months_kb(months), parse_mode="Markdown")
    else: await event.answer(txt, reply_markup=months_kb(months), parse_mode="Markdown")

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
        await cb.answer(); return

    kb = InlineKeyboardBuilder()
    for d in dates:
        kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
    kb.button(text="⬅️ Назад к месяцам", callback_data="back_to_months")
    kb.adjust(1)

    await cb.message.answer("📆 **Шаг 2/6:** Выберите дату:", reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data == "back_to_months")
async def back_to_months(cb: CallbackQuery, state: FSMContext):
    await _show_months(cb, state, is_callback=True)
    await cb.answer()
        
@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    date_iso = cb.data.split(":")[1]
    await state.update_data(date=date_iso)

    now = datetime.now(STUDIO_TZ)
    threshold = now + timedelta(hours=1, minutes=30)  # Мин. 1.5 часа до начала

    async with async_session() as s:
        res = await s.execute(select(Slot).where(
            Slot.date == date_iso, Slot.is_active, ~Slot.is_booked
        ).order_by(Slot.start_time))
        all_slots = res.scalars().all()

    # 🔥 Корректная фильтрация времени
    if date_iso == now.strftime("%Y-%m-%d"):
        slots = []
        for sl in all_slots:
            # Создаём datetime-объект слота в вашем часовом поясе
            slot_dt = datetime.strptime(f"{date_iso} {sl.start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=STUDIO_TZ)
            if slot_dt >= threshold:
                slots.append(sl)
    else:
        # Для будущих дат показываем все свободные слоты
        slots = all_slots

    if not slots:
        kb = InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="book_cancel")
        await cb.message.answer("❌ Нет доступных часов для бронирования.", reply_markup=kb.as_markup())
        await cb.answer()
        return

    price_per_hour = int(slots[0].price)
    await state.set_state(BookFSM.slots)
    await state.update_data(selected_slots=[])

    kb = InlineKeyboardBuilder()
    for sl in slots:
        kb.button(text=f"⏳ {sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done")
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="back_to_date"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )

    await cb.message.answer(
        f"📆 {format_date_display(date_iso)} | 💰 **{price_per_hour}₽/час**\n"
        f"⏰ **Шаг 3/6:** Выберите нужные часы:",
        reply_markup=kb.as_markup(), parse_mode="Markdown"
    )
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
    
    kb = InlineKeyboardBuilder()
    for sl in slots:
        is_sel = sl.id in sel
        kb.button(text=f"{'✅ ' if is_sel else '⏳ '}{sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done")
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="back_to_date"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    
    try: await cb.message.edit_reply_markup(reply_markup=kb.as_markup())
    except Exception: pass
    await cb.answer()

@router.callback_query(F.data == "slots_done")
async def finish_slots(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("selected_slots"):
        await cb.answer("⚠️ Выберите хотя бы один час!", show_alert=True); return

    user = await get_user(cb.from_user.id)
    if user and user.client_name and user.phone:
        kb = InlineKeyboardBuilder()
        kb.row(
            InlineKeyboardButton(text="✅ Использовать сохранённые", callback_data="use_saved_data"),
            InlineKeyboardButton(text="📝 Ввести новые", callback_data="enter_new_data")
        )
        kb.row(
            InlineKeyboardButton(text="⬅️ Назад к времени", callback_data="back_to_slots"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
        )
        await cb.message.answer(
            f"👤 **Шаг 4/6:** Данные уже сохранены:\n👤 Имя: `{user.client_name}`\n📞 Телефон: `{user.phone}`",
            reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    else:
        await state.set_state(BookFSM.name)
        hint = f"\n(Ранее было: `{user.client_name}`)" if user and user.client_name else ""
        kb = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="⬅️ Назад к времени", callback_data="back_to_slots"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
        )
        await cb.message.answer(f"👤 **Шаг 4/6:** Введите ваше имя{hint}:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "use_saved_data")
async def use_saved(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    await state.update_data(client_name=user.client_name, phone=user.phone)
    await cb.message.edit_text("✅ Использованы сохранённые данные. Переходим к услугам...")
    await _show_services(cb.message, state)
    await cb.answer()

@router.callback_query(F.data == "enter_new_data")
async def enter_new(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к времени", callback_data="back_to_slots"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await cb.message.edit_text("👤 Введите ваше имя:", reply_markup=kb.as_markup())
    await cb.answer()

@router.message(BookFSM.name)
async def save_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2: return await m.answer("⚠️ Имя должно содержать минимум 2 символа.")
    await state.update_data(client_name=name)

    async with async_session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, client_name=name))
        else:
            user.client_name = name
        await s.commit()

    await state.set_state(BookFSM.phone)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к имени", callback_data="back_to_name"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await m.answer("📞 **Шаг 5/6:** Введите номер телефона:", reply_markup=kb.as_markup())

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    phone = m.text.strip()
    if not validate_phone(phone):
        return await m.answer("⚠️ Некорректный номер. Введите только цифры, +, -, пробелы (мин. 7 цифр).")
    await state.update_data(phone=phone)

    async with async_session() as s:
        user = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not user:
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=phone))
        else:
            user.phone = phone
        await s.commit()
    
    await _show_services(m, state)

async def _show_services(event, state: FSMContext):
    await state.set_state(BookFSM.services)
    await state.update_data(selected_services=[])
    async with async_session() as s:
        svcs = (await s.execute(select(Service).where(Service.is_active))).scalars().all()
    
    kb = InlineKeyboardBuilder()
    for svc in svcs: kb.button(text=f"{svc.name} ({int(svc.price)}₽)", callback_data=f"book_svc:{svc.id}")
    kb.button(text="✅ Завершить выбор", callback_data="book_svcs_done")
    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к телефону", callback_data="back_to_phone"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await event.answer("🛠 **Шаг 6/6:** Выберите доп. услуги (или 'Завершить'):", reply_markup=kb.as_markup(), parse_mode="Markdown")

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
        
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Подтвердить бронь", callback_data="book_confirm")
        kb.row(
            InlineKeyboardButton(text="⬅️ Изменить услуги", callback_data="back_to_services"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
        )
        
        await cb.message.answer(
            f"📋 **Итог бронирования:**\n👤 {data['client_name']}\n📞 {data['phone']}\n"
            f"📅 {format_date_display(data['date'])}\n⏰ {', '.join(times_str)}\n"
            f"🎙️ Часы: {int(slot_total)}₽\n💰 Услуги: {int(svc_total)}₽\n💵 **Всего: {int(total)}₽**",
            reply_markup=kb.as_markup(), parse_mode="Markdown"
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
        await cb.message.answer("⏳ Данные утеряны. Начните заново по команде /start"); await state.clear(); return

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                await cb.message.answer(f"❌ Слот {sl.start_time if sl else 'N/A'}-{sl.end_time if sl else ''} только что забронировали."); await state.clear(); return
            slots.append(sl)
        for sl in slots: sl.is_booked = True
        svc_total = sum(x["price"] for x in data.get("selected_services", []))
        slot_total = sum(sl.price for sl in slots)
        total_price = slot_total + svc_total
        times_str = [f"{sl.start_time}-{sl.end_time}" for sl in slots]

        new_booking = Booking(
            user_tg_id=cb.from_user.id, 
            slot_ids=json.dumps(data["selected_slots"]), 
            services=json.dumps([x["id"] for x in data.get("selected_services", [])]), 
            total_price=total_price
        )
        s.add(new_booking)
        await s.commit()

    await _notify_new_booking(cb.bot, new_booking.id, data, times_str, total_price)
    await cb.message.answer(f"✅ Бронь на {format_date_display(data['date'])} создана! Сумма: {int(total_price)}₽. За 2 часа пришлём напоминание.")
    await state.clear(); await cb.answer()

@router.callback_query(F.data.startswith("rem_confirm:") | F.data.startswith("rem_cancel:"))
async def handle_reminder(cb: CallbackQuery):
    action, bid_str = cb.data.split(":")
    bid = int(bid_str)

    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.status != "confirmed":
            return await cb.answer("⛔ Бронь уже изменена или не найдена.", show_alert=True)

        if cb.from_user.id != b.user_tg_id and cb.from_user.id not in ADMIN_IDS:
            return await cb.answer("⛔ Это не ваша бронь.", show_alert=True)

        if action == "rem_confirm":
            b.status = "confirmed_reminder"
            answer_text = "✅ Запись подтверждена! Ждём вас."
        else:
            b.status = "cancelled"
            answer_text = "❌ Запись отменена. Слоты освобождены."
            for sid in json.loads(b.slot_ids):
                sl = await s.get(Slot, sid)
                if sl: sl.is_booked = False
        await s.commit()

    try:
        await cb.message.edit_text(f"{answer_text}\n🆔 Бронь #{bid}")
    except Exception:
        pass
    await cb.answer()
    await _notify_admins(cb.bot, b, "confirmed" if action == "rem_confirm" else "cancelled")

@router.callback_query(F.data == "book_cancel")
async def cancel_booking(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.answer("❌ Бронирование отменено.", reply_markup=welcome_kb())
    await cb.answer()

@router.callback_query(F.data == "back_to_date")
async def back_to_date(cb: CallbackQuery, state: FSMContext):
    await _show_dates(cb, state, is_callback=True)
    await cb.answer()

@router.callback_query(F.data == "back_to_slots")
async def back_to_slots(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.slots)
    data = await state.get_data()
    sel = data.get("selected_slots", [])
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        slots = res.scalars().all()
    price = int(slots[0].price) if slots else 0
    
    kb = InlineKeyboardBuilder()
    for sl in slots:
        is_sel = sl.id in sel
        kb.button(text=f"{'✅ ' if is_sel else '⏳ '}{sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done")
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(text="⬅️ Назад к датам", callback_data="back_to_date"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await cb.message.edit_text(
        f"📆 {format_date_display(data['date'])} | 💰 **{price}₽/час**\n⏰ Выберите нужные часы:",
        reply_markup=kb.as_markup(), parse_mode="Markdown"
    )
    await cb.answer()

@router.callback_query(F.data == "back_to_name")
async def back_to_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к времени", callback_data="back_to_slots"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await cb.message.edit_text("👤 Введите ваше имя:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "back_to_phone")
async def back_to_phone(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.phone)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к имени", callback_data="back_to_name"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await cb.message.edit_text("📞 Введите номер телефона:", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "back_to_services")
async def back_to_services(cb: CallbackQuery, state: FSMContext):
    await _show_services(cb.message, state)
    await cb.answer()

@router.callback_query(F.data == "my_bookings")
async def show_my_bookings(cb: CallbackQuery):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Booking).where(Booking.user_tg_id == cb.from_user.id).order_by(Booking.created_at.desc()).limit(20))
        all_bookings = res.scalars().all()

    active_bookings = []
    for b in all_bookings:
        _, slots, _ = await get_booking_details(b.id)
        if not slots: continue
        if slots[0].date >= today and b.status in ["confirmed", "confirmed_reminder"]:
            active_bookings.append((b, slots))

    if not active_bookings:
        kb = InlineKeyboardBuilder().button(text="⬅️ В главное меню", callback_data="main_menu")
        await cb.message.answer("📭 У вас нет активных записей на сегодня или в будущем.", reply_markup=kb.as_markup())
        await cb.answer()
        return

    msg = "📋 **Ваши активные записи:**\n"
    kb = InlineKeyboardBuilder()
    for b, slots in active_bookings:
        times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
        msg += f"\n🟢 #{b.id} | {format_date_display(slots[0].date)} ⏰ {times}\n💰 {int(b.total_price)}₽"
        kb.button(text=f"Отменить #{b.id}", callback_data=f"my_cancel:{b.id}")
    kb.adjust(1)
    kb.button(text="⬅️ В главное меню", callback_data="main_menu")  # 🆕 Кнопка назад
    
    await cb.message.answer(msg, parse_mode="Markdown", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("my_cancel:"))
async def my_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status != "confirmed": return await cb.answer("⛔ Нельзя отменить эту запись", show_alert=True)
        b.status = "cancelled"
        for sid in json.loads(b.slot_ids):
            sl = await s.get(Slot, sid)
            if sl: sl.is_booked = False
        await s.commit()
    await cb.message.edit_text("❌ Вы отменили запись. Слоты освобождены."); await cb.answer()
    await _notify_admins(cb.bot, b, "cancelled")

async def _notify_new_booking(bot, booking_id: int,  dict, times_str: list, total_price: float):
    msg = (
        f"🆕 **Новая бронь #{booking_id}**\n"
        f"👤 {data['client_name']} | 📞 `{data['phone']}`\n"
        f"📅 {format_date_display(data['date'])}\n"
        f"⏰ {', '.join(times_str)}\n"
        f"💰 {int(total_price)}₽"
    )
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"❌ Notify admin {aid} failed: {e}")
        await asyncio.sleep(0.3)

async def _notify_new_booking(bot, booking_id: int, data: dict, times_str: list, total_price: float):
    msg = (
        f"🆕 **Новая бронь #{booking_id}**\n"
        f"👤 {data['client_name']} | 📞 `{data['phone']}`\n"
        f"📅 {format_date_display(data['date'])}\n"
        f"⏰ {', '.join(times_str)}\n"
        f"💰 {int(total_price)}₽"
    )
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"❌ Notify admin {aid} failed: {e}")
        await asyncio.sleep(0.3)

async def _notify_admins(bot, booking, action):
    user = await get_user(booking.user_tg_id)
    tag = f"@{user.username}" if user and user.username else f"ID:{booking.user_tg_id}"
    msg = f"{'✅' if action == 'confirmed' else '❌'} Клиент {tag} ответил на напоминание.\n🆔 Бронь #{booking.id}"
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Admin notify fail {aid}: {e}")
        await asyncio.sleep(0.3)

@router.callback_query(F.data == "main_menu")
async def go_to_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()  # Сбрасываем зависшие состояния бронирования
    await cb.message.answer("🎙️ Добро пожаловать в студию подкастов! Выберите нужное действие:", reply_markup=welcome_kb())
    await cb.answer()
