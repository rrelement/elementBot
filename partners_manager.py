"""
Модуль для управления партнерами (битмейкеры и звукоинженеры).
Хранит данные в SQLite базе данных.
"""
import aiosqlite
import logging
from datetime import datetime
from typing import Dict, List, Optional
from database import get_db

async def add_partner(user_id: int, username: str, partner_type: str = "partner", name: str = None) -> bool:
    """
    Добавляет партнера.
    
    Args:
        user_id: Telegram ID партнера
        username: Username партнера
        partner_type: Тип партнера (по умолчанию "partner" - универсальный)
        name: Имя партнера (опционально)
    
    Returns:
        True если успешно добавлен, False если уже существует
    """
    db = await get_db()
    try:
        # Проверяем, не существует ли уже партнер
        cursor = await db.execute(
            "SELECT user_id FROM partners WHERE user_id = ?",
            (user_id,)
        )
        if await cursor.fetchone():
            return False
        
        # Добавляем партнера
        await db.execute("""
            INSERT INTO partners (user_id, username, name, type, active, orders_accepted, orders_completed)
            VALUES (?, ?, ?, ?, 1, 0, 0)
        """, (user_id, username, name or username, partner_type))
        await db.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка при добавлении партнера: {e}")
        await db.rollback()
        raise
    finally:
        await db.close()

async def remove_partner(user_id: int) -> bool:
    """Удаляет партнера."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM partners WHERE user_id = ?", (user_id,))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def get_partner(user_id: int) -> Optional[Dict]:
    """Получает информацию о партнере по ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM partners WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            d = dict(row)
            d["active"] = bool(d.get("active", 0))
            return d
        return None
    finally:
        await db.close()

async def get_active_partners(partner_type: str = None) -> List[Dict]:
    """
    Получает список активных партнеров.
    
    Args:
        partner_type: Тип партнера (не используется, оставлено для обратной совместимости)
    
    Returns:
        Список активных партнеров (все партнеры могут принимать оба типа заказов)
    """
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM partners WHERE active = 1"
        )
        rows = await cursor.fetchall()
        result = []
        for row in rows:
            d = dict(row)
            d["active"] = bool(d.get("active", 0))
            result.append(d)
        return result
    finally:
        await db.close()

async def set_partner_active(user_id: int, active: bool) -> bool:
    """Активирует/деактивирует партнера."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "UPDATE partners SET active = ? WHERE user_id = ?",
            (1 if active else 0, user_id)
        )
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()

async def increment_partner_orders(user_id: int, order_type: str = "accepted") -> bool:
    """Увеличивает счетчик заказов партнера."""
    db = await get_db()
    try:
        if order_type == "accepted":
            await db.execute(
                "UPDATE partners SET orders_accepted = orders_accepted + 1 WHERE user_id = ?",
                (user_id,)
            )
        elif order_type == "completed":
            await db.execute(
                "UPDATE partners SET orders_completed = orders_completed + 1 WHERE user_id = ?",
                (user_id,)
            )
        await db.commit()
        return True
    finally:
        await db.close()

# ========== Система регистрации партнеров ==========

async def create_partner_request(user_id: int, username: str, partner_type: str = "partner", name: str = None, message: str = None) -> bool:
    """
    Создает заявку на регистрацию партнера.
    
    Args:
        user_id: Telegram ID пользователя
        username: Username пользователя
        partner_type: Тип партнера (по умолчанию "partner" - универсальный)
        name: Имя партнера (опционально)
        message: Дополнительное сообщение от пользователя (опционально)
    
    Returns:
        True если заявка создана, False если уже существует
    """
    db = await get_db()
    try:
        # Проверяем, не существует ли уже активная заявка (в рамках того же соединения)
        cursor = await db.execute("""
            SELECT user_id FROM partner_requests 
            WHERE user_id = ? AND status = 'pending'
        """, (user_id,))
        if await cursor.fetchone():
            return False
        
        # Проверяем, не является ли уже партнером (в рамках того же соединения)
        cursor = await db.execute(
            "SELECT user_id FROM partners WHERE user_id = ?",
            (user_id,)
        )
        if await cursor.fetchone():
            return False
        
        # Создаем заявку
        created_at = datetime.now().isoformat()
        await db.execute("""
            INSERT INTO partner_requests (user_id, username, name, type, message, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, (user_id, username, name or username, partner_type, message or "", created_at))
        await db.commit()
        return True
    finally:
        await db.close()

async def get_partner_request(user_id: int) -> Optional[Dict]:
    """Получает заявку партнера по ID."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM partner_requests 
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None
    finally:
        await db.close()

async def get_pending_requests() -> List[Dict]:
    """Получает список всех ожидающих заявок."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM partner_requests 
            WHERE status = 'pending'
            ORDER BY created_at DESC
        """)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]
    finally:
        await db.close()

async def approve_partner_request(user_id: int, admin_id: int) -> bool:
    """
    Одобряет заявку на регистрацию партнера.
    
    Returns:
        True если успешно одобрено, False если заявка не найдена
    """
    db = await get_db()
    try:
        # Находим заявку
        cursor = await db.execute("""
            SELECT * FROM partner_requests 
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if not row:
            return False
        
        request = dict(row)
        
        # Проверяем, не существует ли уже партнер (в рамках того же соединения)
        cursor = await db.execute(
            "SELECT user_id FROM partners WHERE user_id = ?",
            (request["user_id"],)
        )
        if await cursor.fetchone():
            # Партнер уже существует, просто одобряем заявку
            reviewed_at = datetime.now().isoformat()
            await db.execute("""
                UPDATE partner_requests 
                SET status = 'approved', reviewed_at = ?, reviewed_by = ?
                WHERE user_id = ? AND created_at = ?
            """, (reviewed_at, admin_id, user_id, request["created_at"]))
            await db.commit()
            return True
        
        # Одобряем заявку
        reviewed_at = datetime.now().isoformat()
        await db.execute("""
            UPDATE partner_requests 
            SET status = 'approved', reviewed_at = ?, reviewed_by = ?
            WHERE user_id = ? AND created_at = ?
        """, (reviewed_at, admin_id, user_id, request["created_at"]))
        
        # Добавляем партнера в рамках того же соединения
        await db.execute("""
            INSERT INTO partners (user_id, username, name, type, active, orders_accepted, orders_completed)
            VALUES (?, ?, ?, ?, 1, 0, 0)
        """, (request["user_id"], request["username"], request["name"] or request["username"], request["type"]))
        
        await db.commit()
        return True
    finally:
        await db.close()

async def reject_partner_request(user_id: int, admin_id: int) -> bool:
    """
    Отклоняет заявку на регистрацию партнера.
    
    Returns:
        True если успешно отклонено, False если заявка не найдена
    """
    db = await get_db()
    try:
        # Находим заявку
        cursor = await db.execute("""
            SELECT * FROM partner_requests 
            WHERE user_id = ? AND status = 'pending'
            ORDER BY created_at DESC
            LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if not row:
            return False
        
        request = dict(row)
        
        # Отклоняем заявку
        reviewed_at = datetime.now().isoformat()
        cursor = await db.execute("""
            UPDATE partner_requests 
            SET status = 'rejected', reviewed_at = ?, reviewed_by = ?
            WHERE user_id = ? AND created_at = ?
        """, (reviewed_at, admin_id, user_id, request["created_at"]))
        await db.commit()
        return cursor.rowcount > 0
    finally:
        await db.close()
