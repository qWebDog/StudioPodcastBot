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
                text = "📋 **Ваши активные записи:**\n"
                kb = InlineKeyboardBuilder()

                # 🔹 Объединение смежных часов в интервалы
                def merge_intervals(slts):
                    if not slts: return ""
                    times = sorted([(str(sl.start_time)[:5], str(sl.end_time)[:5]) for sl in slts])
                    merged = []
                    curr_s, curr_e = times[0]
                    for s_t, e_t in times[1:]:
                        if s_t == curr_e:
                            curr_e = e_t
                        else:
                            merged.append(f"{curr_s}-{curr_e}")
                            curr_s, curr_e = s_t, e_t
                    merged.append(f"{curr_s}-{curr_e}")
                    return "\n⏰ ".join(merged)

                admin_id = str(ADMIN_IDS[0]) if ADMIN_IDS else "Не указан"

                for i, (b, slots) in enumerate(active):
                    # Добавляем разделитель между записями (кроме первой)
                    if i > 0:
                        text += "\n➖➖➖➖➖➖➖➖➖➖➖➖➖➖➖\n"

                    date_str = format_date_display(slots[0].date)
                    times_str = merge_intervals(slots)

                    text += (
                        f"📅 {date_str}\n"
                        f"⏰ {times_str}\n"
                        f"💰 {int(b.total_price)}₽\n"
                        f"Администратор: `{admin_id}`"
                    )
                    # ID записи скрыт от пользователя, но остаётся в callback_data
                    kb.button(text="❌ Отменить запись", callback_data=f"my_cancel:{b.id}")

                kb.adjust(1)
                kb.button(text="⬅️ В главное меню", callback_data="view_main")
                kb = kb.as_markup()

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
    else:
        await enter_new_data(cb, state)
    await cb.answer()


@router.callback_query(F.data == "use_saved_data")
async def use_saved_data(cb: CallbackQuery, state: FSMContext):
    user = await get_user(cb.from_user.id)
    if user:
        await state.update_data(client_name=user.client_name, phone=user.phone)
        await _create_booking(cb, state)
    else:
        await enter_new_data(cb, state)

@router.callback_query(F.data == "enter_new_data")
async def enter_new_data(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    txt = "👤 Введите ваше имя:"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(cb, state, txt, kb.as_markup())
    await cb.answer()

@router.callback_query(F.data == "book_name_back")
async def back_to_name(cb: CallbackQuery, state: FSMContext):
    await state.set_state(BookFSM.name)
    txt = "👤 Введите ваше имя:"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к итогу", callback_data="book_summary_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(cb, state, txt, kb.as_markup())
    await cb.answer()

@router.message(BookFSM.name)
async def save_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if len(name) < 2: return await m.answer("⚠️ Минимум 2 символа.")
    await state.update_data(client_name=name)
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not u: 
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, client_name=name))
        else: 
            u.client_name = name
        await s.commit()
        
    await state.set_state(BookFSM.phone)
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="⬅️ Назад к имени", callback_data="book_name_back"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    )
    await edit_booking_msg(m, state, "📞 Введите телефон (формат `+79991111111`):", kb.as_markup())

@router.message(BookFSM.phone)
async def save_phone(m: Message, state: FSMContext):
    phone = m.text.strip()
    if not validate_phone(phone): 
        return await m.answer("⚠️ Введите строго `+79991111111`", parse_mode="Markdown")
    await state.update_data(phone=phone)
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == m.from_user.id))).scalar_one_or_none()
        if not u: 
            s.add(User(tg_id=m.from_user.id, username=m.from_user.username, phone=phone))
        else: 
            u.phone = phone
        await s.commit()
    await _create_booking(m, state)

async def _create_booking(event, state: FSMContext):
    data = await state.get_data()
    deleted_buffers = []

    async with async_session() as s:
        slots = []
        for sid in data["selected_slots"]:
            sl = await s.get(Slot, sid)
            if not sl or sl.is_booked or not sl.is_active:
                return await edit_booking_msg(event, state, "❌ Слот занят. Начните заново.")
            slots.append(sl)
            sl.is_booked = True

        # 🔍 Собираем уникальные времена окончания выбранных слотов
        end_times = set(str(sl.end_time).strip()[:5] for sl in slots)

        # Загружаем все свободные слоты на эту дату
        free_slots = (await s.execute(select(Slot).where(
            Slot.date == data["date"],
            Slot.is_active == True,
            Slot.is_booked == False
        ))).scalars().all()

        # Удаляем слоты, которые начинаются ровно в время окончания любого booked-слота
        for fs in free_slots:
            start_norm = str(fs.start_time).strip()[:5]
            if start_norm in end_times:
                deleted_buffers.append({
                    "date": fs.date,
                    "start": fs.start_time,
                    "end": fs.end_time,
                    "price": float(fs.price)
                })
                await s.delete(fs)

        services_data = {"camera": data["camera_type"]}
        if deleted_buffers:
            services_data["buffer_deleted"] = deleted_buffers

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

        # 🔄 Восстановление всех удалённых буферных слотов
        svc = json.loads(b.services) if b.services else {}
        buffers = svc.get("buffer_deleted", [])
        if not isinstance(buffers, list): buffers = [buffers]  # Совместимость со старыми бронями

        for buf in buffers:
            s.add(Slot(
                date=buf["date"],
                start_time=buf["start"],
                end_time=buf["end"],
                price=buf["price"],
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
# 📢 Уведомление о новой брони
async def _notify_new_booking(bot, booking_id: int, data: dict, times_str: list, total_price: float):
    # 🔹 Утилита: объединяет подряд идущие интервалы и возвращает список строк
    def merge_slots(times):
        if not times: return []
        slots = sorted([t.split("-") for t in times], key=lambda x: x[0])
        merged = []
        curr_start, curr_end = slots[0]
        count = 1

        for start, end in slots[1:]:
            if start == curr_end:
                curr_end = end
                count += 1
            else:
                h = "час" if count == 1 else "часа" if 2 <= count <= 4 else "часов"
                merged.append(f"{curr_start}-{curr_end} ({count} {h})")
                curr_start, curr_end = start, end
                count = 1

        h = "час" if count == 1 else "часа" if 2 <= count <= 4 else "часов"
        merged.append(f"{curr_start}-{curr_end} ({count} {h})")
        return merged

    time_lines = merge_slots(times_str)
    cam = "Без камер" if data.get("camera_type") == "0" else f"{data.get('camera_type')} кам."

    # ✅ Имя и телефон вынесены на отдельные строки
    msg = (
        f"🆕 **Бронь #{booking_id}**\n"
        f"👤 {data['client_name']}\n"
        f"📞 `{data['phone']}`\n"
        f"📅 {format_date_display(data['date'])}\n"
        f"⏰ " + "\n⏰ ".join(time_lines) + "\n"
        f"📹 {cam}\n"
        f"💰 {int(total_price)}₽"
    )
    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Notify fail {aid}: {e}")
        await asyncio.sleep(0.3)

# 📢 Уведомление админам (отмена/подтверждение)
async def _notify_admins(bot, booking, action):
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.tg_id == booking.user_tg_id))).scalar_one_or_none()
        name = u.client_name if u else "Не указано"
        phone = u.phone if u else "Не указан"
        tag = f"@{u.username}" if u and u.username else f"`{booking.user_tg_id}`"

        slot_ids = json.loads(booking.slot_ids)
        slots = (await s.execute(select(Slot).where(Slot.id.in_(slot_ids)).order_by(Slot.start_time))).scalars().all()

        # 🔹 Объединяем смежные интервалы (без указания кол-ва часов)
        def merge_intervals(times):
            if not times: return []
            s_list = sorted([t.split("-") for t in times], key=lambda x: x[0])
            merged = []
            curr_start, curr_end = s_list[0]
            for start, end in s_list[1:]:
                if start == curr_end:
                    curr_end = end
                else:
                    merged.append(f"{curr_start}-{curr_end}")
                    curr_start, curr_end = start, end
            merged.append(f"{curr_start}-{curr_end}")
            return merged

        times = [f"{sl.start_time}-{sl.end_time}" for sl in slots]
        merged_intervals = merge_intervals(times)
        date_str = format_date_display(slots[0].date) if slots else "Не указано"

    if action == "cancelled":
        intervals_text = "\n".join([f"⏰ {t}" for t in merged_intervals])
        msg = (
            f"❌ **Бронь #{booking.id} отменена**\n"
            f"📅 {date_str}\n"
            f"{intervals_text}\n"
            f"👤 Клиент: {name}\n"
            f"🆔 ID: {tag}\n"
            f"📞 Телефон: `{phone}`"
        )
    else:
        msg = f"{'✅' if action == 'confirmed' else '❌'} Клиент {tag} ответил.\n🆔 Бронь #{booking.id}"

    for aid in ADMIN_IDS:
        try: await bot.send_message(aid, msg, parse_mode="Markdown")
        except Exception as e: logger.error(f"Admin notify fail {aid}: {e}")
        await asyncio.sleep(0.3)
