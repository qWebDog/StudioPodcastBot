import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
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

# 🛠 Утилиты
def _get_msg(event):
    """Безопасно получает объект Message из CallbackQuery или Message"""
    return event.message if isinstance(event, CallbackQuery) else event

def load_prices():
    defaults = {"rental": 2000, "cam1": 3000, "cam2": 3500, "cam3": 4000, "editing": 5000}
    try:
        with open(PRICES_FILE, "r") as f: return {**defaults, **json.load(f)}
    except: return defaults

def save_prices(prices: dict):
    with open(PRICES_FILE, "w") as f: json.dump(prices, f)

def fmt_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt.day:02d}.{dt.month:02d} {['Пн','Вт','Ср','Чт','Пт','Сб','Вс'][dt.weekday()]}"
    except: return d

async def _send_text(event, text: str, kb: InlineKeyboardMarkup = None):
    msg = _get_msg(event)
    try: await msg.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except: await msg.answer(text, reply_markup=kb, parse_mode="Markdown")

# 🛠 Главное меню
@router.message(F.text == "/admin", F.from_user.id.in_(ADMIN_IDS))
async def cmd_admin(m: Message): await _show_admin_menu(m)

@router.callback_query(F.data == "admin_menu", F.from_user.id.in_(ADMIN_IDS))
async def admin_menu_cb(cb: CallbackQuery): await _show_admin_menu(cb); await cb.answer()

async def _show_admin_menu(event):
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
    await _send_text(event, "🛠️ **Панель администратора:**", kb.as_markup())

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
async def admin_slots_cb(cb: CallbackQuery): await _show_slots(cb); await cb.answer()

# ➕ Создание слотов на день
@router.callback_query(F.data == "admin_add_slot", F.from_user.id.in_(ADMIN_IDS))
async def add_slot_start(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text("📅 **Создать слоты на целый день**\nВведите дату в формате `ДД.ММ`:", parse_mode="Markdown")
    await state.set_state(AdminFSM.add_date)
    await cb.answer()

@router.message(AdminFSM.add_date, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_date(m: Message, state: FSMContext):
    try:
        d, mo = map(int, m.text.strip().split("."))
        y = datetime.now().year
        dt = datetime(y, mo, d)
        if dt.date() < datetime.now().date(): dt = dt.replace(year=y+1)
        await state.update_data(slot_date=dt.strftime("%Y-%m-%d"))
        await m.answer("⏰ Время начала (напр. `10:00`):")
        await state.set_state(AdminFSM.add_start)
    except: await m.answer("⚠️ Формат: `25.12`")

@router.message(AdminFSM.add_start, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_start_time(m: Message, state: FSMContext):
    try:
        h, mi = map(int, m.text.strip().split(":"))
        if not (0 <= h <= 23 and 0 <= mi <= 59): raise ValueError
        await state.update_data(slot_start=f"{h:02d}:{mi:02d}")
        await m.answer("⏰ Время окончания (напр. `22:00`):")
        await state.set_state(AdminFSM.add_end)
    except: await m.answer("⚠️ Формат: `22:00`")

@router.message(AdminFSM.add_end, F.from_user.id.in_(ADMIN_IDS))
async def add_slot_end_time(m: Message, state: FSMContext):
    try:
        he, me = map(int, m.text.strip().split(":"))
        if not (0 <= he <= 23 and 0 <= me <= 59): raise ValueError
        data = await state.get_data()
        s_dt = datetime.strptime(data["slot_start"], "%H:%M")
        e_dt = datetime.strptime(f"{he:02d}:{me:02d}", "%H:%M")
        
        slots = []
        cur = s_dt
        while cur < e_dt:
            nxt = cur + timedelta(hours=1)
            if nxt > e_dt: break
            slots.append((cur.strftime("%H:%M"), nxt.strftime("%H:%M"))); cur = nxt
            
        if not slots: return await m.answer("❌ Разница должна быть ≥ 1 часа.")
        
        async with async_session() as s:
            if (await s.execute(select(Slot).where(Slot.date == data["slot_date"]))).first():
                return await m.answer("⚠️ Слоты на эту дату уже есть.")
            for st, et in slots:
                s.add(Slot(date=data["slot_date"], start_time=st, end_time=et, price=0.0, is_active=True, is_booked=False))
            await s.commit()
        await m.answer(f"✅ Создано {len(slots)} слотов на {data['slot_date']}\n⏰ {data['slot_start']} – {he:02d}:{me:02d} | 💰 0₽")
        await state.clear()
        await _show_slots(m)
    except Exception as e: await m.answer(f"⚠️ Ошибка: {e}")

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
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        return await _send_text(event, "📭 Броней нет.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for b in bks:
        em = {"confirmed":"🟢","confirmed_reminder":"🔵","cancelled":"🔴"}.get(b.status, "⚪")
        kb.button(text=f"{em} #{b.id} | {int(b.total_price)}₽", callback_data=f"adm_booking:{b.id}")
    kb.adjust(1); kb.button(text="🔙 В меню", callback_data="admin_menu")
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
        kb = InlineKeyboardBuilder().button(text="🔙 В меню", callback_data="admin_menu")
        return await _send_text(event, "📭 Нет броней.", kb.as_markup())
    kb = InlineKeyboardBuilder()
    for d in dates: kb.button(text=fmt_date(d), callback_data=f"adm_bookings_date:{d}")
    kb.adjust(1); kb.button(text="🔙 В меню", callback_data="admin_menu")
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

# 💰 Цены
async def _show_prices(event):
    p = load_prices()
    txt = f"💰 **Цены:**\n🎙️ Аренда: {p['rental']}₽\n📹 1 кам: {p['cam1']}₽\n📹 2 кам: {p['cam2']}₽\n📹 3 кам: {p['cam3']}₽\n🎬 Монтаж: {p['editing']}₽"
    kb = InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="✏️ Аренда", callback_data="set_rental"),
        InlineKeyboardButton(text="✏️ 1 кам.", callback_data="set_cam1"),
        InlineKeyboardButton(text="✏️ 2 кам.", callback_data="set_cam2")
    ).row(
        InlineKeyboardButton(text="✏️ 3 кам.", callback_data="set_cam3"),
        InlineKeyboardButton(text="✏️ Монтаж", callback_data="set_editing")
    ).row(InlineKeyboardButton(text="🔙 В меню", callback_data="admin_menu"))
    await _send_text(event, txt, kb.as_markup())

@router.callback_query(F.data == "admin_prices", F.from_user.id.in_(ADMIN_IDS))
async def prices_cb(cb: CallbackQuery): await _show_prices(cb); await cb.answer()

@router.callback_query(F.data.startswith("set_"), F.from_user.id.in_(ADMIN_IDS))
async def ask_price_cb(cb: CallbackQuery, state: FSMContext):
    await state.update_data(price_key=cb.data.split("_")[1])
    await state.set_state(AdminFSM.waiting_price_key)
    await cb.message.edit_text(f"💸 Введите цену (только число):")
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
