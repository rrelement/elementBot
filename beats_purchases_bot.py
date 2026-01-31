"""
Бот для управления покупками готовых битов.
Получает покупки из основного бота и позволяет админу управлять ими.
"""
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
import os
# Импорты из orders_manager теперь делаются локально, так как функции асинхронные

load_dotenv()

logging.basicConfig(level=logging.INFO)

# Токен бота для покупок (нужно будет указать в .env)
PURCHASES_BOT_TOKEN = os.getenv("PURCHASES_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "830030557"))

# Импортируем основной бот для отправки сообщений клиентам (после load_dotenv)
MAIN_BOT_TOKEN = os.getenv("TOKEN", "8588087035:AAGSyPJesse5NnbIx98wovIeJGtQGUThJsw")
main_bot = None
if MAIN_BOT_TOKEN:
    try:
        from aiogram import Bot
        from aiogram.client.session.aiohttp import AiohttpSession
        
        # Используем те же настройки таймаутов и прокси
        PROXY_URL = os.getenv("PROXY_URL", None)
        if PROXY_URL:
            main_session = AiohttpSession(proxy=PROXY_URL)
            main_session.timeout = 60
        else:
            main_session = AiohttpSession()
            main_session.timeout = 60
        main_bot = Bot(token=MAIN_BOT_TOKEN, session=main_session)
        logging.info("Основной бот инициализирован для отправки сообщений клиентам.")
    except Exception as e:
        logging.error(f"Ошибка инициализации основного бота: {e}")

if not PURCHASES_BOT_TOKEN:
    raise ValueError("PURCHASES_BOT_TOKEN не найден в .env файле")

# Настройки прокси (если Telegram заблокирован, укажите в .env)
PROXY_URL = os.getenv("PROXY_URL", None)

# Настройки таймаутов (увеличены для нестабильных соединений)
from aiogram.client.session.aiohttp import AiohttpSession

# Создаем сессию с увеличенными таймаутами
if PROXY_URL:
    session = AiohttpSession(proxy=PROXY_URL)
    session.timeout = 60  # Устанавливаем таймаут как число (в секундах)
else:
    session = AiohttpSession()
    session.timeout = 60  # Устанавливаем таймаут как число (в секундах)

bot = Bot(token=PURCHASES_BOT_TOKEN, session=session)
dp = Dispatcher()

# ID чата, куда будут приходить покупки
PURCHASES_CHAT_ID = ADMIN_ID  # По умолчанию личка админа, можно изменить на ID канала

# Отслеживание, какую покупку админ отправляет (purchase_id -> user_id)
dp.admin_sending_file = {}  # {purchase_id: user_id}

# Отслеживание, кому админ отправляет реквизиты карты
dp.admin_sending_card = None  # user_id клиента, которому админ сейчас отправляет реквизиты

async def get_user_language(user_id: int) -> str:
    """Получает язык пользователя из БД или возвращает дефолтный 'ru'."""
    try:
        from orders_manager import get_user_language as get_lang_from_db
        return await get_lang_from_db(user_id)
    except Exception as e:
        logging.error(f"Ошибка получения языка пользователя {user_id}: {e}")
        return "ru"  # Дефолтный язык

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start для админа."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Этот бот доступен только администратору.")
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    
    logging.info(f"cmd_start: Загружено покупок: {len(purchases)}")
    if purchases:
        logging.info(f"cmd_start: Пример статусов: {[p.get('status', 'NO_STATUS') for p in purchases[:5]]}")
    
    # Статистика
    pending = len([p for p in purchases if p.get("status") == "pending_payment"])
    waiting = len([p for p in purchases if p.get("status") == "payment_received"])
    waiting_card = len([p for p in purchases if p.get("waiting_card_details", False) and not p.get("card_details_sent", False) and p.get("status") != "completed"])
    completed = len([p for p in purchases if p.get("status") == "completed"])
    
    logging.info(f"cmd_start: Статистика - pending={pending}, waiting={waiting}, waiting_card={waiting_card}, completed={completed}")
    
    # Общая выручка
    total_revenue = 0
    for purchase in purchases:
        if purchase["status"] in ["payment_received", "completed"]:
            try:
                price_str = purchase.get("price", "").replace("$", "").replace("EXCLUSIVE — ", "").strip()
                price = float(price_str)
                total_revenue += price
            except:
                pass
    
    text = (
        "🤖 <b>Бот для управления покупками</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"🔍 На проверке: {pending}\n"
        f"💳 Ждет реквизиты: {waiting_card}\n"
        f"⏳ Ждет отправки: {waiting}\n"
        f"✅ Завершены: {completed}\n"
        f"📦 Всего покупок: {len(purchases)}\n"
        f"💵 Общая выручка: ${total_revenue:.0f}"
    )
    
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
    # Улучшенное меню - более структурированное и понятное
    menu_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все покупки")],
            [KeyboardButton(text="🔍 На проверке"), KeyboardButton(text="💳 Ждет реквизиты")],
            [KeyboardButton(text="⏳ Ждет отправки"), KeyboardButton(text="✅ Завершенные")],
            [KeyboardButton(text="📊 Статистика")]
        ],
        resize_keyboard=True
    )
    
    # Добавляем inline кнопки для быстрого доступа к фильтрам
    quick_access_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔍 На проверке ({})".format(pending), callback_data="filter_pending"),
                InlineKeyboardButton(text="💳 Ждет реквизиты ({})".format(waiting_card), callback_data="filter_card")
            ],
            [
                InlineKeyboardButton(text="⏳ Ждет отправки ({})".format(waiting), callback_data="filter_waiting"),
                InlineKeyboardButton(text="✅ Завершены ({})".format(completed), callback_data="filter_completed")
            ],
            [InlineKeyboardButton(text="📋 Все покупки", callback_data="filter_all")]
        ]
    )
    
    await message.answer(text, reply_markup=menu_kb, parse_mode="HTML")
    await message.answer("🔍 <b>Быстрый доступ к покупкам:</b>", reply_markup=quick_access_kb, parse_mode="HTML")

@dp.message(Command("purchases"))
async def cmd_purchases(message: Message, page: int = 0):
    """Показать все покупки."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    if not purchases:
        await message.answer("Покупок пока нет.")
        return
    
    # Сортируем по дате создания (новые первые)
    from datetime import datetime
    purchases_sorted = sorted(purchases, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(purchases_sorted) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(purchases_sorted))
    
    # Показываем компактный список с кнопками
    text = f"📋 <b>Все покупки ({len(purchases)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in purchases_sorted[start_idx:end_idx]:
        status_emoji = {
            "pending_payment": "🔍",
            "payment_received": "💰",
            "completed": "✅"
        }
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        emoji = status_emoji.get(purchase.get('status', ''), '❓')
        # Если покупка ждет реквизиты, показываем соответствующий эмодзи
        if purchase.get("waiting_card_details", False):
            emoji = "💳"
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, emoji),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"purchases_page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"purchases_page_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Подробная статистика."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    
    if not purchases:
        await message.answer("Покупок пока нет. Статистика недоступна.")
        return
    
    logging.info(f"cmd_stats: Всего покупок: {len(purchases)}")
    if purchases:
        logging.info(f"cmd_stats: Пример статусов: {[p.get('status', 'NO_STATUS') for p in purchases[:5]]}")
    
    # Статистика по статусам
    pending = len([p for p in purchases if p.get("status") == "pending_payment"])
    waiting = len([p for p in purchases if p.get("status") == "payment_received"])
    waiting_card = len([p for p in purchases if p.get("waiting_card_details", False) and not p.get("card_details_sent", False) and p.get("status") != "completed"])
    completed = len([p for p in purchases if p.get("status") == "completed"])
    
    logging.info(f"cmd_stats: Статистика - pending={pending}, waiting={waiting}, completed={completed}")
    
    # Статистика по лицензиям - подсчитываем каждую лицензию отдельно
    license_counts = {}
    for purchase in purchases:
        license_str = purchase.get("license", "").upper()
        if not license_str:
            continue
        
        # Определяем тип лицензии
        if "EXCLUSIVE" in license_str:
            license_type = "EXCLUSIVE"
        elif "TRACK OUT" in license_str or "TRACKOUT" in license_str:
            license_type = "TRACK OUT"
        elif "WAV" in license_str:
            license_type = "WAV"
        elif "MP3" in license_str:
            license_type = "MP3"
        else:
            # Если не распознано, используем первые слова из лицензии
            license_type = license_str.split(" — ")[0].split(" $")[0].strip()
            if not license_type:
                continue  # Пропускаем пустые лицензии
            
            # Проверяем, не является ли это просто ценой (начинается с $ или только цифры)
            import re
            # Убираем пробелы и проверяем
            license_clean = license_type.replace(" ", "").replace("$", "")
            # Если это только цифры (возможно с точкой) - это цена, пропускаем
            if re.match(r'^[\d.]+$', license_clean):
                continue  # Пропускаем лицензии, которые являются просто ценой
        
        license_counts[license_type] = license_counts.get(license_type, 0) + 1
    
    # Общая выручка
    total_revenue = 0
    for purchase in purchases:
        status = purchase.get("status", "")
        if status in ["payment_received", "completed"]:
            try:
                price_str = purchase.get("price", "")
                # Убираем все лишнее из строки цены
                price_str = price_str.replace("$", "").replace("EXCLUSIVE — ", "").replace("MP3 — ", "").replace("WAV — ", "").replace("TRACK OUT — ", "").strip()
                # Пытаемся извлечь число
                import re
                price_match = re.search(r'[\d.]+', price_str)
                if price_match:
                    price = float(price_match.group())
                total_revenue += price
            except Exception as e:
                logging.debug(f"Ошибка парсинга цены для покупки {purchase.get('id')}: {e}")
                pass
    
    # Формируем текст статистики
    text = (
        f"📊 <b>Подробная статистика</b>\n\n"
        f"<b>По статусам:</b>\n"
        f"🔍 На проверке: {pending}\n"
        f"💳 Ждет реквизиты: {waiting_card}\n"
        f"⏳ Ждет отправки: {waiting}\n"
        f"✅ Завершены: {completed}\n\n"
    )
    
    # Добавляем статистику по лицензиям
    if license_counts:
        text += f"<b>По лицензиям:</b>\n"
        # Сортируем по количеству (от большего к меньшему)
        sorted_licenses = sorted(license_counts.items(), key=lambda x: x[1], reverse=True)
        for license_type, count in sorted_licenses:
            # Добавляем эмодзи в зависимости от типа лицензии
            emoji = "⭐" if license_type == "EXCLUSIVE" else "🎵" if license_type == "TRACK OUT" else "💿" if license_type == "WAV" else "🎧" if license_type == "MP3" else "📄"
            # Приводим к нижнему регистру для отображения
            license_display = license_type.lower().replace("_", " ")
            text += f"{emoji} {license_display}: {count}\n"
        text += "\n"
    
    text += (
        f"💰 Общая выручка: ${total_revenue:.0f}\n"
        f"📦 Всего покупок: {len(purchases)}\n"
    )
    
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📋 Все покупки")
async def handle_all_purchases(message: Message):
    """Обработка кнопки 'Все покупки'."""
    await cmd_purchases(message, page=0)

@dp.message(F.text == "🔍 На проверке")
async def handle_pending(message: Message):
    """Обработка кнопки 'На проверке'."""
    await cmd_pending(message)

@dp.message(F.text == "💰 Оплачены")
async def handle_paid(message: Message):
    """Обработка кнопки 'Оплачены'."""
    await cmd_paid(message)

@dp.message(F.text == "💳 Ждет реквизиты")
async def handle_waiting_card(message: Message):
    """Обработка кнопки 'Ждет реквизиты'."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    
    # Детальное логирование для отладки
    logging.info(f"handle_waiting_card: Всего покупок: {len(purchases)}")
    for p in purchases[:5]:  # Логируем первые 5 покупок для отладки
        logging.info(f"Покупка №{p.get('id')}: status={p.get('status')}, waiting_card_details={p.get('waiting_card_details')}, card_details_sent={p.get('card_details_sent')}")
    
    # Исправляем покупки, у которых waiting_card_details=True, но card_details_sent=True
    # Это может произойти, если реквизиты были запрошены повторно
    from orders_manager import update_beats_purchase_status
    fixed_count = 0
    for p in purchases:
        if p.get("waiting_card_details", False) and p.get("card_details_sent", False) and p.get("status") != "completed":
            # Сбрасываем card_details_sent, если покупка ждет реквизиты
            await update_beats_purchase_status(p["id"], p.get("status", "pending_payment"), card_details_sent=0)
            logging.info(f"Исправлена покупка №{p['id']}: сброшен card_details_sent")
            fixed_count += 1
    
    if fixed_count > 0:
        # Перезагружаем покупки после исправления
        purchases = await get_all_beats_purchases()
        logging.info(f"Исправлено покупок: {fixed_count}")
    
    # Показываем только те покупки, которые ждут реквизиты И реквизиты еще не отправлены
    # Исключаем завершенные покупки
    waiting_card = [
        p for p in purchases 
        if p.get("waiting_card_details", False) 
        and not p.get("card_details_sent", False)
        and p.get("status") != "completed"
    ]
    
    logging.info(f"handle_waiting_card: Всего покупок: {len(purchases)}, ожидающих реквизиты: {len(waiting_card)}")
    if waiting_card:
        logging.info(f"handle_waiting_card: Примеры покупок, ожидающих реквизиты: {[p['id'] for p in waiting_card[:5]]}")
    else:
        # Проверяем, есть ли покупки с waiting_card_details=True, но card_details_sent тоже True
        with_waiting = [p for p in purchases if p.get("waiting_card_details", False)]
        logging.info(f"handle_waiting_card: Покупок с waiting_card_details=True: {len(with_waiting)}")
        if with_waiting:
            logging.info(f"handle_waiting_card: Примеры: {[(p['id'], p.get('card_details_sent')) for p in with_waiting[:5]]}")
    
    if not waiting_card:
        await message.answer("Нет покупок, ожидающих реквизиты.")
        return
    
    # Сортируем по дате (новые первые)
    from datetime import datetime
    waiting_card_sorted = sorted(waiting_card, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(waiting_card_sorted) + items_per_page - 1) // items_per_page
    page = 0
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(waiting_card_sorted))
    
    # Показываем компактный список с кнопками
    text = f"💳 <b>Ждет реквизиты ({len(waiting_card)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in waiting_card_sorted[start_idx:end_idx]:
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, "💳"),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_card_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_card_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "⏳ Ждет отправки")
async def handle_sent(message: Message):
    """Обработка кнопки 'Ждет отправки'."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    waiting = [p for p in purchases if p.get("status") == "payment_received"]
    
    if not waiting:
        await message.answer("Нет покупок, ожидающих отправки файла.")
        return
    
    # Сортируем по дате (новые первые)
    from datetime import datetime
    waiting_sorted = sorted(waiting, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(waiting_sorted) + items_per_page - 1) // items_per_page
    page = 0
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(waiting_sorted))
    
    # Показываем компактный список с кнопками
    text = f"⏳ <b>Ждет отправки ({len(waiting)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in waiting_sorted[start_idx:end_idx]:
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, "🔍"),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_waiting_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_waiting_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "📊 Статистика")
async def handle_stats(message: Message):
    """Обработка кнопки 'Статистика'."""
    await cmd_stats(message)

@dp.message(F.text == "✅ Завершенные")
async def handle_completed(message: Message):
    """Обработка кнопки 'Завершенные'."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    completed = [p for p in purchases if p.get("status") == "completed"]
    
    if not completed:
        await message.answer("Нет завершенных покупок.")
        return
    
    # Сортируем по дате (новые первые)
    from datetime import datetime
    completed_sorted = sorted(completed, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(completed_sorted) + items_per_page - 1) // items_per_page
    page = 0
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(completed_sorted))
    
    # Показываем компактный список с кнопками
    text = f"✅ <b>Завершенные покупки ({len(completed)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in completed_sorted[start_idx:end_idx]:
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, "✅"),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_completed_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_completed_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "/menu")
async def handle_menu(message: Message):
    """Обработка команды /menu."""
    await cmd_start(message)

@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    """Показать покупки на проверке."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    pending = [p for p in purchases if p["status"] == "pending_payment"]
    
    if not pending:
        await message.answer("Нет покупок на проверке.")
        return
    
    # Сортируем по дате создания (новые первые)
    from datetime import datetime
    pending_sorted = sorted(pending, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(pending_sorted) + items_per_page - 1) // items_per_page
    page = 0
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(pending_sorted))
    
    # Показываем компактный список
    text = f"🔍 <b>На проверке ({len(pending)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in pending_sorted[start_idx:end_idx]:
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, "⏳"),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_pending_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_pending_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("paid"))
async def cmd_paid(message: Message):
    """Показать оплаченные покупки."""
    if message.from_user.id != ADMIN_ID:
        return
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    paid = [p for p in purchases if p.get("status") == "payment_received"]
    
    if not paid:
        await message.answer("Нет оплаченных покупок.")
        return
    
    # Сортируем по дате (новые первые)
    from datetime import datetime
    paid_sorted = sorted(paid, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(paid_sorted) + items_per_page - 1) // items_per_page
    page = 0
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(paid_sorted))
    
    # Показываем компактный список с кнопками
    text = f"💰 <b>Оплачены ({len(paid)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in paid_sorted[start_idx:end_idx]:
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, "🔍"),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_paid_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

def format_license_and_price(license_str: str, price_str: str = None) -> tuple[str, str]:
    """Форматирует лицензию и цену, разделяя их на две строки.
    Возвращает (license_text, price_text)"""
    # Если лицензия содержит " — ", разделяем на тип лицензии и цену
    if " — " in license_str:
        parts = license_str.split(" — ", 1)
        license_type = parts[0].strip()
        license_price = parts[1].strip() if len(parts) > 1 else None
        # Если цена не передана отдельно, используем цену из лицензии
        if price_str is None and license_price:
            price_str = license_price
        return license_type, price_str or license_price or ""
    else:
        # Если лицензия не содержит " — ", используем как есть
        return license_str, price_str or ""
    
def format_compact_button_text(purchase: dict, emoji: str = "") -> str:
    """Форматирует компактный текст для кнопки покупки."""
    from orders_manager import format_purchase_number
    purchase_num = format_purchase_number(purchase["id"], purchase.get("created_at"))
    return f"{emoji}{purchase_num}"
    
def format_purchase_message(purchase: dict) -> str:
    """Форматирует сообщение о покупке."""
    from datetime import datetime
    from orders_manager import format_purchase_number
    
    purchase_num = format_purchase_number(purchase["id"], purchase.get("created_at"))
    
    status_emoji = {
        "pending_payment": "⏳",
        "payment_received": "💰",
        "completed": "✅",
        "payment_rejected": "❌",
        "cancelled_by_client": "🚫"
    }
    
    status_text = {
        "pending_payment": "На проверке",
        "payment_received": "Оплата получена",
        "completed": "Завершена",
        "payment_rejected": "Оплата отклонена",
        "cancelled_by_client": "Отменена клиентом"
    }
    
    # Форматируем лицензию и цену
    license_text, price_text = format_license_and_price(purchase.get('license', ''), purchase.get('price', ''))
    
    # Извлекаем только цену из price_text (убираем тип лицензии, если есть)
    # Если price_text содержит " — ", берем только часть после " — "
    if " — " in price_text:
        price_text = price_text.split(" — ", 1)[1].strip()
    # Убеждаемся, что цена содержит знак доллара
    if price_text and not price_text.startswith("$"):
        # Если цена не начинается с $, добавляем его
        price_clean = price_text.replace('$', '').strip()
        if price_clean:
            price_text = f"${price_clean}"
    
    # Если покупка ждет реквизиты (и не завершена), показываем это вместо статуса rejected
    base_status = purchase.get('status', '')
    if purchase.get("waiting_card_details", False) and base_status != "completed":
        status_display = "Ждет реквизиты"
        status_emoji_display = "💳"
    else:
        status_display = status_text.get(base_status, base_status)
        status_emoji_display = status_emoji.get(base_status, '❓')
    
    text = (
        f"💿 <b>ПОКУПКА {purchase_num}</b>\n"
        f"📊 Статус: {status_emoji_display} <b>{status_display}</b>\n\n"
        f"👤 Пользователь: @{purchase['username']} (ID: {purchase['user_id']})\n"
        f"🎵 Бит: {purchase['beat']}\n"
        f"📜 Лицензия: {license_text}\n"
        f"💰 Цена: {price_text}\n"
    )
    
    if purchase.get("created_at"):
        created = datetime.fromisoformat(purchase["created_at"])
        text += f"📅 Создана: {created.strftime('%d.%m.%Y %H:%M')}\n"
    
    if purchase.get("payment_received_at"):
        paid = datetime.fromisoformat(purchase["payment_received_at"])
        text += f"💵 Оплата получена: {paid.strftime('%d.%m.%Y %H:%M')}\n"
    
    return text

def get_purchase_keyboard(purchase: dict) -> InlineKeyboardMarkup:
    """Создает клавиатуру для управления покупкой."""
    buttons = []
    
    status = purchase.get("status", "")
    waiting_card_details = purchase.get("waiting_card_details", False)
    card_details_sent = purchase.get("card_details_sent", False)
    logging.info(f"get_purchase_keyboard: Статус покупки №{purchase.get('id')}: '{status}', waiting_card_details: {waiting_card_details}, card_details_sent: {card_details_sent}")
    
    # Если покупка ждет реквизиты (и не завершена, и реквизиты еще не отправлены), показываем кнопку "Отправить реквизиты"
    # Проверяем ПЕРВЫМ, чтобы приоритет был выше, чем проверка статуса
    if waiting_card_details and status != "completed" and not card_details_sent:
        buttons.append([
            InlineKeyboardButton(
                text="📩 Отправить реквизиты",
                callback_data=f"send_card_{purchase['user_id']}"
            )
        ])
    # Если реквизиты уже отправлены или покупка не ждет реквизиты, показываем стандартные кнопки
    elif status == "pending_payment":
        buttons.append([
            InlineKeyboardButton(
                text="✅ Подтвердить оплату",
                callback_data=f"confirm_payment_{purchase['id']}_{purchase['user_id']}"
            )
        ])
        buttons.append([
            InlineKeyboardButton(
                text="❌ Отклонить оплату",
                callback_data=f"reject_payment_{purchase['id']}_{purchase['user_id']}"
            )
        ])
    elif status == "payment_received":
        buttons.append([
            InlineKeyboardButton(
                text="📤 Отправить файл",
                callback_data=f"send_file_{purchase['id']}"
            )
        ])
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("mark_paid_"))
async def mark_paid(callback: CallbackQuery):
    """Отметить покупку как оплаченную."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отмечать оплаты.", show_alert=True)
        return
    
    purchase_id = int(callback.data.split("_")[-1])
    
    from orders_manager import update_beats_purchase_status
    purchase = await update_beats_purchase_status(purchase_id, "payment_received")
    if purchase:
        await callback.message.edit_text(
            format_purchase_message(purchase),
            reply_markup=get_purchase_keyboard(purchase),
            parse_mode="HTML"
        )
        
        # Отправляем уведомление о том, что оплата отмечена
        await callback.message.answer(
            f"✅ Оплата отмечена для покупки №{purchase_id}\n"
            f"Клиент: @{purchase['username']} (ID: {purchase['user_id']})\n"
            f"Бит: {purchase['beat']}\n"
            f"Цена: {purchase['price']}"
        )
        
        await callback.answer("Оплата отмечена!")
    else:
        await callback.answer("Ошибка: покупка не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("send_file_"))
async def send_file(callback: CallbackQuery):
    """Запрос на отправку файла - отправляем сообщение в основной бот для загрузки файла."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отправлять файлы.", show_alert=True)
        return
    
    purchase_id = int(callback.data.split("_")[-1])
    
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    
    if not purchase:
        await callback.answer("Ошибка: покупка не найдена.", show_alert=True)
        return
    
    # Проверяем, что покупка уже подтверждена и еще не завершена
    current_status = purchase.get("status", "")
    if current_status != "payment_received":
        status_text = {
            "pending_payment": "Сначала подтвердите оплату.",
            "completed": "Файл уже отправлен. Покупка завершена.",
            "rejected": "Оплата была отклонена."
        }.get(current_status, "Покупка не готова к отправке файла.")
        await callback.answer(f"⚠️ {status_text}", show_alert=True)
        return
    
    # Проверяем, не отправляется ли уже файл для этой покупки
    if purchase_id in dp.admin_sending_file:
        await callback.answer("⚠️ Файл уже отправляется для этой покупки.", show_alert=True)
        return
    
    # Сохраняем информацию о том, что админ отправляет файл для этой покупки
    dp.admin_sending_file[purchase_id] = purchase['user_id']
    
    # Просим админа загрузить файл в этом боте
    # Форматируем лицензию и цену
    license_text, price_text = format_license_and_price(purchase.get('license', ''), purchase.get('price', ''))
    
    await callback.message.answer(
        f"📤 Отправьте файл для покупки №{purchase_id}\n\n"
        f"Клиент: @{purchase['username']} (ID: {purchase['user_id']})\n"
        f"Бит: {purchase['beat']}\n"
        f"Лицензия: {license_text}\n"
        f"Цена: {price_text}\n\n"
        f"Загрузите файл (mp3, wav или архив) в этом чате."
    )
    await callback.answer("✅ Теперь загрузите файл в этом чате.")

@dp.callback_query(F.data.startswith("send_card_"))
async def send_card_details_callback(callback: CallbackQuery):
    """Админ нажал кнопку 'Отправить реквизиты'."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отправлять реквизиты.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Находим самую новую покупку для отображения номера
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    purchase = None
    
    # Фильтруем покупки пользователя (не завершенные)
    user_purchases = [p for p in purchases if p["user_id"] == client_user_id and p.get("status") != "completed"]
    
    if user_purchases:
        # Сортируем по ID (самая новая - с наибольшим ID)
        user_purchases.sort(key=lambda x: x.get("id", 0), reverse=True)
        purchase = user_purchases[0]  # Берем самую новую покупку
    
    # Сохраняем, что админ сейчас будет отправлять реквизиты этому клиенту
    dp.admin_sending_card = client_user_id
    
    if purchase:
        # Проверяем, не были ли реквизиты уже отправлены
        if purchase.get("card_details_sent", False):
            await callback.answer("⚠️ Реквизиты для этой покупки уже были отправлены ранее.", show_alert=True)
            return
        
        # Форматируем лицензию и цену для отображения
        license_text, price_text = format_license_and_price(purchase.get('license', ''), purchase.get('price', ''))
        await callback.message.answer(
            f"💳 Отправка реквизитов для покупки №{purchase['id']}\n\n"
            f"👤 Клиент: @{purchase.get('username', 'no_username')} (ID: {client_user_id})\n"
            f"🎵 Бит: {purchase.get('beat', '-')}\n"
            f"📜 Лицензия: {license_text}\n"
            f"💰 Цена: {price_text}\n\n"
            f"Напиши реквизиты для оплаты картой:"
        )
    else:
        await callback.message.answer(
            f"💳 Отправка реквизитов\n\n"
            f"👤 Клиент ID: {client_user_id}\n\n"
            f"Напиши реквизиты для оплаты картой:"
    )
    await callback.answer()

@dp.message((F.from_user.id == ADMIN_ID) & (F.audio | F.document))
async def handle_admin_file(message: Message):
    """Обработка файлов от админа - отправка файла клиенту."""
    # Проверяем, что админ отправляет файл для какой-то покупки
    if not hasattr(dp, 'admin_sending_file') or not dp.admin_sending_file:
        return  # Админ не отправляет файл
    
    # Находим покупку, для которой админ отправляет файл
    purchase_id = None
    user_id = None
    for pid, uid in dp.admin_sending_file.items():
        purchase_id = pid
        user_id = uid
        break
    
    if not purchase_id or not user_id:
        await message.answer("Ошибка: не найдена информация о покупке. Нажмите кнопку 'Отправить файл' снова.")
        return
    
    # Получаем информацию о покупке
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    
    if not purchase:
        await message.answer("Ошибка: покупка не найдена.")
        dp.admin_sending_file.pop(purchase_id, None)
        return
    
    # Проверяем, что покупка еще не завершена (дополнительная проверка перед отправкой)
    current_status = purchase.get("status", "")
    if current_status == "completed":
        await message.answer("⚠️ Файл уже отправлен. Покупка завершена.")
        dp.admin_sending_file.pop(purchase_id, None)
        return
    
    if current_status != "payment_received":
        await message.answer(f"⚠️ Покупка не готова к отправке файла. Текущий статус: {current_status}")
        dp.admin_sending_file.pop(purchase_id, None)
        return
    
    # Дополнительная проверка перед отправкой (защита от race condition)
    purchase_check = await get_beats_purchase_by_id(purchase_id)
    if purchase_check and purchase_check.get("status") != "payment_received":
        await message.answer(f"⚠️ Статус покупки изменился. Текущий статус: {purchase_check.get('status')}")
        dp.admin_sending_file.pop(purchase_id, None)
        return
    
    # Отправляем файл клиенту через основной бот
    if main_bot:
        try:
            lang = await get_user_language(user_id)
            file_sent_text = ""  # Убираем текст "Ваш файл"/"Your file"
            contact_text = "Связаться" if lang == "ru" else "Contact"
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
            contact_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=contact_text, callback_data=f"contact_admin_{user_id}")]
                ]
            )
            
            # Скачиваем файл и отправляем через BufferedInputFile, так как file_id не работает между ботами
            if message.audio:
                # Скачиваем файл
                file_info = await bot.get_file(message.audio.file_id)
                file_data = await bot.download_file(file_info.file_path)
                file_bytes = file_data.read()
                
                # Отправляем через BufferedInputFile
                await main_bot.send_audio(
                    chat_id=user_id, 
                    audio=BufferedInputFile(file_bytes, filename=message.audio.file_name or "audio.mp3"), 
                    caption=None, 
                    reply_markup=contact_kb
                )
            elif message.document:
                # Скачиваем файл
                file_info = await bot.get_file(message.document.file_id)
                file_data = await bot.download_file(file_info.file_path)
                file_bytes = file_data.read()
                
                # Отправляем через BufferedInputFile
                await main_bot.send_document(
                    chat_id=user_id, 
                    document=BufferedInputFile(file_bytes, filename=message.document.file_name or "file"), 
                    caption=None, 
                    reply_markup=contact_kb
                )
            
            # Обновляем статус покупки на "completed" после отправки файла
            from orders_manager import update_beats_purchase_status
            updated_purchase = await update_beats_purchase_status(purchase_id, "completed")
            
            # Обновляем сообщение с чеком, если есть client_message_id
            if updated_purchase and updated_purchase.get("client_message_id"):
                try:
                    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                    client_message_id = updated_purchase.get("client_message_id")
                    
                    # Получаем текущее сообщение для обновления
                    try:
                        # Пробуем обновить caption/текст, убрав кнопки и добавив статус "Завершена"
                        try:
                            # Получаем текущий caption из покупки или используем дефолтный
                            caption = f"💿 Покупка бита {purchase_id}\n✅ Оплата подтверждена админом\n✅ Завершена"
                            await bot.edit_message_caption(
                                chat_id=ADMIN_ID,
                                message_id=client_message_id,
                                caption=caption,
                                reply_markup=None,
                                parse_mode="HTML"
                            )
                            logging.info(f"Обновлено сообщение с чеком (caption) для покупки №{purchase_id}")
                        except Exception as e2:
                            logging.debug(f"Не удалось обновить caption: {e2}")
                            # Пробуем обновить как текстовое сообщение
                            try:
                                text = f"💿 Покупка бита {purchase_id}\n✅ Оплата подтверждена админом\n✅ Завершена"
                                await bot.edit_message_text(
                                    chat_id=ADMIN_ID,
                                    message_id=client_message_id,
                                    text=text,
                                    reply_markup=None,
                                    parse_mode="HTML"
                                )
                                logging.info(f"Обновлено сообщение с чеком (текст) для покупки №{purchase_id}")
                            except Exception as e3:
                                logging.error(f"Не удалось обновить сообщение с чеком для покупки №{purchase_id}: {e3}")
                    except Exception as e:
                        logging.error(f"Ошибка при обновлении сообщения с чеком: {e}")
                except Exception as e:
                    logging.error(f"Ошибка при обработке client_message_id: {e}")
            
            # Убираем из ожидания
            dp.admin_sending_file.pop(purchase_id, None)
            
            await message.answer(f"✅ Файл отправлен клиенту!\n\n📦 Покупка {purchase_id}")
        except Exception as e:
            logging.error(f"Ошибка отправки файла клиенту: {e}")
            await message.answer(f"❌ Ошибка отправки файла: {str(e)}")
    else:
        await message.answer("❌ Основной бот не инициализирован. Файл не может быть отправлен.")

@dp.message((F.from_user.id == ADMIN_ID) & F.text)
async def handle_admin_card_message(message: Message):
    """Обработка текстовых сообщений от админа - отправка реквизитов карты клиенту."""
    # Пропускаем файлы - их обработает handle_admin_file
    if message.audio or message.document or message.voice or message.photo:
        return
    
    # Проверяем, что админ отправляет реквизиты
    if not hasattr(dp, 'admin_sending_card') or dp.admin_sending_card is None:
        return  # Админ не отправляет реквизиты - пропускаем другим обработчикам
    
    client_user_id = dp.admin_sending_card
    
    # Отправляем реквизиты клиенту через основной бот
    if main_bot:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            # Определяем язык клиента
            lang = await get_user_language(client_user_id)
            
            paid_button_ru = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Я оплатил", callback_data="paid")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
                ]
            )
            paid_button_en = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="✅ I paid", callback_data="paid")],
                    [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")]
                ]
            )
            
            paid_button = paid_button_ru if lang == "ru" else paid_button_en
            
            msg = await main_bot.send_message(
                client_user_id,
                message.text,
                reply_markup=paid_button,
            )
            
            # Сохраняем message_id и текст сообщения с реквизитами для последующего удаления кнопок
            # Используем файл для передачи данных в основной бот
            import json
            payment_details_file = "payment_details.json"
            try:
                if os.path.exists(payment_details_file):
                    with open(payment_details_file, "r", encoding="utf-8") as f:
                        details = json.load(f)
                else:
                    details = {}
                
                details[str(client_user_id)] = {
                    "payment_details_message_id": msg.message_id,
                    "payment_details_message_text": message.text,
                    "timestamp": asyncio.get_event_loop().time()
                }
                
                with open(payment_details_file, "w", encoding="utf-8") as f:
                    json.dump(details, f, ensure_ascii=False, indent=2)
                logging.info(f"beats_purchases_bot: Сохранено payment_details_message_id={msg.message_id} для пользователя {client_user_id}")
            except Exception as e:
                logging.error(f"Ошибка сохранения payment_details_message_id: {e}")
            
            # Убираем флаг ожидания реквизитов из покупки
            # Ищем последнюю покупку пользователя (любого статуса, кроме completed)
            from orders_manager import get_all_beats_purchases, get_beats_purchase_by_user_id, update_beats_purchase_status
            purchase = await get_beats_purchase_by_user_id(client_user_id)
            
            if purchase and purchase.get("status") != "completed":
                # Проверяем, не были ли реквизиты уже отправлены
                if purchase.get("card_details_sent", False):
                    await message.answer(f"⚠️ Реквизиты для покупки №{purchase['id']} уже были отправлены ранее.")
                    dp.admin_sending_card = None
                    return
                
                # Обновляем покупку через update_beats_purchase_status
                # Не сбрасываем статус payment_rejected - отмененная покупка должна остаться отмененной
                # Обновляем через update_beats_purchase_status (убираем waiting_card_details, устанавливаем card_details_sent)
                await update_beats_purchase_status(purchase["id"], purchase.get("status", "pending_payment"), waiting_card_details=0, card_details_sent=1)
                logging.info(f"Убран флаг waiting_card_details и установлен card_details_sent для покупки №{purchase['id']}")
            
            # Убираем из ожидания
            dp.admin_sending_card = None
            
            # Подтверждение админу с номером покупки
            if purchase:
                await message.answer(f"✅ Реквизиты отправлены клиенту.\n\n📦 Покупка {purchase['id']}")
            else:
                await message.answer("✅ Реквизиты отправлены клиенту.")
        except Exception as e:
            logging.error(f"Ошибка отправки реквизитов клиенту: {e}")
            await message.answer(f"❌ Ошибка отправки реквизитов: {str(e)}")
    else:
        await message.answer("❌ Основной бот не инициализирован. Реквизиты не могут быть отправлены.")

async def send_purchase_to_bot(purchase: dict):
    """Отправляет покупку в бот покупок. Вызывается из основного бота."""
    purchase_text = format_purchase_message(purchase)
    kb = get_purchase_keyboard(purchase)
    
    msg = await bot.send_message(chat_id=PURCHASES_CHAT_ID, text=purchase_text, reply_markup=kb, parse_mode="HTML")
    # Сохраняем ID сообщения в покупке
    from orders_manager import update_beats_purchase_status
    await update_beats_purchase_status(purchase["id"], purchase["status"], client_message_id=msg.message_id)

@dp.callback_query(F.data.startswith("offer_accept_"))
async def accept_offer_callback(callback: CallbackQuery):
    """Админ принял предложение по цене для покупки готового бита."""
    logging.info(f"Обработчик offer_accept_ вызван. Callback data: {callback.data}")
    
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать предложения.", show_alert=True)
        return
    
    try:
        user_id = int(callback.data.split("_", maxsplit=2)[2])
        logging.info(f"User ID извлечен: {user_id}")
    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка извлечения user_id из callback.data: {e}")
        await callback.answer("Ошибка обработки запроса.", show_alert=True)
        return
    
    # Получаем предложение из сообщения
    message_text = callback.message.text or ""
    # Парсим данные из сообщения
    lines = message_text.split("\n")
    beat = ""
    lic = ""
    price = ""
    for line in lines:
        if line.startswith("Beat:"):
            beat = line.replace("Beat:", "").strip()
        elif line.startswith("License:"):
            lic = line.replace("License:", "").strip()
        elif line.startswith("Предложенная цена:"):
            price = line.replace("Предложенная цена:", "").strip()
    
    if not price:
        await callback.answer("Не удалось найти цену в сообщении.", show_alert=True)
        return
    
    # Отправляем сообщение клиенту через основной бот и обновляем purchase_state
    if main_bot:
        try:
            # Определяем язык клиента
            lang = await get_user_language(user_id)
            
            # Обновляем purchase_state в основном боте через файл или напрямую
            # Для этого нужно импортировать или использовать общий механизм
            # Пока просто отправляем сообщение, а обновление purchase_state произойдет при оплате
            
            # Форматируем лицензию - убираем цену из лицензии
            # Если лицензия содержит " — ", разделяем на тип лицензии и цену
            if " — " in lic:
                license_type = lic.split(" — ", 1)[0].strip()
            else:
                license_type = lic
            
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            price_clean = price.replace('$', '').strip()
            price_display = f"${price_clean}" if price_clean and price_clean != '-' else price
            
            if lang == "ru":
                client_text = (
                    f"✅ Отлично! Я принял твоё предложение по цене.\n\n"
                    f"Бит: {beat}\n"
                    f"Лицензия: {license_type}\n"
                    f"Цена: {price_display}\n\n"
                    "Теперь выбери способ оплаты:"
                )
            else:
                client_text = (
                    f"✅ Great! I've accepted your price offer.\n\n"
                    f"Beat: {beat}\n"
                    f"License: {license_type}\n"
                    f"Price: {price_display}\n\n"
                    "Now choose the payment method:"
                )
            
            # Создаем кнопки оплаты
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            
            payment_inline_ru = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto"),
                        InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal"),
                    ],
                    [
                        InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp"),
                        InlineKeyboardButton(text="🏦 Карта", callback_data="pay_card"),
                    ],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
                ]
            )
            
            payment_inline_en = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto"),
                        InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal"),
                    ],
                    [
                        InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp"),
                        InlineKeyboardButton(text="🏦 Card transfer", callback_data="pay_card"),
                    ],
                    [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")]
                ]
            )
            
            payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
            
            # Отправляем сообщение клиенту с кнопками оплаты
            msg = await main_bot.send_message(user_id, client_text, reply_markup=payment_kb)
            
            # Сохраняем принятую цену и message_id в файл для обновления purchase_state в основном боте
            # Используем JSON файл для обмена данными между ботами
            import json
            price_update_file = "accepted_price.json"
            try:
                # Читаем существующие обновления
                if os.path.exists(price_update_file):
                    with open(price_update_file, "r", encoding="utf-8") as f:
                        updates = json.load(f)
                else:
                    updates = {}
                
                # Добавляем обновление для этого пользователя
                # Сохраняем цену с символом $ для единообразия
                price_with_dollar = price if price.startswith("$") else f"${price}"
                updates[str(user_id)] = {
                    "price": price_with_dollar,
                    "beat": beat,
                    "license": lic,  # Сохраняем исходную лицензию (например, "TRACK OUT — $99")
                    "payment_selection_message_id": msg.message_id,  # Сохраняем message_id сообщения с выбором способа оплаты
                    "payment_selection_message_text": client_text,  # Сохраняем текст сообщения
                    "timestamp": asyncio.get_event_loop().time()
                }
                
                # Сохраняем обратно
                with open(price_update_file, "w", encoding="utf-8") as f:
                    json.dump(updates, f, ensure_ascii=False, indent=2)
                logging.info(f"beats_purchases_bot: Сохранено payment_selection_message_id={msg.message_id} для пользователя {user_id}")
            except Exception as e:
                logging.error(f"Ошибка сохранения принятой цены: {e}")
            
            # Обновляем сообщение в боте покупок
            await callback.message.edit_text(
                f"{message_text}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
            )
            await callback.answer("✅ Цена принята. Клиенту отправлено уведомление.")
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
            await callback.answer("❌ Ошибка отправки сообщения клиенту", show_alert=True)
    else:
        await callback.answer("❌ Основной бот не инициализирован", show_alert=True)

@dp.callback_query(F.data.startswith("offer_reject_"))
async def reject_offer_callback(callback: CallbackQuery):
    """Админ отклонил предложение по цене для покупки готового бита."""
    logging.info(f"Обработчик offer_reject_ вызван. Callback data: {callback.data}")
    
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять предложения.", show_alert=True)
        return
    
    try:
        user_id = int(callback.data.split("_", maxsplit=2)[2])
        logging.info(f"User ID извлечен: {user_id}")
    except (ValueError, IndexError) as e:
        logging.error(f"Ошибка извлечения user_id из callback.data: {e}")
        await callback.answer("Ошибка обработки запроса.", show_alert=True)
        return
    
    # Отправляем сообщение клиенту через основной бот
    if main_bot:
        try:
            lang = await get_user_language(user_id)
            
            if lang == "ru":
                client_text = (
                    "❌ К сожалению, я не могу принять твоё предложение по цене.\n\n"
                    "Можешь попробовать предложить другую цену или выбрать стандартную цену из каталога."
                )
            else:
                client_text = (
                    "❌ Unfortunately, I can't accept your price offer.\n\n"
                    "You can try to make another offer or choose the standard price from the catalog."
                )
            
            await main_bot.send_message(user_id, client_text)
            
            # Обновляем сообщение в боте покупок
            message_text = callback.message.text or ""
            await callback.message.edit_text(
                f"{message_text}\n\n❌ Цена отклонена. Клиенту отправлено сообщение."
            )
            await callback.answer("❌ Цена отклонена. Клиенту отправлено уведомление.")
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
            await callback.answer("❌ Ошибка отправки сообщения клиенту", show_alert=True)
    else:
        await callback.answer("❌ Основной бот не инициализирован", show_alert=True)

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment(callback: CallbackQuery):
    """Подтвердить оплату покупки."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может подтверждать оплаты.", show_alert=True)
        return
    
    # Формат: confirm_payment_{purchase_id}_{user_id}
    parts = callback.data.split("_")
    if len(parts) >= 4:
        purchase_id = int(parts[2])
        user_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import get_beats_purchase_by_id, update_beats_purchase_status
    
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    if not purchase:
        await callback.answer("Ошибка: покупка не найдена.", show_alert=True)
        return
    
    # Проверяем, что покупка еще не подтверждена
    current_status = purchase.get("status", "")
    if current_status != "pending_payment":
        status_text = {
            "payment_received": "Оплата уже подтверждена.",
            "completed": "Покупка уже завершена.",
            "payment_rejected": "Оплата была отклонена.",
            "cancelled_by_client": "Покупка была отменена клиентом.",
            "rejected": "Оплата была отклонена."
        }.get(current_status, "Покупка уже обработана.")
        await callback.answer(f"⚠️ {status_text}", show_alert=True)
        return
    
    # Дополнительная проверка перед обновлением (защита от race condition)
    purchase_check = await get_beats_purchase_by_id(purchase_id)
    if purchase_check and purchase_check.get("status") != "pending_payment":
        await callback.answer("⚠️ Оплата уже подтверждена другим запросом.", show_alert=True)
        return
    
    # Обновляем статус покупки
    from orders_manager import update_beats_purchase_status
    await update_beats_purchase_status(purchase_id, "payment_received")
    
    # Отправляем сообщение клиенту
    if main_bot:
        try:
            # Получаем язык пользователя
            lang = await get_user_language(user_id)
            
            client_text = (
                "✅ Оплата подтверждена. В скором времени вы получите ваш файл."
                if lang == "ru"
                else "✅ Payment confirmed. You'll receive your file soon."
            )
            await main_bot.send_message(user_id, client_text)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    # Уведомление админу с номером покупки
    await callback.answer(f"✅ Оплата подтверждена! Покупка {purchase_id}. Клиенту отправлено уведомление.")
    
    # Обновляем сообщение с чеком, сохраняя фото и текст, и добавляя статус
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    if purchase:
        # Получаем текущий текст/caption сообщения
        current_caption = callback.message.caption or callback.message.text or ""
        
        # Убираем старый статус, если есть
        current_caption = current_caption.replace("⏳ Ожидает подтверждения оплаты", "")
        current_caption = current_caption.replace("✅ Оплата подтверждена. Ожидает отправки файла.", "")
        current_caption = current_caption.replace("✅ Оплата подтверждена админом", "")
        
        # Добавляем новый статус
        status_text = "\n\n✅ Оплата подтверждена админом"
        new_caption = current_caption.strip() + status_text
        
        # Создаем клавиатуру с кнопкой "Отправить файл"
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        send_file_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📤 Отправить файл", callback_data=f"send_file_{purchase_id}")]
            ]
        )
        
        # Обновляем сообщение в зависимости от типа
        try:
            if callback.message.photo:
                # Сохраняем фото и обновляем caption
                await callback.message.edit_caption(caption=new_caption, reply_markup=send_file_kb)
            elif callback.message.document:
                # Сохраняем документ и обновляем caption
                await callback.message.edit_caption(caption=new_caption, reply_markup=send_file_kb)
            else:
                # Текстовое сообщение
                await callback.message.edit_text(new_caption, reply_markup=send_file_kb)
        except Exception as e:
            logging.error(f"Ошибка при обновлении сообщения с чеком: {e}")
            # Если не удалось обновить, пробуем просто убрать кнопки подтверждения
            try:
                if callback.message.photo:
                    await callback.message.edit_caption(caption=new_caption)
                elif callback.message.document:
                    await callback.message.edit_caption(caption=new_caption)
                else:
                    await callback.message.edit_text(new_caption)
            except Exception as e2:
                logging.error(f"Ошибка при обновлении сообщения без кнопок: {e2}")

@dp.callback_query(F.data.startswith("view_purchase_"))
async def view_purchase(callback: CallbackQuery):
    """Просмотр конкретной покупки по номеру."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может просматривать покупки.", show_alert=True)
        return
    
    purchase_id = int(callback.data.split("_")[-1])
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    if not purchase:
        await callback.answer("Покупка не найдена.", show_alert=True)
        return
    
    purchase_text = format_purchase_message(purchase)
    kb = get_purchase_keyboard(purchase)
    
    await callback.message.answer(purchase_text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("purchases_page_"))
async def handle_purchases_page(callback: CallbackQuery):
    """Обработка переключения страниц списка покупок."""
    if callback.from_user.id != ADMIN_ID:
        return
    
    page = int(callback.data.split("_")[-1])
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    if not purchases:
        await callback.answer("Покупок пока нет.", show_alert=True)
        return
    
    # Сортируем по дате создания (новые первые)
    from datetime import datetime
    purchases_sorted = sorted(purchases, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(purchases_sorted) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(purchases_sorted))
    
    # Показываем компактный список с кнопками
    text = f"📋 <b>Все покупки ({len(purchases)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in purchases_sorted[start_idx:end_idx]:
        status_emoji = {
            "pending_payment": "🔍",
            "payment_received": "💰",
            "completed": "✅"
        }
        created = ""
        if purchase.get("created_at"):
            try:
                created_dt = datetime.fromisoformat(purchase["created_at"])
                created = created_dt.strftime("%d.%m %H:%M")
            except:
                pass
        
        emoji = status_emoji.get(purchase.get('status', ''), '❓')
        # Если покупка ждет реквизиты, показываем соответствующий эмодзи
        if purchase.get("waiting_card_details", False):
            emoji = "💳"
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, emoji),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"purchases_page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"purchases_page_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data == "back_to_list")
async def back_to_list(callback: CallbackQuery):
    """Возврат к списку покупок."""
    if callback.from_user.id != ADMIN_ID:
        return
    
    # Используем логику из cmd_purchases для показа списка всех покупок
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    if not purchases:
        await callback.message.edit_text("Покупок пока нет.")
        await callback.answer()
        return
    
    # Сортируем по дате создания (новые первые)
    from datetime import datetime
    purchases_sorted = sorted(purchases, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(purchases_sorted) + items_per_page - 1) // items_per_page
    page = 0  # Всегда начинаем с первой страницы
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(purchases_sorted))
    
    # Показываем компактный список с кнопками
    text = f"📋 <b>Все покупки ({len(purchases)})</b>\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in purchases_sorted[start_idx:end_idx]:
        status_emoji = {
            "pending_payment": "🔍",
            "payment_received": "💰",
            "completed": "✅"
        }
        emoji = status_emoji.get(purchase.get('status', ''), '❓')
        # Если покупка ждет реквизиты, показываем соответствующий эмодзи
        if purchase.get("waiting_card_details", False):
            emoji = "💳"
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, emoji),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"purchases_page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"purchases_page_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("filter_"))
async def filter_purchases(callback: CallbackQuery):
    """Фильтрация покупок по статусу."""
    if callback.from_user.id != ADMIN_ID:
        return
    
    # Парсим filter_type и page из callback_data
    # Формат: filter_all, filter_pending, filter_paid_0, filter_paid_1 и т.д.
    parts = callback.data.split("_")
    if len(parts) == 2:
        # filter_all, filter_pending, filter_paid, filter_waiting, filter_completed
        filter_type = parts[1]
        page = 0
    elif len(parts) >= 3:
        # filter_paid_0, filter_pending_1 и т.д.
        filter_type = parts[1]
        try:
            page = int(parts[2])
        except ValueError:
            page = 0
    else:
        filter_type = "all"
        page = 0
    
    from orders_manager import get_all_beats_purchases
    purchases = await get_all_beats_purchases()
    
    if filter_type == "pending":
        filtered = [p for p in purchases if p["status"] == "pending_payment"]
        status_text = "🔍 На проверке"
    elif filter_type == "card":
        # Показываем только те покупки, которые ждут реквизиты И реквизиты еще не отправлены
        filtered = [p for p in purchases if p.get("waiting_card_details", False) and not p.get("card_details_sent", False)]
        status_text = "💳 Ждет реквизиты"
    elif filter_type == "paid":
        filtered = [p for p in purchases if p["status"] == "payment_received"]
        status_text = "💰 Оплачены"
    elif filter_type == "waiting":
        filtered = [p for p in purchases if p["status"] == "payment_received"]
        status_text = "⏳ Ждет отправки"
    elif filter_type == "completed":
        filtered = [p for p in purchases if p["status"] == "completed"]
        status_text = "✅ Завершены"
    else:  # all
        filtered = purchases
        status_text = "📋 Все покупки"
    
    if not filtered:
        await callback.answer(f"Нет покупок со статусом '{status_text}'", show_alert=True)
        return
    
    # Сортируем по дате (новые первые)
    from datetime import datetime
    filtered_sorted = sorted(filtered, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 покупок на страницу
    items_per_page = 10
    total_pages = (len(filtered_sorted) + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, len(filtered_sorted))
    
    text = f"{status_text} ({len(filtered)})\n"
    text += f"<i>Страница {page + 1} из {total_pages}</i>\n\n"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    buttons = []
    for purchase in filtered_sorted[start_idx:end_idx]:
        # Определяем эмодзи в зависимости от типа фильтра
        if filter_type == "card":
            emoji = "💳"
        elif filter_type == "completed":
            emoji = "✅"
        elif filter_type == "pending":
            emoji = "🔍"
        elif filter_type == "paid" or filter_type == "waiting":
            emoji = "💰"
        else:
            # Для всех остальных используем эмодзи по статусу
            status_emoji = {
                "pending_payment": "🔍",
                "payment_received": "💰",
                "completed": "✅"
            }
            emoji = status_emoji.get(purchase.get('status', ''), '❓')
            # Если покупка ждет реквизиты, показываем соответствующий эмодзи
            if purchase.get("waiting_card_details", False):
                emoji = "💳"
        buttons.append([
            InlineKeyboardButton(
                text=format_compact_button_text(purchase, emoji),
                callback_data=f"view_purchase_{purchase['id']}"
            )
        ])
    
    # Кнопки навигации
    nav_buttons = []
    if total_pages > 1:
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"filter_{filter_type}_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(InlineKeyboardButton(text="▶️ Вперед", callback_data=f"filter_{filter_type}_{page + 1}"))
        if nav_buttons:
            buttons.append(nav_buttons)
    
    # Кнопка возврата к списку
    buttons.append([
        InlineKeyboardButton(
            text="🔙 К списку покупок",
            callback_data="back_to_list"
        )
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("complete_purchase_"))
async def complete_purchase(callback: CallbackQuery):
    """Завершить покупку."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может завершать покупки.", show_alert=True)
        return
    
    purchase_id = int(callback.data.split("_")[-1])
    from orders_manager import update_beats_purchase_status, get_beats_purchase_by_id
    
    from orders_manager import update_beats_purchase_status
    purchase = await update_beats_purchase_status(purchase_id, "completed")
    if purchase:
        purchase_text = format_purchase_message(purchase)
        kb = get_purchase_keyboard(purchase)
        
        # Обновляем сообщение в зависимости от типа (фото/документ/текст)
        try:
            if callback.message.photo:
                # Сообщение с фото - обновляем caption
                current_caption = callback.message.caption or ""
                # Убираем старые статусы и добавляем новый
                new_caption = current_caption.replace("⏳ Ожидает подтверждения оплаты", "")
                new_caption = new_caption.replace("✅ Оплата подтверждена админом", "")
                new_caption = new_caption.replace("📤 Файл отправлен", "")
                new_caption = new_caption.strip()
                if new_caption and not new_caption.endswith("✅ Завершена"):
                    new_caption += "\n\n✅ Завершена"
                elif not new_caption:
                    new_caption = f"💿 Покупка бита {purchase_id}\n\n✅ Завершена"
                
                await callback.message.edit_caption(caption=new_caption, reply_markup=kb, parse_mode="HTML")
            elif callback.message.document:
                # Сообщение с документом - обновляем caption
                current_caption = callback.message.caption or ""
                # Убираем старые статусы и добавляем новый
                new_caption = current_caption.replace("⏳ Ожидает подтверждения оплаты", "")
                new_caption = new_caption.replace("✅ Оплата подтверждена админом", "")
                new_caption = new_caption.replace("📤 Файл отправлен", "")
                new_caption = new_caption.strip()
                if new_caption and not new_caption.endswith("✅ Завершена"):
                    new_caption += "\n\n✅ Завершена"
                elif not new_caption:
                    new_caption = f"💿 Покупка бита {purchase_id}\n\n✅ Завершена"
                
                await callback.message.edit_caption(caption=new_caption, reply_markup=kb, parse_mode="HTML")
            else:
                # Текстовое сообщение - обновляем текст
                await callback.message.edit_text(purchase_text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка при обновлении сообщения в complete_purchase: {e}")
            # Пробуем просто обновить кнопки
            try:
                if callback.message.photo or callback.message.document:
                    await callback.message.edit_reply_markup(reply_markup=kb)
                else:
                    await callback.message.edit_reply_markup(reply_markup=kb)
            except Exception as e2:
                logging.error(f"Ошибка при обновлении кнопок в complete_purchase: {e2}")
        
        await callback.answer("✅ Покупка завершена!")
    else:
        await callback.answer("Ошибка: покупка не найдена.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment(callback: CallbackQuery):
    """Отклонить оплату покупки."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять оплаты.", show_alert=True)
        return
    
    # Формат: reject_payment_{purchase_id}_{user_id}
    parts = callback.data.split("_")
    if len(parts) >= 4:
        purchase_id = int(parts[2])
        user_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import get_beats_purchase_by_id, update_beats_purchase_status
    
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    if not purchase:
        await callback.answer("Ошибка: покупка не найдена.", show_alert=True)
        return
    
    # Проверяем, что покупка еще не обработана
    current_status = purchase.get("status", "")
    if current_status != "pending_payment":
        status_text = {
            "payment_received": "Оплата уже подтверждена.",
            "completed": "Покупка уже завершена.",
            "payment_rejected": "Оплата уже отклонена.",
            "cancelled_by_client": "Покупка уже отменена клиентом."
        }.get(current_status, "Покупка уже обработана.")
        await callback.answer(f"⚠️ {status_text}", show_alert=True)
        return
    
    # Дополнительная проверка перед обновлением (защита от race condition)
    purchase_check = await get_beats_purchase_by_id(purchase_id)
    if purchase_check and purchase_check.get("status") != "pending_payment":
        await callback.answer("⚠️ Статус покупки уже изменен.", show_alert=True)
        return
    
    # Отправляем сообщение клиенту
    if main_bot:
        try:
            lang = await get_user_language(user_id)
            client_text = (
                "❌ К сожалению, оплата не подтверждена. Пожалуйста, проверьте реквизиты и попробуйте снова."
                if lang == "ru"
                else "❌ Unfortunately, payment was not confirmed. Please check the details and try again."
            )
            await main_bot.send_message(user_id, client_text)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    # Обновляем статус покупки
    from orders_manager import update_beats_purchase_status
    await update_beats_purchase_status(purchase_id, "payment_rejected")
    
    # Уведомление админу с номером покупки
    await callback.answer(f"❌ Оплата отклонена. Покупка {purchase_id}. Клиенту отправлено уведомление.")
    
    # Обновляем сообщение с чеком, убирая кнопки
    from orders_manager import get_beats_purchase_by_id
    purchase = await get_beats_purchase_by_id(purchase_id)
    if purchase:
        # Получаем текущий текст/caption сообщения
        current_caption = callback.message.caption or callback.message.text or ""
        
        # Убираем старый статус, если есть
        current_caption = current_caption.replace("⏳ Ожидает подтверждения оплаты", "")
        current_caption = current_caption.replace("✅ Оплата подтверждена админом", "")
        
        # Добавляем новый статус
        status_text = "\n\n❌ Оплата отклонена"
        new_caption = current_caption.strip() + status_text
        
        # Обновляем сообщение без кнопок
        try:
            if callback.message.photo:
                await callback.message.edit_caption(caption=new_caption, reply_markup=None)
            elif callback.message.document:
                await callback.message.edit_caption(caption=new_caption, reply_markup=None)
            else:
                await callback.message.edit_text(text=new_caption, reply_markup=None)
        except Exception as e:
            logging.error(f"Ошибка при обновлении сообщения с чеком: {e}")

async def main():
    """Запуск бота."""
    from database import init_db
    
    # Инициализируем БД при запуске
    await init_db()
    
    logging.info("Запуск бота для покупок...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

