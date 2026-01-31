"""
Модуль для работы с базой данных SQLite.
Инициализирует БД и создает все необходимые таблицы.
"""
import aiosqlite
import logging
from typing import Optional

DB_FILE = "bot_database.db"

async def get_db() -> aiosqlite.Connection:
    """Получает соединение с БД."""
    db = await aiosqlite.connect(DB_FILE)
    db.row_factory = aiosqlite.Row  # Для доступа к колонкам по имени
    return db

async def init_db():
    """Инициализирует БД и создает все таблицы."""
    db = await get_db()
    try:
        # Таблица заказов (custom_beat и mixing)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                type TEXT NOT NULL,  -- 'custom_beat' или 'mixing'
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                description TEXT,
                file_id TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                price TEXT,
                partner_price TEXT,
                client_price TEXT,
                first_payment INTEGER DEFAULT 0,  -- 0 или 1 (boolean)
                second_payment INTEGER DEFAULT 0,  -- 0 или 1 (boolean)
                created_at TEXT NOT NULL,
                accepted_at TEXT,
                completed_at TEXT,
                rejected_at TEXT,
                cancelled_at TEXT,
                client_message_id INTEGER,
                partner_id INTEGER,
                partner_username TEXT,
                payment_logs TEXT,  -- JSON строка
                accept_lock TEXT,
                partner_message_ids TEXT,  -- JSON строка: {"partner_id": message_id, ...}
                UNIQUE(type, id)
            )
        """)
        
        # Индексы для быстрого поиска
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_user_id ON orders(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_partner_id ON orders(partner_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_type_id ON orders(type, id)")
        
        # Добавляем поле partner_message_ids, если его нет (миграция для существующих БД)
        try:
            await db.execute("ALTER TABLE orders ADD COLUMN partner_message_ids TEXT")
            logging.info("Добавлено поле partner_message_ids в таблицу orders")
        except Exception as e:
            # Поле уже существует, игнорируем ошибку
            if "duplicate column" not in str(e).lower():
                logging.debug(f"Поле partner_message_ids уже существует или другая ошибка: {e}")
        
        # Таблица покупок готовых битов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS beats_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                beat TEXT NOT NULL,
                license TEXT NOT NULL,
                price TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending_payment',
                created_at TEXT NOT NULL,
                payment_received_at TEXT,
                file_sent_at TEXT,
                client_message_id INTEGER,
                waiting_card_details INTEGER DEFAULT 0,
                card_details_sent INTEGER DEFAULT 0
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_purchases_user_id ON beats_purchases(user_id)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_purchases_status ON beats_purchases(status)")
        
        # Таблица партнеров
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partners (
                user_id INTEGER PRIMARY KEY,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'partner',
                active INTEGER NOT NULL DEFAULT 1,  -- 0 или 1 (boolean)
                orders_accepted INTEGER NOT NULL DEFAULT 0,
                orders_completed INTEGER NOT NULL DEFAULT 0
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_partners_active ON partners(active)")
        
        # Таблица заявок на регистрацию партнеров
        await db.execute("""
            CREATE TABLE IF NOT EXISTS partner_requests (
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'partner',
                message TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL,
                reviewed_at TEXT,
                reviewed_by INTEGER,
                PRIMARY KEY (user_id, created_at)
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_requests_status ON partner_requests(status)")
        
        # Таблица языков пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_languages (
                user_id INTEGER PRIMARY KEY,
                language TEXT NOT NULL DEFAULT 'ru',
                updated_at TEXT NOT NULL
            )
        """)
        
        await db.execute("CREATE INDEX IF NOT EXISTS idx_user_languages_user_id ON user_languages(user_id)")
        
        await db.commit()
        logging.info("База данных инициализирована успешно")
    except Exception as e:
        logging.error(f"Ошибка инициализации БД: {e}")
        await db.rollback()
        raise
    finally:
        await db.close()

async def close_db(db: Optional[aiosqlite.Connection]):
    """Закрывает соединение с БД."""
    if db:
        await db.close()

