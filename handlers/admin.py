import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select
from database import async_session, Slot, Booking, User
from config import ADMIN_IDS
import calendar

router = Router()
logger = logging.getLogger(__name__)
PRICES_FILE = os.path.join(os.getcwd(), "prices.json")

class AdminFSM(StatesGroup):
    waiting_price_key = State()
    waiting_broadcast = State()
    waiting_phone_search = State()
    # Один слот
    single_date = State()
    single_start = State()
    single_end = State()
    # День
    day_date = State()
    day_start = State()
    day_end = State()
    # Месяц
    month_month = State()
    month_start = State()
    month_end = State()
    # Период
    period_start_date = State()
    period_end_date = State()
    period_start = State()
    period_end = State()
    
# 💰 Загрузка цен (автоматически заменяет старый ключ 'editing' на 'no_cam')
def load_prices():
    defaults = {"rental": 0, "cam1": 3000, "cam2": 3500, "cam3": 4000, "no_cam": 0}
    try:
        with open(PRICES_FILE, "r") as f:
            saved = json.load(f)
            if "editing" in saved:
                saved["no_cam"] = saved.pop("editing")
            return {**defaults, **saved}
    except:
        return defaults

def save_prices(prices: dict):
    with open(PRICES_FILE, "w") as f:
        json.dump(prices, f)

def fmt_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.day:02d}.{dt.month:02d} {['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][dt.weekday()]}"
    except: return d

def _get_msg(event):
    return event.message if isinstance(event, CallbackQuery) else event

async def _send_text(event, text: str, kb: InlineKeyboardMarkup = None):
    msg = _get_msg(event)
    try: await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except: await msg.answer(text, reply_markup=kb, parse_mode="Markdown")

# 🛠 Главное меню
async def _show_admin_menu(event):
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📋 Все слоты", callback_data="admin_slots_list"),
        InlineKeyboardButton(text="📖 Брони", callback_data="admin_bookings_menu")
    ).row(
        InlineKeyboardButton(text="💰 Редактор цен", callback_data="admin_prices"),
        InlineKeyboardButton(text="🔍 Поиск по тел.", callback_data="adm_search_phone")
    ).row(
        InlineKeyboardButton(text="➕ Создание слотов", callback_data="admin_create_slot_menu")
    ).row(
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
    )
    await _send_text(event, "🛠️ **Панель администратора:**", kb.as_markup())

@router.message(F.text == "/admin", F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(m: Message): await _show_admin_menu(m)
@router.callback_query(F.data == "admin_menu", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu_cb(cb: CallbackQuery): await _show_admin_menu(cb); await cb.answer()

# ➕ ПОДМЕНЮ СОЗДАНИЯ СЛОТОВ
@router.callback_query(F.data == "admin_create_slot_menu", F.from_user.id.in_(ADMIN_IDS))
async def create_slot_menu(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📅 Один слот", callback_data="create_single"))
    kb.row(InlineKeyboardButton(text="📆 Слоты на день", callback_data="create_day"))
    kb.row(InlineKeyboardButton(text="🗓️ Слоты на месяц", callback_data="create_month"))
    kb.row(InlineKeyboardButton(text="📅 Слоты период", callback_data="create_period"))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
    await _send_text(cb, "➕ **Выберите режим создания:**", kb.as_markup())
    await cb.answer()

# 🔹 1. ОДИН СЛОТ
@router.callback_query(F.data == "create_single", F.from_user.id.in_(ADMIN_IDS))
async def start_single(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📅 **Один слот**\nВведите дату (`ДД.ММ`):")
    await state.set_state(AdminFSM.single_date); await cb.answer()

@router.message(AdminFSM.single_date, F.from_user.id.in_(ADMIN_IDS))
async def single_date(m: Message, state: FSMContext):
    try:
        d, mo = map(int, m.text.strip().split("."))
        y = datetime.now().year
        dt = datetime(y, mo, d)
        if dt.date() < datetime.now().date(): dt = dt.replace(year=y+1)
        await state.update_data(slot_date=dt.strftime("%Y-%m-%d"))
        await m.answer("⏰ Время начала (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.single_start)
    except: await m.answer("⚠️ Формат: `25.12`")

@router.message(AdminFSM.single_start, F.from_user.id.in_(ADMIN_IDS))
async def single_start(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0<=h<=23 and 0<=mi<=59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Время окончания (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.single_end)
    except: await m.answer("⚠️ Формат: `22:00`")

@router.message(AdminFSM.single_end, F.from_user.id.in_(ADMIN_IDS))
async def single_end(m: Message, state: FSMContext):
    try:
        he, me = map(int, m.text.strip().split(":"))
        if not (0<=he<=23 and 0<=me<=59): raise ValueError
        data = await state.get_data()
        s_dt = datetime.strptime(data["slot_start"], "%H:%M")
        e_dt = datetime.strptime(f"{he:02d}:{me:02d}", "%H:%M")
        if (e_dt - s_dt).total_seconds() <= 3600: return await m.answer("❌ Минимум 1 час.")
        async with async_session() as s:
            s.add(Slot(date=data["slot_date"], start_time=data["slot_start"], end_time=f"{he:02d}:{me:02d}", price=0.0, is_active=True, is_booked=False))
            await s.commit()
        await m.answer(f"✅ Слот создан: {data['slot_date']} | {data['slot_start']}-{he:02d}:{me:02d}")
        await state.clear()
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
        await m.answer("✅ Готово.", reply_markup=kb.as_markup())
    except Exception as e: await m.answer(f"⚠️ Ошибка: {e}")

# 🔹 2. СЛОТЫ НА ДЕНЬ
@router.callback_query(F.data == "create_day", F.from_user.id.in_(ADMIN_IDS))
async def start_day(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📆 **Слоты на день**\nВведите дату (`ДД.ММ`):")
    await state.set_state(AdminFSM.day_date); await cb.answer()

@router.message(AdminFSM.day_date, F.from_user.id.in_(ADMIN_IDS))
async def day_date(m: Message, state: FSMContext):
    try:
        d, mo = map(int, m.text.strip().split("."))
        y = datetime.now().year
        dt = datetime(y, mo, d)
        if dt.date() < datetime.now().date(): dt = dt.replace(year=y+1)
        await state.update_data(slot_date=dt.strftime("%Y-%m-%d"))
        await m.answer("⏰ Время начала (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.day_start)
    except: await m.answer("⚠️ Формат: `25.12`")

@router.message(AdminFSM.day_start, F.from_user.id.in_(ADMIN_IDS))
async def day_start(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0<=h<=23 and 0<=mi<=59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Время окончания (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.day_end)
    except: await m.answer("⚠️ Формат: `22:00`")

@router.message(AdminFSM.day_end, F.from_user.id.in_(ADMIN_IDS))
async def day_end(m: Message, state: FSMContext):
    try:
        he, me = map(int, m.text.strip().split(":"))
        if not (0<=he<=23 and 0<=me<=59): raise ValueError
        data = await state.get_data()
        s_dt = datetime.strptime(data["slot_start"], "%H:%M")
        e_dt = datetime.strptime(f"{he:02d}:{me:02d}", "%H:%M")
        slots = []; cur = s_dt
        while cur < e_dt:
            nxt = cur + timedelta(hours=1)
            if nxt > e_dt: break
            slots.append((cur.strftime("%H:%M"), nxt.strftime("%H:%M"))); cur = nxt
        if not slots: return await m.answer("❌ Минимум 1 час.")
        async with async_session() as s:
            if (await s.execute(select(Slot).where(Slot.date == data["slot_date"]))).first():
                return await m.answer("⚠️ Слоты на эту дату уже есть.")
            for st, et in slots: s.add(Slot(date=data["slot_date"], start_time=st, end_time=et, price=0.0, is_active=True, is_booked=False))
            await s.commit()
        await m.answer(f"✅ Создано {len(slots)} слотов на {data['slot_date']}")
        await state.clear()
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
        await m.answer("✅ Готово.", reply_markup=kb.as_markup())
    except Exception as e: await m.answer(f"⚠️ Ошибка: {e}")

# 🔹 3. СЛОТЫ НА МЕСЯЦ
@router.callback_query(F.data == "create_month", F.from_user.id.in_(ADMIN_IDS))
async def start_month(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🗓️ **Слоты на месяц**\nВведите месяц (`ММ.ГГГГ`):")
    await state.set_state(AdminFSM.month_month); await cb.answer()

@router.message(AdminFSM.month_month, F.from_user.id.in_(ADMIN_IDS))
async def month_month(m: Message, state: FSMContext):
    try:
        mo, y = map(int, m.text.strip().split("."))
        dt = datetime(y, mo, 1)
        await state.update_data(slot_month=dt.strftime("%Y-%m"))
        await m.answer("⏰ Время начала (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.month_start)
    except: await m.answer("⚠️ Формат: `05.2024`")

@router.message(AdminFSM.month_start, F.from_user.id.in_(ADMIN_IDS))
async def month_start(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0<=h<=23 and 0<=mi<=59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Время окончания (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.month_end)
    except: await m.answer("⚠️ Формат: `22:00`")

@router.message(AdminFSM.month_end, F.from_user.id.in_(ADMIN_IDS))
async def month_end(m: Message, state: FSMContext):
    try:
        he, me = map(int, m.text.strip().split(":"))
        data = await state.get_data()
        s_dt = datetime.strptime(data["slot_start"], "%H:%M")
        e_dt = datetime.strptime(f"{he:02d}:{me:02d}", "%H:%M")
        if (e_dt - s_dt).total_seconds() <= 3600: return await m.answer("❌ Минимум 1 час.")
        
        month_str = data["slot_month"]
        first = datetime.strptime(month_str + "-01", "%Y-%m-%d")
        days_count = calendar.monthrange(first.year, first.month)[1]
        created = 0
        async with async_session() as s:
            for i in range(days_count):
                day_dt = first + timedelta(days=i)
                day_str = day_dt.strftime("%Y-%m-%d")
                if (await s.execute(select(Slot).where(Slot.date == day_str))).first(): continue
                cur = s_dt
                while cur < e_dt:
                    nxt = cur + timedelta(hours=1)
                    if nxt > e_dt: break
                    s.add(Slot(date=day_str, start_time=cur.strftime("%H:%M"), end_time=nxt.strftime("%H:%M"), price=0.0, is_active=True, is_booked=False))
                    cur = nxt
                created += 1
            await s.commit()
        await m.answer(f"✅ Созданы слоты на {created} дней в {month_str.replace('-', '.')}")
        await state.clear()
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
        await m.answer("✅ Готово.", reply_markup=kb.as_markup())
    except Exception as e: await m.answer(f"⚠️ Ошибка: {e}")

# 🔹 4. СЛОТЫ ПЕРИОД
@router.callback_query(F.data == "create_period", F.from_user.id.in_(ADMIN_IDS))
async def start_period(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📅 **Слоты период**\nДата начала (`ДД.ММ.ГГГГ`):")
    await state.set_state(AdminFSM.period_start_date); await cb.answer()

@router.message(AdminFSM.period_start_date, F.from_user.id.in_(ADMIN_IDS))
async def period_start_date(m: Message, state: FSMContext):
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y")
        await state.update_data(period_start=dt.strftime("%Y-%m-%d"))
        await m.answer("📅 Дата окончания (`ДД.ММ.ГГГГ`):")
        await state.set_state(AdminFSM.period_end_date)
    except: await m.answer("⚠️ Формат: `25.12.2024`")

@router.message(AdminFSM.period_end_date, F.from_user.id.in_(ADMIN_IDS))
async def period_end_date(m: Message, state: FSMContext):
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y")
        await state.update_data(period_end=dt.strftime("%Y-%m-%d"))
        await m.answer("⏰ Время начала (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.period_start)
    except: await m.answer("⚠️ Формат: `25.12.2024`")

@router.message(AdminFSM.period_start, F.from_user.id.in_(ADMIN_IDS))
async def period_start(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0<=h<=23 and 0<=mi<=59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Время окончания (`ЧЧ:ММ`):")
        await state.set_state(AdminFSM.period_end)
    except: await m.answer("⚠️ Формат: `22:00`")

@router.message(AdminFSM.period_end, F.from_user.id.in_(ADMIN_IDS))
async def period_end(m: Message, state: FSMContext):
    try:
        he, me = map(int, m.text.strip().split(":"))
        data = await state.get_data()
        s_dt = datetime.strptime(data["slot_start"], "%H:%M")
        e_dt = datetime.strptime(f"{he:02d}:{me:02d}", "%H:%M")
        if (e_dt - s_dt).total_seconds() <= 3600: return await m.answer("❌ Минимум 1 час.")
        
        start_d = datetime.strptime(data["period_start"], "%Y-%m-%d")
        end_d = datetime.strptime(data["period_end"], "%Y-%m-%d")
        days = (end_d - start_d).days + 1
        if days <= 0: return await m.answer("❌ Конец раньше начала.")
        
        created = 0
        async with async_session() as s:
            for i in range(days):
                day_dt = start_d + timedelta(days=i)
                day_str = day_dt.strftime("%Y-%m-%d")
                if (await s.execute(select(Slot).where(Slot.date == day_str))).first(): continue
                cur = s_dt
                while cur < e_dt:
                    nxt = cur + timedelta(hours=1)
                    if nxt > e_dt: break
                    s.add(Slot(date=day_str, start_time=cur.strftime("%H:%M"), end_time=nxt.strftime("%H:%M"), price=0.0, is_active=True, is_booked=False))
                    cur = nxt
                created += 1
            await s.commit()
        await m.answer(f"✅ Созданы слоты на {created} дней в период")
        await state.clear()
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
        await m.answer("✅ Готово.", reply_markup=kb.as_markup())
    except Exception as e: await m.answer(f"⚠️ Ошибка: {e}")

# 📖 НОВОЕ ПОДМЕНЮ БРОНЕЙ
@router.callback_query(F.data == "admin_bookings_menu", F.from_user.id.in_(ADMIN_IDS))
async def admin_bookings_menu(cb: CallbackQuery):
    kb = InlineKeyboardBuilder()
    kb.row(InlineKeyboardButton(text="📖 Все брони", callback_data="admin_bookings_list"))
    kb.row(InlineKeyboardButton(text="🗓️ Брони по дате", callback_data="admin_bookings_by_date"))
    kb.row(InlineKeyboardButton(text="🔙 В главное меню", callback_data="admin_menu"))
    await _send_text(cb, "📖 **Управление бронями:**", kb.as_markup())
    await cb.answer()

@router.message(F.text == "/admin", F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(m: Message): await _show_admin_menu(m)

@router.callback_query(F.data == "admin_menu", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu_cb(cb: CallbackQuery): await _show_admin_menu(cb); await cb.answer()

# 📅 Список слотов
async def _show_slots(event):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date >= today).order_by(Slot.date, Slot.start_time))
        slots = res.scalars().all()[:50]
    if not slots:
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        return await _send_text(event, "📭 Нет будущих слотов.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for sl in slots:
        icon = "🔒" if sl.is_booked else ("✅" if sl.is_active else "❌")
        kb.button(text=f"{icon} {fmt_date(sl.date)} | {sl.start_time}-{sl.end_time}", callback_data=f"slot_manage:{sl.id}")
    kb.adjust(1); kb.button(text="🔙 В меню", callback_data="admin_menu")
    await _send_text(event, "📋 **Слоты (ближайшие 50):**", kb.as_markup())

@router.callback_query(F.data == "admin_slots_list", F.from_user.id.in_(ADMIN_IDS))
async def admin_slots_months(cb: CallbackQuery):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.date >= today).distinct().order_by(Slot.date))
        dates = [r[0] for r in res]
    if not dates:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
        return await _send_text(cb, "📭 Нет будущих слотов.", kb.as_markup())
    months = {}
    for d in dates: months.setdefault(d[:7], True)
    kb = InlineKeyboardBuilder()
    for ym in sorted(months.keys()):
        y, m = ym.split("-")
        kb.row(InlineKeyboardButton(text=f"{m}.{y}", callback_data=f"admin_slots_month:{ym}"))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
    await _send_text(cb, "📅 **Выберите месяц:**", kb.as_markup())
    await cb.answer()

# 📅 ШАГ 2: Дни
@router.callback_query(F.data.startswith("admin_slots_month:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_slots_days(cb: CallbackQuery):
    ym = cb.data.split(":")[1]
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.date >= datetime.now().date().strftime("%Y-%m-%d"), Slot.date.startswith(ym)).distinct().order_by(Slot.date))
        dates = [r[0] for r in res]
    if not dates:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 К месяцам", callback_data="admin_slots_list"))
        return await _send_text(cb, "📭 Нет дней.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for d in dates: kb.row(InlineKeyboardButton(text=fmt_date(d), callback_data=f"admin_slots_day:{d}"))
    kb.row(InlineKeyboardButton(text="🔙 К месяцам", callback_data="admin_slots_list"))
    await _send_text(cb, f"📅 **Дни в {ym.replace('-', '.')}:**", kb.as_markup())
    await cb.answer()

# 📋 ШАГ 3: Слоты на день (1 кнопка на строку)
@router.callback_query(F.data.startswith("admin_slots_day:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_slots_for_day(cb: CallbackQuery):
    date_str = cb.data.split(":")[1]
    async with async_session() as s:
        res = await s.execute(select(Slot).where(Slot.date == date_str).order_by(Slot.start_time))
        slots = res.scalars().all()
    if not slots:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 К дням", callback_data=f"admin_slots_month:{date_str[:7]}"))
        return await _send_text(cb, "📭 Нет слотов.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for sl in slots:
        icon = "🔒" if sl.is_booked else ("✅" if sl.is_active else "❌")
        kb.row(InlineKeyboardButton(text=f"{icon} {sl.start_time}-{sl.end_time}", callback_data=f"slot_manage:{sl.id}"))
    kb.row(InlineKeyboardButton(text="🔙 К дням", callback_data=f"admin_slots_month:{date_str[:7]}"))
    await _send_text(cb, f"📋 **Слоты на {fmt_date(date_str)}:**", kb.as_markup())
    await cb.answer()

# ➕ Создание слотов на день
@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def add_slot_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📅 **Создать слоты на целый день**\nВведите дату в формате `ДД.ММ`:", parse_mode="Markdown")
    await state.set_state(AdminFSM.add_date); await cb.answer()

# 🔧 Управление слотом
async def _manage_slot(event, sid: int):
    async with async_session() as s: sl = await s.get(Slot, sid)
    if not sl: return await event.answer("❌ Не найден", show_alert=True) if isinstance(event, CallbackQuery) else await event.answer("❌ Не найден")
    txt = f"📅 **Слот #{sl.id}**\n{fmt_date(sl.date)} | {sl.start_time}-{sl.end_time}\n{'✅' if sl.is_active else '❌'} | {'🔒' if sl.is_booked else '⏳'}\n💰 {int(sl.price)}₽"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="🔄 " + ("Выкл" if sl.is_active else "Вкл"), callback_data=f"slot_toggle:{sl.id}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"slot_delete:{sl.id}")
    ).button(text="🔙 Назад", callback_data="admin_slots_list")
    await _send_text(event, txt, kb.as_markup())

@router.callback_query(F.data.startswith("slot_manage:"), F.from_user.id.in_(ADMIN_IDS))
async def slot_manage_cb(cb: CallbackQuery): await _manage_slot(cb, int(cb.data.split(":")[1])); await cb.answer()
@router.callback_query(F.data.startswith("slot_toggle:"), F.from_user.id.in_(ADMIN_IDS))
async def slot_toggle_cb(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s: sl = await s.get(Slot, sid); sl.is_active = not sl.is_active; await s.commit()
    await _manage_slot(cb, sid); await cb.answer()
@router.callback_query(F.data.startswith("slot_delete:"), F.from_user.id.in_(ADMIN_IDS))
async def slot_delete_cb(cb: CallbackQuery):
    sid = int(cb.data.split(":")[1])
    async with async_session() as s:
        sl = await s.get(Slot, sid)
        if sl and not sl.is_booked: await s.delete(sl); await s.commit()
        else: return await cb.answer("⛔ Забронированный слот нельзя удалить", show_alert=True)
    await _show_slots(cb); await cb.answer()

# 📖 Список броней
async def _show_bookings(event):
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(30))
        bks = res.scalars().all()
    if not bks:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_bookings_menu"))
        return await _send_text(event, "📭 Броней нет.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for b in bks:
        em = {"confirmed":"🟢","confirmed_reminder":"🔵","cancelled":"🔴"}.get(b.status, "⚪")
        sids = json.loads(b.slot_ids)
        day_str = "Не указана"
        if sids:
            sl = (await s.execute(select(Slot).where(Slot.id == sids[0]))).scalar_one_or_none()
            if sl: day_str = fmt_date(sl.date)
        kb.row(InlineKeyboardButton(text=f"{em} #{b.id} | {day_str}", callback_data=f"adm_booking:{b.id}"))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_bookings_menu"))
    await _send_text(event, "📖 **Последние 30 броней:**", kb.as_markup())

@router.callback_query(F.data == "admin_bookings_list", F.from_user.id.in_(ADMIN_IDS))
async def bks_list_cb(cb: CallbackQuery): await _show_bookings(cb); await cb.answer()

# 🔍 Детали брони
async def _show_booking_detail(event, bid: int):
    async with async_session() as s:
        b = await s.get(Booking, bid)
        if not b: return await event.answer("❌ Не найдена", show_alert=True) if isinstance(event, CallbackQuery) else await event.answer("❌")
        u = (await s.execute(select(User).where(User.tg_id == b.user_tg_id))).scalar_one_or_none()
        sl_res = await s.execute(select(Slot).where(Slot.id.in_(json.loads(b.slot_ids))))
        sls = sl_res.scalars().all()
        svc = json.loads(b.services) if b.services else {}
    txt = (f"🆔 **#{b.id}** | `{b.status}`\n👤 {u.client_name if u else '?'} | 📞 `{u.phone if u else '?'}`\n"
           f"📅 {fmt_date(sls[0].date)} | ⏰ {' | '.join(f'{s.start_time}-{s.end_time}' for s in sls)}\n"
           f"📹 {svc.get('camera','?')} кам. | 🎬 {'Да' if svc.get('editing')=='yes' else 'Нет'}\n💵 {int(b.total_price)}₽")
    kb = InlineKeyboardBuilder()
    if b.status == "confirmed":
        kb.row(InlineKeyboardButton(text="✅ Подтв.", callback_data=f"adm_confirm:{b.id}"),
               InlineKeyboardButton(text="❌ Отмена", callback_data=f"adm_cancel:{b.id}"))
    kb.button(text="🔙 Назад", callback_data="admin_bookings_list")
    await _send_text(event, txt, kb.as_markup())

@router.callback_query(F.data.startswith("adm_booking:"), F.from_user.id.in_(ADMIN_IDS))
async def bks_detail_cb(cb: CallbackQuery): await _show_booking_detail(cb, int(cb.data.split(":")[1])); await cb.answer()

@router.callback_query(F.data.startswith("adm_confirm:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_confirm_cb(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s: b = await s.get(Booking, bid); b.status = "confirmed"; await s.commit()
    await cb.answer("✅"); await _show_booking_detail(cb, bid)

@router.callback_query(F.data.startswith("adm_cancel:"), F.from_user.id.in_(ADMIN_IDS))
async def adm_cancel_cb(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    async with async_session() as s:
        b = await s.get(Booking, bid); b.status = "cancelled"
        for sid in json.loads(b.slot_ids): sl = await s.get(Slot, sid); sl.is_booked = False
        await s.commit()
    await cb.answer("❌"); await _show_booking_detail(cb, bid)

# 🗓️ Брони по дате
async def _show_dates_with_bookings(event):
    today = datetime.now().date().strftime("%Y-%m-%d")
    async with async_session() as s:
        res = await s.execute(select(Slot.date).where(Slot.is_booked, Slot.date >= today).distinct().order_by(Slot.date.desc()))
        dates = [r[0] for r in res]
    if not dates:
        kb = InlineKeyboardBuilder().row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_bookings_menu"))
        return await _send_text(event, "📭 Нет броней.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for d in dates: kb.row(InlineKeyboardButton(text=fmt_date(d), callback_data=f"adm_bookings_date:{d}"))
    kb.row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_bookings_menu"))
    await _send_text(event, "🗓️ **Выберите дату:**", kb.as_markup())

@router.callback_query(F.data == "admin_bookings_by_date", F.from_user.id.in_(ADMIN_IDS))
async def dates_cb(cb: CallbackQuery): await _show_dates_with_bookings(cb); await cb.answer()

@router.callback_query(F.data.startswith("adm_bookings_date:"), F.from_user.id.in_(ADMIN_IDS))
async def date_bks_cb(cb: CallbackQuery):
    date_str = cb.data.split(":")[1]
    async with async_session() as s:
        res = await s.execute(select(Booking).order_by(Booking.created_at.desc()).limit(50))
        bks = [b for b in res.scalars().all() if any(sl.date == date_str for sl in (await s.execute(select(Slot).where(Slot.id.in_(json.loads(b.slot_ids))))).scalars().all())]
    if not bks: return await cb.answer("Нет броней", show_alert=True)
    kb = InlineKeyboardBuilder()
    for b in bks: kb.button(text=f"🟢 #{b.id} | {int(b.total_price)}₽", callback_data=f"adm_booking:{b.id}")
    kb.adjust(1); kb.button(text="⬅️ Назад", callback_data="admin_bookings_by_date")
    await _send_text(cb, f"📅 Брони на {fmt_date(date_str)}:", kb.as_markup())
    await cb.answer()

# 💰 РЕДАКТОР ЦЕН 
async def _show_prices(event):
    p = load_prices()
    txt = (f"💰 **Цены:**\n"
           f"📹 1 кам: {p['cam1']}₽\n"
           f"📹 2 кам: {p['cam2']}₽\n"
           f"📹 3 кам: {p['cam3']}₽\n"
           f"🏢 Без камер: {p['no_cam']}₽")
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="✏️ 1 кам.", callback_data="set_cam1"),
        InlineKeyboardButton(text="✏️ 2 кам.", callback_data="set_cam2"),
        InlineKeyboardButton(text="✏️ 3 кам.", callback_data="set_cam3")
    ).row(
        InlineKeyboardButton(text="✏️ Без камер", callback_data="set_no_cam")
    ).row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
    await _send_text(event, txt, kb.as_markup())
    
@router.callback_query(F.data == "admin_prices", F.from_user.id.in_(ADMIN_IDS))
async def prices_cb(cb: CallbackQuery): await _show_prices(cb); await cb.answer()

@router.callback_query(F.data.startswith("set_"), F.from_user.id.in_(ADMIN_IDS))
async def ask_price_cb(cb: CallbackQuery, state: FSMContext):
    
    key = cb.data.removeprefix("set_")
    
    await state.update_data(price_key=key)
    await state.set_state(AdminFSM.waiting_price_key)
    names = {"cam1": "1 камера", "cam2": "2 камеры", "cam3": "3 камеры", "no_cam": "Студия без камер"}
    await cb.message.edit_text(f"💸 Введите цену для `{names[key]}` (только число):")
    await cb.answer()
    
@router.message(AdminFSM.waiting_price_key, F.from_user.id.in_(ADMIN_IDS))
async def save_price_msg(m: Message, state: FSMContext):
    if not m.text.isdigit(): return await m.answer("⚠️ Только цифры.")
    data = await state.get_data(); p = load_prices()
    p[data["price_key"]] = int(m.text); save_prices(p); await state.clear()
    await m.answer(f"✅ Сохранено: {int(m.text)}₽")
    await _show_prices(m)

# 🔍 Поиск
@router.callback_query(F.data == "adm_search_phone", F.from_user.id.in_(ADMIN_IDS))
async def search_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("🔍 Введите номер (`+7999...`):")
    await state.set_state(AdminFSM.waiting_phone_search); await cb.answer()

@router.message(AdminFSM.waiting_phone_search, F.from_user.id.in_(ADMIN_IDS))
async def search_exec_msg(m: Message, state: FSMContext):
    phone = m.text.strip()
    async with async_session() as s:
        u = (await s.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if not u: return await m.answer("❌ Не найден."); await state.clear()
    async with async_session() as s: bks = (await s.execute(select(Booking).where(Booking.user_tg_id == u.tg_id).order_by(Booking.created_at.desc()).limit(5))).scalars().all()
    txt = f"👤 **{u.client_name or '?'}**\n📱 `{u.phone}`\n🆔 `{u.tg_id}`\n📊 Броней: {len(bks)}"
    await m.answer(txt, parse_mode="Markdown"); await state.clear()

# 📢 Рассылка
@router.callback_query(F.data == "admin_broadcast", F.from_user.id.in_(ADMIN_IDS))
async def broadcast_start_cb(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📢 Введите текст:")
    await state.set_state(AdminFSM.waiting_broadcast); await cb.answer()

@router.message(AdminFSM.waiting_broadcast, F.from_user.id.in_(ADMIN_IDS))
async def broadcast_exec_msg(m: Message, state: FSMContext):
    await m.answer("⏳ Рассылка...")
    async with async_session() as s: targets = [r[0] for r in (await s.execute(select(User.tg_id).distinct())).all()]
    sent = 0
    for tid in targets:
        try: await m.bot.send_message(tid, m.text); sent += 1; await asyncio.sleep(0.3)
        except: pass
    await m.answer(f"✅ Доставлено: {sent}/{len(targets)}")
    await state.clear()
    await _show_admin_menu(m)
