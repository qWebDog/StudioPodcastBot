from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

def welcome_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().button(text="📅 Забронировать запись", callback_data="book_start").button(
        text="📋 Мои записи", callback_data="my_bookings"
    ).adjust(1).as_markup()

def admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().row(
        InlineKeyboardButton(text="📋 Все слоты", callback_data="admin_slots_list"),
        InlineKeyboardButton(text="📖 Все брони", callback_data="admin_bookings_list")
    ).row(
        InlineKeyboardButton(text="➕ Создать слот", callback_data="admin_add_slot"),
        InlineKeyboardButton(text="💰 Услуги", callback_data="admin_services")
    ).row(
        InlineKeyboardButton(text="📅 Фильтр по дате", callback_data="adm_filter_date"),
        InlineKeyboardButton(text="📱 Поиск по тел.", callback_data="adm_search_phone")
    ).row(
        InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
    ).adjust(2).as_markup()

def dates_kb(dates: list[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for d in sorted(dates): kb.button(text=d, callback_data=f"book_date:{d}")
    return kb.as_markup()

def slots_kb(slots: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in slots:
        kb.button(text=f"⏰ {s.start_time}-{s.end_time} | 💰 {int(s.price)}₽", callback_data=f"book_time:{s.id}")
    return kb.as_markup()

def services_kb(svcs: list) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for s in svcs: kb.button(text=f"{s.name} ({int(s.price)}₽)", callback_data=f"book_svc:{s.id}")
    kb.button(text="✅ Завершить выбор", callback_data="book_svcs_done")
    kb.adjust(2)
    return kb.as_markup()

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardBuilder().button(text="✅ Подтвердить бронь", callback_data="book_confirm").as_markup()

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
