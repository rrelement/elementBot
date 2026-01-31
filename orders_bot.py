"""
Бот для управления заказами (сведение и бит на заказ).
Получает заказы из основного бота и позволяет админу управлять ими.
"""
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from dotenv import load_dotenv
import os
# Все функции теперь асинхронные
# Импортируем по мере необходимости в функциях
from payment_logger import log_payment, update_payment_log_status

load_dotenv()

logging.basicConfig(level=logging.INFO)

# Токен бота для заказов (нужно будет указать в .env)
ORDERS_BOT_TOKEN = os.getenv("ORDERS_BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "830030557"))
MAIN_BOT_TOKEN = os.getenv("TOKEN", "8588087035:AAGSyPJesse5NnbIx98wovIeJGtQGUThJsw")  # Токен основного бота

if not ORDERS_BOT_TOKEN:
    raise ValueError("ORDERS_BOT_TOKEN не найден в .env файле")

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

bot = Bot(token=ORDERS_BOT_TOKEN, session=session)
dp = Dispatcher()

# Отслеживание, какой заказ админ отправляет (order_id -> (order_type, user_id))
dp.admin_sending_file = {}  # {order_id: (order_type, user_id)}

# Состояния для ожидания суммы заказа
dp.waiting_partner_price = {}  # {user_id: (order_id, order_type)} - партнер должен указать сумму
dp.waiting_client_price = {}  # {user_id: (order_id, order_type)} - клиент должен указать сумму

# Основной бот для отправки сообщений клиентам
main_bot = None
if MAIN_BOT_TOKEN:
    try:
        main_bot = Bot(token=MAIN_BOT_TOKEN, session=session)
        logging.info("Основной бот инициализирован для отправки сообщений клиентам.")
    except Exception as e:
        logging.error(f"Ошибка инициализации основного бота: {e}")

# ID чата, куда будут приходить заказы (можно использовать ADMIN_ID или создать канал)
ORDERS_CHAT_ID = ADMIN_ID  # По умолчанию личка админа, можно изменить на ID канала

@dp.message(Command("register"))
async def cmd_register(message: Message):
    """Регистрация партнера."""
    from partners_manager import get_partner, get_partner_request
    
    user_id = message.from_user.id
    username = message.from_user.username or "no_username"
    
    # Проверяем, не является ли уже партнером
    if await get_partner(user_id):
        await message.answer("Вы уже зарегистрированы как партнер.")
        return
    
    # Проверяем, нет ли уже активной заявки
    existing_request = await get_partner_request(user_id)
    if existing_request:
        await message.answer(
            "У вас уже есть активная заявка на регистрацию. "
            "Ожидайте рассмотрения администратором."
        )
        return
    
    # Создаем инлайн-клавиатуру для регистрации
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Зарегистрироваться",
                callback_data=f"register_confirm_{user_id}"
            )
        ]
    ])
    
    await message.answer(
        "Салют! Для регистрации в качестве партнера нажми кнопку ниже.\n\n"
        "После регистрации ты сможешь принимать заказы на биты и сведение.\n\n"
        "Твоя заявка будет отправлена администратору на рассмотрение.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "register_prompt")
async def handle_register_prompt(callback: CallbackQuery):
    """Обработка кнопки регистрации."""
    await cmd_register(callback.message)
    await callback.answer()

@dp.callback_query(F.data.startswith("register_confirm_"))
async def handle_register_type(callback: CallbackQuery):
    """Обработка подтверждения регистрации партнера."""
    user_id = callback.from_user.id
    
    # Формат: register_confirm_123456789
    parts = callback.data.split("_")
    if len(parts) < 3:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    request_user_id = int(parts[2]) if len(parts) > 2 else user_id
    
    # Проверяем, что пользователь регистрирует сам себя
    if request_user_id != user_id:
        await callback.answer("Вы можете регистрировать только себя.", show_alert=True)
        return
    
    username = callback.from_user.username or "no_username"
    name = callback.from_user.first_name or username
    
    # Создаем заявку (без типа, так как партнер может делать и биты, и сведение)
    from partners_manager import create_partner_request
    success = await create_partner_request(
        user_id=user_id,
        username=username,
        partner_type="partner",  # Универсальный тип
        name=name
    )
    
    if not success:
        await callback.answer("Ошибка при создании заявки.", show_alert=True)
        return
    
    # Уведомляем пользователя
    await callback.message.edit_text(
        f"✅ Заявка на регистрацию отправлена!\n\n"
        f"Администратор рассмотрит твою заявку в ближайшее время. "
        f"Ты получишь уведомление о результате."
    )
    await callback.answer("Заявка отправлена!")
    
    # Уведомляем админа
    try:
        admin_text = (
            f"📝 <b>Новая заявка на регистрацию партнера</b>\n\n"
            f"👤 Пользователь: @{username} (ID: {user_id})\n"
            f"📛 Имя: {name}\n\n"
            f"Используй команды для управления:\n"
            f"/partner_requests - список заявок\n"
            f"/approve_partner {user_id} - одобрить\n"
            f"/reject_partner {user_id} - отклонить"
        )
        await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления админу: {e}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    """Команда /start для админа и партнеров."""
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    user_id = message.from_user.id
    
    # Если это партнер, показываем ему информацию
    partner = await get_partner(user_id)
    if partner:
        orders = await get_all_orders()
        my_orders = []
        for o in orders:
            if o.get("partner_id") == user_id:
                my_orders.append(o)
        
        # В работе: только заказы, которые реально в работе (не ожидают сумму)
        in_work = [o for o in my_orders if o["status"] in ["accepted", "in_progress", "first_payment_received"]]
        # Для партнера: выполненные заказы = "completed" ИЛИ ("awaiting_price" + есть partner_price)
        completed = [
            o for o in my_orders 
            if o["status"] == "completed" 
            or (o["status"] == "awaiting_price" and o.get("partner_price") is not None)
        ]
        
        text = (
            f"👨‍💼 <b>Привет, {partner.get('name', partner.get('username'))}!</b>\n\n"
            f"📦 Всего заказов: {len(my_orders)}\n"
            f"🔨 В работе: {len(in_work)}\n"
            f"✅ Выполнено: {len(completed)}\n\n"
            f"Новые заказы на биты и сведение будут приходить автоматически."
        )
        
        from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
        menu_kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📦 Мои заказы"), KeyboardButton(text="🔨 В работе")],
                [KeyboardButton(text="✅ Выполнены")]
            ],
            resize_keyboard=True
        )
        
        await message.answer(text, reply_markup=menu_kb, parse_mode="HTML")
        return
    
    # Если это админ
    if user_id != ADMIN_ID:
        await message.answer("Этот бот доступен только администратору и партнерам.")
        return
    
    orders = await get_all_orders()
    
    # Статистика
    pending = len([o for o in orders if o["status"] == "pending"])
    accepted = len([o for o in orders if o["status"] == "accepted"])
    in_progress = len([o for o in orders if o["status"] == "in_progress"])
    first_payment = len([o for o in orders if o["status"] == "first_payment_received"])
    completed = len([o for o in orders if o["status"] == "completed"])
    rejected = len([o for o in orders if o["status"] == "rejected"])
    
    text = (
        "🤖 <b>Бот для управления заказами</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"⏳ Ожидают принятия: {pending}\n"
        f"📋 Приняты: {accepted}\n"
        f"🔨 В работе: {in_progress}\n"
        f"✅ Выполнены: {completed}\n"
        f"❌ Отклонены: {rejected}\n"
        f"📦 Всего заказов: {len(orders)}\n\n"
        f"<b>Команды:</b>\n"
        f"/orders - все заказы\n"
        f"/pending - ожидающие\n"
        f"/in_progress - в работе\n"
        f"/stats - подробная статистика\n"
        f"/partners - управление партнерами\n"
        f"/menu - главное меню"
    )
    
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    menu_kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📋 Все заказы"), KeyboardButton(text="⏳ Ожидающие")],
            [KeyboardButton(text="🔨 В работе"), KeyboardButton(text="👨‍💼 Заказы партнеров")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="✅ Выполненные")]
        ],
        resize_keyboard=True
    )
    
    await message.answer(text, reply_markup=menu_kb, parse_mode="HTML")

@dp.message(Command("orders"))
async def cmd_orders(message: Message, page: int = 0):
    """Показать все заказы с пагинацией."""
    from orders_manager import get_all_orders
    
    if message.from_user.id != ADMIN_ID:
        return
    
    orders = await get_all_orders()
    if not orders:
        await message.answer("Заказов пока нет.")
        return
    
    # Сортируем по дате создания (новые первые)
    orders_sorted = sorted(orders, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Статусы на русском
    status_text = {
        "pending": "⏳ Ждет",
        "accepted": "📋 Принят",
        "in_progress": "🔨 В работе",
        "first_payment_received": "💰 Оплата",
        "awaiting_price": "💰 Сумма",
        "completed": "✅ Выполнен",
        "rejected": "❌ Отклонен",
        "cancelled": "❌ Отменен"
    }
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(orders_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(orders_sorted))
    
    text = f"📋 <b>Все заказы ({len(orders_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы
    for order in orders_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        status = status_text.get(order["status"], order["status"])
        text += f"📦 {order_type} {order['id']} | {status}\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца: 5 слева, 5 справа)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if orders_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in orders_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=f"view_order_{order_type_short}_{order['id']}"
                )
            )
        
        # Группируем кнопки по 2 в ряд (5 слева, 5 справа)
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации внизу
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orders_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"orders_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("pending"))
async def cmd_pending(message: Message):
    """Показать ожидающие заказы."""
    from orders_manager import get_all_orders
    
    if message.from_user.id != ADMIN_ID:
        return
    
    orders = await get_all_orders()
    pending = [o for o in orders if o["status"] == "pending"]
    
    if not pending:
        await message.answer("Нет ожидающих заказов.")
        return
    
    for order in pending:
        order_text = format_order_message(order, message.from_user.id)
        kb = get_order_keyboard(order, message.from_user.id)
        await message.answer(order_text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("in_progress"))
async def cmd_in_progress(message: Message):
    """Показать заказы в работе."""
    from orders_manager import get_all_orders
    
    if message.from_user.id != ADMIN_ID:
        return
    
    orders = await get_all_orders()
    in_progress = [o for o in orders if o["status"] in ["in_progress", "first_payment_received"]]
    
    if not in_progress:
        await message.answer("Нет заказов в работе.")
        return
    
    text = f"🔨 <b>Заказы в работе ({len(in_progress)})</b>\n\n"
    for order in in_progress:
        order_type = "Бит на заказ" if order["type"] == "custom_beat" else "Сведение"
        text += f"📦 {order_type} {order['id']} | @{order['username']}\n"
    
    await message.answer(text, parse_mode="HTML")
    
    # Показываем детали каждого заказа
    for order in in_progress:
        order_text = format_order_message(order, message.from_user.id)
        kb = get_order_keyboard(order, message.from_user.id)
        await message.answer(order_text, reply_markup=kb, parse_mode="HTML")

@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    """Подробная статистика."""
    from orders_manager import get_all_orders
    
    if message.from_user.id != ADMIN_ID:
        return
    
    orders = await get_all_orders()
    custom_orders = [o for o in orders if o["type"] == "custom_beat"]
    mixing_orders = [o for o in orders if o["type"] == "mixing"]
    
    # Статистика по битам на заказ
    custom_pending = len([o for o in custom_orders if o["status"] == "pending"])
    custom_in_progress = len([o for o in custom_orders if o["status"] == "in_progress"])
    custom_completed = len([o for o in custom_orders if o["status"] == "completed"])
    
    # Статистика по сведению
    mixing_pending = len([o for o in mixing_orders if o["status"] == "pending"])
    mixing_in_progress = len([o for o in mixing_orders if o["status"] == "in_progress"])
    mixing_completed = len([o for o in mixing_orders if o["status"] == "completed"])
    
    # Общая сумма (если есть цены)
    total_revenue = 0
    total_partner_sum = 0
    total_client_sum = 0
    completed_with_prices = 0
    orders_with_partner_price = 0
    orders_with_client_price = 0
    
    for order in orders:
        # Подсчитываем суммы для всех заказов (не только completed)
        if order.get("partner_price"):
            try:
                total_partner_sum += float(order["partner_price"])
                orders_with_partner_price += 1
            except:
                pass
        if order.get("client_price"):
            try:
                total_client_sum += float(order["client_price"])
                orders_with_client_price += 1
            except:
                pass
        
        # Итоговая сумма только для completed заказов
        if order.get("status") == "completed":
            if order.get("price"):
                try:
                    total_revenue += float(str(order["price"]).replace("$", "").strip())
                    completed_with_prices += 1
                except:
                    pass
    
    text = (
        f"📊 <b>Подробная статистика</b>\n\n"
        f"<b>Биты на заказ:</b>\n"
        f"⏳ Ожидают: {custom_pending}\n"
        f"🔨 В работе: {custom_in_progress}\n"
        f"✅ Выполнены: {custom_completed}\n"
        f"📦 Всего: {len(custom_orders)}\n\n"
        f"<b>Сведение:</b>\n"
        f"⏳ Ожидают: {mixing_pending}\n"
        f"🔨 В работе: {mixing_in_progress}\n"
        f"✅ Выполнены: {mixing_completed}\n"
        f"📦 Всего: {len(mixing_orders)}\n\n"
    )
    
    text += f"📋 Всего заказов: {len(orders)}"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(F.text == "📋 Все заказы")
async def handle_all_orders(message: Message):
    """Обработка кнопки 'Все заказы'."""
    await cmd_orders(message, page=0)

@dp.callback_query(F.data.startswith("orders_page_"))
async def orders_page_callback(callback: CallbackQuery):
    """Обработка пагинации для всех заказов."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может просматривать заказы.", show_alert=True)
        return
    
    page = int(callback.data.split("_")[-1])
    
    # Получаем заказы
    from orders_manager import get_all_orders
    orders = await get_all_orders()
    if not orders:
        await callback.message.edit_text("Заказов пока нет.")
        await callback.answer()
        return
    
    # Сортируем по дате создания (новые первые)
    orders_sorted = sorted(orders, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Статусы на русском
    status_text = {
        "pending": "⏳ Ждет",
        "accepted": "📋 Принят",
        "in_progress": "🔨 В работе",
        "first_payment_received": "💰 Оплата",
        "awaiting_price": "💰 Сумма",
        "completed": "✅ Выполнен",
        "rejected": "❌ Отклонен",
        "cancelled": "❌ Отменен"
    }
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(orders_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(orders_sorted))
    
    text = f"📋 <b>Все заказы ({len(orders_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы
    for order in orders_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        status = status_text.get(order["status"], order["status"])
        text += f"📦 {order_type} {order['id']} | {status}\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца: 5 слева, 5 справа)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if orders_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in orders_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=f"view_order_{order_type_short}_{order['id']}"
                )
            )
        
        # Группируем кнопки по 2 в ряд (5 слева, 5 справа)
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации внизу
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orders_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"orders_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("completed_page_"))
async def completed_page_callback(callback: CallbackQuery):
    """Обработка пагинации для выполненных заказов."""
    user_id = callback.from_user.id
    
    # Проверяем, является ли пользователь партнером
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    partner = await get_partner(user_id)
    is_partner = partner is not None
    is_admin = user_id == ADMIN_ID
    
    if not is_partner and not is_admin:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    
    page = int(callback.data.split("_")[-1])
    
    # Получаем заказы
    orders = await get_all_orders()
    if is_partner:
        # Для партнера: выполненные заказы = "completed" ИЛИ ("awaiting_price" + есть partner_price)
        completed = [
            o for o in orders 
            if o.get("partner_id") == user_id 
            and (
                o["status"] == "completed" 
                or (o["status"] == "awaiting_price" and o.get("partner_price") is not None)
            )
        ]
    else:
        completed = [o for o in orders if o["status"] == "completed"]
    
    if not completed:
        await callback.message.edit_text("Нет выполненных заказов.")
        await callback.answer()
        return
    
    # Сортируем по дате создания (новые первые)
    completed_sorted = sorted(completed, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(completed_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(completed_sorted))
    
    text = f"✅ <b>Выполненные заказы ({len(completed_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы в компактном формате
    for order in completed_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        text += f"📦 {order_type} {order['id']} | ✅ Выполнен\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if completed_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in completed_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            if is_partner:
                callback_data = f"partner_view_order_{order_type_short}_{order['id']}"
            else:
                callback_data = f"view_order_{order_type_short}_{order['id']}"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=callback_data
                )
            )
        
        # Группируем кнопки по 2 в ряд
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации внизу
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"completed_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"completed_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("view_order_"))
async def view_order_callback(callback: CallbackQuery):
    """Просмотр деталей заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может просматривать заказы.", show_alert=True)
        return
    
    # Формат: view_order_beat_14 или view_order_mixing_5
    parts = callback.data.split("_")
    if len(parts) >= 4:
        order_type = "custom_beat" if parts[2] == "beat" else "mixing"
        order_id = int(parts[3])
        
        from orders_manager import get_order_by_id
        order = await get_order_by_id(order_id, order_type)
        if order:
            order_text = format_order_message(order, callback.from_user.id)
            kb = get_order_keyboard(order, callback.from_user.id)
            await callback.message.answer(order_text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
        else:
            await callback.answer("Заказ не найден.", show_alert=True)

@dp.message(F.text == "⏳ Ожидающие")
async def handle_pending(message: Message):
    """Обработка кнопки 'Ожидающие'."""
    await cmd_pending(message)

@dp.message(F.text == "🔨 В работе")
async def handle_in_progress(message: Message):
    """Обработка кнопки 'В работе'."""
    user_id = message.from_user.id
    
    # Проверяем, является ли пользователь партнером
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    partner = await get_partner(user_id)
    if partner:
        # Если это партнер, обрабатываем через handle_partner_in_work
        orders = await get_all_orders()
        # В работе: только заказы, которые реально в работе (не ожидают сумму)
        # Исключаем "awaiting_price" - такие заказы должны быть в "Выполненные"
        in_work = [o for o in orders if o.get("partner_id") == user_id and o["status"] in ["accepted", "in_progress", "first_payment_received"]]
        
        if not in_work:
            await message.answer("Нет заказов в работе.")
            return
        
        # Сортируем по дате создания (новые первые)
        in_work_sorted = sorted(in_work, key=lambda x: x.get("created_at", ""), reverse=True)
        
        status_text = {
            "accepted": "📋 Принят",
            "in_progress": "🔨 В работе",
            "first_payment_received": "💰 Оплата",
            "awaiting_price": "💰 Сумма"
        }
        
        # Пагинация: по 10 заказов на страницу
        per_page = 10
        total_pages = (len(in_work_sorted) + per_page - 1) // per_page
        start_idx = 0
        end_idx = min(per_page, len(in_work_sorted))
        
        text = f"🔨 <b>Заказы в работе ({len(in_work_sorted)})</b>\n"
        text += f"Страница 1 из {total_pages}\n\n"
        
        # Показываем заказы текущей страницы в компактном формате
        for order in in_work_sorted[start_idx:end_idx]:
            order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
            status = status_text.get(order["status"], order["status"])
            text += f"📦 {order_type} {order['id']} | {status}\n"
        
        # Кнопки для просмотра деталей заказа (в 2 столбца)
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        buttons = []
        
        if in_work_sorted[start_idx:end_idx]:
            detail_buttons = []
            for order in in_work_sorted[start_idx:end_idx]:
                order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
                detail_buttons.append(
                    InlineKeyboardButton(
                        text=f"📋 {order['id']}",
                        callback_data=f"partner_view_order_{order_type_short}_{order['id']}"
                    )
                )
            
            # Группируем кнопки по 2 в ряд
            for i in range(0, len(detail_buttons), 2):
                row = [detail_buttons[i]]
                if i + 1 < len(detail_buttons):
                    row.append(detail_buttons[i + 1])
                buttons.append(row)
        
        kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
        return
    
    # Если это админ, обрабатываем через cmd_in_progress
    if user_id != ADMIN_ID:
        return
    
    await cmd_in_progress(message)

@dp.message(F.text == "📊 Статистика")
async def handle_stats(message: Message):
    """Обработка кнопки 'Статистика'."""
    await cmd_stats(message)

@dp.message(F.text == "👨‍💼 Заказы партнеров")
async def handle_partner_orders(message: Message):
    """Обработка кнопки 'Заказы партнеров'."""
    from orders_manager import get_all_orders
    
    if message.from_user.id != ADMIN_ID:
        return
    
    orders = await get_all_orders()
    partner_orders = [o for o in orders if o.get("partner_id") and o.get("status") not in ["completed", "rejected", "cancelled"]]
    
    if not partner_orders:
        await message.answer("Нет активных заказов партнеров.")
        return
    
    # Группируем по партнерам
    from collections import defaultdict
    orders_by_partner = defaultdict(list)
    for order in partner_orders:
        partner_id = order.get("partner_id")
        partner_username = order.get("partner_username", f"user{partner_id}")
        orders_by_partner[partner_username].append(order)
    
    text = f"👨‍💼 <b>Заказы партнеров ({len(partner_orders)})</b>\n\n"
    
    for partner_username, partner_orders_list in orders_by_partner.items():
        text += f"<b>@{partner_username}:</b> {len(partner_orders_list)} заказ(ов)\n"
        for order in partner_orders_list[:5]:  # Показываем первые 5 заказов
            order_type_text = "Бит на заказ" if order["type"] == "custom_beat" else "Сведение"
            status_text = {
                "pending": "⏳ Ждет",
                "accepted": "📋 Принят",
                "in_progress": "🔨 В работе",
                "first_payment_received": "💰 Ожидает оплату",
                "awaiting_price": "💰 Ожидает сумму"
            }.get(order.get("status"), order.get("status", "unknown"))
            text += f"  • {order_type_text} {order['id']} - {status_text}\n"
        if len(partner_orders_list) > 5:
            text += f"  ... и еще {len(partner_orders_list) - 5}\n"
        text += "\n"
    
    # Добавляем кнопки для просмотра заказов каждого партнера
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    for partner_username in list(orders_by_partner.keys())[:10]:  # Первые 10 партнеров
        buttons.append([
            InlineKeyboardButton(
                text=f"📋 @{partner_username}",
                callback_data=f"view_partner_orders_{partner_username}"
            )
        ])
    
    if buttons:
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, parse_mode="HTML")

@dp.callback_query(F.data.startswith("partner_orders_page_"))
async def partner_orders_page_callback(callback: CallbackQuery):
    """Обработка пагинации для заказов партнера."""
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    user_id = callback.from_user.id
    partner = await get_partner(user_id)
    
    if not partner:
        await callback.answer("Вы не являетесь партнером.", show_alert=True)
        return
    
    page = int(callback.data.split("_")[-1])
    
    orders = await get_all_orders()
    my_orders = [o for o in orders if o.get("partner_id") == user_id]
    
    if not my_orders:
        await callback.message.edit_text("У тебя пока нет принятых заказов.")
        await callback.answer()
        return
    
    # Сортируем по дате создания (новые первые)
    my_orders_sorted = sorted(my_orders, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Статусы на русском
    status_text = {
        "pending": "⏳ Ждет",
        "accepted": "📋 Принят",
        "in_progress": "🔨 В работе",
        "first_payment_received": "💰 Оплата",
        "awaiting_price": "💰 Сумма",
        "completed": "✅ Выполнен",
        "rejected": "❌ Отклонен",
        "cancelled": "❌ Отменен"
    }
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(my_orders_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(my_orders_sorted))
    
    text = f"📦 <b>Мои заказы ({len(my_orders_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы в компактном формате
    for order in my_orders_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        # Для партнера: если статус "awaiting_price" и есть partner_price, показываем "✅ Выполнен"
        if order.get("status") == "awaiting_price" and order.get("partner_price") is not None:
            status = "✅ Выполнен"
        else:
            status = status_text.get(order["status"], order["status"])
        text += f"📦 {order_type} {order['id']} | {status}\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if my_orders_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in my_orders_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=f"partner_view_order_{order_type_short}_{order['id']}"
                )
            )
        
        # Группируем кнопки по 2 в ряд
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"partner_orders_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"partner_orders_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await callback.answer()

@dp.callback_query(F.data.startswith("partner_view_order_"))
async def partner_view_order_callback(callback: CallbackQuery):
    """Просмотр деталей заказа партнером."""
    from partners_manager import get_partner
    from orders_manager import get_order_by_id
    
    user_id = callback.from_user.id
    partner = await get_partner(user_id)
    
    if not partner:
        await callback.answer("Вы не являетесь партнером.", show_alert=True)
        return
    
    # Формат: partner_view_order_beat_14 или partner_view_order_mixing_5
    parts = callback.data.split("_")
    if len(parts) >= 4:
        order_type = "custom_beat" if parts[3] == "beat" else "mixing"
        order_id = int(parts[4])
        
        order = await get_order_by_id(order_id, order_type)
        if order and order.get("partner_id") == user_id:
            order_text = format_order_message(order, user_id)
            kb = get_partner_order_keyboard(order, user_id)
            await callback.message.answer(order_text, reply_markup=kb, parse_mode="HTML")
            await callback.answer()
        else:
            await callback.answer("Заказ не найден или у вас нет доступа.", show_alert=True)

@dp.callback_query(F.data.startswith("view_partner_orders_"))
async def view_partner_orders_callback(callback: CallbackQuery):
    """Просмотр заказов конкретного партнера."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может просматривать заказы партнеров.", show_alert=True)
        return
    
    partner_username = callback.data.replace("view_partner_orders_", "")
    
    from orders_manager import get_all_orders
    orders = await get_all_orders()
    partner_orders = [
        o for o in orders 
        if o.get("partner_username") == partner_username or 
           (o.get("partner_id") and str(o.get("partner_username", "")).replace("ID: ", "") == partner_username)
    ]
    
    if not partner_orders:
        await callback.answer("Заказы не найдены.", show_alert=True)
        return
    
    # Сортируем по дате создания (новые первые)
    partner_orders_sorted = sorted(partner_orders, key=lambda x: x.get("created_at", ""), reverse=True)
    
    text = f"👨‍💼 <b>Заказы партнера @{partner_username}</b>\n\n"
    
    for order in partner_orders_sorted[:20]:  # Показываем первые 20
        order_type_text = "Бит на заказ" if order["type"] == "custom_beat" else "Сведение"
        status_text = {
            "pending": "⏳ Ждет",
            "accepted": "📋 Принят",
            "in_progress": "🔨 В работе",
            "first_payment_received": "💰 Ожидает оплату",
            "awaiting_price": "💰 Ожидает сумму",
            "completed": "✅ Выполнен",
            "rejected": "❌ Отклонен",
            "cancelled": "❌ Отменен"
        }.get(order.get("status"), order.get("status", "unknown"))
        
        text += f"📦 {order_type_text} {order['id']}\n"
        text += f"   Статус: {status_text}\n"
        text += f"   Клиент: @{order.get('username', 'no_username')}\n"
        
        if order.get("partner_price"):
            text += f"   💰 Исполнитель указал: ${order['partner_price']:.2f}\n"
        if order.get("client_price"):
            text += f"   💰 Клиент указал: ${order['client_price']:.2f}\n"
        if order.get("price"):
            text += f"   💰 Итоговая сумма: ${order['price']:.2f}\n"
        
        text += "\n"
    
    if len(partner_orders_sorted) > 20:
        text += f"\n... и еще {len(partner_orders_sorted) - 20} заказ(ов)"
    
    await callback.message.answer(text, parse_mode="HTML")
    await callback.answer()

@dp.message(F.text == "✅ Выполненные")
async def handle_completed(message: Message, page: int = 0):
    """Обработка кнопки 'Выполненные'."""
    user_id = message.from_user.id
    
    # Проверяем, является ли пользователь партнером
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    partner = await get_partner(user_id)
    if partner:
        # Если это партнер, показываем его выполненные заказы:
        # - со статусом "completed" (полностью выполненные)
        # - со статусом "awaiting_price" + есть partner_price (партнер указал сумму, ждет клиента)
        orders = await get_all_orders()
        completed = [
            o for o in orders 
            if o.get("partner_id") == user_id 
            and (
                o["status"] == "completed" 
                or (o["status"] == "awaiting_price" and o.get("partner_price") is not None)
            )
        ]
    elif user_id == ADMIN_ID:
        # Если это админ, показываем только полностью выполненные заказы (со статусом "completed")
        orders = await get_all_orders()
        completed = [o for o in orders if o["status"] == "completed"]
    else:
        return
    
    if not completed:
        await message.answer("Нет выполненных заказов.")
        return
    
    # Сортируем по дате создания (новые первые)
    completed_sorted = sorted(completed, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(completed_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(completed_sorted))
    
    text = f"✅ <b>Выполненные заказы ({len(completed_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы в компактном формате
    for order in completed_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        text += f"📦 {order_type} {order['id']} | ✅ Выполнен\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if completed_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in completed_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            if partner:
                callback_data = f"partner_view_order_{order_type_short}_{order['id']}"
            else:
                callback_data = f"view_order_{order_type_short}_{order['id']}"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=callback_data
                )
            )
        
        # Группируем кнопки по 2 в ряд
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации внизу
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"completed_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"completed_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "/menu")
async def handle_menu(message: Message):
    """Обработка команды /menu."""
    await cmd_start(message)

@dp.message(Command("partners"))
async def cmd_partners(message: Message):
    """Управление партнерами (только для админа)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    from partners_manager import get_active_partners
    partners = await get_active_partners()
    
    if not partners:
        text = "Партнеров пока нет.\n\n"
        text += "Добавить партнера:\n"
        text += "/add_partner <user_id> <name>\n"
        text += "Пример: /add_partner 123456789 Иван"
        await message.answer(text)
        return
    
    text = f"👨‍💼 <b>Партнеры ({len(partners)})</b>\n\n"
    for partner in partners:
        status = "✅ Активен" if partner.get("active", True) else "❌ Неактивен"
        text += (
            f"👤 {partner.get('name', partner.get('username'))}\n"
            f"   ID: {partner['user_id']}\n"
            f"   Статус: {status}\n"
            f"   Принято: {partner.get('orders_accepted', 0)}\n"
            f"   Выполнено: {partner.get('orders_completed', 0)}\n\n"
        )
    
    text += "\n<b>Команды:</b>\n"
    text += "/partner_requests - заявки на регистрацию\n"
    text += "/add_partner &lt;user_id&gt; &lt;name&gt; - добавить партнера\n"
    text += "/remove_partner &lt;user_id&gt; - удалить партнера\n"
    text += "/toggle_partner &lt;user_id&gt; - активировать/деактивировать"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("get_user_id"))
async def cmd_get_user_id(message: Message):
    """Получить user_id из пересланного сообщения."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из пересланного сообщения
    if message.forward_from:
        user_id = message.forward_from.id
        username = message.forward_from.username or "no_username"
        first_name = message.forward_from.first_name or ""
        await message.answer(
            f"👤 <b>Информация о пользователе:</b>\n\n"
            f"ID: <code>{user_id}</code>\n"
            f"Username: @{username}\n"
            f"Имя: {first_name}\n\n"
            f"Используй этот ID в командах:\n"
            f"/approve_partner {user_id}\n"
            f"/add_partner {user_id} beatmaker Имя",
            parse_mode="HTML"
        )
    elif message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        username = message.reply_to_message.from_user.username or "no_username"
        first_name = message.reply_to_message.from_user.first_name or ""
        await message.answer(
            f"👤 <b>Информация о пользователе:</b>\n\n"
            f"ID: <code>{user_id}</code>\n"
            f"Username: @{username}\n"
            f"Имя: {first_name}\n\n"
            f"Используй этот ID в командах:\n"
            f"/approve_partner {user_id}\n"
            f"/add_partner {user_id} beatmaker Имя",
            parse_mode="HTML"
        )
    else:
        await message.answer(
            "❌ Не удалось получить user_id.\n\n"
            "💡 <b>Как получить user_id:</b>\n\n"
            "1. Перешли сообщение от пользователя в этот чат\n"
            "2. Или ответь (reply) на сообщение пользователя\n"
            "3. Затем используй команду /get_user_id\n\n"
            "Или скопируй ID из команды /partner_requests",
            parse_mode="HTML"
        )

@dp.message(Command("add_partner"))
async def cmd_add_partner(message: Message):
    """Добавить партнера."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из reply
    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        logging.info(f"Получен user_id из reply: {user_id}")
    
    # Формат: /add_partner <user_id> <name> (тип больше не требуется)
    parts = message.text.split()
    
    if not user_id:
        if len(parts) < 3:
            await message.answer(
                "Неверный формат команды.\n\n"
                "Использование: /add_partner <user_id> <name>\n"
                "Пример: /add_partner 123456789 Иван\n\n"
                "💡 Чтобы получить user_id, используй /get_user_id (перешли сообщение от пользователя)\n\n"
                "💡 Или ответь (reply) на сообщение пользователя и напиши: /add_partner Имя"
            )
            return
        
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: user_id должен быть числом.")
            return
        name = " ".join(parts[2:])
    else:
        # Если user_id из reply, то только name из команды
        if len(parts) < 2:
            await message.answer(
                "Неверный формат команды.\n\n"
                "Использование: /add_partner <name>\n"
                "Пример: /add_partner Иван"
            )
            return
        name = " ".join(parts[1:])
    
    try:
        from partners_manager import add_partner
        # Получаем username из пересланного сообщения или из команды
        if message.reply_to_message and message.reply_to_message.from_user:
            username = message.reply_to_message.from_user.username or f"user{user_id}"
        else:
            username = f"user{user_id}"
        
        # Добавляем партнера с универсальным типом
        if await add_partner(user_id, username, "partner", name):
            await message.answer(f"✅ Партнер {name} (ID: {user_id}) успешно добавлен!")
        else:
            await message.answer(f"❌ Партнер с ID {user_id} уже существует.")
    except ValueError:
        await message.answer("Ошибка: user_id должен быть числом.")
    except Exception as e:
        logging.error(f"Ошибка добавления партнера: {e}")
        await message.answer(f"Ошибка: {str(e)}")

@dp.message(Command("remove_partner"))
async def cmd_remove_partner(message: Message):
    """Удалить партнера."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из reply
    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        logging.info(f"Получен user_id из reply: {user_id}")
    
    if not user_id:
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer(
                "Использование:\n"
                "1. /remove_partner <user_id>\n"
                "2. Ответь (reply) на сообщение партнера командой /remove_partner\n\n"
                "Пример: /remove_partner 123456789\n\n"
                "💡 Чтобы получить user_id, используй /get_user_id"
            )
            return
        
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: user_id должен быть числом.")
            return
    
    try:
        from partners_manager import remove_partner
        if await remove_partner(user_id):
            await message.answer(f"✅ Партнер с ID {user_id} удален.")
        else:
            await message.answer(f"❌ Партнер с ID {user_id} не найден.")
    except ValueError:
        await message.answer("Ошибка: user_id должен быть числом.")
    except Exception as e:
        logging.error(f"Ошибка удаления партнера: {e}")
        await message.answer(f"Ошибка: {str(e)}")

@dp.message(Command("toggle_partner"))
async def cmd_toggle_partner(message: Message):
    """Активировать/деактивировать партнера."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из reply
    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        logging.info(f"Получен user_id из reply: {user_id}")
    
    if not user_id:
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer(
                "Использование:\n"
                "1. /toggle_partner <user_id>\n"
                "2. Ответь (reply) на сообщение партнера командой /toggle_partner\n\n"
                "Пример: /toggle_partner 123456789\n\n"
                "💡 Чтобы получить user_id, используй /get_user_id"
            )
            return
        
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: user_id должен быть числом.")
            return
    
    try:
        from partners_manager import get_partner, set_partner_active
        partner = await get_partner(user_id)
        if not partner:
            await message.answer(f"❌ Партнер с ID {user_id} не найден.")
            return
        
        new_status = not partner.get("active", True)
        from partners_manager import set_partner_active
        await set_partner_active(user_id, new_status)
        status_text = "активирован" if new_status else "деактивирован"
        await message.answer(f"✅ Партнер {partner.get('name', partner.get('username'))} {status_text}.")
    except ValueError:
        await message.answer("Ошибка: user_id должен быть числом.")
    except Exception as e:
        logging.error(f"Ошибка изменения статуса партнера: {e}")
        await message.answer(f"Ошибка: {str(e)}")

@dp.message(Command("partner_requests"))
async def cmd_partner_requests(message: Message):
    """Показать заявки на регистрацию партнеров (только для админа)."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    from partners_manager import get_pending_requests
    requests = await get_pending_requests()
    
    if not requests:
        await message.answer("Нет ожидающих заявок на регистрацию.")
        return
    
    text = f"📝 <b>Заявки на регистрацию ({len(requests)})</b>\n\n"
    
    for req in requests:
        text += (
            f"👤 @{req['username']} (ID: <code>{req['user_id']}</code>)\n"
            f"📛 Имя: {req.get('name', req['username'])}\n"
        )
        if req.get("message"):
            text += f"💬 Сообщение: {req['message']}\n"
        text += f"📅 Создана: {req.get('created_at', 'N/A')[:10]}\n\n"
    
    text += "\n<b>Команды для управления:</b>\n"
    text += "/approve_partner <user_id> - одобрить заявку\n"
    text += "Или ответь (reply) на это сообщение командой /approve_partner\n"
    text += "/reject_partner <user_id> - отклонить заявку\n"
    text += "Или ответь (reply) на это сообщение командой /reject_partner\n\n"
    text += "💡 <b>Как получить user_id:</b>\n"
    text += "1. Скопируй ID из списка выше (число в скобках)\n"
    text += "2. Или перешли сообщение от пользователя и используй /get_user_id\n"
    text += "3. Или ответь (reply) на сообщение пользователя командой"
    
    await message.answer(text, parse_mode="HTML")

@dp.message(Command("approve_partner"))
async def cmd_approve_partner(message: Message):
    """Одобрить заявку на регистрацию партнера."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из reply (если админ ответил на сообщение)
    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        logging.info(f"Получен user_id из reply: {user_id}")
    
    # Если не получили из reply, пробуем из команды
    if not user_id:
        # Формат: /approve_partner <user_id>
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer(
                "Неверный формат команды.\n\n"
                "Использование:\n"
                "1. /approve_partner <user_id>\n"
                "2. Ответь (reply) на сообщение пользователя командой /approve_partner\n\n"
                "Пример: /approve_partner 123456789\n\n"
                "💡 Чтобы получить user_id, используй /get_user_id (перешли сообщение от пользователя)"
            )
            return
        
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: user_id должен быть числом.")
            return
    
    try:
        admin_id = message.from_user.id
        
        from partners_manager import approve_partner_request
        if await approve_partner_request(user_id, admin_id):
            # Уведомляем пользователя
            from partners_manager import get_partner
            try:
                partner = await get_partner(user_id)
                if partner:
                    await bot.send_message(
                        user_id,
                        f"✅ Поздравляем! Твоя заявка на регистрацию одобрена!\n\n"
                        f"Теперь ты можешь принимать заказы на биты на заказ и сведение. Используй /start для просмотра информации."
                    )
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления партнеру: {e}")
            
            await message.answer(f"✅ Заявка пользователя {user_id} одобрена!")
        else:
            await message.answer(f"❌ Заявка пользователя {user_id} не найдена или уже обработана.")
    except ValueError:
        await message.answer("Ошибка: user_id должен быть числом.")
    except Exception as e:
        logging.error(f"Ошибка одобрения заявки: {e}")
        await message.answer(f"Ошибка: {str(e)}")

@dp.message(Command("reject_partner"))
async def cmd_reject_partner(message: Message):
    """Отклонить заявку на регистрацию партнера."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Пробуем получить user_id из reply (если админ ответил на сообщение)
    user_id = None
    if message.reply_to_message and message.reply_to_message.from_user:
        user_id = message.reply_to_message.from_user.id
        logging.info(f"Получен user_id из reply: {user_id}")
    
    # Если не получили из reply, пробуем из команды
    if not user_id:
        # Формат: /reject_partner <user_id>
        parts = message.text.split()
        if len(parts) < 2:
            await message.answer(
                "Неверный формат команды.\n\n"
                "Использование:\n"
                "1. /reject_partner <user_id>\n"
                "2. Ответь (reply) на сообщение пользователя командой /reject_partner\n\n"
                "Пример: /reject_partner 123456789\n\n"
                "💡 Чтобы получить user_id, используй /get_user_id (перешли сообщение от пользователя)"
            )
            return
        
        try:
            user_id = int(parts[1])
        except ValueError:
            await message.answer("Ошибка: user_id должен быть числом.")
            return
    
    try:
        admin_id = message.from_user.id
        
        from partners_manager import reject_partner_request
        if await reject_partner_request(user_id, admin_id):
            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_id,
                    "❌ К сожалению, твоя заявка на регистрацию в качестве партнера отклонена.\n\n"
                    "Если у тебя есть вопросы, свяжись с администратором."
                )
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления пользователю: {e}")
            
            await message.answer(f"❌ Заявка пользователя {user_id} отклонена.")
        else:
            await message.answer(f"❌ Заявка пользователя {user_id} не найдена или уже обработана.")
    except ValueError:
        await message.answer("Ошибка: user_id должен быть числом.")
    except Exception as e:
        logging.error(f"Ошибка отклонения заявки: {e}")
        await message.answer(f"Ошибка: {str(e)}")

@dp.message(Command("my_orders"))
async def cmd_my_orders(message: Message, page: int = 0):
    """Показать заказы партнера с пагинацией."""
    from partners_manager import get_partner
    from orders_manager import get_all_orders
    
    user_id = message.from_user.id
    partner = await get_partner(user_id)
    
    if not partner:
        await message.answer("Вы не являетесь партнером.")
        return
    
    orders = await get_all_orders()
    my_orders = [o for o in orders if o.get("partner_id") == user_id]
    
    if not my_orders:
        await message.answer("У тебя пока нет принятых заказов.")
        return
    
    # Сортируем по дате создания (новые первые)
    my_orders_sorted = sorted(my_orders, key=lambda x: x.get("created_at", ""), reverse=True)
    
    # Статусы на русском
    status_text = {
        "pending": "⏳ Ждет",
        "accepted": "📋 Принят",
        "in_progress": "🔨 В работе",
        "first_payment_received": "💰 Оплата",
        "awaiting_price": "💰 Сумма",
        "completed": "✅ Выполнен",
        "rejected": "❌ Отклонен",
        "cancelled": "❌ Отменен"
    }
    
    # Пагинация: по 10 заказов на страницу
    per_page = 10
    total_pages = (len(my_orders_sorted) + per_page - 1) // per_page
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(my_orders_sorted))
    
    text = f"📦 <b>Мои заказы ({len(my_orders_sorted)})</b>\n"
    text += f"Страница {page + 1} из {total_pages}\n\n"
    
    # Показываем заказы текущей страницы в компактном формате
    for order in my_orders_sorted[start_idx:end_idx]:
        order_type = "Бит" if order["type"] == "custom_beat" else "Сведение"
        # Для партнера: если статус "awaiting_price" и есть partner_price, показываем "✅ Выполнен"
        if order.get("status") == "awaiting_price" and order.get("partner_price") is not None:
            status = "✅ Выполнен"
        else:
            status = status_text.get(order["status"], order["status"])
        text += f"📦 {order_type} {order['id']} | {status}\n"
    
    # Кнопки для просмотра деталей заказа (в 2 столбца)
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    buttons = []
    
    if my_orders_sorted[start_idx:end_idx]:
        detail_buttons = []
        for order in my_orders_sorted[start_idx:end_idx]:
            order_type_short = "beat" if order["type"] == "custom_beat" else "mixing"
            detail_buttons.append(
                InlineKeyboardButton(
                    text=f"📋 {order['id']}",
                    callback_data=f"partner_view_order_{order_type_short}_{order['id']}"
                )
            )
        
        # Группируем кнопки по 2 в ряд
        for i in range(0, len(detail_buttons), 2):
            row = [detail_buttons[i]]
            if i + 1 < len(detail_buttons):
                row.append(detail_buttons[i + 1])
            buttons.append(row)
    
    # Кнопки пагинации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"partner_orders_page_{page - 1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️ Вперед", callback_data=f"partner_orders_page_{page + 1}"))
    
    if nav_buttons:
        buttons.append(nav_buttons)
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    await message.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.message(F.text == "📦 Мои заказы")
async def handle_partner_my_orders(message: Message):
    """Обработка кнопки 'Мои заказы' для партнера."""
    await cmd_my_orders(message, page=0)


@dp.message(F.text == "✅ Выполнены")
async def handle_partner_completed(message: Message):
    """Обработка кнопки 'Выполнены' для партнера."""
    # Используем общую функцию handle_completed, она сама определит, партнер это или админ
    await handle_completed(message, page=0)

def get_order_display_number(order: dict) -> str:
    """Получает отформатированный номер заказа для отображения."""
    from orders_manager import format_order_number
    return format_order_number(order["id"], order["type"], order.get("created_at"))

def format_order_message(order: dict, user_id: int = None) -> str:
    """Форматирует сообщение о заказе."""
    from orders_manager import format_order_number
    
    order_type = "Бит на заказ" if order["type"] == "custom_beat" else "Сведение"
    order_num = format_order_number(order["id"], order["type"], order.get("created_at"))
    
    # Проверяем, является ли пользователь партнером
    # Проверяем по данным заказа, без обращения к БД
    is_partner = False
    is_other_partner = False  # Партнер, который не принял заказ
    if user_id:
        # Проверяем, что user_id не является админом
        is_admin = (user_id == ORDERS_CHAT_ID)
        
        # Если заказ принят (статус не "pending")
        if order.get("status") != "pending":
            # Если заказ принят партнером
            if order.get("partner_id"):
                if order.get("partner_id") == user_id:
                    # Это партнер, который принял заказ
                    is_partner = True
                elif not is_admin:
                    # Это другой партнер, который не принял заказ
                    is_other_partner = True
            elif not is_admin:
                # Заказ принят админом (partner_id = None), а user_id - это партнер
                # Для партнеров показываем "Принят другим исполнителем"
                is_other_partner = True
    
    status_emoji = {
        "pending": "⏳",
        "accepted": "📋",
        "in_progress": "🔨",
        "first_payment_received": "💰",
        "awaiting_price": "💰",
        "completed": "✅",
        "rejected": "❌",
        "cancelled": "❌"
    }
    
    status_text = {
        "pending": "Ожидает принятия",
        "accepted": "Принят",
        "in_progress": "В работе",
        "first_payment_received": "Ожидает вторую оплату",
        "awaiting_price": "Ожидает сумму",
        "completed": "Выполнен",
        "rejected": "Отклонен",
        "cancelled": "Отменен"
    }
    
    text = (
        f"📦 <b>{order_type} {order_num}</b>\n"
    )
    
    # Для выполненных заказов показываем клиента и исполнителя
    if order.get("status") == "completed":
        text += f"👤 Клиент: @{order['username']} (ID: {order['user_id']})\n"
        if order.get("partner_id"):
            partner_username = order.get("partner_username", f"ID: {order['partner_id']}")
            text += f"👨‍💼 Исполнитель: @{partner_username} (ID: {order['partner_id']})\n"
        else:
            text += f"👨‍💼 Исполнитель: Админ\n"
        text += f"📊 Статус: ✅ Выполнен\n"
        
        # Для партнера показываем просто "Сумма" без валюты
        if is_partner:
            if order.get("partner_price") is not None:
                text += f"Сумма: {order['partner_price']}\n"
            else:
                text += f"Сумма: -\n"
        else:
            # Для админа показываем суммы от исполнителя и клиента
            if order.get("partner_price") is not None or order.get("client_price") is not None:
                if order.get("partner_price") is not None:
                    text += f"Сумма от исполнителя: {order['partner_price']}\n"
                else:
                    text += f"Сумма от исполнителя: -\n"
                
                if order.get("client_price") is not None:
                    text += f"Сумма от клиента: {order['client_price']}\n"
                else:
                    text += f"Сумма от клиента: -\n"
    else:
        # Для остальных заказов показываем полную информацию
        text += f"\n👤 Пользователь: @{order['username']} (ID: {order['user_id']})\n"
        # Описание убрано по запросу
        
        # Если это другой партнер (не принявший заказ), показываем специальный статус
        if is_other_partner:
            text += f"📊 Статус: Принят другим исполнителем\n"
            if order.get("partner_username"):
                text += f"👨‍💼 Исполнитель: @{order['partner_username']}\n"
        else:
            # Для партнера, который принял заказ, или для админа
            # Для партнера: если статус "awaiting_price" и есть partner_price, показываем "Выполнен"
            if is_partner and order.get("status") == "awaiting_price" and order.get("partner_price") is not None:
                text += f"📊 Статус: ✅ Выполнен\n"
            else:
                text += f"📊 Статус: {status_emoji.get(order['status'], '❓')} {status_text.get(order['status'], order['status'])}\n"
        
        if order.get("price"):
            price_display = str(order['price']).replace('$', '').strip() if order.get('price') else '-'
            text += f"💰 Цена: {price_display}\n"
            if order.get("first_payment"):
                text += f"💵 Первая оплата (50%): ✅ Получена\n"
            else:
                text += f"💵 Первая оплата (50%): ❌ Не получена\n"
            if order.get("second_payment"):
                text += f"💵 Вторая оплата (50%): ✅ Получена\n"
            else:
                text += f"💵 Вторая оплата (50%): ❌ Не получена\n"
        
        # Показываем суммы от партнера и клиента, если они указаны (для заказов в процессе)
        # Для партнера показываем только его сумму, для админа - обе суммы
        if is_partner:
            # Для партнера показываем только его сумму
            if order.get("partner_price") is not None:
                text += f"\n💵 Сумма: {order['partner_price']}\n"
        else:
            # Для админа показываем обе суммы
            if order.get("partner_price") is not None or order.get("client_price") is not None:
                text += "\n💵 <b>Суммы заказа:</b>\n"
                if order.get("partner_price") is not None:
                    # partner_price теперь строка, выводим как есть
                    text += f"   Партнер указал: {order['partner_price']}\n"
                else:
                    text += f"   Партнер: ⏳ Ожидает...\n"
                if order.get("client_price") is not None:
                    # client_price теперь строка, выводим как есть
                    text += f"   Клиент указал: {order['client_price']}\n"
                else:
                    text += f"   Клиент: ⏳ Ожидает...\n"
        
        if order.get("created_at"):
            from datetime import datetime
            created = datetime.fromisoformat(order["created_at"])
            text += f"📅 Создан: {created.strftime('%d.%m.%Y %H:%M')}\n"
    
    return text

def get_order_keyboard(order: dict, user_id: int = None) -> InlineKeyboardMarkup:
    """Создает клавиатуру для управления заказом."""
    buttons = []
    
    # Только кнопки "Принять/Отклонить" для админа при статусе pending
    # Если заказ принят партнером, завершен или отклонен, админ не видит эти кнопки
    is_admin = user_id == ADMIN_ID if user_id else False
    if is_admin and order["status"] == "pending" and not order.get("partner_id"):
        buttons.append([
            InlineKeyboardButton(
                text="✅ Принять",
                callback_data=f"accept_{order['type']}_{order['id']}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"reject_{order['type']}_{order['id']}"
            )
        ])
    # Кнопки "Заказ выполнен" и "Отменить" для админа и партнера, если заказ принят
    elif order["status"] in ["in_progress", "first_payment_received"]:
        is_admin = user_id == ADMIN_ID if user_id else False
        is_partner = order.get("partner_id") == user_id if user_id else False
        
        # Админ видит кнопки если заказ принят им (не партнером)
        if is_admin and not order.get("partner_id"):
            buttons.append([
                InlineKeyboardButton(
                    text="✅ Заказ выполнен",
                    callback_data=f"mark_completed_{order['type']}_{order['id']}"
                )
            ])
            buttons.append([
                InlineKeyboardButton(
                    text="❌ Отменить",
                    callback_data=f"mark_cancelled_{order['type']}_{order['id']}"
                )
            ])
    
    # Кнопки для связи с клиентом и партнером (если заказ принят партнером)
    if order.get("partner_id"):
        client_username = order.get("username", "no_username")
        client_link = f"https://t.me/{client_username}" if client_username != "no_username" else f"https://t.me/user{order['user_id']}"
        
        # Проверяем, является ли текущий пользователь партнером, который принял заказ
        is_partner_who_accepted = user_id and order.get("partner_id") == user_id
        
        if is_partner_who_accepted:
            # Для партнера, который принял заказ, показываем только кнопку связи с клиентом
            buttons.append([
                InlineKeyboardButton(
                    text=f"💬 Клиент (@{client_username})",
                    url=client_link
                )
            ])
        else:
            # Для админа показываем обе кнопки
            partner_username = order.get("partner_username", f"user{order['partner_id']}")
            partner_link = f"https://t.me/{partner_username}" if partner_username.startswith("@") else f"https://t.me/{partner_username.replace('ID: ', '')}"
            buttons.append([
                InlineKeyboardButton(
                    text=f"💬 Клиент (@{client_username})",
                    url=client_link
                ),
                InlineKeyboardButton(
                    text=f"👨‍💼 Партнер (@{partner_username.replace('ID: ', '')})",
                    url=partner_link
                )
            ])
    elif order["status"] != "pending":
        # Если заказ принят админом, показываем кнопку для связи с клиентом
        client_username = order.get("username", "no_username")
        client_link = f"https://t.me/{client_username}" if client_username != "no_username" else f"https://t.me/user{order['user_id']}"
        buttons.append([
            InlineKeyboardButton(
                text=f"💬 Клиент (@{client_username})",
                url=client_link
            )
        ])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.callback_query(F.data.startswith("accept_"))
async def accept_order(callback: CallbackQuery):
    """Принять заказ."""
    logging.info(f"Получен callback для принятия заказа: {callback.data}, от пользователя {callback.from_user.id}")
    
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать заказы.", show_alert=True)
        return
    
    # Формат: accept_custom_beat_1 или accept_mixing_1
    parts = callback.data.split("_")
    logging.info(f"Разобранный callback_data: {parts}")
    
    if len(parts) >= 4 and parts[1] == "custom" and parts[2] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[3])
        logging.info(f"Определен тип заказа: {order_type}, ID: {order_id}")
    elif len(parts) >= 3 and parts[1] == "mixing":
        order_type = "mixing"
        order_id = int(parts[2])
        logging.info(f"Определен тип заказа: {order_type}, ID: {order_id}")
    else:
        logging.error(f"Неверный формат callback_data: {callback.data}, parts: {parts}")
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Загружаем заказ из БД
    from orders_manager import get_order_by_id, update_order_status
    
    order = await get_order_by_id(order_id, order_type)
    
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Защита от повторного принятия: проверяем статус
    if order.get("status") in ["completed", "rejected"]:
        await callback.answer("Этот заказ уже завершен или отклонен.", show_alert=True)
        return
    
    # Проверяем, не принят ли заказ уже партнером
    if order.get("partner_id"):
        await callback.answer(
            "Этот заказ уже принят партнером. Вы не можете его принять.",
            show_alert=True
        )
        return
    
    # Если заказ еще не принят партнером, помечаем что админ принял (статус "in_progress")
    if not order.get("partner_id"):
        updated_order = await update_order_status(order_id, order_type, "in_progress", partner_id=None, partner_username="Админ")
    else:
        updated_order = await update_order_status(order_id, order_type, "in_progress")
    
    if updated_order:
        # Отправляем сообщение клиенту только если заказ принят админом (не партнером)
        if main_bot and not order.get("partner_id"):
            try:
                lang = "ru"  # По умолчанию русский
                
                # Получаем информацию об админе для контакта
                try:
                    admin_info = await bot.get_chat(ORDERS_CHAT_ID)
                    admin_username = admin_info.username or f"user{ORDERS_CHAT_ID}"
                except:
                    admin_username = f"user{ORDERS_CHAT_ID}"
                
                order_display_num = get_order_display_number(order)
                if order_type == "custom_beat":
                    client_text = (
                        f"✅ Отлично! Я принял твой заказ на бит. Номер заказа: {order_display_num}\n\n"
                        f"👨‍💼 Исполнитель: @{admin_username}\n\n"
                        "Я свяжусь с тобой для обсуждения деталей."
                    )
                else:  # mixing
                    client_text = (
                        f"✅ Отлично! Я принял твой заказ на сведение. Номер заказа: {order_display_num}\n\n"
                        f"👨‍💼 Исполнитель: @{admin_username}\n\n"
                        "Я свяжусь с тобой для обсуждения деталей."
                    )
                
                await main_bot.send_message(order["user_id"], client_text)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения клиенту: {e}")
        
        # Обновляем сообщение в боте заказов
        await callback.message.edit_text(
            format_order_message(updated_order, callback.from_user.id),
            reply_markup=get_order_keyboard(updated_order, callback.from_user.id),
            parse_mode="HTML"
        )
        
        # Обновляем сообщения у всех партнеров (показываем, что заказ принят админом)
        try:
            partner_message_ids = updated_order.get("partner_message_ids", {})
            if not partner_message_ids:
                partner_message_ids = {}
            elif isinstance(partner_message_ids, str):
                import json
                try:
                    partner_message_ids = json.loads(partner_message_ids)
                except:
                    partner_message_ids = {}
            
            # Обновляем сообщения у всех партнеров
            for pid_str, msg_id in partner_message_ids.items():
                try:
                    pid = int(pid_str)
                    logging.info(f"Обновление сообщения у партнера {pid} (заказ принят админом)")
                    
                    # Формируем текст с обновленным статусом
                    partner_text = format_order_message(updated_order, pid)
                    # Создаем клавиатуру без кнопок (заказ принят админом)
                    partner_kb = get_partner_order_keyboard(updated_order, pid)
                    
                    # Пытаемся обновить сообщение
                    updated = False
                    try:
                        await bot.edit_message_caption(
                            chat_id=pid,
                            message_id=msg_id,
                            caption=partner_text,
                            reply_markup=partner_kb,
                            parse_mode="HTML"
                        )
                        updated = True
                        logging.info(f"Обновлен caption у партнера {pid} (message_id={msg_id})")
                    except:
                        try:
                            await bot.edit_message_text(
                                chat_id=pid,
                                message_id=msg_id,
                                text=partner_text,
                                reply_markup=partner_kb,
                                parse_mode="HTML"
                            )
                            updated = True
                            logging.info(f"Обновлен текст у партнера {pid} (message_id={msg_id})")
                        except Exception as e:
                            logging.error(f"Ошибка обновления сообщения у партнера {pid}: {e}")
                except (ValueError, KeyError) as e:
                    logging.error(f"Ошибка обработки partner_message_id для партнера {pid_str}: {e}")
        except Exception as e:
            logging.error(f"Ошибка обновления сообщений партнеров: {e}")
        
        if order.get("partner_id"):
            await callback.answer("✅ Заказ принят админом. Партнер уже уведомлен, вы можете управлять заказом.")
        else:
            await callback.answer("✅ Заказ принят! Клиенту отправлено уведомление.")
    else:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_order(callback: CallbackQuery):
    """Отклонить заказ."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять заказы.", show_alert=True)
        return
    
    # Формат: reject_custom_beat_1 или reject_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[1] == "custom" and parts[2] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[3])
    elif len(parts) >= 3 and parts[1] == "mixing":
        order_type = "mixing"
        order_id = int(parts[2])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Загружаем заказ из БД
    from orders_manager import get_order_by_id, update_order_status
    
    order = await get_order_by_id(order_id, order_type)
    
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Проверяем, не принят ли заказ уже партнером или не завершен ли он
    if order.get("partner_id"):
        await callback.answer("Этот заказ уже принят партнером. Вы не можете его отклонить.", show_alert=True)
        return
    
    if order.get("status") in ["completed", "rejected"]:
        await callback.answer("Этот заказ уже завершен или отклонен. Вы не можете его отклонить.", show_alert=True)
        return
    
    # Обновляем статус
    updated_order = await update_order_status(order_id, order_type, "rejected")
    
    if updated_order:
        # Отправляем сообщение клиенту через основной бот
        if main_bot:
            try:
                lang = "ru"  # По умолчанию русский
                
                if lang == "ru":
                    client_text = (
                        "❌ К сожалению, я не могу принять твой заказ.\n\n"
                        "Можешь связаться со мной для обсуждения или создать новый заказ."
                    )
                else:
                    client_text = (
                        "❌ Unfortunately, I can't accept your order.\n\n"
                        "You can contact me to discuss or create a new order."
                    )
                
                await main_bot.send_message(order["user_id"], client_text)
            except Exception as e:
                logging.error(f"Ошибка отправки сообщения клиенту: {e}")
        
        # Обновляем сообщение в боте заказов
        await callback.message.edit_text(
            format_order_message(updated_order, callback.from_user.id),
            reply_markup=get_order_keyboard(updated_order, callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("Заказ отклонен. Клиенту отправлено уведомление.")
    else:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("start_"))
async def start_order(callback: CallbackQuery):
    """Начать работу над заказом."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может начинать работу.", show_alert=True)
        return
    
    # Формат: start_custom_beat_1 или start_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[1] == "custom" and parts[2] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[3])
    elif len(parts) >= 3 and parts[1] == "mixing":
        order_type = "mixing"
        order_id = int(parts[2])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import update_order_status
    order = await update_order_status(order_id, order_type, "in_progress")
    if order:
        await callback.message.edit_text(
            format_order_message(order, callback.from_user.id),
            reply_markup=get_order_keyboard(order, callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("Работа начата!")
    else:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("complete_"))
async def complete_order(callback: CallbackQuery):
    """Завершить заказ."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может завершать заказы.", show_alert=True)
        return
    
    # Формат: complete_custom_beat_1 или complete_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[1] == "custom" and parts[2] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[3])
    elif len(parts) >= 3 and parts[1] == "mixing":
        order_type = "mixing"
        order_id = int(parts[2])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Обновляем статус - заказ готов, можно отправлять файл
    from orders_manager import update_order_status
    order = await update_order_status(order_id, order_type, "first_payment_received")
    if order:
        await callback.message.edit_text(
            format_order_message(order, callback.from_user.id),
            reply_markup=get_order_keyboard(order, callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("✅ Заказ готов! Нажмите 'Отправить файл' и загрузите файл в этом чате.")
    else:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("second_payment_"))
async def second_payment(callback: CallbackQuery):
    """Отметить вторую оплату."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отмечать оплаты.", show_alert=True)
        return
    
    # Формат: second_payment_custom_beat_1 или second_payment_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 5 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import update_order_status
    order = await update_order_status(order_id, order_type, "completed", second_payment=True)
    if order:
        await callback.message.edit_text(
            format_order_message(order, callback.from_user.id),
            reply_markup=get_order_keyboard(order, callback.from_user.id),
            parse_mode="HTML"
        )
        await callback.answer("Вторая оплата отмечена!")
    else:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)

@dp.callback_query(F.data.startswith("send_file_"))
async def send_file(callback: CallbackQuery):
    """Запрос на отправку файла - просим админа загрузить файл в этом боте."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отправлять файлы.", show_alert=True)
        return
    
    # Формат: send_file_custom_beat_1 или send_file_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import get_order_by_id
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Сохраняем информацию о том, что админ отправляет файл для этого заказа
    dp.admin_sending_file[order_id] = (order_type, order["user_id"])
    
    # Просим админа загрузить файл в этом боте
    order_name = "Бит на заказ" if order_type == "custom_beat" else "Сведение"
    await callback.message.answer(
        f"📤 Отправьте файл для заказа {order_id}\n\n"
        f"Тип: {order_name}\n"
        f"Клиент: @{order['username']} (ID: {order['user_id']})\n"
        f"Описание: {order.get('description', '-')}\n\n"
        f"Загрузите файл (mp3, wav или архив) в этом чате."
    )
    await callback.answer("✅ Теперь загрузите файл в этом чате.")

@dp.message((F.from_user.id == ADMIN_ID) & (F.audio | F.document))
async def handle_admin_file(message: Message):
    """Обработка файлов от админа - отправка файла клиенту."""
    # Проверяем, что админ отправляет файл для какого-то заказа
    if not hasattr(dp, 'admin_sending_file') or not dp.admin_sending_file:
        return  # Админ не отправляет файл
    
    # Находим заказ, для которого админ отправляет файл
    order_id = None
    order_type = None
    user_id = None
    for oid, (otype, uid) in dp.admin_sending_file.items():
        order_id = oid
        order_type = otype
        user_id = uid
        break
    
    if not order_id or not order_type or not user_id:
        await message.answer("Ошибка: не найдена информация о заказе. Нажмите кнопку 'Отправить файл' снова.")
        return
    
    # Получаем информацию о заказе
    from orders_manager import get_order_by_id
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await message.answer("Ошибка: заказ не найден.")
        dp.admin_sending_file.pop(order_id, None)
        return
    
    # Отправляем файл клиенту через основной бот
    if main_bot:
        try:
            lang = "ru"  # По умолчанию русский
            is_second_payment = order.get("status") == "completed"
            
            if is_second_payment:
                file_sent_text = (
                    "✅ Готовый файл отправлен!\n\n"
                    "Проверьте файл. Если нужны правки, нажмите кнопку 'Связаться'.\n"
                    "После проверки нужно будет оплатить оставшиеся 50%."
                    if lang == "ru"
                    else "✅ Final file sent!\n\n"
                         "Check the file. If you need revisions, press the 'Contact' button.\n"
                         "After checking, you'll need to pay the remaining 50%."
                )
            else:
                file_sent_text = (
                    "✅ Готовый файл отправлен!\n\n"
                    "Проверьте файл. Если нужны правки, нажмите кнопку 'Связаться'.\n"
                    "Если все устраивает, нажмите 'Меня все устраивает' для оплаты второй части (50%)."
                    if lang == "ru"
                    else "✅ Final file sent!\n\n"
                         "Check the file. If you need revisions, press the 'Contact' button.\n"
                         "If everything is fine, press 'I'm satisfied' to pay the second part (50%)."
                )
            
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            if is_second_payment:
                contact_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Связаться", callback_data="contact_admin")]
                    ]
                )
            else:
                contact_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Меня все устраивает", callback_data="accept_order")],
                        [InlineKeyboardButton(text="Связаться", callback_data="contact_admin")]
                    ]
                )
            
            if message.audio:
                await main_bot.send_audio(
                    chat_id=user_id, 
                    audio=message.audio.file_id, 
                    caption=file_sent_text, 
                    reply_markup=contact_kb
                )
            elif message.document:
                await main_bot.send_document(
                    chat_id=user_id, 
                    document=message.document.file_id, 
                    caption=file_sent_text, 
                    reply_markup=contact_kb
                )
            
            # Убираем из ожидания
            dp.admin_sending_file.pop(order_id, None)
            
            await message.answer("✅ Файл отправлен клиенту!")
        except Exception as e:
            logging.error(f"Ошибка отправки файла клиенту: {e}")
            await message.answer(f"❌ Ошибка отправки файла: {str(e)}")
    else:
        await message.answer("❌ Основной бот не инициализирован. Файл не может быть отправлен.")

@dp.callback_query(F.data.startswith("custom_price_accept_"))
async def accept_custom_price(callback: CallbackQuery):
    """Принять цену для кастом-заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать цены.", show_alert=True)
        return
    
    user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Получаем предложение из сообщения
    message_text = callback.message.text or ""
    lines = message_text.split("\n")
    price = ""
    for line in lines:
        if line.startswith("Предложенная цена:"):
            price = line.replace("Предложенная цена:", "").strip()
            break
    
    if not price:
        await callback.answer("Не удалось найти цену в сообщении.", show_alert=True)
        return
    
    # Загружаем заказ
    from orders_manager import get_order_by_user_id, update_order_status
    order = await get_order_by_user_id(user_id, "custom_beat")
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Обновляем цену в заказе
    price_clean = price.replace('$', '').strip()
    await update_order_status(order["id"], "custom_beat", order.get("status", "accepted"), price=price_clean)
    
    # Отправляем сообщение клиенту через основной бот
    if main_bot:
        try:
            lang = "ru"  # По умолчанию русский
            
            try:
                full_price = float(price_clean)
                first_payment = full_price / 2
                client_text = (
                    f"✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Custom Beat\n"
                    f"Описание: {order.get('description', '-')}\n"
                    f"Общая цена: ${full_price:.0f}\n\n"
                    f"⚠️ Оплата разделена на две части:\n"
                    f"💰 Первая оплата (50%): ${first_payment:.0f}\n"
                    f"💰 Вторая оплата (50%): ${first_payment:.0f}\n\n"
                    f"Сначала оплати ${first_payment:.0f} (50%), после выполнения заказа нужно будет оплатить оставшиеся ${first_payment:.0f} (50%).\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            except:
                client_text = (
                    f"✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Custom Beat\n"
                    f"Описание: {order.get('description', '-')}\n"
                    f"Цена: {price.replace('$', '').strip()}\n\n"
                    f"⚠️ Оплата разделена на две части: 50% сейчас, 50% после выполнения заказа.\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            
            # Создаем кнопки оплаты
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            
            payment_inline_ru = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto")],
                    [InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal")],
                    [InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp")],
                    [InlineKeyboardButton(text="🏦 Карта", callback_data="pay_card")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
                ]
            )
            
            payment_inline_en = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto")],
                    [InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal")],
                    [InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp")],
                    [InlineKeyboardButton(text="🏦 Card transfer", callback_data="pay_card")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")]
                ]
            )
            
            payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
            
            await main_bot.send_message(user_id, client_text, reply_markup=payment_kb)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    await callback.message.edit_text(
        f"{message_text}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
    )
    await callback.answer("Цена принята! Клиенту отправлено уведомление.")

@dp.callback_query(F.data.startswith("mixing_price_accept_"))
async def accept_mixing_price(callback: CallbackQuery):
    """Принять цену для сведения."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать цены.", show_alert=True)
        return
    
    user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Получаем предложение из сообщения
    message_text = callback.message.text or ""
    lines = message_text.split("\n")
    price = ""
    for line in lines:
        if line.startswith("Предложенная цена:"):
            price = line.replace("Предложенная цена:", "").strip()
            break
    
    if not price:
        await callback.answer("Не удалось найти цену в сообщении.", show_alert=True)
        return
    
    # Загружаем заказ
    from orders_manager import get_order_by_user_id, update_order_status
    order = await get_order_by_user_id(user_id, "mixing")
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Обновляем цену в заказе
    price_clean = price.replace('$', '').strip()
    await update_order_status(order["id"], "mixing", order.get("status", "accepted"), price=price_clean)
    
    # Отправляем сообщение клиенту через основной бот
    if main_bot:
        try:
            lang = "ru"  # По умолчанию русский
            
            try:
                full_price = float(price_clean)
                first_payment = full_price / 2
                client_text = (
                    f"✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Сведение\n"
                    f"Описание: {order.get('description', '-')}\n"
                    f"Общая цена: ${full_price:.0f}\n\n"
                    f"⚠️ Оплата разделена на две части:\n"
                    f"💰 Первая оплата (50%): ${first_payment:.0f}\n"
                    f"💰 Вторая оплата (50%): ${first_payment:.0f}\n\n"
                    f"Сначала оплати ${first_payment:.0f} (50%), после выполнения заказа нужно будет оплатить оставшиеся ${first_payment:.0f} (50%).\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            except:
                client_text = (
                    f"✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Сведение\n"
                    f"Описание: {order.get('description', '-')}\n"
                    f"Цена: {price.replace('$', '').strip()}\n\n"
                    f"⚠️ Оплата разделена на две части: 50% сейчас, 50% после выполнения заказа.\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            
            # Создаем кнопки оплаты
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            
            payment_inline_ru = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto")],
                    [InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal")],
                    [InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp")],
                    [InlineKeyboardButton(text="🏦 Карта", callback_data="pay_card")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main")]
                ]
            )
            
            payment_inline_en = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="💎 Crypto", callback_data="pay_crypto")],
                    [InlineKeyboardButton(text="💳 PayPal", callback_data="pay_paypal")],
                    [InlineKeyboardButton(text="💵 CashApp", callback_data="pay_cashapp")],
                    [InlineKeyboardButton(text="🏦 Card transfer", callback_data="pay_card")],
                    [InlineKeyboardButton(text="⬅️ Back", callback_data="back_to_main")]
                ]
            )
            
            payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
            
            await main_bot.send_message(user_id, client_text, reply_markup=payment_kb)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    await callback.message.edit_text(
        f"{message_text}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
    )
    await callback.answer("Цена принята! Клиенту отправлено уведомление.")

@dp.callback_query(F.data.startswith("reject_price_"))
async def reject_price(callback: CallbackQuery):
    """Отклонить цену для заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять цены.", show_alert=True)
        return
    
    # Аналогично accept_price
    await callback.answer("Цена отклонена.")

@dp.callback_query(F.data.startswith("confirm_payment_"))
async def confirm_payment(callback: CallbackQuery):
    """Подтвердить оплату заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может подтверждать оплаты.", show_alert=True)
        return
    
    # Формат: confirm_payment_custom_beat_1_123456789_first или confirm_payment_mixing_1_123456789_second
    parts = callback.data.split("_")
    logging.info(f"Парсинг confirm_payment: callback.data={callback.data}, parts={parts}, len={len(parts)}")
    if len(parts) >= 7:
        order_type = f"{parts[2]}_{parts[3]}" if parts[2] == "custom" else parts[2]  # custom_beat или mixing
        order_id = int(parts[4])
        user_id = int(parts[5])
        payment_type = parts[6]  # first или second
        logging.info(f"Распарсено: order_type={order_type}, order_id={order_id}, user_id={user_id}, payment_type={payment_type}")
    else:
        logging.error(f"Неверный формат callback_data: {callback.data}, parts={parts}, len={len(parts)}")
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    from orders_manager import get_order_by_id, update_order_status
    
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Обновляем статус заказа
    price_value = order.get("price")
    if price_value is None:
        logging.error(f"Цена не указана для заказа {order_id} типа {order_type}")
        await callback.answer("Ошибка: цена не указана для заказа.", show_alert=True)
        return
    
    full_price_str = str(price_value).replace("$", "").strip()
    try:
        full_price = float(full_price_str)
        first_payment = full_price / 2
        
        if payment_type == "second":
            await update_order_status(order_id, order_type, "completed", price=str(full_price), second_payment=first_payment)
            status_text = "Вторая оплата подтверждена! Заказ полностью оплачен."
            
            # Логируем оплату
            partner_id = order.get("partner_id")
            if partner_id:
                log_payment(
                    order_id=order_id,
                    order_type=order_type,
                    client_id=user_id,
                    partner_id=partner_id,
                    amount=first_payment,
                    payment_type="second_payment",
                    status="confirmed",
                    notes="Вторая оплата (50%) подтверждена админом"
                )
            # Отправляем сообщение клиенту
            if main_bot:
                try:
                    lang = "ru"  # По умолчанию русский
                    if order_type == "custom_beat":
                        client_text = (
                            "✅ Спасибо за вторую оплату (50%)! Заказ полностью оплачен. Файл уже отправлен."
                            if lang == "ru"
                            else "✅ Thanks for the second payment (50%)! Order is fully paid. File already sent."
                        )
                    else:  # mixing
                        client_text = (
                            "✅ Спасибо за вторую оплату (50%)! Заказ полностью оплачен. Файл уже отправлен."
                            if lang == "ru"
                            else "✅ Thanks for the second payment (50%)! Order is fully paid. File already sent."
                        )
                    await main_bot.send_message(user_id, client_text)
                except Exception as e:
                    logging.error(f"Ошибка отправки сообщения клиенту: {e}")
        else:
            await update_order_status(order_id, order_type, "first_payment_received", price=str(full_price), first_payment=first_payment)
            status_text = "Первая оплата подтверждена! Можно начинать работу."
            
            # Логируем оплату
            partner_id = order.get("partner_id")
            if partner_id:
                log_payment(
                    order_id=order_id,
                    order_type=order_type,
                    client_id=user_id,
                    partner_id=partner_id,
                    amount=first_payment,
                    payment_type="first_payment",
                    status="confirmed",
                    notes="Первая оплата (50%) подтверждена админом"
                )
            # Отправляем сообщение клиенту
            if main_bot:
                try:
                    lang = "ru"  # По умолчанию русский
                    if order_type == "custom_beat":
                        client_text = (
                            "✅ Спасибо за первую оплату (50%)! Я получил твой платеж и начну работу над битом. После выполнения заказа нужно будет оплатить оставшиеся 50%."
                            if lang == "ru"
                            else "✅ Thanks for the first payment (50%)! I've received your payment and will start working on your beat. After completion, you'll need to pay the remaining 50%."
                        )
                    else:  # mixing
                        client_text = (
                            "✅ Спасибо за первую оплату (50%)! Я получил твой платеж и начну работу над сведением. После выполнения заказа нужно будет оплатить оставшиеся 50%."
                            if lang == "ru"
                            else "✅ Thanks for the first payment (50%)! I've received your payment and will start working on your mixing. After completion, you'll need to pay the remaining 50%."
                        )
                    await main_bot.send_message(user_id, client_text)
                except Exception as e:
                    logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    except:
        if payment_type == "second":
            await update_order_status(order_id, order_type, "completed", price=full_price_str, second_payment=True)
        else:
            await update_order_status(order_id, order_type, "first_payment_received", price=full_price_str)
        status_text = f"Оплата подтверждена ({payment_type})!"
    
    # Обновляем сообщение с чеком
    order = await get_order_by_id(order_id, order_type)
    if order:
        # Обновляем caption, убирая "⏳ Ожидает подтверждения"
        caption = callback.message.caption or callback.message.text or ""
        caption = caption.replace("⏳ Ожидает подтверждения оплаты", "✅ Оплата подтверждена")
        caption = caption.replace("⏳ Ожидает подтверждения первой оплаты (50%)", "✅ Первая оплата подтверждена (50%)")
        caption = caption.replace("⏳ Ожидает подтверждения второй оплаты (50%)", "✅ Вторая оплата подтверждена (50%)")
        
        if callback.message.photo:
            await callback.message.edit_caption(caption)
        elif callback.message.document:
            await callback.message.edit_caption(caption)
        else:
            await callback.message.edit_text(caption)
    
    await callback.answer(status_text)

@dp.callback_query(F.data.startswith("reject_payment_"))
async def reject_payment(callback: CallbackQuery):
    """Отклонить оплату заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять оплаты.", show_alert=True)
        return
    
    # Формат: reject_payment_custom_beat_1_123456789 или reject_payment_mixing_1_123456789
    parts = callback.data.split("_")
    logging.info(f"Парсинг reject_payment: callback.data={callback.data}, parts={parts}, len={len(parts)}")
    if len(parts) >= 6:
        order_type = f"{parts[2]}_{parts[3]}" if parts[2] == "custom" else parts[2]  # custom_beat или mixing
        order_id = int(parts[4])
        user_id = int(parts[5]) if len(parts) > 5 else None
        logging.info(f"Распарсено: order_type={order_type}, order_id={order_id}, user_id={user_id}")
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Логируем отклонение оплаты
    from orders_manager import get_order_by_id
    order = await get_order_by_id(order_id, order_type)
    if order:
        partner_id = order.get("partner_id")
        if partner_id and user_id:
            # Определяем тип платежа (first или second)
            payment_type = "first_payment" if order.get("status") != "completed" else "second_payment"
            amount = float(str(order.get("price", "0")).replace("$", "").strip()) / 2
            
            log_payment(
                order_id=order_id,
                order_type=order_type,
                client_id=user_id,
                partner_id=partner_id,
                amount=amount,
                payment_type=payment_type,
                status="rejected",
                notes="Оплата отклонена админом"
            )
    
    # Отправляем сообщение клиенту
    if main_bot and user_id:
        try:
            lang = "ru"  # По умолчанию русский
            client_text = (
                "❌ К сожалению, оплата не подтверждена. Пожалуйста, проверьте реквизиты и попробуйте снова."
                if lang == "ru"
                else "❌ Unfortunately, payment was not confirmed. Please check the details and try again."
            )
            await main_bot.send_message(user_id, client_text)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    # Обновляем сообщение с чеком
    caption = callback.message.caption or callback.message.text or ""
    caption = caption.replace("⏳ Ожидает подтверждения", "❌ Оплата отклонена")
    
    if callback.message.photo:
        await callback.message.edit_caption(caption)
    elif callback.message.document:
        await callback.message.edit_caption(caption)
    else:
        await callback.message.edit_text(caption)
    
    await callback.answer("Оплата отклонена. Клиенту отправлено уведомление.")

@dp.callback_query(F.data.startswith("partner_accept_"))
async def partner_accept_order(callback: CallbackQuery):
    """Партнер принимает заказ."""
    partner_id = callback.from_user.id
    
    # Проверяем, является ли пользователь партнером
    from partners_manager import get_partner
    partner = await get_partner(partner_id)
    if not partner:
        await callback.answer("Вы не являетесь партнером.", show_alert=True)
        return
    
    # Формат: partner_accept_custom_beat_1 или partner_accept_mixing_1
    parts = callback.data.split("_")
    
    if len(parts) >= 5 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Загружаем заказ из БД
    from orders_manager import get_order_by_id, update_order_status
    from datetime import datetime, timedelta
    
    order = await get_order_by_id(order_id, order_type)
    
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Проверяем, не принят ли заказ уже другим партнером
    if order.get("status") != "pending" or order.get("partner_id"):
        # Обновляем сообщение партнера, показывая, что заказ принят другим исполнителем
        try:
            # Получаем обновленный заказ
            updated_order = await get_order_by_id(order_id, order_type)
            if updated_order:
                partner_text = format_order_message(updated_order, partner_id)
                partner_kb = get_partner_order_keyboard(updated_order, partner_id)
                await callback.message.edit_text(partner_text, reply_markup=partner_kb, parse_mode="HTML")
            else:
                order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                partner_username = order.get("partner_username", "другой исполнитель")
                partner_text = (
                    f"Заказ принят другим исполнителем\n\n"
                    f"📦 {order_type_text.capitalize()} {order_id}\n"
                    f"👨‍💼 Исполнитель: @{partner_username}\n\n"
                    f"Этот заказ уже принят другим партнером."
                )
                await callback.message.edit_text(partner_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка обновления сообщения партнера: {e}")
        await callback.answer("Заказ принят другим исполнителем.", show_alert=True)
        return
    
    # Проверяем блокировку (если кто-то уже пытается принять)
    if order.get("accept_lock"):
        lock_time = datetime.fromisoformat(order["accept_lock"])
        # Блокировка действует 5 секунд
        if datetime.now() - lock_time < timedelta(seconds=5):
            await callback.answer("Заказ обрабатывается, попробуйте через секунду.", show_alert=True)
            return
    
    # Устанавливаем блокировку и делаем финальную проверку атомарно
    lock_timestamp = datetime.now().isoformat()
    
    # Двойная проверка и установка блокировки атомарно
    order_check = await get_order_by_id(order_id, order_type)
    if not order_check or order_check.get("status") != "pending" or order_check.get("partner_id"):
        await callback.answer("Этот заказ уже принят другим партнером.", show_alert=True)
        return
    
    # Устанавливаем блокировку
    await update_order_status(order_id, order_type, order_check.get("status", "pending"), accept_lock=lock_timestamp)
    
    # Небольшая задержка для синхронизации
    await asyncio.sleep(0.1)
    
    # Финальная проверка и принятие заказа
    final_order = await get_order_by_id(order_id, order_type)
    if not final_order or final_order.get("status") != "pending" or final_order.get("partner_id"):
        # Снимаем блокировку
        await update_order_status(order_id, order_type, final_order.get("status", "pending"), accept_lock=None)
        await callback.answer("Этот заказ уже принят другим партнером.", show_alert=True)
        return
    
    # Принимаем заказ
    from partners_manager import increment_partner_orders
    
    updated_order = await update_order_status(
        order_id, 
        order_type, 
        "in_progress",  # Статус "В работе" когда партнер принимает заказ
        partner_id=partner_id,
        partner_username=partner.get("username", "ID: " + str(partner_id)),
        accept_lock=None
    )
    
    await increment_partner_orders(partner_id, "accepted")
    
    # Уведомляем клиента
    if main_bot:
        try:
            order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
            order_display_num = get_order_display_number(order)
            partner_username = partner.get('username', 'ID: ' + str(partner_id))
            if order_type == "custom_beat":
                client_text = (
                    f"✅ Отлично! Я принял твой заказ на бит. Номер заказа: {order_display_num}\n\n"
                    f"👨‍💼 Исполнитель: @{partner_username}\n\n"
                    "Я свяжусь с тобой для обсуждения деталей."
                )
            else:  # mixing
                client_text = (
                    f"✅ Отлично! Я принял твой заказ на сведение. Номер заказа: {order_display_num}\n\n"
                    f"👨‍💼 Исполнитель: @{partner_username}\n\n"
                    "Я свяжусь с тобой для обсуждения деталей."
                )
            await main_bot.send_message(order["user_id"], client_text)
        except Exception as e:
            logging.error(f"Ошибка отправки сообщения клиенту: {e}")
    
    # Уведомляем партнера
    try:
        # Получаем обновленный заказ
        if not updated_order:
            updated_order = await get_order_by_id(order_id, order_type)
        if updated_order:
            order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
            partner_text = format_order_message(updated_order, partner_id)
            partner_kb = get_partner_order_keyboard(updated_order, partner_id)
            await callback.message.edit_text(partner_text, reply_markup=partner_kb, parse_mode="HTML")
            await callback.answer("Заказ принят! Свяжись с клиентом для обсуждения деталей.")
        else:
            order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
            partner_text = (
                f"✅ Ты принял заказ на {order_type_text} {order_id}\n\n"
                f"👤 Клиент: @{order['username']} (ID: {order['user_id']})\n\n"
                f"💬 Свяжись с клиентом: https://t.me/{order['username'] if order['username'] != 'no_username' else 'user' + str(order['user_id'])}\n\n"
                f"Договорись с клиентом о деталях, цене и оплате напрямую. После выполнения заказа перечисли процент админу."
            )
            await callback.message.edit_text(partner_text, parse_mode="HTML")
            await callback.answer("Заказ принят! Свяжись с клиентом для обсуждения деталей.")
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения партнеру: {e}")
    
    # Обновляем сообщения у всех партнеров (убираем кнопки Принять/Отклонить)
    try:
        updated_order = await get_order_by_id(order_id, order_type)
        if updated_order:
            partner_message_ids = updated_order.get("partner_message_ids", {})
            if not partner_message_ids:
                partner_message_ids = {}
            elif isinstance(partner_message_ids, str):
                import json
                try:
                    partner_message_ids = json.loads(partner_message_ids)
                except:
                    partner_message_ids = {}
            
            # Обновляем сообщения у всех партнеров, кроме того, кто принял заказ
            for pid_str, msg_id in partner_message_ids.items():
                try:
                    pid = int(pid_str)
                    # Пропускаем партнера, который принял заказ (его сообщение уже обновлено выше)
                    if pid == partner_id:
                        continue
                    
                    logging.info(f"Обновление сообщения у партнера {pid} (заказ принят партнером {partner_id})")
                    
                    # Формируем текст с обновленным статусом
                    partner_text = format_order_message(updated_order, pid)
                    # Создаем клавиатуру без кнопок (заказ принят другим партнером)
                    partner_kb = get_partner_order_keyboard(updated_order, pid)
                    
                    logging.info(f"Текст для партнера {pid}: {partner_text[:100]}...")
                    logging.info(f"Клавиатура для партнера {pid}: {partner_kb}")
                    
                    # Пытаемся обновить сообщение
                    # Сначала пробуем edit_caption (для сообщений с файлом), затем edit_text
                    updated = False
                    try:
                        await bot.edit_message_caption(
                            chat_id=pid,
                            message_id=msg_id,
                            caption=partner_text,
                            reply_markup=partner_kb,
                            parse_mode="HTML"
                        )
                        logging.info(f"Обновлен caption у партнера {pid} (message_id={msg_id})")
                        updated = True
                    except Exception as e1:
                        # Если не удалось обновить caption (сообщение без файла), пробуем edit_text
                        try:
                            await bot.edit_message_text(
                                chat_id=pid,
                                message_id=msg_id,
                                text=partner_text,
                                reply_markup=partner_kb,
                                parse_mode="HTML"
                            )
                            logging.info(f"Обновлено сообщение у партнера {pid} (message_id={msg_id})")
                            updated = True
                        except Exception as e2:
                            logging.error(f"Ошибка обновления сообщения у партнера {pid} (message_id={msg_id}): {e2}")
                    if not updated:
                        logging.warning(f"Не удалось обновить сообщение у партнера {pid} (message_id={msg_id})")
                except (ValueError, KeyError) as e:
                    logging.error(f"Ошибка обработки partner_message_id для партнера {pid_str}: {e}")
                except Exception as e:
                    logging.error(f"Ошибка обновления сообщения у партнера {pid_str}: {e}")
    except Exception as e:
        logging.error(f"Ошибка обновления сообщений партнеров: {e}")
    
    # Уведомляем админа и обновляем сообщение с заказом (убираем кнопки Принять/Отклонить)
    try:
        order_type_text = "Бит на заказ" if order_type == "custom_beat" else "Сведение"
        order_display_num = get_order_display_number(order)
        admin_text = (
            f"✅ Заказ принят партнером\n\n"
            f"📦 {order_type_text} {order_display_num}\n"
            f"👤 Клиент: @{order['username']} (ID: {order['user_id']})\n"
            f"👨‍💼 Партнер: @{partner.get('username', 'ID: ' + str(partner_id))} (ID: {partner_id})\n\n"
            f"💬 Связь с клиентом: https://t.me/{order['username'] if order['username'] != 'no_username' else 'user' + str(order['user_id'])}\n"
            f"💬 Связь с партнером: https://t.me/{partner.get('username', 'user' + str(partner_id))}"
        )
        await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
        
        # Обновляем сообщение с заказом у админа (убираем кнопки Принять/Отклонить)
        try:
            updated_order = await get_order_by_id(order_id, order_type)
            if updated_order and updated_order.get("client_message_id"):
                order_text = format_order_message(updated_order, ORDERS_CHAT_ID)
                admin_kb = get_order_keyboard(updated_order, ORDERS_CHAT_ID)
                await bot.edit_message_text(
                    chat_id=ORDERS_CHAT_ID,
                    message_id=updated_order["client_message_id"],
                    text=order_text,
                    reply_markup=admin_kb,
                    parse_mode="HTML"
                )
        except Exception as e:
            logging.error(f"Ошибка обновления сообщения админа: {e}")
    except Exception as e:
        logging.error(f"Ошибка отправки уведомления админу: {e}")

@dp.callback_query(F.data.startswith("partner_reject_"))
async def partner_reject_order(callback: CallbackQuery):
    """Партнер отклоняет заказ."""
    partner_id = callback.from_user.id
    
    # Проверяем, является ли пользователь партнером
    from partners_manager import get_partner
    partner = await get_partner(partner_id)
    if not partner:
        await callback.answer("Вы не являетесь партнером.", show_alert=True)
        return
    
    # Формат: partner_reject_custom_beat_1 или partner_reject_mixing_1
    parts = callback.data.split("_")
    
    if len(parts) >= 5 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Получаем заказ
    from orders_manager import get_order_by_id
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Проверяем, не принят ли заказ уже другим партнером
    if order.get("status") != "pending" or order.get("partner_id"):
        # Обновляем сообщение партнера, показывая, что заказ принят другим исполнителем
        try:
            # Получаем обновленный заказ
            updated_order = await get_order_by_id(order_id, order_type)
            if updated_order:
                partner_text = format_order_message(updated_order, partner_id)
                partner_kb = get_partner_order_keyboard(updated_order, partner_id)
                await callback.message.edit_text(partner_text, reply_markup=partner_kb, parse_mode="HTML")
            else:
                order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                partner_username = order.get("partner_username", "другой исполнитель")
                partner_text = (
                    f"Заказ принят другим исполнителем\n\n"
                    f"📦 {order_type_text.capitalize()} {order_id}\n"
                    f"👨‍💼 Исполнитель: @{partner_username}\n\n"
                    f"Этот заказ уже принят другим партнером."
                )
                await callback.message.edit_text(partner_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка обновления сообщения партнера: {e}")
        await callback.answer("Заказ принят другим исполнителем.", show_alert=True)
        return
    
    # Обновляем сообщение у админа (убираем кнопки Принять/Отклонить)
    try:
        updated_order = await get_order_by_id(order_id, order_type)
        if updated_order and updated_order.get("client_message_id"):
            order_text = format_order_message(updated_order, ORDERS_CHAT_ID)
            admin_kb = get_order_keyboard(updated_order, ORDERS_CHAT_ID)
            await bot.edit_message_text(
                chat_id=ORDERS_CHAT_ID,
                message_id=updated_order["client_message_id"],
                text=order_text,
                reply_markup=admin_kb,
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"Ошибка обновления сообщения админа: {e}")
    
    # Удаляем сообщение для партнера
    try:
        await callback.message.delete()
        await callback.answer("Заказ отклонен.")
    except Exception as e:
        logging.error(f"Ошибка при отклонении заказа партнером: {e}")
        await callback.answer("Заказ отклонен.")

@dp.callback_query(F.data.startswith("mark_completed_"))
async def mark_order_completed(callback: CallbackQuery):
    """Отметить заказ как выполненный."""
    user_id = callback.from_user.id
    
    # Формат: mark_completed_custom_beat_1 или mark_completed_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Получаем заказ
    from orders_manager import get_order_by_id, update_order_status
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Защита от повторного выполнения: проверяем статус
    if order.get("status") in ["completed", "cancelled", "awaiting_price"]:
        await callback.answer("Этот заказ уже завершен, отменен или ожидает указания суммы.", show_alert=True)
        return
    
    # Проверяем права: только админ или партнер, который принял заказ
    is_admin = user_id == ADMIN_ID
    is_partner = order.get("partner_id") == user_id
    
    if not (is_admin or is_partner):
        await callback.answer("У вас нет прав для выполнения этого действия.", show_alert=True)
        return
    
    # Для админа сразу ставим статус "completed", для партнера - "awaiting_price" (чтобы запросить сумму)
    from orders_manager import update_order_status
    if is_admin:
        # Админ - сразу отмечаем как выполненный
        updated_order = await update_order_status(order_id, order_type, "completed")
        if updated_order:
            order_display_num = get_order_display_number(updated_order)
            await callback.message.answer(
                f"✅ Заказ {order_display_num} отмечен как выполненный!"
            )
    else:
        # Партнер - запрашиваем сумму
        updated_order = await update_order_status(order_id, order_type, "awaiting_price")
        if updated_order:
            dp.waiting_partner_price[user_id] = (order_id, order_type)
            await callback.message.answer(
                f"✅ Заказ {order_id} отмечен как выполненный!\n\n"
                "Напиши сумму заказа:"
            )
    
    if updated_order:
        
        # НЕ запрашиваем сумму у клиента сразу - только после того, как исполнитель укажет сумму
        
        # Обновляем сообщение
        # После нажатия "Заказ выполнен" для партнера кнопки должны исчезнуть
        # Если это партнер и статус "awaiting_price", кнопки не показываем
        if is_partner and updated_order.get("status") == "awaiting_price":
            partner_kb = None  # Убираем кнопки после нажатия "Заказ выполнен"
        else:
            partner_kb = get_partner_order_keyboard(updated_order, user_id) if is_partner else get_order_keyboard(updated_order, user_id)
        
        await callback.message.edit_text(
            format_order_message(updated_order, user_id),
            reply_markup=partner_kb,
            parse_mode="HTML"
        )
        
        # Обновляем сообщения у других партнеров (если заказ принят партнером)
        if is_partner:
            try:
                partner_message_ids = updated_order.get("partner_message_ids", {})
                if not partner_message_ids:
                    partner_message_ids = {}
                elif isinstance(partner_message_ids, str):
                    import json
                    try:
                        partner_message_ids = json.loads(partner_message_ids)
                    except:
                        partner_message_ids = {}
                
                # Обновляем сообщения у других партнеров
                for pid_str, msg_id in partner_message_ids.items():
                    try:
                        pid = int(pid_str)
                        if pid == user_id:
                            continue
                        
                        partner_text = format_order_message(updated_order, pid)
                        partner_kb = get_partner_order_keyboard(updated_order, pid)
                        
                        try:
                            await bot.edit_message_text(
                                chat_id=pid,
                                message_id=msg_id,
                                text=partner_text,
                                reply_markup=partner_kb,
                                parse_mode="HTML"
                            )
                            logging.info(f"Обновлено сообщение у партнера {pid} (message_id={msg_id})")
                        except Exception as e:
                            logging.error(f"Ошибка обновления сообщения у партнера {pid}: {e}")
                    except (ValueError, KeyError) as e:
                        logging.error(f"Ошибка обработки partner_message_id для партнера {pid_str}: {e}")
            except Exception as e:
                logging.error(f"Ошибка обновления сообщений у партнеров: {e}")
        
        if is_partner:
            await callback.answer("✅ Заказ отмечен как выполненный! Укажите сумму.")
        else:
            await callback.answer("✅ Заказ отмечен как выполненный!")
    else:
        await callback.answer("Ошибка: не удалось обновить заказ.", show_alert=True)

@dp.callback_query(F.data.startswith("mark_cancelled_"))
async def mark_order_cancelled(callback: CallbackQuery):
    """Отменить заказ."""
    user_id = callback.from_user.id
    
    # Формат: mark_cancelled_custom_beat_1 или mark_cancelled_mixing_1
    parts = callback.data.split("_")
    if len(parts) >= 4 and parts[2] == "custom" and parts[3] == "beat":
        order_type = "custom_beat"
        order_id = int(parts[4])
    elif len(parts) >= 4 and parts[2] == "mixing":
        order_type = "mixing"
        order_id = int(parts[3])
    else:
        await callback.answer("Ошибка: неверный формат данных.", show_alert=True)
        return
    
    # Получаем заказ
    from orders_manager import get_order_by_id, update_order_status
    order = await get_order_by_id(order_id, order_type)
    if not order:
        await callback.answer("Ошибка: заказ не найден.", show_alert=True)
        return
    
    # Защита от повторной отмены: проверяем статус
    if order.get("status") in ["completed", "cancelled", "rejected"]:
        await callback.answer("Этот заказ уже завершен, отменен или отклонен.", show_alert=True)
        return
    
    # Проверяем права: админ или партнер, который принял заказ, может отменить
    is_admin = user_id == ADMIN_ID
    is_partner = order.get("partner_id") == user_id
    
    if not (is_admin or is_partner):
        await callback.answer("У вас нет прав для отмены этого заказа.", show_alert=True)
        return
    
    # Партнер не может отменить заказ, если он уже указал сумму (статус "awaiting_price" и есть partner_price)
    # Или если заказ уже выполнен (статус "completed")
    if is_partner:
        if order.get("status") == "completed":
            await callback.answer("Нельзя отменить выполненный заказ.", show_alert=True)
            return
        if order.get("status") == "awaiting_price" and order.get("partner_price") is not None:
            await callback.answer("Нельзя отменить заказ после указания суммы. Заказ считается выполненным.", show_alert=True)
            return
    
    # Проверяем, что заказ принят админом или этим партнером
    if is_admin and order.get("partner_id") and order.get("partner_id") != user_id:
        await callback.answer("Этот заказ принят партнером, только он может его отменить.", show_alert=True)
        return
    if is_partner and order.get("partner_id") != user_id:
        await callback.answer("Этот заказ принят другим партнером.", show_alert=True)
        return
    
    # Получаем информацию о партнере
    from partners_manager import get_partner
    partner = await get_partner(user_id)
    
    # Обновляем статус
    from orders_manager import update_order_status
    updated_order = await update_order_status(order_id, order_type, "cancelled")
    
    if updated_order:
        # Уведомляем клиента
        if main_bot:
            try:
                order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                order_display_num = get_order_display_number(order)
                await main_bot.send_message(
                    order["user_id"],
                    f"❌ К сожалению, твой заказ на {order_type_text} {order_display_num} отменен.\n\n"
                    "Если у тебя есть вопросы, свяжись с исполнителем."
                )
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления клиенту: {e}")
        
        # Уведомляем админа
        try:
            order_type_text = "Бит на заказ" if order_type == "custom_beat" else "Сведение"
            admin_text = (
                f"❌ Заказ отменен партнером\n\n"
                f"📦 {order_type_text} {order_id}\n"
                f"👤 Клиент: @{order['username']} (ID: {order['user_id']})\n"
                f"👨‍💼 Партнер: @{partner.get('username', 'ID: ' + str(user_id))} (ID: {user_id})\n\n"
                f"💬 Связь с клиентом: https://t.me/{order['username'] if order['username'] != 'no_username' else 'user' + str(order['user_id'])}\n"
                f"💬 Связь с партнером: https://t.me/{partner.get('username', 'user' + str(user_id))}"
            )
            await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления админу: {e}")
        
        # Обновляем сообщение у партнера/админа, который отменил
        try:
            partner_kb = get_partner_order_keyboard(updated_order, user_id) if is_partner else get_order_keyboard(updated_order, user_id)
            await callback.message.edit_text(
                format_order_message(updated_order, user_id),
                reply_markup=partner_kb,
                parse_mode="HTML"
            )
        except Exception as e:
            logging.error(f"Ошибка обновления сообщения: {e}")
        
        # Обновляем сообщения у админа и других партнеров
        try:
            updated_order = await get_order_by_id(order_id, order_type)
            if updated_order:
                partner_message_ids = updated_order.get("partner_message_ids", {})
                if not partner_message_ids:
                    partner_message_ids = {}
                elif isinstance(partner_message_ids, str):
                    import json
                    try:
                        partner_message_ids = json.loads(partner_message_ids)
                    except:
                        partner_message_ids = {}
                
                # Обновляем сообщение у админа, если заказ отменен партнером
                if is_partner:
                    admin_text = format_order_message(updated_order, ADMIN_ID)
                    admin_kb = get_order_keyboard(updated_order, ADMIN_ID)
                    
                    # Ищем message_id админа
                    admin_msg_id = updated_order.get("client_message_id")
                    if admin_msg_id:
                        try:
                            await bot.edit_message_text(
                                chat_id=ORDERS_CHAT_ID,
                                message_id=admin_msg_id,
                                text=admin_text,
                                reply_markup=admin_kb,
                                parse_mode="HTML"
                            )
                            logging.info(f"Обновлено сообщение у админа (message_id={admin_msg_id})")
                        except Exception as e:
                            logging.error(f"Ошибка обновления сообщения у админа: {e}")
                
                # Обновляем сообщения у других партнеров (если заказ отменен)
                for pid_str, msg_id in partner_message_ids.items():
                    try:
                        pid = int(pid_str)
                        # Пропускаем партнера, который отменил заказ
                        if pid == user_id:
                            continue
                        
                        partner_text = format_order_message(updated_order, pid)
                        partner_kb = get_partner_order_keyboard(updated_order, pid)
                        
                        try:
                            await bot.edit_message_text(
                                chat_id=pid,
                                message_id=msg_id,
                                text=partner_text,
                                reply_markup=partner_kb,
                                parse_mode="HTML"
                            )
                            logging.info(f"Обновлено сообщение у партнера {pid} (message_id={msg_id})")
                        except Exception as e:
                            logging.error(f"Ошибка обновления сообщения у партнера {pid}: {e}")
                    except (ValueError, KeyError) as e:
                        logging.error(f"Ошибка обработки partner_message_id для партнера {pid_str}: {e}")
        except Exception as e:
            logging.error(f"Ошибка обновления сообщений: {e}")
        
        await callback.answer("❌ Заказ отменен.")
    else:
        await callback.answer("Ошибка: не удалось обновить заказ.", show_alert=True)

@dp.message(F.text & ~F.text.startswith("/"))
async def handle_price_input(message: Message):
    """Обработка ввода суммы заказа от исполнителя или клиента."""
    user_id = message.from_user.id
    
    # Проверяем, ожидает ли партнер/админ ввод суммы
    if user_id in dp.waiting_partner_price:
        order_id, order_type = dp.waiting_partner_price[user_id]
        
        try:
            # Сохраняем сумму как текст (любой текст разрешен)
            price_text = message.text.strip()
            
            if not price_text:
                await message.answer("❌ Пожалуйста, укажи сумму заказа:")
                return
            
            # Сохраняем сумму партнера/админа
            from orders_manager import get_order_by_id, update_order_status
            order = await get_order_by_id(order_id, order_type)
            if not order:
                await message.answer("❌ Ошибка: заказ не найден.")
                dp.waiting_partner_price.pop(user_id, None)
                return
            
            # Обновляем заказ с суммой партнера, ставим статус "awaiting_price" (Ожидает сумму от клиента)
            await update_order_status(order_id, order_type, "awaiting_price", partner_price=price_text)
            
            # Убираем из ожидания
            dp.waiting_partner_price.pop(user_id, None)
            
            # Обновляем сообщение у партнера
            try:
                updated_order = await get_order_by_id(order_id, order_type)
                if updated_order:
                    partner_text = format_order_message(updated_order, user_id)
                    # После указания суммы кнопки не показываем (заказ считается выполненным для партнера)
                    partner_kb = None
                    order_display_num = get_order_display_number(updated_order)
                    # Обновляем сообщение с заказом, убирая кнопки
                    # Нужно найти message_id сообщения с заказом
                    partner_message_ids = updated_order.get("partner_message_ids", {})
                    if isinstance(partner_message_ids, str):
                        import json
                        try:
                            partner_message_ids = json.loads(partner_message_ids)
                        except:
                            partner_message_ids = {}
                    
                    msg_id = partner_message_ids.get(str(user_id))
                    if msg_id:
                        try:
                            await bot.edit_message_text(
                                chat_id=user_id,
                                message_id=msg_id,
                                text=partner_text,
                                reply_markup=None,
                                parse_mode="HTML"
                            )
                        except Exception as e:
                            logging.error(f"Ошибка обновления сообщения партнера: {e}")
                    
                    await message.answer(
                        f"✅ Сумма заказа {order_display_num} сохранена: {price_text}"
                    )
            except Exception as e:
                logging.error(f"Ошибка обновления сообщения партнера: {e}")
            
            # Запрашиваем сумму у клиента
            if main_bot:
                try:
                    order = await get_order_by_id(order_id, order_type)
                    if order:
                        order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                        client_user_id = order["user_id"]
                        
                        # Устанавливаем waiting_client_price в elementBot через файл синхронизации
                        try:
                            import json
                            sync_file = "waiting_client_price_sync.json"
                            sync_data = {}
                            if os.path.exists(sync_file):
                                try:
                                    with open(sync_file, 'r', encoding='utf-8') as f:
                                        sync_data = json.load(f)
                                except:
                                    sync_data = {}
                            
                            sync_data[str(client_user_id)] = {
                                "order_id": order_id,
                                "order_type": order_type
                            }
                            
                            with open(sync_file, 'w', encoding='utf-8') as f:
                                json.dump(sync_data, f, ensure_ascii=False, indent=2)
                        except Exception as sync_error:
                            logging.error(f"Ошибка синхронизации waiting_client_price: {sync_error}")
                        
                        # Также устанавливаем в локальном dp (на случай, если клиент отправит в orders_bot)
                        dp.waiting_client_price[client_user_id] = (order_id, order_type)
                        
                        order_display_num = get_order_display_number(order)
                        # Формируем текст в зависимости от типа заказа
                        if order_type == "custom_beat":
                            client_message = f"✅ Твой заказ на бит {order_display_num} выполнен!\n\n"
                        else:  # mixing
                            client_message = f"✅ Твой заказ на сведение {order_display_num} выполнен!\n\n"
                        await main_bot.send_message(
                            client_user_id,
                            client_message + "Напиши сумму заказа:"
                        )
                except Exception as e:
                    logging.error(f"Ошибка отправки запроса суммы клиенту: {e}")
            
            # Уведомляем админа о сумме партнера
            try:
                updated_order = await get_order_by_id(order_id, order_type)
                if updated_order:
                    order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                    admin_text = (
                        f"💰 <b>Исполнитель указал сумму заказа {order_type_text} {order_id}</b>\n\n"
                        f"👨‍💼 Исполнитель указал: {price_text}\n"
                        f"👤 Клиент: ⏳ Ожидает указания суммы\n\n"
                        f"👤 Клиент: @{updated_order['username']} (ID: {updated_order['user_id']})"
                    )
                    if updated_order.get("partner_id"):
                        partner_username = updated_order.get("partner_username", f"ID: {updated_order['partner_id']}")
                        admin_text += f"\n👨‍💼 Исполнитель: @{partner_username} (ID: {updated_order['partner_id']})"
                    await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
            except Exception as e:
                logging.error(f"Ошибка отправки суммы админу: {e}")
            
            return
        except Exception as e:
            logging.error(f"Ошибка обработки суммы от партнера: {e}")
            await message.answer("❌ Произошла ошибка. Попробуй еще раз:")
            return
    
    # Проверяем, ожидает ли клиент ввод суммы (через основной бот)
    if user_id in dp.waiting_client_price:
        order_id, order_type = dp.waiting_client_price[user_id]
        
        try:
            # Сохраняем сумму как текст (любой текст разрешен)
            price_text = message.text.strip()
            
            if not price_text:
                if main_bot:
                    await main_bot.send_message(user_id, "❌ Пожалуйста, укажи сумму заказа:")
                return
            
            # Сохраняем сумму клиента
            from orders_manager import get_order_by_id, update_order_status
            order = await get_order_by_id(order_id, order_type)
            if not order:
                if main_bot:
                    await main_bot.send_message(user_id, "❌ Ошибка: заказ не найден.")
                dp.waiting_client_price.pop(user_id, None)
                return
            
            # Обновляем заказ с суммой клиента (как текст)
            await update_order_status(order_id, order_type, order.get("status", "awaiting_price"), client_price=price_text)
            
            # Убираем из ожидания
            dp.waiting_client_price.pop(user_id, None)
            
            # Удаляем из файла синхронизации
            try:
                import json
                sync_file = "waiting_client_price_sync.json"
                if os.path.exists(sync_file):
                    with open(sync_file, 'r', encoding='utf-8') as f:
                        sync_data = json.load(f)
                    sync_data.pop(str(user_id), None)
                    with open(sync_file, 'w', encoding='utf-8') as f:
                        json.dump(sync_data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logging.error(f"Ошибка удаления waiting_client_price из файла: {e}")
            
            # Проверяем, есть ли уже сумма от партнера/админа
            order = await get_order_by_id(order_id, order_type)
            if order.get("partner_price"):
                # Обе суммы есть - отмечаем как completed
                await update_order_status(order_id, order_type, "completed", client_price=price_text)
                
                # Отправляем суммы админу
                try:
                    order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                    order_display_num = get_order_display_number(order)
                    admin_text = (
                        f"💰 <b>Суммы заказа {order_type_text} {order_display_num}</b>\n\n"
                        f"👨‍💼 Исполнитель указал: {order['partner_price']}\n"
                        f"👤 Клиент указал: {price_text}\n\n"
                        f"👤 Клиент: @{order['username']} (ID: {order['user_id']})"
                    )
                    if order.get("partner_id"):
                        partner_username = order.get("partner_username", f"user{order['partner_id']}")
                        admin_text += f"\n👨‍💼 Исполнитель: @{partner_username} (ID: {order['partner_id']})"
                    
                    await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Ошибка отправки сумм админу: {e}")
                
                if main_bot:
                    await main_bot.send_message(
                        user_id,
                        f"✅ Сумма сохранена: {price_text}\n\n"
                        "Спасибо за заказ!"
                    )
            else:
                # Только клиент указал сумму - отправляем уведомление админу
                try:
                    order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                    order_display_num = get_order_display_number(order)
                    admin_text = (
                        f"💰 <b>Клиент указал сумму заказа {order_type_text} {order_display_num}</b>\n\n"
                        f"👤 Клиент указал: {price_text}\n"
                        f"👨‍💼 Исполнитель: ⏳ Ожидает указания суммы\n\n"
                        f"👤 Клиент: @{order['username']} (ID: {order['user_id']})"
                    )
                    if order.get("partner_id"):
                        partner_username = order.get("partner_username", f"user{order['partner_id']}")
                        admin_text += f"\n👨‍💼 Исполнитель: @{partner_username} (ID: {order['partner_id']})"
                    
                    await bot.send_message(ORDERS_CHAT_ID, admin_text, parse_mode="HTML")
                except Exception as e:
                    logging.error(f"Ошибка отправки суммы админу: {e}")
                
                if main_bot:
                    await main_bot.send_message(
                        user_id,
                        f"✅ Сумма сохранена: {price_text}\n\n"
                        "Спасибо! Заказ будет завершен после обработки."
                    )
        except Exception as e:
            logging.error(f"Ошибка обработки суммы от клиента: {e}")
            if main_bot:
                await main_bot.send_message(user_id, "❌ Произошла ошибка. Попробуй еще раз:")

def get_partner_order_keyboard(order: dict, user_id: int = None) -> InlineKeyboardMarkup:
    """Создает клавиатуру для партнеров с кнопками Принять/Отклонить."""
    buttons = []
    
    # Партнеры видят кнопки "Принять/Отклонить" только для pending заказов
    if order["status"] == "pending" and not order.get("partner_id"):
        buttons.append([
            InlineKeyboardButton(
                text="✅ Принять заказ",
                callback_data=f"partner_accept_{order['type']}_{order['id']}"
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=f"partner_reject_{order['type']}_{order['id']}"
            )
        ])
    # Кнопки "Заказ выполнен" и "Заказ отменен" для принятых заказов
    # НЕ показываем кнопки для заказов со статусом "awaiting_price" с указанной суммой (партнер уже указал сумму, заказ считается выполненным)
    # НЕ показываем кнопки для заказов со статусом "completed" (заказ уже выполнен)
    elif order["status"] in ["in_progress", "first_payment_received"]:
        # Показываем кнопки только если это партнер, который принял заказ
        if order.get("partner_id") == user_id:
            # Кнопки в разных строках для лучшей видимости
            buttons.append([
                InlineKeyboardButton(
                    text="✅ Заказ выполнен",
                    callback_data=f"mark_completed_{order['type']}_{order['id']}"
                )
            ])
            # Кнопку "Заказ отменен" показываем всегда для этих статусов (пока партнер не указал сумму)
            buttons.append([
                InlineKeyboardButton(
                    text="❌ Заказ отменен",
                    callback_data=f"mark_cancelled_{order['type']}_{order['id']}"
                )
            ])
    # Для статуса "awaiting_price" НЕ показываем кнопки, если партнер уже указал сумму
    elif order["status"] == "awaiting_price":
        # Если это партнер, который принял заказ, и он уже указал сумму - не показываем кнопки
        if order.get("partner_id") == user_id and order.get("partner_price") is not None:
            # Не показываем кнопки - заказ считается выполненным
            pass
        # Если партнер еще не указал сумму (не должно быть такого случая, но на всякий случай)
        elif order.get("partner_id") == user_id:
            buttons.append([
                InlineKeyboardButton(
                    text="✅ Заказ выполнен",
                    callback_data=f"mark_completed_{order['type']}_{order['id']}"
                )
            ])
        # Если заказ принят другим партнером, не показываем кнопки
        # (сообщение будет обновлено при попытке нажать кнопку)
    
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

async def send_order_to_bot(order: dict, file_id: str = None, orders_bot_instance: Bot = None, admin_chat_id: int = None):
    """Отправляет заказ в бот заказов партнерам и админу. Вызывается из основного бота."""
    # Импортируем функции для работы с заказами
    from orders_manager import update_order_status
    
    # Используем переданные параметры или глобальные переменные
    bot_instance = orders_bot_instance if orders_bot_instance else bot
    chat_id = admin_chat_id if admin_chat_id else ORDERS_CHAT_ID
    
    logging.info(f"send_order_to_bot вызвана: order_id={order.get('id')}, order_type={order.get('type')}, file_id={file_id}, bot_instance={bot_instance}, chat_id={chat_id}")
    
    if not bot_instance:
        logging.error("Бот для заказов не инициализирован!")
        return
    
    # Получаем всех активных партнеров (партнеры могут принимать оба типа заказов)
    from partners_manager import get_active_partners
    partners = await get_active_partners()
    logging.info(f"Найдено активных партнеров: {len(partners)}")
    
    # Текст для админа
    order_text = format_order_message(order, chat_id)
    logging.info(f"Сформирован текст заказа для админа: {order_text[:100]}...")
    
    # Клавиатура для партнеров (передаем partner_id для каждого партнера отдельно)
    # Клавиатура для админа
    admin_kb = get_order_keyboard(order, chat_id)
    logging.info(f"Клавиатура для админа: {admin_kb}")
    
    # Отправляем заказ админу
    try:
        if file_id:
            if order["type"] == "custom_beat":
                msg = await bot_instance.send_audio(chat_id=chat_id, audio=file_id, caption=order_text, reply_markup=admin_kb, parse_mode="HTML")
            else:
                msg = await bot_instance.send_document(chat_id=chat_id, document=file_id, caption=order_text, reply_markup=admin_kb, parse_mode="HTML")
            # Сохраняем ID сообщения в заказе
            await update_order_status(order["id"], order["type"], order["status"], client_message_id=msg.message_id)
        else:
            msg = await bot_instance.send_message(chat_id=chat_id, text=order_text, reply_markup=admin_kb, parse_mode="HTML")
            # Сохраняем ID сообщения в заказе
            await update_order_status(order["id"], order["type"], order["status"], client_message_id=msg.message_id)
        logging.info(f"✅ Заказ {order['id']} отправлен админу (chat_id: {chat_id}, message_id: {msg.message_id})")
    except Exception as e:
        logging.error(f"❌ Ошибка отправки заказа админу: {e}", exc_info=True)
    
    # Отправляем заказ всем активным партнерам
    partner_message_ids = {}  # Словарь для хранения message_id партнеров
    if partners:
        for partner in partners:
            try:
                partner_id = partner["user_id"]
                # Формируем текст для каждого партнера отдельно с его user_id
                partner_order_text = format_order_message(order, partner_id)
                # Создаем клавиатуру для каждого партнера отдельно
                partner_kb = get_partner_order_keyboard(order, partner_id)
                msg = None
                if file_id:
                    if order["type"] == "custom_beat":
                        msg = await bot_instance.send_audio(
                            chat_id=partner_id,
                            audio=file_id,
                            caption=partner_order_text,
                            reply_markup=partner_kb,
                            parse_mode="HTML"
                        )
                    else:
                        msg = await bot_instance.send_document(
                            chat_id=partner_id,
                            document=file_id,
                            caption=partner_order_text,
                            reply_markup=partner_kb,
                            parse_mode="HTML"
                        )
                else:
                    msg = await bot_instance.send_message(
                        chat_id=partner_id,
                        text=partner_order_text,
                        reply_markup=partner_kb,
                        parse_mode="HTML"
                    )
                # Сохраняем message_id партнера
                if msg:
                    partner_message_ids[str(partner_id)] = msg.message_id
                logging.info(f"Заказ {order['id']} отправлен партнеру {partner_id} ({partner.get('name', partner.get('username'))}), message_id={msg.message_id if msg else None}")
            except Exception as e:
                logging.error(f"Ошибка отправки заказа партнеру {partner.get('user_id')}: {e}", exc_info=True)
        
        # Сохраняем message_id всех партнеров в заказе
        if partner_message_ids:
            from orders_manager import update_order_status
            await update_order_status(order["id"], order["type"], order["status"], partner_message_ids=partner_message_ids)

async def main():
    """Запуск бота."""
    from database import init_db
    
    # Инициализируем БД при запуске
    await init_db()
    
    logging.info("Запуск бота для заказов...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

