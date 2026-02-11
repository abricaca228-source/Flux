import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import text

DATABASE_URL = os.getenv("DATABASE_URL")

# Приводим URL к asyncpg-формату, если он задан без драйвера
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
    elif DATABASE_URL.startswith("postgresql://") and "+asyncpg" not in DATABASE_URL:
        DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
else:
    # Значение по умолчанию для локальной разработки
    DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/flux_db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


async def init_db():
    """
    Создает все нужные таблицы и аккуратно добавляет недостающие колонки,
    чтобы структура БД всегда соответствовала коду.
    """
    async with engine.begin() as conn:
        # USERS: базовое создание
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password TEXT NOT NULL,
                    avatar_url TEXT,
                    bio TEXT,
                    is_admin BOOLEAN DEFAULT FALSE,
                    wallpaper TEXT DEFAULT '',
                    real_name TEXT,
                    location TEXT,
                    birth_date TEXT,
                    social_link TEXT
                )
                """
            )
        )

        # USERS: гарантируем наличие всех используемых колонок
        alter_users_statements = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS wallpaper TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS real_name TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS location TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS birth_date TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS social_link TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS user_id TEXT UNIQUE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS two_factor_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS privacy_settings TEXT DEFAULT '{}'",
        ]
        for stmt in alter_users_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                # Если колонка уже есть или возникла другая не критичная ошибка — продолжаем
                pass

        # MESSAGES: базовое создание с полной схемой
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    content TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_edited BOOLEAN DEFAULT FALSE,
                    reactions TEXT DEFAULT '{}',
                    reply_to INTEGER DEFAULT NULL,
                    read_by TEXT DEFAULT '[]',
                    timer INTEGER DEFAULT 0,
                    viewed_at TEXT DEFAULT NULL
                )
                """
            )
        )

        # MESSAGES: на всякий случай добавляем отсутствующие колонки
        alter_messages_statements = [
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_edited BOOLEAN DEFAULT FALSE",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS reactions TEXT DEFAULT '{}'",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS reply_to INTEGER DEFAULT NULL",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS read_by TEXT DEFAULT '[]'",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS timer INTEGER DEFAULT 0",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS viewed_at TEXT DEFAULT NULL",
        ]
        for stmt in alter_messages_statements:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass

        # Остальные таблицы (DMS, FRIEND_REQUESTS, GROUPS, GROUP_MEMBERS)
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS dms (
                    id SERIAL PRIMARY KEY,
                    user1 TEXT NOT NULL,
                    user2 TEXT NOT NULL,
                    UNIQUE(user1, user2)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS friend_requests (
                    id SERIAL PRIMARY KEY,
                    sender TEXT NOT NULL,
                    receiver TEXT NOT NULL,
                    status TEXT NOT NULL,
                    UNIQUE(sender, receiver)
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    owner TEXT NOT NULL
                )
                """
            )
        )
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS group_members (
                    id SERIAL PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    UNIQUE(group_id, username)
                )
                """
            )
        )
        
        # PINNED MESSAGES: закреплённые сообщения (как в Discord/Telegram)
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS pinned_messages (
                    id SERIAL PRIMARY KEY,
                    message_id INTEGER NOT NULL,
                    channel TEXT NOT NULL,
                    pinned_by TEXT NOT NULL,
                    pinned_at TEXT NOT NULL
                )
                """
            )
        )
        
        # USER STATUS: статусы пользователей (online/offline/recently/away)
        alter_users_status = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'offline'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen TEXT DEFAULT NULL",
        ]
        for stmt in alter_users_status:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        
        # MESSAGES: дополнительные поля для новых функций
        alter_messages_new = [
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS mentions TEXT DEFAULT '[]'",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS forwarded_from TEXT DEFAULT NULL",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_pinned BOOLEAN DEFAULT FALSE",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS link_preview TEXT DEFAULT NULL",
        ]
        for stmt in alter_messages_new:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        
        # VOICE CHANNELS: голосовые каналы (как в Discord)
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS voice_channels (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    group_id INTEGER,
                    created_by TEXT NOT NULL
                )
                """
            )
        )
        
        # VOICE CHANNEL MEMBERS: кто сейчас в голосовом канале
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS voice_channel_members (
                    id SERIAL PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    joined_at TEXT NOT NULL,
                    UNIQUE(channel_id, username)
                )
                """
            )
        )
        
        # USER SETTINGS: настройки пользователя (тема, уведомления и т.д.)
        alter_users_settings = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS theme TEXT DEFAULT 'dark'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_status TEXT DEFAULT NULL",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notification_settings TEXT DEFAULT '{}'",
        ]
        for stmt in alter_users_settings:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        
        # GROUP ROLES: роли в группах (owner, admin, member)
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS group_roles (
                    id SERIAL PRIMARY KEY,
                    group_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    UNIQUE(group_id, username)
                )
                """
            )
        )
        
        # MESSAGE THEMES: цветные темы для сообщений
        alter_messages_themes = [
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS message_theme TEXT DEFAULT NULL",
            "ALTER TABLE messages ADD COLUMN IF NOT EXISTS edit_history TEXT DEFAULT '[]'",
        ]
        for stmt in alter_messages_themes:
            try:
                await conn.execute(text(stmt))
            except Exception:
                pass
        
        # USER ACTIVITY: статистика активности
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS user_activity (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    date TEXT NOT NULL,
                    messages_count INTEGER DEFAULT 0,
                    reactions_given INTEGER DEFAULT 0,
                    reactions_received INTEGER DEFAULT 0
                )
                """
            )
        )
        
        # STICKERS: система стикеров
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS stickers (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL,
                    pack_name TEXT NOT NULL,
                    sticker_data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    is_animated BOOLEAN DEFAULT FALSE
                )
                """
            )
        )
        
        # STICKER PACKS: наборы стикеров
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS sticker_packs (
                    id SERIAL PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    icon TEXT
                )
                """
            )
        )
        
        # USER STICKER PACKS: какие наборы стикеров есть у пользователя
        await conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS user_sticker_packs (
                    id SERIAL PRIMARY KEY,
                    username TEXT NOT NULL,
                    pack_id INTEGER NOT NULL,
                    added_at TEXT NOT NULL,
                    UNIQUE(username, pack_id)
                )
                """
            )
        )