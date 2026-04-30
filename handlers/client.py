import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func
from database import async_session, User, Slot, Service, Booking, get_user, validate_phone, get_booking_details
from keyboards import client_main_kb, back_to_menu_kb, format_date_display, MONTH_NAMES
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)
STUDIO_TZ = ZoneInfo("Europe/Moscow")
PRICES_FILE = os.path.join(os.getcwd(), "prices.json")

class BookFSM(StatesGroup):
    month = State()
    date = State()
    slots = State()
    camera = State()
    name = State()
    phone = State()

# 💰 Загрузка цен
def get_prices():
    defaults = {"rental": 0, "cam1": 3000, "cam2": 3500, "cam3": 4000, "no_cam": 0}
    try:
        with open(PRICES_FILE, "r") as f: return {**defaults, **json.load(f)}
    except: return defaults

# 🔄 Универсальный апдейтер одного сообщения
async def edit_booking_msg(event, state: FSMContext, text: str, kb: InlineKeyboardMarkup = None, parse_mode: str = "Markdown"):
    bot = event.bot
    if isinstance(event, CallbackQuery):
        chat_id = event.message.chat.id
        msg_obj = event.message
    else:
        chat_id = event.chat.id
        msg_obj = event

    data = await state.get_data()
    msg_id = data.get("booking_msg_id")

    if msg_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=msg_id, text=text, reply_markup=kb, parse_mode=parse_mode)
            return
        except Exception:
            pass

    new_msg = await msg_obj.answer(text, reply_markup=kb, parse_mode=parse_mode)
    await state.update_data(booking_msg_id=new_msg.message_id)

# 🔄 Переключатель главного меню
async def switch_view(cb: CallbackQuery, view: str):
    text = ""
    kb = back_to_menu_kb()

    if view == "main":
        text = "🎙️ Добро пожаловать в студию подкастов! Выберите нужное действие:"
        kb = client_main_kb()
    elif view == "contact":
        text = "📞 **Связь с админом:**\n👤 TG: `@ваш_ник`\n📱 Тел: `+79990000000`\n🕒 10:00–22:00"
    elif view == "bookings":
        today = datetime.now().date().strftime("%Y-%m-%d")
        async with async_session() as s:
            res = await s.execute(select(Booking).where(Booking.user_tg_id == cb.from_user.id).order_by(Booking.created_at.desc()).limit(20))
            all_bookings = res.scalars().all()
        active = []
        for b in all_bookings:
            _, slots, _ = await get_booking_details(b.id)
            if not slots: continue
            if slots[0].date >= today and b.status in ["confirmed", "confirmed_reminder"]:
                active.append((b, slots))
        if not active:
            text = "📭 У вас нет активных записей."
        else:
            text = "📋 **Ваши записи:**\n"
            kb = InlineKeyboardBuilder()
            for b, slots in active:
                times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
                text += f"\n🟢 #{b.id} | {format_date_display(slots[0].date)} ⏰ {times}\n💰 {int(b.total_price)}₽"
                kb.button(text=f"Отменить #{b.id}", callback_data=f"my_cancel:{b.id}")
            kb.adjust(1)
            kb.button(text="⬅️ В меню", callback_data="view_main")
            kb = kb.as_markup()
    try: await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except: await cb.message.answer(text, reply_markup=kb, parse_mode="Markdown")

@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎙️ Добро пожаловать!", reply_markup=client_main_kb())

@router.callback_query(F.data == "view_main")
async def go_main(cb: CallbackQuery, state: FSMContext): await state.clear(); await switch_view(cb, "main"); await cb.answer()
    
@router.callback_query(F.data == "view_price")
async def go_price(cb: CallbackQuery):
    p = get_prices()
    await cb.answer(
        "📹\n"
        f"1 камера — {p['cam1']}₽\n"
        f"2 камеры — {p['cam2']}₽\n"
        f"3 камеры — {p['cam3']}₽\n\n"
        "🎙️\n"
        f"Студия без камер — {p['no_cam']}₽",
        show_alert=True
    )

@router.callback_query(F.data == "view_contact")
async def go_contact(cb: CallbackQuery): await switch_view(cb, "contact"); await cb.answer()

@router.callback_query(F.data == "view_bookings")
async def go_bookings(cb: CallbackQuery): await switch_view(cb, "bookings"); await cb.answer()

# 📅 ШАГ 1/6: Месяцы
@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    await _show_months(cb, state, is_callback=True); await cb.answer()

async def _show_months(event, state: FSMContext, is_callback: bool = False):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= today).distinct())
        dates = [r[0] for r in res]
    months_dict = {d[:7]: None for d in dates}
    if not months_dict:
        txt, kb = "❌ Нет свободных дат.", InlineKeyboardBuilder().row(InlineKeyboardButton(text="📞 Админ", callback_data="view_contact"), InlineKeyboardButton(text="⬅️ Меню", callback_data="view_main")).as_markup()
        if is_callback: await event.message.edit_text(txt, reply_markup=kb)
        else: await event.answer(txt)
        await state.clear(); return
    await state.set_state(BookFSM.month)
    kb = InlineKeyboardBuilder()
    for ym in sorted(months_dict.keys()):
        year, month = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[month]} {year}", callback_data=f"book_month:{ym}")
    kb.adjust(1); kb.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="view_main"))
    await edit_booking_msg(event, state, "📅 **Шаг 1/6:** Выберите месяц:", kb.as_markup())

@router.callback_query(F.data.startswith("book_month:"))
async def select_month(cb: CallbackQuery, state: FSMContext):
    await state.update_data(year_month=cb.data.split(":")[1]); await _show_dates(cb, state); await cb.answer()

async def _show_dates(cb, state):
    ym = (await state.get_data())["year_month"]
    await state.set_state(BookFSM.date)
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, func.strftime('%Y-%m', Slot.date) == ym, Slot.date >= datetime.now().date().strftime("%Y-%m-%d")).distinct().order_by(Slot.date))
        dates = [r[0] for r in res]
    if not dates: return await cb.message.answer("❌ Нет дней.", reply_markup=back_to_menu_kb())
    kb = InlineKeyboardBuilder()
    for d in dates: kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
    kb.button(text="⬅️ Назад", callback_data="back_to_months"); kb.adjust(1)
    await edit_booking_msg(cb, state, "📆 **Шаг 2/6:** Выберите дату:", kb.as_markup())

@router.callback_query(F.data == "back_to_months")
async def back_to_months(cb: CallbackQuery, state: FSMContext): await _show_months(cb, state, is_callback=True); await cb.answer()

# ⏰ ШАГ 3/6: Время
@router.callback_query(F.data.startswith("book_date:"))
async def select_date(cb: CallbackQuery, state: FSMContext):
    date_iso = cb.data.split(":")[1]; await state.update_data(date=date_iso)
    now, threshold = datetime.now(STUDIO_TZ), datetime.now(STUDIO_TZ) + timedelta(hours=1, minutes=30)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_iso, Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        all_slots = res.scalars().all()
    slots = []
    if date_iso == now.strftime("%Y-%m-%d"):
        for sl in all_slots:
            if datetime.strptime(f"{date_iso} {sl.start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=STUDIO_TZ) >= threshold: slots.append(sl)
    else: slots = all_slots
    if not slots:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"), InlineKeyboardButton(text="📞 Админ", callback_data="view_contact"))
        await edit_booking_msg(cb, state, "❌ Нет часов.", kb.as_markup()); await cb.answer(); return
    await state.set_state(BookFSM.slots); await state.update_data(selected_slots=[])
    kb = InlineKeyboardBuilder()
    for sl in slots: kb.button(text=f"⏳ {sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done"); kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"))
    await edit_booking_msg(cb, state, "⏰ **Шаг 3/6:** Выберите время:\n💡 *Нажмите на выбранный час, чтобы убрать его*", kb.as_markup()); await cb.answer()

@router.callback_query(F.data == "back_to_date")
async def back_to_date(cb: CallbackQuery, state: FSMContext):
    ym = (await state.get_data()).get("year_month")
    if ym: await _show_dates(cb, state)
    else: await _show_months(cb, state, is_callback=True)
    await cb.answer()

@router.callback_query(F.data.startswith("slot_toggle:"))
async def toggle_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1]); data = await state.get_data(); sel = data.get("selected_slots", [])
    if sid in sel: sel.remove(sid)  # 🔥 Удаление часа из брони
    else: sel.append(sid)
    await state.update_data(selected_slots=sel)
    now, threshold = datetime.now(STUDIO_TZ), datetime.now(STUDIO_TZ) + timedelta(hours=1, minutes=30)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == data["date"], Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        all_slots = res.scalars().all()
    slots = []
    if data["date"] == now.strftime("%Y-%m-%d"):
        for sl in all_slots:
            if datetime.strptime(f"{data['date']} {sl.start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=STUDIO_TZ) >= threshold: slots.append(sl)
    else: slots = all_slots
    kb = InlineKeyboardBuilder()
    for sl in slots: kb.button(text=f"{'✅ ' if sl.id in sel else '⏳ '}{sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done"); kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"))
    try: await cb.message.edit_reply_markup(reply_markup=kb.as_markup())
    except: await edit_booking_msg(cb, state, cb.message.text, kb.as_markup())
    await cb.answer()

# 📹 ШАГ 4/6: Камеры / Без камер
@router.callback_query(F.data == "slots_done")
async def finish_slots(cb: CallbackQuery, state: FSMContext):
    if not (await state.get_data()).get("selected_slots"): return await cb.answer("⚠️ Выберите час!", show_alert=True)
    await state.set_state(BookFSM.camera)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📹 1 камера", callback_data="camera:1"),
        InlineKeyboardButton(text="📹 2 камеры", callback_data="camera:2"),
        InlineKeyboardButton(text="📹 3 камеры", callback_data="camera:3")
    ).row(
        InlineKeyboardButton(text="🏢 Студия без камер", callback_data="camera:0")
    ).row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"))
    await edit_booking_msg(cb, state, "📹 **Шаг 4/6:** Выберите оборудование:", kb.as_markup()); await cb.answer()

@router.callback_query(F.data.startswith("camera:"))
async def select_camera(cb: CallbackQuery, state: FSMContext):
    await state.update_data(camera_type=cb.data.split(":")[1]); await _show_summary(cb, state)

@router.callback_query(F.data == "back_to_camera")
async def back_to_camera(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.camera)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📹 1 камера", callback_data="camera:1"),
        InlineKeyboardButton(text="📹 2 камеры", callback_data="camera:2"),
        InlineKeyboardButton(text="📹 3 камеры", callback_data="camera:3")
    ).row(
        InlineKeyboardButton(text="🏢 Студия без камер", callback_data="camera:0")
    ).row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"))
    await edit_booking_msg(cb, state, "📹 Выберите оборудование:", kb.as_markup()); await cb.answer()

# 📋 ИТОГ
async def _show_summary(cb, state):
    data = await state.get_data()
    p = get_prices()
    hours = len(data.get("selected_slots", []))
    
    rental = hours * p["rental"]
    cam_type = data.get("camera_type", "0")
    base_price = p.get(f"cam{cam_type}", 0) if cam_type != "0" else p.get("no_cam", 0)
    cam_price = base_price * hours  # ✅ Стоимость услуги/камер умножается на кол-во часов
    
    total = rental + cam_price
    await state.update_data(total_price=total)
    
    cam_label = "Без камер" if cam_type == "0" else f"{cam_type} кам."
    txt = f"📋 **Итог:**\n📅 {format_date_display(data['date'])} ⏰ {hours} ч\n📹 {cam_label}\n💵 **Всего: {total}₽**"
    kb = InlineKeyboardBuilder().button(text="✅ Подтвердить", callback_data="book_confirm")
    kb.row(InlineKeyboardButton(text="⬅️ Изменить камеры", callback_data="back_to_camera"), InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel"))
    await edit_booking_msg(cb, state, txt, kb.as_markup())
    await cb.answer()

# 👤 ШАГ 5/6: Имя
@router.callback_query(F.data == "book_confirm")
async def ask_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    user = await get_user(cb.from_user.id)
    saved_name = user.client_name if user else "-"
    txt = f"👤 **Шаг 5/6:** Ваше имя:\n(Сохранено: `{saved_name}`)"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(cb, state, txt, kb.as_markup()); await cb.answer()

@router.message(BookFSM.name)
async def save_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2: return await m.answer("⚠️ Минимум 2 символа.")
    await state.update_data(client_name=name)
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not u: s.add(User(tg_id=m.from_user.id, username=m.from_user.username, client_name=name))
        else: u.client_name = name
        await s.commit()
    await state.set_state(BookFSM.phone)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к имени", callback_data="book_name_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(m, state, "📞 **Шаг 6/6:** Телефон (формат `+79991111111`):", kb.as_markup())

# 📱 ШАГ 6/6: Телефон → Финал
@router.callback_query(F.data == "book_name_back")
async def back_to_name(cb: CallbackQuery, state: FSMContext): await state.set_state(BookFSM.name); await ask_name(cb, state)
@router.callback_query(F.data == "book_summary_back")
async def back_to_summary(cb: CallbackQuery, state: FSMContext): await _show_summary(cb, state)

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    phone = m.text.strip()
    if not validate_phone(phone): return await m.answer("⚠️ Введите строго `+79991111111`", parse_mode="Markdown")
    await state.update_data(phone=phone)
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not u: s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=phone))
        else: u.phone = phone
        await s.commit()
    await _create_booking(m, state)

async def _create_booking(event, state: FSMContext):
    data = await state.get_data()
    deleted_buffer = None

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                return await edit_booking_msg(event, state, "❌ Слот занят. Начните заново.")
            slots.append(sl)
            sl.is_booked = True

        # 🔥 Автоудаление следующего слота (буфер) с сохранением данных для восстановления
        if slots:
            max_end_time = max(sl.end_time for sl in slots)
            next_slot = (await s.execute(select(Slot).where(
                Slot.date == data["date"],
                Slot.start_time == max_end_time,
                ~Slot.is_booked,
                Slot.is_active
            ))).scalar_one_or_none()

            if next_slot:
                deleted_buffer = {
                    "date": next_slot.date,
                    "start": next_slot.start_time,
                    "end": next_slot.end_time,
                    "price": float(next_slot.price)
                }
                await s.delete(next_slot)

        services_data = {"camera": data["camera_type"]}
        if deleted_buffer:
            services_data["buffer_deleted"] = deleted_buffer

        b = Booking(
            user_tg_id=event.from_user.id,
            slot_ids=json.dumps(data["selected_slots"]),
            services=json.dumps(services_data),
            total_price=data["total_price"]
        )
        s.add(b)
        await s.commit()

    times = [f"{sl.start_time}-{sl.end_time}" for sl in slots]
    await _notify_new_booking(event.bot, b.id, data, times, data["total_price"])
    await edit_booking_msg(event, state, f"✅ Бронь #{b.id} создана! Сумма: {int(data['total_price'])}₽. Ждём вас.")
    await state.clear()

@router.callback_query(F.data == "book_cancel")
async def cancel_booking(cb: CallbackQuery, state: FSMContext): await state.clear(); await switch_view(cb, "main"); await cb.answer()

# 📋 Отмена брони
@router.callback_query(F.data.startswith("my_cancel:"))
async def my_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status != "confirmed": 
            return await cb.answer("⛔ Нельзя отменить", show_alert=True)
        b.status = "cancelled"
        
        for sid in json.loads(b.slot_ids):
            sl = await s.get(Slot, sid)
            if sl: sl.is_booked = False
        
        # 🔄 Восстановление удалённого буферного слота
        svc = json.loads(b.services) if b.services else {}
        buffer = svc.get("buffer_deleted")
        if buffer:
            s.add(Slot(
                date=buffer["date"],
                start_time=buffer["start"],
                end_time=buffer["end"],
                price=buffer["price"],
                is_active=True,
                is_booked=False
            ))
            
        await s.commit()
        
    await cb.answer("✅ Отменено")
    await switch_view(cb, "bookings")
    await _notify_admins(cb.bot, b, "cancelled")

# 🔔 Напоминания
@router.callback_query(F.data.startswith("rem_confirm:") | F.data.startswith("rem_cancel:"))
async def handle_reminder(cb: CallbackQuery):
    action, bid = cb.data.split(":")[0], int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.status != "confirmed" or (cb.from_user.id != b.user_tg_id and cb.from_user.id not in ADMIN_IDS):
            return await cb.answer("⛔ Ошибка.", show_alert=True)
        b.status = "confirmed_reminder" if action == "rem_confirm" else "cancelled"
        if action == "rem_cancel":
            for sid in json.loads(b.slot_ids):
                sl = await s.get(Slot, sid)
                if sl: sl.is_booked = False
        await s.commit()
    try: await cb.message.edit_text(f"{'✅' if action=='rem_confirm' else '❌'} Запись изменена.\n🆔 #{bid}")
    except: pass
    await cb.answer(); await _notify_admins(cb.bot, b, "confirmed" if action == "rem_confirm" else "cancelled")

# 📢 Уведомления (✅ ИСПРАВЛЕНО: параметр data: dict)
async def _notify_new_booking(bot, booking_id: int,  data, times_str: list, total_price: float):
    cam = "Без камер" if data.get("camera_type") == "0" else f"{data.get('camera_type')} кам."
    msg = f"🆕 **Бронь #{booking_id}**\n👤 {data['client_name']} | 📞 `{data['phone']}`\n📅 {format_date_display(data['date'])} ⏰ {', '.join(times_str)}\n📹 {cam}\n💰 {int(total_price)}₽"
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Notify fail {aid}: {e}")
        await asyncio.sleep(0.3)

async def _notify_admins(bot, booking, action):
    tag = f"ID:{booking.user_tg_id}"
    msg = f"{'✅' if action == 'confirmed' else '❌'} Клиент {tag} ответил.\n🆔 #{booking.id}"
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg)
        except Exception as e: logger.error(f"Admin notify fail {aid}: {e}")
        await asyncio.sleep(0.3)
