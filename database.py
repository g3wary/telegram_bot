"""SQLAlchemy 2.0 async, одна таблица на сущность."""
from datetime import datetime
from sqlalchemy import BigInteger, String, Integer, DateTime, Boolean, select, func
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from config import DB_URL, ADMIN_ID

engine = create_async_engine(DB_URL, echo=False)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # telegram id
    username: Mapped[str] = mapped_column(String(64), default="")
    full_name: Mapped[str] = mapped_column(String(128), default="")
    purchases: Mapped[int] = mapped_column(Integer, default=0)
    tos_accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Admin(Base):
    __tablename__ = "admins"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    added_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    country: Mapped[str] = mapped_column(String(32))
    year: Mapped[int] = mapped_column(Integer)
    price: Mapped[int] = mapped_column(Integer)  # в рублях
    status: Mapped[str] = mapped_column(String(16), default="available")  # available / sold
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ─── Хелперы ───
async def get_or_create_user(session: AsyncSession, tg_user) -> User:
    u = await session.get(User, tg_user.id)
    if not u:
        u = User(
            id=tg_user.id,
            username=tg_user.username or "",
            full_name=tg_user.full_name or "",
        )
        session.add(u)
        await session.commit()
    return u


async def is_admin(tg_id: int) -> bool:
    if tg_id == ADMIN_ID:
        return True
    async with SessionLocal() as s:
        r = await s.get(Admin, tg_id)
        return r is not None


async def add_admin(tg_id: int, username: str = ""):
    async with SessionLocal() as s:
        if not await s.get(Admin, tg_id):
            s.add(Admin(id=tg_id, username=username))
            await s.commit()


async def remove_admin(tg_id: int):
    async with SessionLocal() as s:
        a = await s.get(Admin, tg_id)
        if a:
            await s.delete(a)
            await s.commit()


async def list_admins() -> list[Admin]:
    async with SessionLocal() as s:
        r = await s.execute(select(Admin).order_by(Admin.added_at))
        return list(r.scalars())


async def add_account(phone: str, country: str, year: int, price: int):
    async with SessionLocal() as s:
        if await s.get(Account, phone) is None:
            s.add(Account(phone=phone, country=country, year=year, price=price))
            await s.commit()


async def countries() -> list[str]:
    async with SessionLocal() as s:
        r = await s.execute(
            select(Account.country)
            .where(Account.status == "available")
            .distinct()
            .order_by(Account.country)
        )
        return [row[0] for row in r.all()]


async def years(country: str) -> list[int]:
    async with SessionLocal() as s:
        r = await s.execute(
            select(Account.year)
            .where(Account.status == "available", Account.country == country)
            .distinct()
            .order_by(Account.year.desc())
        )
        return [row[0] for row in r.all()]


async def pick_account(country: str, year: int) -> Account | None:
    async with SessionLocal() as s:
        r = await s.execute(
            select(Account)
            .where(
                Account.status == "available",
                Account.country == country,
                Account.year == year,
            )
            .limit(1)
        )
        return r.scalar_one_or_none()


async def mark_sold(account_id: int):
    async with SessionLocal() as s:
        a = await s.get(Account, account_id)
        if a:
            # удаляем из каталога, чтобы не продалось дважды
            await s.delete(a)
            await s.commit()


async def inc_purchases(user_id: int):
    async with SessionLocal() as s:
        u = await s.get(User, user_id)
        if u:
            u.purchases += 1
            await s.commit()


async def stats() -> dict:
    async with SessionLocal() as s:
        u = (await s.execute(select(func.count(User.id)))).scalar()
        # всего продано = (все аккаунты) - (доступные)
        total_acc = (await s.execute(select(func.count(Account.id)))).scalar()
        avail = (await s.execute(
            select(func.count(Account.id)).where(Account.status == "available")
        )).scalar()
    return {
        "users": u or 0,
        "sold": (total_acc or 0) - (avail or 0),
        "available": avail or 0,
          }
