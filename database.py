import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import text

DATABASE_URL = os.getenv("DATABASE_URL")

if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://"):
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/flux_db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def init_db():
    async with engine.begin() as conn:
        # Создаем таблицу, если её нет
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                avatar_url TEXT,
                bio TEXT,
                is_admin BOOLEAN DEFAULT FALSE,
                real_name TEXT,
                location TEXT,
                birth_date TEXT,
                social_link TEXT
            )
        """))
        
        # --- МИГРАЦИЯ: ДОБАВЛЯЕМ НОВЫЕ КОЛОНКИ В СУЩЕСТВУЮЩУЮ БАЗУ ---
        # Сервер попытается добавить эти колонки. Если они есть — ничего страшного.
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE"))
        except: pass
        
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS real_name TEXT"))
        except: pass

        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS location TEXT"))
        except: pass

        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS birth_date TEXT"))
        except: pass
        
        try: await conn.execute(text("ALTER TABLE users ADD COLUMN IF NOT EXISTS social_link TEXT"))
        except: pass

        # Остальные таблицы
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
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS groups (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                owner TEXT NOT NULL
            )
        """))
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS group_members (
                id SERIAL PRIMARY KEY,
                group_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                UNIQUE(group_id, username)
            )
        """))