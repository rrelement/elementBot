"""
Скрипт для миграции данных из JSON файлов в SQLite базу данных.
Запустите этот скрипт один раз перед использованием новой версии с БД.
"""
import json
import os
import asyncio
import logging
from database import init_db, get_db

logging.basicConfig(level=logging.INFO)

async def migrate_orders():
    """Мигрирует заказы из orders.json в БД."""
    if not os.path.exists("orders.json"):
        logging.info("orders.json не найден, пропускаем миграцию заказов")
        return
    
    db = await get_db()
    try:
        with open("orders.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        custom_orders = data.get("custom_orders", [])
        mixing_orders = data.get("mixing_orders", [])
        
        migrated = 0
        
        # Мигрируем custom_beat заказы
        for order in custom_orders:
            try:
                payment_logs = json.dumps(order.get("payment_logs", []), ensure_ascii=False) if order.get("payment_logs") else None
                await db.execute("""
                    INSERT OR IGNORE INTO orders (
                        id, type, user_id, username, description, file_id, status,
                        price, partner_price, client_price, first_payment, second_payment,
                        created_at, accepted_at, completed_at, rejected_at, cancelled_at,
                        client_message_id, partner_id, partner_username, payment_logs, accept_lock
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order.get("id"),
                    "custom_beat",
                    order.get("user_id"),
                    order.get("username"),
                    order.get("description"),
                    order.get("file_id"),
                    order.get("status", "pending"),
                    order.get("price"),
                    order.get("partner_price"),
                    order.get("client_price"),
                    1 if order.get("first_payment") else 0,
                    1 if order.get("second_payment") else 0,
                    order.get("created_at"),
                    order.get("accepted_at"),
                    order.get("completed_at"),
                    order.get("rejected_at"),
                    order.get("cancelled_at"),
                    order.get("client_message_id"),
                    order.get("partner_id"),
                    order.get("partner_username"),
                    payment_logs,
                    order.get("accept_lock")
                ))
                migrated += 1
            except Exception as e:
                logging.warning(f"Ошибка миграции заказа {order.get('id')}: {e}")
        
        # Мигрируем mixing заказы
        for order in mixing_orders:
            try:
                payment_logs = json.dumps(order.get("payment_logs", []), ensure_ascii=False) if order.get("payment_logs") else None
                await db.execute("""
                    INSERT OR IGNORE INTO orders (
                        id, type, user_id, username, description, file_id, status,
                        price, partner_price, client_price, first_payment, second_payment,
                        created_at, accepted_at, completed_at, rejected_at, cancelled_at,
                        client_message_id, partner_id, partner_username, payment_logs, accept_lock
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    order.get("id"),
                    "mixing",
                    order.get("user_id"),
                    order.get("username"),
                    order.get("description"),
                    order.get("file_id"),
                    order.get("status", "pending"),
                    order.get("price"),
                    order.get("partner_price"),
                    order.get("client_price"),
                    1 if order.get("first_payment") else 0,
                    1 if order.get("second_payment") else 0,
                    order.get("created_at"),
                    order.get("accepted_at"),
                    order.get("completed_at"),
                    order.get("rejected_at"),
                    order.get("cancelled_at"),
                    order.get("client_message_id"),
                    order.get("partner_id"),
                    order.get("partner_username"),
                    payment_logs,
                    order.get("accept_lock")
                ))
                migrated += 1
            except Exception as e:
                logging.warning(f"Ошибка миграции заказа {order.get('id')}: {e}")
        
        await db.commit()
        logging.info(f"Мигрировано {migrated} заказов")
    finally:
        await db.close()

async def migrate_beats_purchases():
    """Мигрирует покупки из beats_purchases.json в БД."""
    if not os.path.exists("beats_purchases.json"):
        logging.info("beats_purchases.json не найден, пропускаем миграцию покупок")
        return
    
    db = await get_db()
    try:
        with open("beats_purchases.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        purchases = data.get("purchases", [])
        migrated = 0
        
        for purchase in purchases:
            try:
                await db.execute("""
                    INSERT OR IGNORE INTO beats_purchases (
                        id, user_id, username, beat, license, price, status,
                        created_at, payment_received_at, file_sent_at, client_message_id, waiting_card_details
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    purchase.get("id"),
                    purchase.get("user_id"),
                    purchase.get("username"),
                    purchase.get("beat"),
                    purchase.get("license"),
                    purchase.get("price"),
                    purchase.get("status", "pending_payment"),
                    purchase.get("created_at"),
                    purchase.get("payment_received_at"),
                    purchase.get("file_sent_at"),
                    purchase.get("client_message_id"),
                    1 if purchase.get("waiting_card_details") else 0
                ))
                migrated += 1
            except Exception as e:
                logging.warning(f"Ошибка миграции покупки {purchase.get('id')}: {e}")
        
        await db.commit()
        logging.info(f"Мигрировано {migrated} покупок")
    finally:
        await db.close()

async def migrate_partners():
    """Мигрирует партнеров из partners.json в БД."""
    if not os.path.exists("partners.json"):
        logging.info("partners.json не найден, пропускаем миграцию партнеров")
        return
    
    db = await get_db()
    try:
        with open("partners.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        partners = data.get("partners", [])
        migrated = 0
        
        for partner in partners:
            try:
                await db.execute("""
                    INSERT OR IGNORE INTO partners (
                        user_id, username, name, type, active, orders_accepted, orders_completed
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    partner.get("user_id"),
                    partner.get("username"),
                    partner.get("name"),
                    partner.get("type", "partner"),
                    1 if partner.get("active", True) else 0,
                    partner.get("orders_accepted", 0),
                    partner.get("orders_completed", 0)
                ))
                migrated += 1
            except Exception as e:
                logging.warning(f"Ошибка миграции партнера {partner.get('user_id')}: {e}")
        
        await db.commit()
        logging.info(f"Мигрировано {migrated} партнеров")
    finally:
        await db.close()

async def migrate_partner_requests():
    """Мигрирует заявки партнеров из partner_requests.json в БД."""
    if not os.path.exists("partner_requests.json"):
        logging.info("partner_requests.json не найден, пропускаем миграцию заявок")
        return
    
    db = await get_db()
    try:
        with open("partner_requests.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        requests = data.get("requests", [])
        migrated = 0
        
        for req in requests:
            try:
                await db.execute("""
                    INSERT OR IGNORE INTO partner_requests (
                        user_id, username, name, type, message, status,
                        created_at, reviewed_at, reviewed_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    req.get("user_id"),
                    req.get("username"),
                    req.get("name"),
                    req.get("type", "partner"),
                    req.get("message", ""),
                    req.get("status", "pending"),
                    req.get("created_at"),
                    req.get("reviewed_at"),
                    req.get("reviewed_by")
                ))
                migrated += 1
            except Exception as e:
                logging.warning(f"Ошибка миграции заявки {req.get('user_id')}: {e}")
        
        await db.commit()
        logging.info(f"Мигрировано {migrated} заявок")
    finally:
        await db.close()

async def migrate_user_languages():
    """Мигрирует языки пользователей из user_languages.json в БД."""
    if not os.path.exists("user_languages.json"):
        logging.info("user_languages.json не найден, пропускаем миграцию языков")
        return
    
    db = await get_db()
    try:
        with open("user_languages.json", 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        migrated = 0
        from datetime import datetime
        now = datetime.now().isoformat()
        
        for user_id_str, language in data.items():
            try:
                user_id = int(user_id_str)
                await db.execute("""
                    INSERT OR REPLACE INTO user_languages (user_id, language, updated_at)
                    VALUES (?, ?, ?)
                """, (user_id, language, now))
                migrated += 1
            except (ValueError, Exception) as e:
                logging.warning(f"Ошибка миграции языка для пользователя {user_id_str}: {e}")
        
        await db.commit()
        logging.info(f"✅ Мигрировано языков пользователей: {migrated}")
    except Exception as e:
        logging.error(f"Ошибка миграции языков пользователей: {e}")
        await db.rollback()
    finally:
        await db.close()

async def main():
    """Основная функция миграции."""
    logging.info("Начинаем миграцию данных из JSON в SQLite...")
    
    # Инициализируем БД
    await init_db()
    
    # Мигрируем данные
    await migrate_orders()
    await migrate_beats_purchases()
    await migrate_partners()
    await migrate_partner_requests()
    await migrate_user_languages()
    
    logging.info("Миграция завершена!")
    logging.info("Теперь можно использовать новую версию с SQLite.")
    logging.info("Старые JSON файлы можно оставить как резервную копию или удалить.")

if __name__ == "__main__":
    asyncio.run(main())



