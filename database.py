import json
import re
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, func, select

Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tg_id = Column(Integer, unique=True, nullable=False)
    username = Column(String)
    client_name = Column(String)  # 🆕 Явное имя пользователя
    phone = Column(String)
    created_at = Column(DateTime, server_default=func.now())

class Service(Base):
    __tablename__ = "services"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False)
    price = Column(Float, default=0.0)
    is_active = Column(Boolean, default=True)

class Slot(Base):
    __tablename__ = "slots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(String, nullable=False)
    start_time = Column(String, nullable=False)
    end_time = Column(String, nullable=False)
    price = Column(Float, default=0.0)
    is_booked = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)

class Booking(Base):
    __tablename__ = "bookings"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_tg_id = Column(Integer, nullable=False)
    slot_ids = Column(String, nullable=False)  # 🆕 JSON список ID часов
    services = Column(String, nullable=True)
    total_price = Column(Float, default=0.0)
    status = Column(String, default="confirmed")
    reminder_sent = Column(Boolean, default=False)
    created_at = Column(DateTime, server_default=func.now())

DB_URL = "sqlite+aiosqlite:///./studio.db"
engine = create_async_engine(DB_URL, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_user(tg_id: int) -> User | None:
    async with async_session() as s:
        return (await s.execute(select(User).where(User.tg_id == tg_id))).scalar_one_or_none()

def validate_phone(phone: str) -> bool:
    """Принимает строго формат +7XXXXXXXXXX (ровно 12 символов)"""
    return bool(re.fullmatch(r'\+7\d{10}', phone.strip()))

async def get_booking_details(booking_id: int):
    async with async_session() as s:
        b = await s.get(Booking, booking_id)
        if not b: return None, [], None
        slot_ids = json.loads(b.slot_ids)
        slots = []
        for sid in slot_ids:
            sl = await s.get(Slot, sid)
            if sl: slots.append(sl)
        user = await get_user(b.user_tg_id)
        return b, slots, user
