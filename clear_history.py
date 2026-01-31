"""
Скрипт для очистки истории покупок и заказов.
ВНИМАНИЕ: Это удалит ВСЕ данные о покупках и заказах из базы данных!
"""
import asyncio
import logging
from database import init_db, get_db

logging.basicConfig(level=logging.INFO)

async def clear_all_orders():
    """Удаляет все заказы из БД."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM orders")
        deleted_count = cursor.rowcount
        await db.commit()
        logging.info(f"Удалено заказов: {deleted_count}")
        return deleted_count
    except Exception as e:
        logging.error(f"Ошибка удаления заказов: {e}")
        await db.rollback()
        return 0
    finally:
        await db.close()

async def clear_all_purchases():
    """Удаляет все покупки из БД."""
    db = await get_db()
    try:
        cursor = await db.execute("DELETE FROM beats_purchases")
        deleted_count = cursor.rowcount
        await db.commit()
        logging.info(f"Удалено покупок: {deleted_count}")
        return deleted_count
    except Exception as e:
        logging.error(f"Ошибка удаления покупок: {e}")
        await db.rollback()
        return 0
    finally:
        await db.close()

async def reset_partners_statistics():
    """Обнуляет статистику партнеров (orders_accepted, orders_completed)."""
    db = await get_db()
    try:
        cursor = await db.execute("UPDATE partners SET orders_accepted = 0, orders_completed = 0")
        updated_count = cursor.rowcount
        await db.commit()
        logging.info(f"Обнулена статистика для партнеров: {updated_count}")
        return updated_count
    except Exception as e:
        logging.error(f"Ошибка обнуления статистики партнеров: {e}")
        await db.rollback()
        return 0
    finally:
        await db.close()

async def reset_auto_increment():
    """Сбрасывает автоинкрементные счетчики для orders и beats_purchases."""
    db = await get_db()
    try:
        # Сбрасываем счетчик для orders
        await db.execute("DELETE FROM sqlite_sequence WHERE name='orders'")
        # Сбрасываем счетчик для beats_purchases
        await db.execute("DELETE FROM sqlite_sequence WHERE name='beats_purchases'")
        await db.commit()
        logging.info("Счетчики автоинкремента сброшены")
        return True
    except Exception as e:
        logging.error(f"Ошибка сброса счетчиков: {e}")
        await db.rollback()
        return False
    finally:
        await db.close()

async def main(auto_confirm=False):
    """Основная функция очистки."""
    print("=" * 60)
    print("ВНИМАНИЕ: Этот скрипт удалит ВСЕ заказы и покупки!")
    print("=" * 60)
    
    # Запрашиваем подтверждение (если не автоматическое)
    if not auto_confirm:
        confirm = input("\nВы уверены? Введите 'YES' для подтверждения: ")
        if confirm != "YES":
            print("Операция отменена.")
            return
    else:
        print("\nАвтоматическое подтверждение: YES")
    
    # Инициализируем БД
    await init_db()
    
    # Получаем количество записей перед удалением
    db = await get_db()
    try:
        cursor = await db.execute("SELECT COUNT(*) as count FROM orders")
        orders_count = (await cursor.fetchone())["count"]
        
        cursor = await db.execute("SELECT COUNT(*) as count FROM beats_purchases")
        purchases_count = (await cursor.fetchone())["count"]
        
        print(f"\nТекущая статистика:")
        print(f"   Заказов: {orders_count}")
        print(f"   Покупок: {purchases_count}")
    finally:
        await db.close()
    
    # Очищаем данные
    print("\nНачинаем очистку...")
    
    orders_deleted = await clear_all_orders()
    purchases_deleted = await clear_all_purchases()
    partners_reset = await reset_partners_statistics()
    auto_increment_reset = await reset_auto_increment()
    
    print("\n" + "=" * 60)
    print("Очистка завершена!")
    print(f"   Удалено заказов: {orders_deleted}")
    print(f"   Удалено покупок: {purchases_deleted}")
    print(f"   Обнулена статистика партнеров: {partners_reset}")
    if auto_increment_reset:
        print(f"   Счетчики автоинкремента сброшены")
    print("=" * 60)

if __name__ == "__main__":
    import sys
    auto_confirm = "--yes" in sys.argv or "-y" in sys.argv
    asyncio.run(main(auto_confirm=auto_confirm))

