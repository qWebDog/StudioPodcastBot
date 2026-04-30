import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from database import async_session, Slot, Booking, User
from config import ADMIN_IDS

router = Router()
logger = logging.getLogger(__name__)
PRICES_FILE = os.path.join(os.getcwd(), "prices.json")

class AdminFSM(StatesGroup):
    add_date = State()
    add_start = State()
    add_end = State()
    waiting_price_key = State()
    waiting_broadcast = State()
    waiting_phone_search = State()

# 💰 Управление ценами
def load_prices():
    defaults = {"rental": 2000, "cam1": 3000, "cam2": 3500, "cam3": 4000, "editing": 5000}
    try:
        with open(PRICES_FILE, "r") as f:
            saved = json.load(f)
            return {**defaults, **saved}
    except:
        return defaults

def save_prices(prices: dict):
    with open(PRICES_FILE, "w") as f:
        json.dump(prices, f)

def fmt_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        return f"{dt.day:02d}.{dt.month:02d} {days[dt.weekday()]}"
    except:
        return d

# 🛠 Универсальная функция отправки меню (без FSMContext)
async def send_admin_menu(event):
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📋 Все слоты", callback_data="admin_slots_list"),
        InlineKeyboardButton(text="📖 Все брони", callback_data="admin_bookings_list")
    ).row(
        InlineKeyboardButton(text="➕ Создать слот", callback_data="admin_add_slot"),
        InlineKeyboardButton(text="💰 Редактор цен", callback_data="admin_prices")
    ).row(
        InlineKeyboardButton(text="🗓️ Брони по дате", callback_data="admin_bookings_by_date"),
        InlineKeyboardButton(text="🔍 Поиск по тел.", callback_data="adm_search_phone")
    ).row(InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast"))
    kb.adjust(2)
    try:
        await event.message.edit_text("🛠️ **Панель администратора:**", reply_markup=kb.as_markup(), parse_mode="Markdown")
    except:
        await event.answer("🛠️ **Панель администратора:**", reply_markup=kb.as_markup(), parse_mode="Markdown")

# 🛠 Команда /admin
@router.message(F.text == "/admin", F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(m: Message):
    await send_admin_menu(m)

@router.callback_query(F.data == "admin_menu", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu(cb: CallbackQuery):
    await send_admin_menu(cb)
    await cb.answer()

# 📅 Список слотов
@router.callback_query(F.data == "admin_slots_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_slots_list(cb: CallbackQuery):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date >= today).order_by(Slot.date, Slot.start_time))
        slots = res.scalars().all()[:50]
    if not slots:
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        try: await cb.message.edit_text("📭 Нет будущих слотов.", reply_markup=kb.as_markup())
        except: await cb.message.answer("📭 Нет будущих слотов.", reply_markup=kb.as_markup())
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for sl in slots:
        icon = "🔒" if sl.is_booked else ("✅" if sl.is_active else "❌")
        kb.button(text=f"{icon} {fmt_date(sl.date)} | {sl.start_time}-{sl.end_time}", callback_data=f"slot_manage:{sl.id}")
    kb.adjust(1)
    kb.button(text="🔙 В меню", callback_data="admin_menu")
    try: await cb.message.edit_text("📋 **Слоты (ближайшие 50):**", reply_markup=kb.as_markup())
    except: await cb.message.answer("📋 **Слоты (ближайшие 50):**", reply_markup=kb.as_markup())
    await cb.answer()

# ➕ Создание слотов на день (Шаг 1)
@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def add_slot_day_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📅 **Создать слоты на целый день**\nВведите дату в формате `ДД.ММ`:", parse_mode="Markdown")
    await state.set_state(AdminFSM.add_date)
    await cb.answer()

@router.message(AdminFSM.add_date, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_day_date(m: Message, state: FSMContext):
    try:
        day, month = map(int, m.text.strip().split("."))
        year = datetime.now().year
        dt = datetime(year, month, day)
        if dt.date() < datetime.now().date():
            dt = dt.replace(year=year + 1)
        await state.update_data(slot_date=dt.strftime("%Y-%m-%d"))
        await m.answer("⏰ Введите время начала работы студии (например, `10:00`):")
        await state.set_state(AdminFSM.add_start)
    except Exception:
        await m.answer("⚠️ Неверный формат. Пример: `25.12`")

@router.message(AdminFSM.add_start, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_day_start_time(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= mi <= 59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Введите время окончания работы (например, `22:00`):")
        await state.set_state(AdminFSM.add_end)
    except Exception:
        await m.answer("⚠️ Неверный формат. Пример: `22:00`")

@router.message(AdminFSM.add_end, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_day_end_time(m: Message, state: FSMContext):
    try:
        h_end, mi_end = map(int, m.text.strip().split(":"))
        if not (0 <= h_end <= 23 and 0 <= mi_end <= 59): raise ValueError
        end_time = f"{h_end:02d}:{mi_end:02d}"

        data = await state.get_data()
        date_str = data["slot_date"]
        start_str = data["slot_start"]

        start_dt = datetime.strptime(start_str, "%H:%M")
        end_dt = datetime.strptime(end_time, "%H:%M")

        slots_to_create = []
        current = start_dt
        while current < end_dt:
            next_dt = current + timedelta(hours=1)
            if next_dt > end_dt: break
            slots_to_create.append((current.strftime("%H:%M"), next_dt.strftime("%H:%M")))
            current = next_dt

        if not slots_to_create:
            return await m.answer("❌ Время окончания должно быть позже начала минимум на 1 час.")

        async with async_session() as s:
            existing = await s.execute(select(Slot).where(Slot.date == date_str))
            if len(existing.scalars().all()) > 0:
                return await m.answer(f"⚠️ На {date_str} уже есть слоты. Удалите их или выберите другую дату.")

            for st, et in slots_to_create:
                s.add(Slot(date=date_str, start_time=st, end_time=et, price=0.0, is_active=True, is_booked=False))
            await s.commit()

        await m.answer(f"✅ Создано {len(slots_to_create)} слотов на {date_str}\n⏰ {start_str} – {end_time}\n💰 Цена: 0₽/час")
        await state.clear()
        await admin_slots_list(m)
    except Exception as e:
        await m.answer(f"⚠️ Ошибка: {str(e)}")

# 🔧 Управление слотом
@router.callback_query(F.data.startswith("slot_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def manage_slot(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        sl = await s.get(Slot, sid)
    if not sl:
        return await cb.answer("❌ Слот не найден", show_alert=True)
    status = "✅ Активен" if sl.is_active else "❌ Отключен"
    booked = "🔒 Забронирован" if sl.is_booked else "⏳ Свободен"
    txt = f"📅 **Слот #{sl.id}**\n{fmt_date(sl.date)} | {sl.start_time}-{sl.end_time}\n{status} | {booked}\n💰 Цена: {int(sl.price)}₽"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="🔄 " + ("Отключить" if sl.is_active else "Включить"), callback_data=f"slot_toggle:{sl.id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"slot_delete:{sl.id}")
    ).button(text="🔙 Назад", callback_data="admin_slots_list")
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    except: await cb.message.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("slot_toggle:"), F.from_user.id.in_(ADMIN_IDS))
async def toggle_slot_active(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        sl = await s.get(Slot, sid)
        if sl:
            sl.is_active = not sl.is_active
            await s.commit()
    await manage_slot(cb)

@router.callback_query(F.data.startswith("slot_delete:"), F.from_user.id.in_(ADMIN_IDS))
async def delete_slot(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        sl = await s.get(Slot, sid)
        if sl and not sl.is_booked:
            await s.delete(sl)
            await s.commit()
            await cb.answer("🗑 Слот удалён")
        else:
            await cb.answer("⛔ Нельзя удалить забронированный слот", show_alert=True)
    await admin_slots_list(cb)

# 📖 Список броней
@router.callback_query(F.data == "admin_bookings_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_bookings_list(cb: CallbackQuery):
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(30))
        bookings = res.scalars().all()
    if not bookings:
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        try: await cb.message.edit_text("📭 Броней нет.", reply_markup=kb.as_markup())
        except: await cb.message.answer("📭 Броней нет.", reply_markup=kb.as_markup())
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for b in bookings:
        em = {"confirmed": "🟢", "confirmed_reminder": "🔵", "cancelled": "🔴"}.get(b.status, "⚪")
        kb.button(text=f"{em} #{b.id} | {int(b.total_price)}₽", callback_data=f"adm_booking:{b.id}")
    kb.adjust(1)
    kb.button(text="🔙 В меню", callback_data="admin_menu")
    try: await cb.message.edit_text("📖 **Последние 30 броней:**", reply_markup=kb.as_markup())
    except: await cb.message.answer("📖 **Последние 30 броней:**", reply_markup=kb.as_markup())
    await cb.answer()

# 🔍 Детали брони
@router.callback_query(F.data.startswith("adm_booking:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_booking_detail(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b: return await cb.answer("❌ Не найдена", show_alert=True)
        user = (await s.execute(select(User).where(User.tg_id == b.user_tg_id))).scalar_one_or_none()
        slot_ids = json.loads(b.slot_ids)
        res_sl = await s.execute(select(Slot).where(Slot.id.in_(slot_ids)))
        slots = res_sl.scalars().all()
        svc = json.loads(b.services) if b.services else {}
    times = " | ".join([f"{sl.start_time}-{sl.end_time}" for sl in slots])
    txt = (f"🆔 **Бронь #{b.id}** | `{b.status}`\n"
           f"👤 {user.client_name if user else '?'} | 📞 `{user.phone if user else '?'} `\n"
           f"📅 {fmt_date(slots[0].date)} | ⏰ {times}\n"
           f"📹 {svc.get('camera','?')} кам. | 🎬 Монтаж: {'Да' if svc.get('editing')=='yes' else 'Нет'}\n"
           f"💵 {int(b.total_price)}₽")
    kb = InlineKeyboardBuilder()
    if b.status == "confirmed":
        kb.row(InlineKeyboardButton(text="✅ Подтв.", callback_data=f"adm_confirm:{b.id}"),
               InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_cancel:{b.id}"))
    kb.button(text="🔙 Назад", callback_data="admin_bookings_list")
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    except: await cb.message.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("adm_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_confirm(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if b and b.status != "confirmed_reminder":
            b.status = "confirmed"
            await s.commit()
    await cb.answer("✅ Подтверждено")
    await adm_booking_detail(cb)

@router.callback_query(F.data.startswith("adm_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if b and b.status != "cancelled":
            b.status = "cancelled"
            for sid in json.loads(b.slot_ids):
                sl = await s.get(Slot, sid)
                if sl: sl.is_booked = False
            await s.commit()
    await cb.answer("❌ Отменено")
    await adm_booking_detail(cb)

# 🗓️ Брони по дате
@router.callback_query(F.data == "admin_bookings_by_date", F.from_user.id.in_(ADMIN_IDS))
async def admin_bookings_by_date(cb: CallbackQuery):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_booked == True, Slot.date >= today).distinct().order_by(Slot.date.desc()))
        dates = [row[0] for row in res]
    if not dates:
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        try: await cb.message.edit_text("📭 Нет броней на сегодня/будущее.", reply_markup=kb.as_markup())
        except: await cb.message.answer("📭 Нет броней на сегодня/будущее.", reply_markup=kb.as_markup())
        await cb.answer()
        return
    kb = InlineKeyboardBuilder()
    for d in dates: kb.button(text=fmt_date(d), callback_data=f"adm_bookings_date:{d}")
    kb.adjust(1)
    kb.button(text="🔙 В меню", callback_data="admin_menu")
    try: await cb.message.edit_text("🗓️ **Выберите дату:**", reply_markup=kb.as_markup())
    except: await cb.message.answer("🗓️ **Выберите дату:**", reply_markup=kb.as_markup())
    await cb.answer()

@router.callback_query(F.data.startswith("adm_bookings_date:"), F.from_user.id.in_(ADMIN_IDS))
async def show_date_bookings(cb: CallbackQuery):
    date_str = cb.data.split(":")[1]
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(50))
        bookings = []
        target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        for b in res.scalars().all():
            sids = json.loads(b.slot_ids)
            sl_res = await s.execute(select(Slot).where(Slot.id.in_(sids)))
            sls = sl_res.scalars().all()
            if any(sl.date == date_str for sl in sls):
                bookings.append(b)
    if not bookings:
        await cb.answer("Нет броней", show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    for b in bookings: kb.button(text=f"🟢 #{b.id} | {int(b.total_price)}₽", callback_data=f"adm_booking:{b.id}")
    kb.adjust(1)
    kb.button(text="⬅️ Назад к датам", callback_data="admin_bookings_by_date")
    try: await cb.message.edit_text(f"📅 Брони на {fmt_date(date_str)}:", reply_markup=kb.as_markup())
    except: await cb.message.answer(f"📅 Брони на {fmt_date(date_str)}:", reply_markup=kb.as_markup())
    await cb.answer()

# 💰 Редактор цен
@router.callback_query(F.data == "admin_prices", F.from_user.id.in_(ADMIN_IDS))
async def show_prices(cb: CallbackQuery):
    p = load_prices()
    txt = (f"💰 **Текущие цены:**\n🎙️ Аренда: {p['rental']}₽/час\n📹 1 кам: {p['cam1']}₽/час\n📹 2 кам: {p['cam2']}₽/час\n📹 3 кам: {p['cam3']}₽/час\n🎬 Монтаж: {p['editing']}₽")
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="✏️ Аренда", callback_data="set_rental"),
        InlineKeyboardButton(text="✏️ 1 кам.", callback_data="set_cam1"),
        InlineKeyboardButton(text="✏️ 2 кам.", callback_data="set_cam2")
    ).row(
        InlineKeyboardButton(text="✏️ 3 кам.", callback_data="set_cam3"),
        InlineKeyboardButton(text="✏️ Монтаж", callback_data="set_editing")
    ).row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
    try: await cb.message.edit_text(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    except: await cb.message.answer(txt, reply_markup=kb.as_markup(), parse_mode="Markdown")
    await cb.answer()

@router.callback_query(F.data.startswith("set_"), F.from_user.id.in_(ADMIN_IDS))
async def ask_price(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split("_")[1]
    await state.update_data(price_key=key)
    await state.set_state(AdminFSM.waiting_price_key)
    names = {"rental": "Аренда/час", "cam1": "1 камера/час", "cam2": "2 камеры/час", "cam3": "3 камеры/час", "editing": "Монтаж"}
    await cb.message.edit_text(f"💸 Введите цену для `{names[key]}` (только число):")
    await cb.answer()

@router.message(AdminFSM.waiting_price_key, F.from_user.id.in_(ADMIN_IDS))
async def save_price(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Только цифры.")
    data = await state.get_data()
    p = load_prices()
    p[data["price_key"]] = int(m.text)
    save_prices(p)
    await state.clear()
    await m.answer(f"✅ Цена сохранена: {int(m.text)}₽")
    await send_admin_menu(m)

# 🔍 Поиск по телефону
@router.callback_query(F.data == "adm_search_phone", F.from_user.id.in_(ADMIN_IDS))
async def search_phone_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🔍 Введите номер для поиска (`+7999...`):")
    await state.set_state(AdminFSM.waiting_phone_search)
    await cb.answer()

@router.message(AdminFSM.waiting_phone_search, F.from_user.id.in_(ADMIN_IDS))
async def search_phone_exec(m: Message, state: FSMContext):
    phone = m.text.strip()
    async with async_session() as s:
        res = await s.execute(select(User).where(User.phone == phone))
        user = res.scalar_one_or_none()
    if not user:
        await m.answer("❌ Пользователь не найден.")
        await state.clear()
        return
    async with async_session() as s:
        res_b = await s.execute(select(Booking).where(Booking.user_tg_id == user.tg_id).order_by(Booking.created_at.desc()).limit(10))
        bookings = res_b.scalars().all()
    txt = f"👤 **{user.client_name or 'Без имени'}**\n📱 `{user.phone}`\n🆔 TG: `{user.tg_id}`\n📊 Всего броней: {len(bookings)}"
    if bookings:
        txt += "\n🔹 Последние:\n"
        for b in bookings[:5]:
            sid = json.loads(b.slot_ids)[0]
            async with async_session() as s2: sl = await s2.get(Slot, sid)
            txt += f"  • #{b.id} | {fmt_date(sl.date) if sl else '?'} | {b.status} | {int(b.total_price)}₽\n"
    await m.answer(txt, parse_mode="Markdown")
    await state.clear()

# 📢 Рассылка
@router.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def broadcast_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📢 **Рассылка:**\nВведите текст сообщения:")
    await state.set_state(AdminFSM.waiting_broadcast)
    await cb.answer()

@router.message(AdminFSM.waiting_broadcast, F.from_user.id.in_(ADMIN_IDS))
async def broadcast_exec(m: Message, state: FSMContext):
    await m.answer("⏳ Рассылка запущена...")
    async with async_session() as s:
        res = await s.execute(select(User.tg_id).distinct())
        targets = [r[0] for r in res]
    sent = 0
    for tid in targets:
        try: await m.bot.send_message(tid, m.text); sent += 1; await asyncio.sleep(0.3)
        except: pass
    await m.answer(f"✅ Рассылка завершена. Доставлено: {sent}/{len(targets)}")
    await state.clear()
    await send_admin_menu(m)
