import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import text

# --- УМНАЯ НАСТРОЙКА ---
# Если мы в облаке (Render), берем адрес оттуда.
# Если мы дома, используем локальный адрес.
DATABASE_URL = os.getenv("DATABASE_URL")

# Исправление для Render (там адрес начинается с postgres://, а нам нужно postgresql://)
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif not DATABASE_URL:
    # Запасной вариант для твоего компьютера
    DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/flux_db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def init_db():
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                avatar_url TEXT,
                bio TEXT
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                username TEXT NOT NULL,
                content TEXT NOT NULL,
                channel TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS dms (
                id SERIAL PRIMARY KEY,
                user1 TEXT NOT NULL,
                user2 TEXT NOT NULL,
                UNIQUE(user1, user2)
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS friend_requests (
                id SERIAL PRIMARY KEY,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                status TEXT NOT NULL, 
                UNIQUE(sender, receiver)
            )
        """))