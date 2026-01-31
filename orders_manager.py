"""
Модуль для управления заказами.
Хранит заказы в SQLite базе данных.
"""
import json
import aiosqlite
from datetime import datetime
from typing import Dict, List, Optional
from database import get_db

def format_order_number(order_id: int, order_type: str, created_at: str = None) -> str:
    """
    Форматирует номер заказа в красивый формат.
    Формат: CB-001 (Custom Beat), MX-001 (Mixing)
    
    Args:
        order_id: ID заказа из БД
        order_type: Тип заказа ('custom_beat' или 'mixing')
        created_at: Дата создания в формате ISO (опционально, не используется)
    
    Returns:
        Отформатированный номер заказа
    """
    # Префикс по типу заказа
    prefix = "CB" if order_type == "custom_beat" else "MX"
    
    # Номер заказа (используем последние 3 цифры ID для краткости, или полный ID если < 1000)
    order_num = f"{order_id:03d}" if order_id < 1000 else str(order_id)[-3:]
    
    return f"{prefix}-{order_num}"

def format_purchase_number(purchase_id: int, created_at: str = None) -> str:
    """
    Форматирует номер покупки в красивый формат.
    Формат: BP-001 (Beat Purchase)
    
    Args:
        purchase_id: ID покупки из БД
        created_at: Дата создания в формате ISO (опционально, не используется)
    
    Returns:
        Отформатированный номер покупки
    """
    prefix = "BP"
    
    # Номер покупки
    purchase_num = f"{purchase_id:03d}" if purchase_id < 1000 else str(purchase_id)[-3:]
    
    return f"{prefix}-{purchase_num}"

async def create_custom_order(user_id: int, username: str, description: str, file_id: Optional[str] = None) -> Dict:
    """Создает новый заказ бита на заказ."""
    db = await get_db()
    try:
        created_at = datetime.now().isoformat()
        cursor = await db.execute("""
            INSERT INTO orders (type, user_id, username, description, file_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, ("custom_beat", user_id, username, description, file_id, created_at))
        await db.commit()
        order_id = cursor.lastrowid
        
        return {
            "id": order_id,
            "type": "custom_beat",
            "user_id": user_id,
            "username": username,
            "description": description,
            "file_id": file_id,
            "status": "pending",
            "price": None,
            "partner_price": None,
            "client_price": None,
            "first_payment": None,
            "second_payment": None,
            "created_at": created_at,
            "accepted_at": None,
            "completed_at": None,
            "rejected_at": None,
            "client_message_id": None,
            "partner_id": None,
            "partner_username": None,
            "payment_logs": [],
            "accept_lock": None,
            "partner_message_ids": {},
        }
    finally:
        await db.close()

async def create_mixing_order(user_id: int, username: str, description: str, file_id: Optional[str] = None) -> Dict:
    """Создает новый заказ на сведение."""
    db = await get_db()
    try:
        created_at = datetime.now().isoformat()
        cursor = await db.execute("""
            INSERT INTO orders (type, user_id, username, description, file_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?)
        """, ("mixing", user_id, username, description, file_id, created_at))
        await db.commit()
        order_id = cursor.lastrowid
        
        return {
            "id": order_id,
            "type": "mixing",
            "user_id": user_id,
            "username": username,
            "description": description,
            "file_id": file_id,
            "status": "pending",
            "price": None,
            "partner_price": None,
            "client_price": None,
            "first_payment": None,
            "second_payment": None,
            "created_at": created_at,
            "accepted_at": None,
            "completed_at": None,
            "rejected_at": None,
            "client_message_id": None,
            "partner_id": None,
            "partner_username": None,
            "payment_logs": [],
            "accept_lock": None,
            "partner_message_ids": {},
        }
    finally:
        await db.close()

async def get_order_by_user_id(user_id: int, order_type: str = None) -> Optional[Dict]:
    """Находит заказ по user_id. Если order_type указан, ищет только этот тип."""
    db = await get_db()
    try:
        if order_type:
            query = """
                SELECT * FROM orders 
                WHERE user_id = ? AND type = ? 
                AND status IN ('pending', 'accepted', 'in_progress', 'first_payment_received')
                ORDER BY created_at DESC
                LIMIT 1
            """
            cursor = await db.execute(query, (user_id, order_type))
        else:
            query = """
                SELECT * FROM orders 
                WHERE user_id = ? 
                AND status IN ('pending', 'accepted', 'in_progress', 'first_payment_received')
                ORDER BY created_at DESC
                LIMIT 1
            """
            cursor = await db.execute(query, (user_id,))
        
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)
        return None
    finally:
        await db.close()

async def get_order_by_id(order_id: int, order_type: str) -> Optional[Dict]:
    """Находит заказ по ID и типу."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM orders WHERE id = ? AND type = ?",
            (order_id, order_type)
        )
        row = await cursor.fetchone()
        if row:
            return _row_to_dict(row)
        return None
    finally:
        await db.close()

async def update_order_status(order_id: int, order_type: str, status: str, **kwargs) -> Optional[Dict]:
    """Обновляет статус заказа и другие поля."""
    db = await get_db()
    try:
        # Подготавливаем обновления
        updates = ["status = ?"]
        values = [status]
        
        # Обновляем временные метки в зависимости от статуса
        now = datetime.now().isoformat()
        if status == "accepted":
            updates.append("accepted_at = ?")
            values.append(now)
        elif status == "completed":
            updates.append("completed_at = ?")
            values.append(now)
        elif status == "rejected":
            updates.append("rejected_at = ?")
            values.append(now)
        elif status == "cancelled":
            updates.append("cancelled_at = ?")
            values.append(now)
        
        # Обновляем дополнительные поля
        for key, value in kwargs.items():
            if key in ["price", "partner_price", "client_price", "partner_id", "partner_username", 
                       "client_message_id", "first_payment", "second_payment", "accept_lock"]:
                updates.append(f"{key} = ?")
                values.append(value)
            elif key == "payment_logs" and isinstance(value, list):
                updates.append("payment_logs = ?")
                values.append(json.dumps(value, ensure_ascii=False))
            elif key == "partner_message_ids" and isinstance(value, dict):
                updates.append("partner_message_ids = ?")
                values.append(json.dumps(value, ensure_ascii=False))
        
        values.extend([order_id, order_type])
        
        query = f"""
            UPDATE orders 
            SET {', '.join(updates)}
            WHERE id = ? AND type = ?
        """
        
        await db.execute(query, values)
        await db.commit()
        
        # Возвращаем обновленный заказ
        return await get_order_by_id(order_id, order_type)
    finally:
        await db.close()

async def get_all_orders(order_type: str = None) -> List[Dict]:
    """Возвращает все заказы. Если order_type указан, возвращает только этот тип."""
    db = await get_db()
    try:
        if order_type:
            cursor = await db.execute(
                "SELECT * FROM orders WHERE type = ? ORDER BY created_at DESC",
                (order_type,)
            )
        else:
            cursor = await db.execute("SELECT * FROM orders ORDER BY created_at DESC")
        
        rows = await cursor.fetchall()
        return [_row_to_dict(row) for row in rows]
    finally:
        await db.close()

async def create_beats_purchase(user_id: int, username: str, beat: str, license: str, price: str) -> Dict:
    """Создает новую покупку готового бита."""
    db = await get_db()
    try:
        created_at = datetime.now().isoformat()
        cursor = await db.execute("""
            INSERT INTO beats_purchases (user_id, username, beat, license, price, status, created_at)
            VALUES (?, ?, ?, ?, ?, 'pending_payment', ?)
        """, (user_id, username, beat, license, price, created_at))
        await db.commit()
        purchase_id = cursor.lastrowid
        
        return {
            "id": purchase_id,
            "user_id": user_id,
            "username": username,
            "beat": beat,
            "license": license,
            "price": price,
            "status": "pending_payment",
            "created_at": created_at,
            "payment_received_at": None,
            "file_sent_at": None,
            "client_message_id": None,
        }
    finally:
        await db.close()

async def get_beats_purchase_by_user_id(user_id: int) -> Optional[Dict]:
    """Находит самую новую активную покупку по user_id (по ID, не завершенную и не отмененную)."""
    db = await get_db()
    try:
        cursor = await db.execute("""
            SELECT * FROM beats_purchases 
            WHERE user_id = ? AND status != 'completed' AND status != 'payment_rejected' AND status != 'cancelled_by_client'
            ORDER BY id DESC
            LIMIT 1
        """, (user_id,))
        row = await cursor.fetchone()
        if row:
            return _purchase_row_to_dict(row)
        return None
    finally:
        await db.close()

async def get_beats_purchase_by_id(purchase_id: int) -> Optional[Dict]:
    """Находит покупку по ID."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT * FROM beats_purchases WHERE id = ?",
            (purchase_id,)
        )
        row = await cursor.fetchone()
        if row:
            return _purchase_row_to_dict(row)
        return None
    finally:
        await db.close()

async def update_beats_purchase_status(purchase_id: int, status: str, **kwargs) -> Optional[Dict]:
    """Обновляет статус покупки готового бита."""
    db = await get_db()
    try:
        updates = ["status = ?"]
        values = [status]
        
        now = datetime.now().isoformat()
        if status == "payment_received":
            updates.append("payment_received_at = ?")
            values.append(now)
        elif status == "file_sent":
            updates.append("file_sent_at = ?")
            values.append(now)
        
        # Обновляем дополнительные поля
        for key, value in kwargs.items():
            if key in ["client_message_id", "waiting_card_details", "card_details_sent", "beat", "license", "price"]:
                updates.append(f"{key} = ?")
                values.append(value)
        
        values.append(purchase_id)
        
        query = f"""
            UPDATE beats_purchases 
            SET {', '.join(updates)}
            WHERE id = ?
        """
        
        await db.execute(query, values)
        await db.commit()
        
        return await get_beats_purchase_by_id(purchase_id)
    finally:
        await db.close()

async def get_all_beats_purchases() -> List[Dict]:
    """Возвращает все покупки готовых битов."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT * FROM beats_purchases ORDER BY created_at DESC")
        rows = await cursor.fetchall()
        return [_purchase_row_to_dict(row) for row in rows]
    finally:
        await db.close()

def _row_to_dict(row) -> Dict:
    """Преобразует строку БД в словарь."""
    d = dict(row)
    # Преобразуем boolean значения
    d["first_payment"] = bool(d.get("first_payment", 0))
    d["second_payment"] = bool(d.get("second_payment", 0))
    # Преобразуем payment_logs из JSON
    if d.get("payment_logs"):
        try:
            d["payment_logs"] = json.loads(d["payment_logs"])
        except:
            d["payment_logs"] = []
    else:
        d["payment_logs"] = []
    # Преобразуем partner_message_ids из JSON
    if d.get("partner_message_ids"):
        try:
            d["partner_message_ids"] = json.loads(d["partner_message_ids"])
        except:
            d["partner_message_ids"] = {}
    else:
        d["partner_message_ids"] = {}
    return d

def _purchase_row_to_dict(row) -> Dict:
    """Преобразует строку покупки БД в словарь."""
    d = dict(row)
    # Преобразуем INTEGER (0/1) в boolean
    d["waiting_card_details"] = bool(d.get("waiting_card_details", 0))
    d["card_details_sent"] = bool(d.get("card_details_sent", 0))
    return d

# === Функции для работы с языками пользователей ===

async def get_user_language(user_id: int) -> str:
    """Получает язык пользователя из БД. Возвращает 'ru' по умолчанию."""
    db = await get_db()
    try:
        cursor = await db.execute(
            "SELECT language FROM user_languages WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        if row:
            return row["language"]
        return "ru"  # Дефолтный язык
    finally:
        await db.close()

async def set_user_language(user_id: int, language: str) -> bool:
    """Устанавливает язык пользователя в БД. Возвращает True при успехе."""
    db = await get_db()
    try:
        now = datetime.now().isoformat()
        await db.execute("""
            INSERT OR REPLACE INTO user_languages (user_id, language, updated_at)
            VALUES (?, ?, ?)
        """, (user_id, language, now))
        await db.commit()
        return True
    except Exception as e:
        logging.error(f"Ошибка сохранения языка пользователя {user_id}: {e}")
        await db.rollback()
        return False
    finally:
        await db.close()

async def get_all_user_languages() -> Dict[int, str]:
    """Возвращает словарь всех языков пользователей {user_id: language}."""
    db = await get_db()
    try:
        cursor = await db.execute("SELECT user_id, language FROM user_languages")
        rows = await cursor.fetchall()
        return {row["user_id"]: row["language"] for row in rows}
    finally:
        await db.close()
