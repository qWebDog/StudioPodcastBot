from datetime import datetime
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

RU_DAYS = {"Monday": "Пн", "Tuesday": "Вт", "Wednesday": "Ср", "Thursday": "Чт", "Friday": "Пт", "Saturday": "Сб", "Sunday": "Вс"}

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

def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📅 Забронировать запись", callback_data="book_start")
    ).row(
        InlineKeyboardButton(text="📋 Мои записи", callback_data="my_bookings"),
        InlineKeyboardButton(text="📞 Связаться с админом", callback_data="contact_admin")
    ).adjust(1).as_markup()

def dates_kb(dates: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in sorted(dates): kb.button(text=format_date_display(d), callback_data=f"book_date:{d}")
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
    kb.adjust(2)
    return kb.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().button(text="✅ Подтвердить бронь", callback_data="book_confirm").as_markup()

# Остальные админ-клавиатуры оставляем без изменений...
