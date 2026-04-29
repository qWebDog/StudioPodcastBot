from datetime import datetime
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RU_DAYS = {"Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср", "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Вс"}
MONTH_NAMES = {
    "01": "Январь", "02": "Февраль", "03": "Март", "04": "Апрель",
    "05": "Май", "06": "Июнь", "07": "Июль", "08": "Август",
    "09": "Сентябрь", "10": "Октябрь", "11": "Ноябрь", "12": "Декабрь"
}

def format_date_display(date_str: str) -> str:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    ru_day = RU_DAYS.get(dt.strftime("%A"), dt.strftime("%A")[:2])
    return f"{dt.day:02d}.{dt.month:02d} {ru_day}"

def parse_admin_date(date_input: str) -> str:
    try: day, month = map(int, date_input.replace(" ", "").split("-"))
    except ValueError: raise ValueError("Неверный формат")
    year = datetime.now().year
    dt = datetime(year, month, day)
    if dt.date() < datetime.now().date(): year += 1; dt = datetime(year, month, day)
    return dt.strftime("%Y-%m-%d")

# 📱 Клиентское меню (одно сообщение)
def client_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="💰 Прайс", callback_data="view_price")
    ).row(
        InlineKeyboardButton(text="📅 Забронировать", callback_data="book_start")
    ).row(
        InlineKeyboardButton(text="📋 Мои записи", callback_data="view_bookings"),
        InlineKeyboardButton(text="📞 Связаться с админом", callback_data="view_contact")
    ).adjust(1).as_markup()

def back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().button(text="⬅️ В главное меню", callback_data="view_main").as_markup()

def back_cancel_kb(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬅️ Назад", callback_data=back_cb),
        InlineKeyboardButton(text="❌ Отмена", callback_data="book_cancel")
    ]])

# 📅 Навигация по датам
def months_kb(year_months: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for ym in sorted(year_months):
        year, month = ym.split("-")
        kb.button(text=f"{MONTH_NAMES[month]} {year}", callback_data=f"book_month:{ym}")
    kb.adjust(1)
    return kb.as_markup()

def dates_kb(dates: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in sorted(dates): kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
    kb.button(text="⬅️ Назад к месяцам", callback_data="back_to_months")
    kb.adjust(1)
    return kb.as_markup()

def time_slots_kb(slots: list, selected_ids: list[int]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in slots:
        is_sel = s.id in selected_ids
        kb.button(text=f"{'✅ ' if is_sel else '⏳ '}{s.start_time}-{s.end_time}", callback_data=f"slot_toggle:{s.id}")
    kb.button(text="📝 Далее", callback_data="slots_done")
    kb.adjust(2)
    return kb.as_markup()

def services_kb(svcs: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in svcs: kb.button(text=f"{s.name} ({int(s.price)}₽)", callback_data=f"book_svc:{s.id}")
    kb.button(text="✅ Завершить выбор", callback_data="book_svcs_done")
    kb.adjust(1)
    return kb.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().button(text="✅ Подтвердить бронь", callback_data="book_confirm").as_markup()

# 🛡 Админ-клавиатуры
def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📋 Все слоты", callback_data="admin_slots_list"),
        InlineKeyboardButton(text="📖 Все брони", callback_data="admin_bookings_list")
    ).row(
        InlineKeyboardButton(text="➕ Создать слот", callback_data="admin_add_slot"),
        InlineKeyboardButton(text="🔄 Автопродление", callback_data="admin_auto_extend")
    ).row(
        InlineKeyboardButton(text="💰 Услуги", callback_data="admin_services"),
        InlineKeyboardButton(text="🗓️ Брони по дате", callback_data="admin_bookings_by_date")
    ).row(
        InlineKeyboardButton(text="📱 Поиск по тел.", callback_data="adm_search_phone"),
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
    ).adjust(2).as_markup()

def slot_list_kb(slots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in slots:
        icon = "🔒" if s.is_booked else "⏳"
        kb.button(text=f"{icon} {s.date} | {s.start_time}-{s.end_time} | 💰{int(s.price)}₽", callback_data=f"slot_manage:{s.id}")
    kb.adjust(1)
    return kb.as_markup()

def slot_action_kb(slot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="❌ Отменить", callback_data=f"slot_cancel:{slot_id}"),
        InlineKeyboardButton(text="💰 Цена", callback_data=f"slot_edit_price:{slot_id}")
    ).row(
        InlineKeyboardButton(text="🔄 Перенести", callback_data=f"slot_move:{slot_id}")
    ).button(text="🔙 Назад", callback_data="admin_slots_list").adjust(1).as_markup()

def booking_action_kb(booking_id: int, status: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if status == "confirmed":
        kb.button(text="✅ Подтв.", callback_data=f"adm_confirm:{booking_id}")
        kb.button(text="❌ Отмена", callback_data=f"adm_cancel:{booking_id}")
    kb.button(text="🔙 Назад", callback_data="admin_bookings_list")
    return kb.adjust(2).as_markup()

def dates_with_bookings_kb(dates: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in sorted(dates):
        kb.button(text=format_date_display(d), callback_data=f"adm_bookings_date:{d}")
    kb.adjust(1)
    return kb.as_markup()
