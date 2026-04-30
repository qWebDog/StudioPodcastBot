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
from sqlalchemy import select
from database import async_session, User, Slot, Booking, get_user, validate_phone, get_booking_details
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
        with open(PRICES_FILE, "r") as f:
            saved = json.load(f)
            if "editing" in saved: saved["no_cam"] = saved.pop("editing")
            return {**defaults, **saved}
    except: return defaults

def _merge_slots_display(slots):
    if not slots: return ""
    times = sorted([(str(sl.start_time)[:5], str(sl.end_time)[:5]) for sl in slots])
    merged, curr_s, curr_e = [], times[0][0], times[0][1]
    for ns, ne in times[1:]:
        if ns == curr_e: curr_e = ne
        else: merged.append(f"{curr_s}-{curr_e}"); curr_s, curr_e = ns, ne
    merged.append(f"{curr_s}-{curr_e}")
    return "\n⏰ ".join(merged)
    
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
        except Exception: pass

    new_msg = await msg_obj.answer(text, reply_markup=kb, parse_mode=parse_mode)
    await state.update_data(booking_msg_id=new_msg.message_id)

# 🔄 Переключатель главного меню (только main/contact)
async def switch_view(cb: CallbackQuery, view: str):
    text = ""
    kb = back_to_menu_kb()
    if view == "main":
        text = "🎙️ Добро пожаловать в студию подкастов! Выберите нужное действие:"
        kb = client_main_kb()
    elif view == "contact":
        text = "📞 **Связь с админом:**\n👤 TG: `@ваш_ник`\n📱 Тел: `+79990000000`\n🕒 10:00–22:00"
    try: await cb.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except: await cb.message.answer(text, reply_markup=kb, parse_mode="Markdown")

# 📋 Активные брони
async def _get_active_bookings(user_id: int):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Booking).where(
            Booking.user_tg_id == user_id,
            Booking.status.in_(["confirmed", "confirmed_reminder"])
        ).order_by(Booking.created_at.desc()))
        all_b = res.scalars().all()
    active = []
    for b in all_b:
        sl_res = await s.execute(select(Slot).where(Slot.id.in_(json.loads(b.slot_ids))).order_by(Slot.start_time))
        slots = sl_res.scalars().all()
        if slots and slots[0].date >= today:
            active.append((b, slots))
    return active

# 📥 СТАРТ
@router.message(F.text == "/start")
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🎙️ Добро пожаловать!", reply_markup=client_main_kb())

@router.callback_query(F.data == "view_main")
async def go_main(cb: CallbackQuery, state: FSMContext): await state.clear(); await switch_view(cb, "main"); await cb.answer()
@router.callback_query(F.data == "view_contact")
async def go_contact(cb: CallbackQuery): await switch_view(cb, "contact"); await cb.answer()

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

# 📅 ШАГ 1/6: Месяцы
@router.callback_query(F.data == "book_start")
async def start_booking(cb: CallbackQuery, state: FSMContext):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= today).distinct())
        dates = [r[0] for r in res]
    months_dict = {d[:7]: None for d in dates}
    if not months_dict:
        txt, kb = "❌ Нет свободных дат.", InlineKeyboardBuilder().row(InlineKeyboardButton(text="📞 Админ", callback_data="view_contact"), InlineKeyboardButton(text="⬅️ Меню", callback_data="view_main")).as_markup()
        await cb.message.edit_text(txt, reply_markup=kb); await cb.answer(); return
    await state.set_state(BookFSM.month)
    kb = InlineKeyboardBuilder()
    for ym in sorted(months_dict.keys()):
        y, m = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[m]} {y}", callback_data=f"book_month:{ym}")
    kb.adjust(1); kb.row(InlineKeyboardButton(text="⬅️ Меню", callback_data="view_main"))
    await edit_booking_msg(cb, state, "📅 **Шаг 1/6:** Выберите месяц:", kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("book_month:"))
async def select_month(cb: CallbackQuery, state: FSMContext):
    await state.update_data(year_month=cb.data.split(":")[1])
    await state.set_state(BookFSM.date)
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= datetime.now().date().strftime("%Y-%m-%d"), Slot.date.startswith(cb.data.split(":")[1])).distinct().order_by(Slot.date))
        dates = [r[0] for r in res]
    if not dates: return await cb.answer("❌ Нет дней в этом месяце", show_alert=True)
    kb = InlineKeyboardBuilder()
    for d in dates: kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
    kb.button(text="⬅️ Назад", callback_data="back_to_months"); kb.adjust(1)
    await edit_booking_msg(cb, state, "📆 **Шаг 2/6:** Выберите дату:", kb.as_markup())
    await cb.answer()

# 📅 НАЗАД: К месяцам
@router.callback_query(F.data == "back_to_months")
async def back_to_months(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.month)
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_active, ~Slot.is_booked, Slot.date >= today).distinct())
        dates = [r[0] for r in res]
    months_dict = {d[:7]: None for d in dates}
    if not months_dict:
        txt, kb = "❌ Нет свободных дат.", InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="📞 Админ", callback_data="view_contact"),
            InlineKeyboardButton(text="⬅️ Меню", callback_data="view_main")
        ).as_markup()
        await cb.message.edit_text(txt, reply_markup=kb)
        await cb.answer(); return

    kb = InlineKeyboardBuilder()
    for ym in sorted(months_dict.keys()):
        y, m = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[m]} {y}", callback_data=f"book_month:{ym}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="view_main"))
    await edit_booking_msg(cb, state, "📅 **Шаг 1/6:** Выберите месяц:", kb.as_markup())
    await cb.answer()
    
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
    await edit_booking_msg(cb, state, "⏰ **Шаг 3/6:** Выберите время:", kb.as_markup()); await cb.answer()

@router.callback_query(F.data == "back_to_date")
async def back_to_date(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.date)
    data = await state.get_data()
    ym = data.get("year_month")
    if not ym: return await back_to_months(cb, state)  # Fallback

    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(
            Slot.is_active, ~Slot.is_booked,
            Slot.date >= datetime.now().date().strftime("%Y-%m-%d"),
            Slot.date.startswith(ym)
        ).distinct().order_by(Slot.date))
        dates = [r[0] for r in res]
    if not dates: return await cb.answer("❌ Нет дней в этом месяце", show_alert=True)

    kb = InlineKeyboardBuilder()
    for d in dates: kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
    kb.button(text="⬅️ Назад", callback_data="back_to_months")
    kb.adjust(1)
    await edit_booking_msg(cb, state, "📆 **Шаг 2/6:** Выберите дату:", kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("slot_toggle:"))
async def toggle_slot(cb: CallbackQuery, state: FSMContext):
    sid = int(cb.data.split(":")[1]); data = await state.get_data(); sel = data.get("selected_slots", [])
    if sid in sel: sel.remove(sid)
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

# 📹 ШАГ 4/6: Камеры
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
    ).row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_slots"))
    await edit_booking_msg(cb, state, "📹 **Шаг 4/6:** Выберите оборудование:", kb.as_markup()); await cb.answer()

@router.callback_query(F.data.startswith("camera:"))
async def select_camera(cb: CallbackQuery, state: FSMContext):
    await state.update_data(camera_type=cb.data.split(":")[1]); await _show_summary(cb, state)

@router.callback_query(F.data == "back_to_slots")
async def back_to_slots(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.slots)
    data = await state.get_data()
    date_iso = data.get("date")
    if not date_iso: return await back_to_date(cb, state)

    now, threshold = datetime.now(STUDIO_TZ), datetime.now(STUDIO_TZ) + timedelta(hours=1, minutes=30)
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_iso, Slot.is_active, ~Slot.is_booked).order_by(Slot.start_time))
        all_slots = res.scalars().all()
    slots = []
    if date_iso == now.strftime("%Y-%m-%d"):
        for sl in all_slots:
            if datetime.strptime(f"{date_iso} {sl.start_time}", "%Y-%m-%d %H:%M").replace(tzinfo=STUDIO_TZ) >= threshold:
                slots.append(sl)
    else: slots = all_slots

    kb = InlineKeyboardBuilder()
    sel = data.get("selected_slots", [])
    for sl in slots: kb.button(text=f"{'✅ ' if sl.id in sel else '⏳ '}{sl.start_time}-{sl.end_time}", callback_data=f"slot_toggle:{sl.id}")
    kb.button(text="📝 Далее", callback_data="slots_done"); kb.adjust(2)
    kb.row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_date"))
    await edit_booking_msg(cb, state, "⏰ **Шаг 3/6:** Выберите время:\n💡 *Нажмите на выбранный час, чтобы убрать его*", kb.as_markup())
    await cb.answer()
    
# 📋 ИТОГ
async def _show_summary(cb, state):
    data = await state.get_data(); p = get_prices()
    hours = len(data.get("selected_slots", []))
    rental = hours * p["rental"]
    cam_type = data.get("camera_type", "0")
    base_price = p.get(f"cam{cam_type}", 0) if cam_type != "0" else p.get("no_cam", 0)
    cam_price = base_price * hours
    total = rental + cam_price
    await state.update_data(total_price=total)
    cam_label = "Без камер" if cam_type == "0" else f"{cam_type} кам."
    txt = f"📋 **Итог:**\n📅 {format_date_display(data['date'])} ⏰ {hours} ч\n📹 {cam_label}\n💵 **Всего: {total}₽**"
    kb = InlineKeyboardBuilder().button(text="✅ Подтвердить", callback_data="book_confirm")
    kb.row(InlineKeyboardButton(text="⬅️ Изменить камеры", callback_data="back_to_camera"), InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel"))
    await edit_booking_msg(cb, state, txt, kb.as_markup()); await cb.answer()

@router.callback_query(F.data == "back_to_camera")
async def back_to_camera(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.camera)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📹 1 камера", callback_data="camera:1"),
        InlineKeyboardButton(text="📹 2 камеры", callback_data="camera:2"),
        InlineKeyboardButton(text="📹 3 камеры", callback_data="camera:3")
    ).row(
        InlineKeyboardButton(text="🏢 Студия без камер", callback_data="camera:0")
    ).row(InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_slots"))
    await edit_booking_msg(cb, state, "📹 **Шаг 4/6:** Выберите оборудование:", kb.as_markup())
    await cb.answer()

# 👤 ШАГ 5/6: Имя / Телефон
@router.callback_query(F.data == "book_confirm")
async def check_saved_data(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user and user.client_name and user.phone:
        txt = f"👤 **Данные уже сохранены:**\n👤 Имя: `{user.client_name}`\n📞 Телефон: `{user.phone}`"
        kb = InlineKeyboardBuilder().row(
            InlineKeyboardButton(text="✅ Использовать", callback_data="use_saved_data"),
            InlineKeyboardButton(text="📝 Ввести новые", callback_data="enter_new_data")
        ).row(
            InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
        )
        await edit_booking_msg(cb, state, txt, kb.as_markup())
    else: await enter_new_data(cb, state)
    await cb.answer()

@router.callback_query(F.data == "use_saved_data")
async def use_saved_data(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user:
        await state.update_data(client_name=user.client_name, phone=user.phone)
        await _create_booking(cb, state)
    else: await enter_new_data(cb, state)

@router.callback_query(F.data == "enter_new_data")
async def enter_new_data(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    txt = "👤 Введите ваше имя:"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(cb, state, txt, kb.as_markup()); await cb.answer()

@router.callback_query(F.data == "book_summary_back")
async def back_to_summary(cb: CallbackQuery, state: FSMContext):
    await _show_summary(cb, state)
    await cb.answer()
    
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
    await edit_booking_msg(m, state, "📞 Введите телефон (формат `+79991111111`):", kb.as_markup())

@router.callback_query(F.data == "book_name_back")
async def back_to_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    user = await get_user(cb.from_user.id)
    saved_name = user.client_name if user else "-"
    txt = f"👤 Введите ваше имя:\n(Сохранено: `{saved_name}`)"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(cb, state, txt, kb.as_markup())
    await cb.answer()

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

# 🛠 СОЗДАНИЕ БРОНИ
async def _create_booking(event, state: FSMContext):
    data = await state.get_data()
    deleted_buffers = []

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                return await edit_booking_msg(event, state, "❌ Слот занят. Начните заново.")
            slots.append(sl); sl.is_booked = True

        if slots:
            end_times = set(str(sl.end_time).strip()[:5] for sl in slots)
            free_slots = (await s.execute(select(Slot).where(
                Slot.date == data["date"], Slot.is_active, ~Slot.is_booked
            ))).scalars().all()

            for fs in free_slots:
                if str(fs.start_time).strip()[:5] in end_times:
                    deleted_buffers.append({"date": fs.date, "start": fs.start_time, "end": fs.end_time, "price": float(fs.price)})
                    await s.delete(fs)

        services_data = {"camera": data["camera_type"]}
        if deleted_buffers: services_data["buffer_deleted"] = deleted_buffers

        b = Booking(user_tg_id=event.from_user.id, slot_ids=json.dumps(data["selected_slots"]), services=json.dumps(services_data), total_price=data["total_price"])
        s.add(b); await s.commit()

    times = [f"{sl.start_time}-{sl.end_time}" for sl in slots]
    await _notify_new_booking(event.bot, b.id, data, times, data["total_price"])
    await edit_booking_msg(event, state, f"✅ Бронь #{b.id} создана! Сумма: {int(data['total_price'])}₽. Ждём вас.")
    await state.clear()

@router.callback_query(F.data == "book_cancel")
async def cancel_booking(cb: CallbackQuery, state: FSMContext): await state.clear(); await switch_view(cb, "main"); await cb.answer()

# 📋 МОИ ЗАПИСИ (ПОШАГОВО)
@router.callback_query(F.data == "view_bookings")
async def view_bookings_months(cb: CallbackQuery):
    active = await _get_active_bookings(cb.from_user.id)
    if not active:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="⬅️ В меню", callback_data="view_main"))
        try: await cb.message.edit_text("📭 У вас нет активных записей.", reply_markup=kb.as_markup())
        except: await cb.message.answer("📭 У вас нет активных записей.", reply_markup=kb.as_markup())
        return await cb.answer()

    months = {}
    for _, slots in active: months.setdefault(slots[0].date[:7], True)
    txt, kb = "📋 **Выберите месяц:**", InlineKeyboardBuilder()
    for ym in sorted(months.keys()):
        y, m = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[m]} {y}", callback_data=f"bkg_month:{ym}")
    kb.adjust(1)  # 👈 Месяцы строго по 1 в ряд
    kb.row(InlineKeyboardButton(text="⬅️ В главное меню", callback_data="view_main"))  # 👈 Назад 1 в ряд под всеми
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup())
    except: await cb.message.answer(txt, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("bkg_month:"))
async def view_bookings_days(cb: CallbackQuery):
    ym = cb.data.split(":")[1]
    active = await _get_active_bookings(cb.from_user.id)
    days = {slots[0].date for _, slots in active if slots[0].date[:7] == ym}
    txt, kb = "📅 **Выберите день:**", InlineKeyboardBuilder()
    for d in sorted(days):
        kb.button(text=format_date_display(d), callback_data=f"bkg_date:{d}")
    kb.adjust(2)  # 👈 Дни строго по 2 в ряд
    kb.row(InlineKeyboardButton(text="⬅️ К месяцам", callback_data="view_bookings"))  # 👈 Назад 1 в ряд под всеми
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup())
    except: await cb.message.answer(txt, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("bkg_date:"))
async def view_bookings_day_details(cb: CallbackQuery):
    date_str = cb.data.split(":")[1]
    active = await _get_active_bookings(cb.from_user.id)
    day_bookings = [(b, sl) for b, sl in active if sl[0].date == date_str]
    if not day_bookings:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="⬅️ К дням", callback_data=f"bkg_month:{date_str[:7]}"))
        try: await cb.message.edit_text("📭 На этот день записей нет.", reply_markup=kb.as_markup())
        except: await cb.message.answer("📭 На этот день записей нет.", reply_markup=kb.as_markup())
        return await cb.answer()

    admin_tag = "Не указан"
    if ADMIN_IDS:
        async with async_session() as s:
            admin_u = (await s.execute(select(User).where(User.tg_id == int(ADMIN_IDS[0])))).scalar_one_or_none()
            if admin_u and admin_u.username:
                admin_tag = f"@{admin_u.username}"
            else:
                admin_tag = f"{ADMIN_IDS[0]}"  # ✅ Без кавычек

    txt = f"📅 **Записи на {format_date_display(date_str)}:**\n"
    kb = InlineKeyboardBuilder()

    for i, (b, slots) in enumerate(day_bookings):
        if i > 0: txt += "\n➖➖➖➖➖➖➖➖➖➖\n"
        times_str = _merge_slots_display(slots)
        txt += f"🆔 #{b.id}\n⏰ {times_str}\n💰 {int(b.total_price)}₽\nАдминистратор: {admin_tag}"  # ✅ Без кавычек
        kb.button(text=f"❌ Отменить #{b.id}", callback_data=f"cancel_select:{b.id}")
    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="⬅️ К дням", callback_data=f"bkg_month:{date_str[:7]}"))
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    except: await cb.message.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()
    
@router.callback_query(F.data.startswith("cancel_select:"))
async def cancel_booking_view(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status not in ["confirmed", "confirmed_reminder"]:
            return await cb.answer("⛔ Запись не найдена или уже отменена", show_alert=True)
        sl_res = await s.execute(select(Slot).where(Slot.id.in_(json.loads(b.slot_ids))).order_by(Slot.start_time))
        slots = sl_res.scalars().all()

    txt = (
        f"❌ **Отмена брони #{b.id}**\n"
        f"📅 {format_date_display(slots[0].date)}\n"
        f"⏰ {_merge_slots_display(slots)}\n\n"
        "Вы уверены, что хотите отменить запись?"
    )
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="✅ Да, отменить", callback_data=f"cancel_do:{b.id}"))
    kb.row(InlineKeyboardButton(text="❌ Нет, не отменять", callback_data=f"bkg_date:{slots[0].date}"))
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup())
    except: await cb.message.answer(txt, reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("cancel_do:"))
async def cancel_booking_confirm(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b or b.user_tg_id != cb.from_user.id or b.status != "confirmed":
            return await cb.answer("⛔ Нельзя отменить", show_alert=True)
        b.status = "cancelled"
        for sid in json.loads(b.slot_ids):
            sl = await s.get(Slot, sid)
            if sl: sl.is_booked = False
        svc = json.loads(b.services) if b.services else {}
        buffers = svc.get("buffer_deleted", [])
        if not isinstance(buffers, list): buffers = [buffers]
        for buf in buffers:
            s.add(Slot(date=buf["date"], start_time=buf["start"], end_time=buf["end"], price=buf["price"], is_active=True, is_booked=False))
        await s.commit()
    await cb.answer("✅ Запись отменена")
    await _notify_admins(cb.bot, b, "cancelled")
    kb = InlineKeyboardBuilder().button(text="📋 В мои записи", callback_data="view_bookings").as_markup()
    try: await cb.message.edit_text("✅ **Бронь успешно отменена.**\nСлоты возвращены в расписание.", reply_markup=kb)
    except: await cb.message.answer("✅ Бронь отменена.", reply_markup=kb)

# 📢 УВЕДОМЛЕНИЯ
async def _notify_new_booking(bot, booking_id: int, data: dict, times_str: list, total_price: float):
    def merge_slots(times):
        if not times: return []
        slots = sorted([t.split("-") for t in times], key=lambda x: x[0])
        merged, curr_start, curr_end, count = [], slots[0][0], slots[0][1], 1
        for start, end in slots[1:]:
            if start == curr_end: curr_end, count = end, count + 1
            else:
                h = "час" if count == 1 else "часа" if 2 <= count <= 4 else "часов"
                merged.append(f"{curr_start}-{curr_end} ({count} {h})"); curr_start, curr_end, count = start, end, 1
        h = "час" if count == 1 else "часа" if 2 <= count <= 4 else "часов"
        merged.append(f"{curr_start}-{curr_end} ({count} {h})")
        return merged
    time_lines = merge_slots(times_str)
    cam = "Без камер" if data.get("camera_type") == "0" else f"{data.get('camera_type')} кам."
    msg = (
        f"🆕 **Бронь #{booking_id}**\n"
        f"👤 {data['client_name']}\n"
        f"📞 {data['phone']}\n"  # ✅ Без кавычек
        f"📅 {format_date_display(data['date'])}\n"
        f"⏰ " + "\n⏰ ".join(time_lines) + "\n"
        f"📹 {cam}\n"
        f"💰 {int(total_price)}₽"
    )
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Notify fail {aid}: {e}")
        await asyncio.sleep(0.3)
        
async def _notify_admins(bot, booking, action):
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == booking.user_tg_id))).scalar_one_or_none()
        name, phone = u.client_name if u else "Не указано", u.phone if u else "Не указан"
        tag = f"@{u.username}" if u and u.username else f"{booking.user_tg_id}"  # ✅ Без кавычек
        slots = (await s.execute(select(Slot).where(Slot.id.in_(json.loads(booking.slot_ids))).order_by(Slot.start_time))).scalars().all()
        def merge_intervals(times):
            if not times: return []
            s_list = sorted([t.split("-") for t in times], key=lambda x: x[0])
            merged, curr_s, curr_e = [], s_list[0][0], s_list[0][1]
            for start, end in s_list[1:]:
                if start == curr_e: curr_e = end
                else: merged.append(f"{curr_s}-{curr_e}"); curr_s, curr_e = start, end
            merged.append(f"{curr_s}-{curr_e}")
            return merged
        times = [f"{sl.start_time}-{sl.end_time}" for sl in slots]
        merged_intervals = merge_intervals(times)
        date_str = format_date_display(slots[0].date) if slots else "Не указано"
    if action == "cancelled":
        intervals_text = "\n".join([f"⏰ {t}" for t in merged_intervals])
        msg = (
            f"❌ **Бронь #{booking.id} отменена**\n"
            f"📅 {date_str}\n{intervals_text}\n"
            f"👤 Клиент: {name}\n"
            f"🆔 ID: {tag}\n"  # ✅ Без кавычек
            f"📞 Телефон: {phone}"  # ✅ Без кавычек
        )
    else: msg = f"{'✅' if action == 'confirmed' else '❌'} Клиент {tag} ответил.\n🆔 Бронь #{booking.id}"
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Admin notify fail {aid}: {e}")
        await asyncio.sleep(0.3)
