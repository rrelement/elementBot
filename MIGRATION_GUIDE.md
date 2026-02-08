# Руководство по миграции на SQLite

## Шаги миграции

### 1. Установите зависимости
```bash
pip install aiosqlite
```

### 2. Запустите скрипт миграции данных
```bash
python migrate_to_sqlite.py
```

Этот скрипт:
- Создаст базу данных `bot_database.db`
- Мигрирует все данные из JSON файлов в БД
- Сохранит старые JSON файлы как резервную копию

### 3. Обновите код для использования асинхронных функций

**ВАЖНО:** Все функции из `orders_manager` и `partners_manager` теперь асинхронные!

#### Старый код (синхронный):
```python
from orders_manager import get_order_by_id, update_order_status
order = get_order_by_id(order_id, order_type)
update_order_status(order_id, order_type, "completed")
```

#### Новый код (асинхронный):
```python
from orders_manager import get_order_by_id, update_order_status
order = await get_order_by_id(order_id, order_type)
await update_order_status(order_id, order_type, "completed")
```

### 4. Обновите все вызовы функций

Нужно добавить `await` перед всеми вызовами:
- `get_order_by_id()` → `await get_order_by_id()`
- `get_order_by_user_id()` → `await get_order_by_user_id()`
- `get_all_orders()` → `await get_all_orders()`
- `update_order_status()` → `await update_order_status()`
- `create_custom_order()` → `await create_custom_order()`
- `create_mixing_order()` → `await create_mixing_order()`
- `get_partner()` → `await get_partner()`
- `get_active_partners()` → `await get_active_partners()`
- `increment_partner_orders()` → `await increment_partner_orders()`
- И все остальные функции из этих модулей

### 5. Удалены функции

Эти функции больше не существуют (не нужны с БД):
- `load_orders()` - используйте `get_all_orders()`
- `save_orders()` - обновления делаются через `update_order_status()`
- `load_partners()` - используйте `get_active_partners()` или `get_partner()`
- `save_partners()` - обновления делаются через функции типа `add_partner()`, `set_partner_active()`

### 6. Инициализация БД при запуске

Добавьте в начало `main()` функции в обоих ботах:

```python
from database import init_db

async def main():
    # Инициализируем БД при запуске
    await init_db()
    
    # Остальной код...
    await dp.start_polling(bot)
```

## Проверка миграции

После миграции проверьте:
1. Все данные перенесены (запустите бот и проверьте статистику)
2. Создание новых заказов работает
3. Обновление статусов работает
4. Работа с партнерами работает

## Откат (если что-то пошло не так)

Если нужно вернуться к JSON:
1. Остановите бот
2. Удалите `bot_database.db`
3. Восстановите старые версии `orders_manager.py` и `partners_manager.py` из git
4. Перезапустите бот

Старые JSON файлы остаются нетронутыми после миграции.















