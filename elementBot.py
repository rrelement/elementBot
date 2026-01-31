import asyncio
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, Voice, CallbackQuery
)
import logging
import aiohttp
import os
import tempfile
import librosa
import numpy as np
from collections import Counter
from dotenv import load_dotenv
from openai import AsyncOpenAI
import httpx
import json
import re
from datetime import datetime
# Импорты из orders_manager теперь делаются локально, так как функции асинхронные

# Загружаем переменные из .env файла
load_dotenv()

logging.basicConfig(level=logging.INFO)

# Загружаем токены из переменных окружения
TOKEN = os.getenv("TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "830030557"))

if not TOKEN:
    raise ValueError("TOKEN не найден в переменных окружения. Проверьте файл .env")

# Токены для новых ботов (нужно указать в .env)
ORDERS_BOT_TOKEN = os.getenv("ORDERS_BOT_TOKEN", "")
PURCHASES_BOT_TOKEN = os.getenv("PURCHASES_BOT_TOKEN", "")

# Настройки прокси (если Telegram заблокирован, укажите в .env)
# Формат: PROXY_URL=http://user:pass@proxy.com:port или PROXY_URL=socks5://user:pass@proxy.com:port
PROXY_URL = os.getenv("PROXY_URL", None)

# Настройки таймаутов (увеличены для нестабильных соединений)
from aiogram.client.session.aiohttp import AiohttpSession

# Создаем сессию с увеличенными таймаутами
# В aiogram timeout должен быть числом (в секундах), не ClientTimeout объектом
if PROXY_URL:
    session = AiohttpSession(proxy=PROXY_URL)
else:
    session = AiohttpSession()

# Устанавливаем таймаут как число (в секундах) - aiogram использует это значение
session.timeout = 60  # 60 секунд общий таймаут

# Боты для заказов и покупок
orders_bot = None
purchases_bot = None

if ORDERS_BOT_TOKEN:
    try:
        orders_bot = Bot(token=ORDERS_BOT_TOKEN, session=session)
        logging.info("Бот для заказов инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка инициализации бота для заказов: {e}")

if PURCHASES_BOT_TOKEN:
    try:
        purchases_bot = Bot(token=PURCHASES_BOT_TOKEN, session=session)
        logging.info("Бот для покупок инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка инициализации бота для покупок: {e}")

# DeepSeek настройки
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")  # API ключ DeepSeek

# Проверка загрузки ключа
if not DEEPSEEK_API_KEY:
    logging.warning("DEEPSEEK_API_KEY не найден в переменных окружения. Проверьте файл .env")
else:
    logging.info(f"DeepSeek API ключ загружен: {DEEPSEEK_API_KEY[:15]}...{DEEPSEEK_API_KEY[-10:]}")

# Создаем основной бот с увеличенными таймаутами
bot = Bot(token=TOKEN, session=session)
dp = Dispatcher()

# Инициализация DeepSeek клиента (совместим с OpenAI API)
deepseek_client = None
if DEEPSEEK_API_KEY:
    try:
        # DeepSeek использует OpenAI-совместимый API
        deepseek_client = AsyncOpenAI(
            api_key=DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com/v1"
        )
        logging.info("DeepSeek клиент успешно инициализирован.")
    except Exception as e:
        logging.error(f"Ошибка инициализации DeepSeek клиента: {e}")
        deepseek_client = None
else:
    deepseek_client = None
    logging.warning("DeepSeek не настроен. AI чат будет недоступен.")

# Состояния пользователей
dp.current_payment_users = set()  # пользователи, которые нажали "Я оплатил"
dp.user_language = {}  # user_id -> "ru" / "en"
dp.purchase_state = {}  # user_id -> {"beat": str, "license": str}
dp.custom_order_waiting = set()  # пользователи, ждущие отправки рефа / описания для бита на заказ
dp.offer_waiting_price = set()  # пользователи, которые нажали "Предложить цену" и должны прислать цену
dp.pending_offers = {}  # user_id -> {"beat": str, "license": str, "price": str} - предложения, ожидающие ответа админа
dp.pending_custom_orders = {}  # user_id -> {"description": str, "file_id": str or None} - заказы битов на заказ, ожидающие ответа админа
dp.custom_order_waiting_price = set()  # пользователи, которые отправили цену для кастом-заказа и ждут ответа админа
dp.mixing_order_waiting = set()  # пользователи, которые заказывают сведение
dp.pending_mixing_orders = {}  # user_id -> {"description": str, "file_id": str or None} - заказы на сведение, ожидающие ответа админа
dp.mixing_order_waiting_price = set()  # пользователи, которые отправили цену для сведения и ждут ответа админа
dp.waiting_card_details = {}  # user_id клиента -> user_id клиента (для админа, который будет отправлять реквизиты
dp.admin_sending_file = None  # user_id клиента, которому админ сейчас отправляет файл)
dp.admin_sending_card = None  # user_id клиента, которому админ сейчас отправляет реквизиты
dp.waiting_client_price = {}  # {user_id: (order_id, order_type)} - клиент должен указать сумму заказа
dp.admin_offering_price = {}  # user_id клиента -> user_id клиента (для админа, который предлагает цену)
dp.pending_admin_offers = {}  # user_id клиента -> {"price": str, "beat": str} - предложения цены от админа, ожидающие ответа клиента
dp.key_bpm_waiting = set()  # пользователи, которые используют Key & BPM
dp.contact_waiting = set()  # пользователи, которые хотят связаться с админом
dp.contact_history = {}  # user_id -> [{"role": "user"/"assistant", "content": str}] - история разговоров для AI

# --- Функции AI ---
async def generate_ai_response(user_message: str, user_id: int, lang: str = "ru") -> str:
    """Генерирует ответ через DeepSeek API на основе сообщения пользователя и истории разговора."""
    if not deepseek_client:
        if lang == "ru":
            return "Извини, AI чат временно недоступен. Свяжись с админом: https://t.me/rrelement1"
        else:
            return "Sorry, AI chat is temporarily unavailable. Contact admin: https://t.me/rrelement1"
    
    # Проверяем запросы на внешние API перед отправкой в AI
    message_lower = user_message.lower()
    
    # Инициализируем переменные для финансовых данных
    financial_context = ""
    mentioned_assets = []
    asset = None
    
    # Функция для извлечения тикеров из текста (например, AVGO, CAT, MSFT)
    def extract_tickers(text):
        """Извлекает возможные тикеры из текста."""
        # Черный список слов, которые не являются тикерами
        blacklist = {
            'AND', 'OR', 'NOT', 'THE', 'FOR', 'ARE', 'BUT', 'YOU', 'ALL', 'CAN', 'HER', 'WAS', 'ONE', 'OUR', 'OUT', 'DAY', 'GET', 'HAS', 'HIM', 'HIS', 'HOW', 'ITS', 'MAY', 'NEW', 'NOW', 'OLD', 'SEE', 'TWO', 'WAY', 'WHO', 'BOY', 'DID', 'ITS', 'LET', 'PUT', 'SAY', 'SHE', 'TOO', 'USE', 'KEY', 'BPM', 'TG', 'ID', 'AI', 'API', 'URL', 'HTTP', 'HTTPS', 'WWW', 'COM', 'ORG', 'NET', 'RU', 'EN', 'US', 'UK', 'EU', 'PY', 'PYTHON', 'JS', 'JAVASCRIPT', 'HTML', 'CSS', 'SQL', 'PHP', 'GO', 'RUST', 'JAVA', 'C++', 'CPP', 'C#', 'CSHARP', 'SWIFT', 'KOTLIN', 'DART', 'RUBY', 'PERL', 'LUA', 'SCALA', 'R', 'MATLAB', 'JULIA', 'HASKELL', 'ERLANG', 'ELIXIR', 'CLOJURE', 'F#', 'FSHARP', 'VB', 'VBNET', 'DELPHI', 'PASCAL', 'FORTRAN', 'COBOL', 'ASSEMBLY', 'ASM', 'BASH', 'SHELL', 'POWERSHELL', 'ZSH', 'FISH', 'VIM', 'EMACS', 'GIT', 'SVN', 'CVS', 'DOCKER', 'KUBERNETES', 'K8S', 'AWS', 'AZURE', 'GCP', 'GCE', 'S3', 'EC2', 'LAMBDA', 'API', 'REST', 'SOAP', 'GRAPHQL', 'JSON', 'XML', 'YAML', 'TOML', 'INI', 'CSV', 'TSV', 'PDF', 'DOC', 'DOCX', 'XLS', 'XLSX', 'PPT', 'PPTX', 'ZIP', 'RAR', '7Z', 'TAR', 'GZ', 'BZ2', 'XZ', 'MP3', 'MP4', 'AVI', 'MKV', 'FLV', 'WEBM', 'OGG', 'WAV', 'AAC', 'FLAC', 'M4A', 'WMA', 'JPG', 'JPEG', 'PNG', 'GIF', 'BMP', 'TIFF', 'SVG', 'WEBP', 'ICO', 'ICO', 'PSD', 'AI', 'EPS', 'PDF', 'TXT', 'RTF', 'MD', 'MARKDOWN', 'HTML', 'HTM', 'CSS', 'JS', 'JSX', 'TS', 'TSX', 'VUE', 'SVELTE', 'ANGULAR', 'REACT', 'NODE', 'NPM', 'YARN', 'PNPM', 'BUN', 'DENO', 'NEXT', 'NUXT', 'GATSBY', 'VITE', 'WEBPACK', 'ROLLUP', 'ESBUILD', 'SWC', 'BABEL', 'TYPESCRIPT', 'JAVASCRIPT', 'COFFEESCRIPT', 'LIVESCRIPT', 'DART', 'FLUTTER', 'REACTNATIVE', 'IONIC', 'CORDOVA', 'PHONEGAP', 'XAMARIN', 'MAUI', 'UNITY', 'UNREAL', 'GODOT', 'CRYENGINE', 'BLENDER', 'MAYA', '3DSMAX', 'CINEMA4D', 'HOUDINI', 'ZBRUSH', 'SUBSTANCE', 'MARVELOUS', 'CLO', 'OPTITEX', 'LECTRA', 'GERBER', 'AUTOCAD', 'SOLIDWORKS', 'CATIA', 'INVENTOR', 'FUSION360', 'SKETCHUP', 'REVIT', 'ARCHICAD', 'RHINO', 'GRASSHOPPER', 'MAYA', '3DSMAX', 'CINEMA4D', 'HOUDINI', 'ZBRUSH', 'SUBSTANCE', 'MARVELOUS', 'CLO', 'OPTITEX', 'LECTRA', 'GERBER', 'AUTOCAD', 'SOLIDWORKS', 'CATIA', 'INVENTOR', 'FUSION360', 'SKETCHUP', 'REVIT', 'ARCHICAD', 'RHINO', 'GRASSHOPPER'
        }
        
        tickers = []
        # Паттерн для тикеров: минимум 3 символа, максимум 10, только заглавные буквы/цифры
        # Исключаем слишком короткие слова (меньше 3 символов), чтобы не ловить обычные слова
        ticker_pattern = r'\b([A-Z0-9]{3,10}(?:\.[A-Z])?|[A-Z0-9]{3,10}=F|\^[A-Z0-9]{3,10}|[A-Z0-9]{3,10}=X)\b'
        found_tickers = re.findall(ticker_pattern, text.upper())
        
        # Фильтруем по черному списку
        for ticker in found_tickers:
            # Убираем специальные символы для проверки
            clean_ticker = ticker.split('=')[0].split('.')[0].replace('^', '')
            if clean_ticker not in blacklist and len(clean_ticker) >= 3:
                tickers.append(ticker)
        
        # Паттерн для валютных пар (EUR/USD, GBP/USD и т.д.)
        currency_pair_pattern = r'\b([A-Z]{3}/[A-Z]{3})\b'
        currency_pairs = re.findall(currency_pair_pattern, text.upper())
        # Конвертируем в формат Yahoo Finance (EURUSD=X)
        for pair in currency_pairs:
            tickers.append(pair.replace('/', '') + "=X")
        
        return list(set(tickers))  # Убираем дубликаты
    
    # Функция для маппинга названий компаний к тикерам
    company_name_map = {
        # Популярные компании
        "broadcom": "AVGO",
        "caterpillar": "CAT",
        "apple": "AAPL",
        "microsoft": "MSFT",
        "google": "GOOGL",
        "alphabet": "GOOGL",
        "tesla": "TSLA",
        "nvidia": "NVDA",
        "amazon": "AMZN",
        "meta": "META",
        "facebook": "META",
        "netflix": "NFLX",
        "jpmorgan": "JPM",
        "bank of america": "BAC",
        "walmart": "WMT",
        "coca cola": "KO",
        "pepsi": "PEP",
        "disney": "DIS",
        "visa": "V",
        "mastercard": "MA",
        "paypal": "PYPL",
        "intel": "INTC",
        "amd": "AMD",
        "ibm": "IBM",
        "oracle": "ORCL",
        "cisco": "CSCO",
        "boeing": "BA",
        "general electric": "GE",
        "ford": "F",
        "general motors": "GM",
        "exxon": "XOM",
        "chevron": "CVX",
        "johnson & johnson": "JNJ",
        "pfizer": "PFE",
        "merck": "MRK",
        "procter & gamble": "PG",
        "unilever": "UL",
        "nike": "NKE",
        "adidas": "ADDYY",
        "mcdonalds": "MCD",
        "starbucks": "SBUX",
        "home depot": "HD",
        "lowes": "LOW",
        "target": "TGT",
        "costco": "COST",
    }
    
    # Проверка на финансовую аналитику (расширенный список ключевых слов)
    financial_keywords = [
        "нефть", "oil", "wti", "brent", "crude",
        "золото", "gold", "xau",
        "серебро", "silver",
        "акции", "stock", "акция", "stocks", "etf", "актив", "asset",
        "sp500", "nasdaq", "dow",
        "стоит ли покупать", "стоит покупать", "should i buy", "buy",
        "прогноз", "forecast", "анализ", "analysis", "тренд", "trend",
        "что будет", "what will", "ближайшие", "next",
        "цена", "price", "курс", "rate", "котировки", "quotes"
    ]
    
    # Извлекаем тикеры из сообщения
    extracted_tickers = extract_tickers(user_message)
    
    # Проверяем названия компаний в тексте
    mentioned_companies = []
    for company_name, ticker in company_name_map.items():
        if company_name in message_lower:
            mentioned_companies.append(ticker)
    
    # Объединяем все найденные инструменты
    all_found_instruments = extracted_tickers + mentioned_companies
    
    # Проверяем, не является ли запрос про изучение языков программирования или другие нефинансовые темы
    programming_keywords = ["изучить", "изучение", "учить", "обучение", "learn", "study", "tutorial", "курс", "course", 
                           "python", "py", "javascript", "js", "java", "c++", "cpp", "c#", "csharp", "php", "ruby", 
                           "go", "rust", "swift", "kotlin", "dart", "scala", "r", "matlab", "julia", "haskell",
                           "программирование", "programming", "код", "code", "разработка", "development", "dev",
                           "время", "time", "сколько", "how long", "how much time", "потратить", "spend"]
    
    is_programming_query = any(keyword in message_lower for keyword in programming_keywords)
    
    # Если найдены финансовые инструменты или есть финансовые ключевые слова
    # НО не является запросом про программирование
    if (all_found_instruments or any(keyword in message_lower for keyword in financial_keywords)) and not is_programming_query:
        # Определяем, о каком инструменте идет речь
        asset = None
        
        # Нефть
        if any(word in message_lower for word in ["wti", "нефть wti", "crude oil"]):
            asset = "wti"
        elif any(word in message_lower for word in ["brent", "нефть brent"]):
            asset = "brent"
        elif "нефть" in message_lower or "oil" in message_lower:
            asset = "wti"  # По умолчанию WTI
        
        # Золото
        elif any(word in message_lower for word in ["золото", "gold", "xau"]):
            asset = "gold"
        
        # Серебро
        elif any(word in message_lower for word in ["серебро", "silver"]):
            asset = "silver"
        
        # Индексы
        elif any(word in message_lower for word in ["sp500", "s&p 500", "s&p500", "s&p"]):
            asset = "sp500"
        elif any(word in message_lower for word in ["nasdaq", "ndx", "nasdaq-100", "nasdaq100"]):
            # Если упомянут NDX или NASDAQ-100, используем NDX
            if any(word in message_lower for word in ["ndx", "nasdaq-100", "nasdaq100", "100"]):
                asset = "ndx"
            else:
                asset = "nasdaq"
        elif "dow" in message_lower or "dow jones" in message_lower:
            asset = "dow"
        elif "индекс" in message_lower or "index" in message_lower:
            # Если просто "индекс", по умолчанию используем S&P 500
            asset = "sp500"
        
        # Акции
        elif any(word in message_lower for word in ["apple", "aapl"]):
            asset = "apple"
        elif any(word in message_lower for word in ["microsoft", "msft"]):
            asset = "microsoft"
        elif any(word in message_lower for word in ["google", "googl", "alphabet"]):
            asset = "google"
        elif any(word in message_lower for word in ["tesla", "tsla"]):
            asset = "tesla"
        elif any(word in message_lower for word in ["nvidia", "nvda"]):
            asset = "nvidia"
        
        # ETF
        elif any(word in message_lower for word in ["arkk", "ark innovation"]):
            asset = "arkk"
        elif any(word in message_lower for word in ["tecl", "direxion technology"]):
            asset = "tecl"
        elif any(word in message_lower for word in ["spy", "spdr s&p"]):
            asset = "spy"
        elif any(word in message_lower for word in ["qqq", "invesco qqq"]):
            asset = "qqq"
        elif any(word in message_lower for word in ["vti", "vanguard total"]):
            asset = "vti"
        elif any(word in message_lower for word in ["voo", "vanguard s&p"]):
            asset = "voo"
        elif any(word in message_lower for word in ["arkq", "ark autonomous"]):
            asset = "arkq"
        elif any(word in message_lower for word in ["arkg", "ark genomic"]):
            asset = "arkg"
        elif any(word in message_lower for word in ["arkw", "ark next generation"]):
            asset = "arkw"
        elif any(word in message_lower for word in ["soxl", "direxion semiconductor"]):
            asset = "soxl"
        elif any(word in message_lower for word in ["tqqq", "proshares ultrapro qqq"]):
            asset = "tqqq"
        elif any(word in message_lower for word in ["sqqq", "proshares ultrapro short"]):
            asset = "sqqq"
        
        # Функция для нормализации названия актива
        def normalize_asset_name(asset_name):
            if asset_name in ["msft"]:
                return "microsoft"
            elif asset_name in ["aapl"]:
                return "apple"
            elif asset_name in ["nvda"]:
                return "nvidia"
            elif asset_name in ["tsla"]:
                return "tesla"
            elif asset_name in ["googl"]:
                return "google"
            elif asset_name in ["btc", "bitcoin", "биткоин", "биткойн"]:
                return "bitcoin"
            elif asset_name in ["eth", "ethereum", "эфириум", "эфир"]:
                return "ethereum"
            elif asset_name in ["usdt", "tether", "тезер"]:
                return "usdt"
            elif asset_name in ["ltc", "litecoin", "лайткоин"]:
                return "ltc"
            elif asset_name in ["золото"]:
                return "gold"
            elif asset_name in ["серебро"]:
                return "silver"
            elif asset_name in ["sp500", "s&p 500", "s&p500"]:
                return "sp500"
            else:
                return asset_name
        
        # Все возможные активы для сравнения
        all_assets = [
            # Акции
            "microsoft", "msft", "apple", "aapl", "nvidia", "nvda", "tesla", "tsla", "google", "googl",
            # ETF
            "arkk", "tecl", "spy", "qqq", "vti", "voo", "arkq", "arkg", "arkw", "soxl", "tqqq", "sqqq",
            # Криптовалюты
            "bitcoin", "btc", "ethereum", "eth", "usdt", "tether", "ltc", "litecoin",
            # Товары
            "wti", "brent", "gold", "золото", "silver", "серебро",
            # Индексы
            "sp500", "nasdaq", "dow"
        ]
        
        # Проверка на упоминание нескольких активов одновременно
        mentioned_assets = []
        for asset_name in all_assets:
            if asset_name in message_lower:
                normalized = normalize_asset_name(asset_name)
                if normalized not in mentioned_assets:
                    mentioned_assets.append(normalized)
        
        # Добавляем найденные тикеры и компании
        for ticker in all_found_instruments:
            if ticker not in mentioned_assets:
                mentioned_assets.append(ticker)
        
        # Ключевые слова для полного анализа (вопросы про покупку, прогноз и т.д.)
        analysis_keywords = ["прогноз", "forecast", "анализ", "analysis", "тренд", "trend", 
                            "стоит", "should", "buy", "покупать", "можно", "брать", "можно брать",
                            "что будет", "what will", "можно ли", "can i", "рекомендация", "recommendation",
                            "что с", "what about", "скажи", "tell me", "про", "about"]
        
        # Получаем финансовые данные для добавления в контекст AI
        financial_context = ""
        
        # Если упомянуто несколько активов (2 или больше), получаем данные для сравнения
        # Ограничиваем количество активов для сравнения (максимум 5), чтобы избежать ошибок
        if len(mentioned_assets) > 1:
            try:
                # Берем только первые 5 активов для сравнения
                assets_to_compare = mentioned_assets[:5]
                comparison_result = await compare_assets(assets_to_compare, lang)
                # Добавляем финансовые данные в контекст сообщения для AI
                financial_context = f"\n\n[Финансовые данные для справки: {comparison_result}]"
            except Exception as e:
                logging.error(f"Ошибка при сравнении активов: {e}")
                # Продолжаем без финансового контекста, если сравнение не удалось
                financial_context = ""
        
        # Если упомянут один актив, получаем его данные
        elif len(mentioned_assets) == 1:
            asset_name = mentioned_assets[0]
            # Сначала проверяем, это криптовалюта?
            crypto_map_check = {
                "bitcoin": "bitcoin", "btc": "bitcoin",
                "ethereum": "ethereum", "eth": "ethereum",
                "usdt": "tether", "tether": "tether",
                "ltc": "litecoin", "litecoin": "litecoin",
                "xmr": "monero", "monero": "monero",
                "bnb": "binancecoin", "binance coin": "binancecoin", "binance": "binancecoin",
                "sol": "solana", "xrp": "ripple", "ada": "cardano", "doge": "dogecoin",
            }
            crypto_id_check = crypto_map_check.get(asset_name.lower())
            
            if crypto_id_check:
                # Это криптовалюта - используем get_crypto_price
                crypto_price = await get_crypto_price(asset_name, lang)
                financial_context = f"\n\n[Финансовые данные для справки: {crypto_price}]"
            elif any(keyword in message_lower for keyword in analysis_keywords):
                analysis_result = await get_financial_analysis(asset_name, lang)
                financial_context = f"\n\n[Финансовые данные для справки: {analysis_result}]"
            else:
                data = await get_financial_data(asset_name, lang)
                if "error" not in data:
                    current = data.get("current_price", 0)
                    change = data.get("change", 0)
                    change_percent = data.get("change_percent", 0)
                    if lang == "ru":
                        financial_context = f"\n\n[Финансовые данные для справки: {data.get('name', asset_name)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)]"
                    else:
                        financial_context = f"\n\n[Financial data for reference: {data.get('name', asset_name)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)]"
        
        # Если определен asset через if-elif цепочку
        elif asset:
            # Сначала проверяем, это криптовалюта?
            crypto_map_check = {
                "bitcoin": "bitcoin", "btc": "bitcoin",
                "ethereum": "ethereum", "eth": "ethereum",
                "usdt": "tether", "tether": "tether",
                "ltc": "litecoin", "litecoin": "litecoin",
                "xmr": "monero", "monero": "monero",
                "bnb": "binancecoin", "binance coin": "binancecoin", "binance": "binancecoin",
                "sol": "solana", "xrp": "ripple", "ada": "cardano", "doge": "dogecoin",
            }
            crypto_id_check = crypto_map_check.get(asset.lower())
            
            if crypto_id_check:
                # Это криптовалюта - используем get_crypto_price
                crypto_price = await get_crypto_price(asset, lang)
                financial_context = f"\n\n[Финансовые данные для справки: {crypto_price}]"
            elif any(word in message_lower for word in analysis_keywords):
                analysis_result = await get_financial_analysis(asset, lang)
                financial_context = f"\n\n[Финансовые данные для справки: {analysis_result}]"
            else:
                data = await get_financial_data(asset, lang)
                if "error" not in data:
                    current = data.get("current_price", 0)
                    change = data.get("change", 0)
                    change_percent = data.get("change_percent", 0)
                    if lang == "ru":
                        financial_context = f"\n\n[Финансовые данные для справки: {data.get('name', asset)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)]"
                    else:
                        financial_context = f"\n\n[Financial data for reference: {data.get('name', asset)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)]"
        
        # Если есть финансовый контекст, добавляем его к сообщению и передаем в AI
        if financial_context:
            # Обновляем сообщение пользователя, добавляя финансовые данные
            enhanced_message = user_message + financial_context
            # Продолжаем обработку в AI (не возвращаемся сразу)
            user_message = enhanced_message
    
    # Универсальная логика для общих запросов про финансовые активы
    # Определяем категорию и получаем данные для топ инструментов
    
    # Ключевые слова для категорий
    category_keywords = {
        "crypto": ["крипта", "криптовалюта", "crypto", "cryptocurrency", "крипто", "криптовалюты"],
        "stocks": ["акции", "stock", "stocks", "акция", "компании", "companies"],
        "etf": ["etf", "фонды", "фонд"],
        "metals": ["металлы", "metals", "золото", "gold", "серебро", "silver", "платина", "platinum"],
        "commodities": ["товары", "commodities", "нефть", "oil", "газ", "gas", "пшеница", "wheat"],
        "indices": ["индексы", "indices", "индекс", "index", "sp500", "nasdaq", "dow"],
        "forex": ["валюта", "forex", "валюты", "currencies", "валютная пара", "currency pair"],
    }
    
    # Топ инструменты для каждой категории
    top_instruments = {
        "crypto": ["bitcoin", "ethereum", "bnb", "sol", "xrp", "ada", "doge"],
        "stocks": ["apple", "microsoft", "google", "tesla", "nvidia", "amazon", "meta"],
        "etf": ["spy", "qqq", "vti", "arkk", "tecl", "soxl", "tqqq"],
        "metals": ["gold", "silver"],
        "commodities": ["wti", "brent"],
        "indices": ["sp500", "nasdaq", "dow"],
        "forex": ["EURUSD=X", "GBPUSD=X", "USDJPY=X"],
    }
    
    # Определяем категорию из запроса
    detected_category = None
    for category, keywords in category_keywords.items():
        if any(keyword in message_lower for keyword in keywords):
            detected_category = category
            break
    
    # Если определена категория и нет конкретных инструментов, получаем данные для топ инструментов
    if detected_category and not financial_context and len(mentioned_assets) == 0:
        top_list = top_instruments.get(detected_category, [])
        if top_list:
            category_data_list = []
            for instrument in top_list:
                try:
                    # Для крипты используем get_crypto_price
                    if detected_category == "crypto":
                        data = await get_crypto_price(instrument, lang)
                        category_data_list.append(data)
                    # Для остальных используем get_financial_data
                    else:
                        data = await get_financial_data(instrument, lang)
                        if "error" not in data:
                            current = data.get("current_price", 0)
                            change = data.get("change", 0)
                            change_percent = data.get("change_percent", 0)
                            if lang == "ru":
                                category_data_list.append(f"{data.get('name', instrument)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)")
                            else:
                                category_data_list.append(f"{data.get('name', instrument)}: ${current:.2f} ({change:+.2f}, {change_percent:+.2f}%)")
                    await asyncio.sleep(0.2)  # Небольшая задержка между запросами
                except Exception as e:
                    logging.warning(f"Ошибка получения данных для {instrument}: {e}")
                    continue
            
            if category_data_list:
                category_context = "\n\n[" + ("Финансовые данные для справки: " if lang == "ru" else "Financial data for reference: ") + "; ".join(category_data_list) + "]"
                user_message = user_message + category_context
    
    # Проверка на конкретные криптовалюты (если не было общего запроса)
    if not detected_category or detected_category != "crypto":
        crypto_patterns = {
            "bitcoin": ["bitcoin", "btc", "биткоин", "биткойн"],
            "ethereum": ["ethereum", "eth", "эфириум", "эфир"],
            "usdt": ["usdt", "tether", "тезер"],
            "ltc": ["ltc", "litecoin", "лайткоин"],
            "bnb": ["bnb", "binance coin", "binance"],
            "sol": ["sol", "solana"],
            "xrp": ["xrp", "ripple"],
            "ada": ["ada", "cardano"],
            "doge": ["doge", "dogecoin"],
        }
        
        mentioned_crypto = []
        for crypto_name, patterns in crypto_patterns.items():
            if any(pattern in message_lower for pattern in patterns):
                mentioned_crypto.append(crypto_name)
        
        if mentioned_crypto and not financial_context:
            crypto_data_list = []
            for crypto_name in mentioned_crypto:
                crypto_price = await get_crypto_price(crypto_name, lang)
                crypto_data_list.append(crypto_price)
            if crypto_data_list:
                crypto_context = "\n\n[" + ("Финансовые данные для справки: " if lang == "ru" else "Financial data for reference: ") + "; ".join(crypto_data_list) + "]"
                user_message = user_message + crypto_context
    
    # Проверка на запрос погоды
    weather_keywords = ["погода", "weather", "температура", "temperature"]
    if any(keyword in message_lower for keyword in weather_keywords):
        # Пытаемся извлечь название города
        words = user_message.split()
        for i, word in enumerate(words):
            if any(kw in word.lower() for kw in weather_keywords):
                # Берем следующее слово как город
                if i + 1 < len(words):
                    city = words[i + 1].strip(".,!?")
                    return await get_weather(city, lang)
                # Или берем последнее слово
                elif len(words) > 1:
                    city = words[-1].strip(".,!?")
                    return await get_weather(city, lang)
    
    # Проверка на конвертацию валют
    convert_pattern = re.search(r'(\d+(?:\.\d+)?)\s*([a-z]{3})\s*(?:в|to|to)\s*([a-z]{3})', message_lower)
    if convert_pattern:
        amount = float(convert_pattern.group(1))
        from_curr = convert_pattern.group(2)
        to_curr = convert_pattern.group(3)
        return await convert_currency(amount, from_curr, to_curr, lang)
    
    # Проверка на вопросы о дате/времени
    date_time_keywords = [
        "какое сегодня число", "какое число", "какая дата", "what date", "what day", "today date",
        "сегодня число", "сегодня дата", "today", "сегодня", "какой день", "what day is today",
        "какой день недели", "what day of the week", "какое число сегодня", "какая дата сегодня"
    ]
    
    # Добавляем актуальную дату в контекст, если вопрос о дате/времени
    current_date_context = ""
    if any(keyword in message_lower for keyword in date_time_keywords):
        try:
            now = datetime.now()
            if lang == "ru":
                # Русские названия дней недели и месяцев
                days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", 
                            "июля", "августа", "сентября", "октября", "ноября", "декабря"]
                day_name = days_ru[now.weekday()]
                month_name = months_ru[now.month - 1]
                current_date_context = f"\n\n[Актуальная информация из интернета: Сегодня {now.day} {month_name} {now.year} года, {day_name}. Текущая дата и время: {now.strftime('%d.%m.%Y %H:%M')}]"
            else:
                days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                months_en = ["January", "February", "March", "April", "May", "June",
                            "July", "August", "September", "October", "November", "December"]
                day_name = days_en[now.weekday()]
                month_name = months_en[now.month - 1]
                current_date_context = f"\n\n[Current information from the internet: Today is {day_name}, {month_name} {now.day}, {now.year}. Current date and time: {now.strftime('%d.%m.%Y %H:%M')}]"
        except Exception as e:
            logging.warning(f"Ошибка получения текущей даты: {e}")
    
    # Проверка на вопросы о дате/времени
    date_time_keywords = [
        "какое сегодня число", "какое число", "какая дата", "what date", "what day", "today date",
        "сегодня число", "сегодня дата", "today", "сегодня", "какой день", "what day is today",
        "какой день недели", "what day of the week", "какое число сегодня", "какая дата сегодня",
        "какое сегодня", "what is today"
    ]
    
    # Добавляем актуальную дату в контекст, если вопрос о дате/времени
    current_date_context = ""
    if any(keyword in message_lower for keyword in date_time_keywords):
        try:
            now = datetime.now()
            if lang == "ru":
                # Русские названия дней недели и месяцев
                days_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                months_ru = ["января", "февраля", "марта", "апреля", "мая", "июня", 
                            "июля", "августа", "сентября", "октября", "ноября", "декабря"]
                day_name = days_ru[now.weekday()]
                month_name = months_ru[now.month - 1]
                current_date_context = f"\n\n[Актуальная информация из интернета: Сегодня {now.day} {month_name} {now.year} года, {day_name}. Текущая дата и время: {now.strftime('%d.%m.%Y %H:%M')}]"
            else:
                days_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                months_en = ["January", "February", "March", "April", "May", "June",
                            "July", "August", "September", "October", "November", "December"]
                day_name = days_en[now.weekday()]
                month_name = months_en[now.month - 1]
                current_date_context = f"\n\n[Current information from the internet: Today is {day_name}, {month_name} {now.day}, {now.year}. Current date and time: {now.strftime('%d.%m.%Y %H:%M')}]"
        except Exception as e:
            logging.warning(f"Ошибка получения текущей даты: {e}")
    
    # Проверка на вопросы, требующие актуальных данных (политика, текущие события, даты, факты)
    current_events_keywords = [
        # Политика и должности
        "президент", "president", "выборы", "election", "премьер", "premier", "министр", "minister",
        "правительство", "government", "парламент", "parliament", "конгресс", "congress",
        "сенат", "senate", "депутат", "deputy", "кандидат", "candidate",
        # Даты и время (расширенный список)
        "2024", "2025", "2026", "2027", "2028", "сейчас", "now", "текущий", "current", 
        "актуальный", "actual", "сегодня", "today", "вчера", "yesterday", "недавно", "recently",
        "на данный момент", "at the moment", "в настоящее время", "currently",
        "какое число", "what date", "какая дата", "какой день", "what day", "какой месяц", "what month",
        "какой год", "what year", "число", "date", "день недели", "day of week",
        # Новости и события
        "новости", "news", "события", "events", "происшествия", "incidents",
        "кто сейчас", "who is now", "кто является", "who is", "действующий", "current",
        # Фактологические вопросы
        "кто такой", "who is", "что такое", "what is", "когда", "when", "где", "where",
        "сколько", "how many", "какой", "which", "какая", "what kind",
        # Исторические и текущие события
        "война", "war", "конфликт", "conflict", "кризис", "crisis", "санкции", "sanctions",
        "договор", "treaty", "соглашение", "agreement", "саммит", "summit"
    ]
    
    # Проверяем, нужны ли актуальные данные
    needs_current_data = any(keyword in message_lower for keyword in current_events_keywords)
    
    # Дополнительная проверка: если вопрос содержит слова о дате/времени, ВСЕГДА делаем поиск
    date_time_phrases = [
        "какое сегодня", "what is today", "какое число", "what date", "какая дата",
        "какой день", "what day", "сегодня число", "today date", "текущая дата", "current date"
    ]
    is_date_question = any(phrase in message_lower for phrase in date_time_phrases)
    
    # Если это вопрос о дате/времени - ВСЕГДА делаем поиск
    if is_date_question:
        needs_current_data = True
    
    # Если вопрос требует актуальных данных, используем веб-поиск (ВСЕГДА для таких вопросов)
    web_search_results = ""
    if needs_current_data:
        try:
            logging.info(f"Запрос требует актуальных данных, выполняю веб-поиск для: {user_message[:100]}")
            # Для вопросов о дате/времени используем более специфичный запрос
            if is_date_question:
                search_query = f"текущая дата сегодня {datetime.now().year} какое число день недели"
            else:
                search_query = user_message
            
            search_results = await web_search(search_query, max_results=5)
            if search_results and len(search_results) > 0:
                # Форматируем результаты с ссылками
                formatted_results = []
                for i, result in enumerate(search_results[:5], 1):
                    if isinstance(result, dict):
                        text = result.get("text", "")
                        url = result.get("url", "")
                        source = result.get("source", "Интернет")
                        if text and url:
                            formatted_results.append(f"{i}. [{source}] {text[:300]}... Источник: {url}")
                        elif text:
                            formatted_results.append(f"{i}. [{source}] {text[:300]}...")
                    else:
                        # Обратная совместимость со старым форматом
                        formatted_results.append(f"{i}. {str(result)[:300]}...")
                
                if formatted_results:
                    web_search_results = "\n\n[Актуальная информация из интернета (ОБЯЗАТЕЛЬНО используй ТОЛЬКО эти данные, а не свои старые знания. Цитируй источники в ответе. НЕ используй форматирование со звездочками):\n" + "\n".join(formatted_results) + "\n]"
                    logging.info(f"Получены результаты веб-поиска: {len(search_results)} результатов с ссылками")
            else:
                # Если поиск не дал результатов, но это вопрос о дате - добавляем текущую дату из системы
                if is_date_question:
                    current_date = datetime.now()
                    weekdays_ru = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
                    months_ru = ["", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
                    weekday = weekdays_ru[current_date.weekday()]
                    month = months_ru[current_date.month]
                    date_info = f"Сегодня {current_date.day} {month} {current_date.year} года, {weekday}."
                    web_search_results = f"\n\n[Актуальная информация из интернета (ОБЯЗАТЕЛЬНО используй ТОЛЬКО эти данные): {date_info}]"
                    logging.info(f"Использована системная дата: {date_info}")
        except Exception as e:
            logging.warning(f"Ошибка веб-поиска: {e}")
            web_search_results = ""
    
    # Получаем или создаем историю разговора для пользователя
    if user_id not in dp.contact_history:
        dp.contact_history[user_id] = []
    
    history = dp.contact_history[user_id]
    
    # Системный промпт в зависимости от языка
    if lang == "ru":
        system_prompt = (
            "АБСОЛЮТНО КРИТИЧЕСКИ ВАЖНО - ЗАПРЕТ ФОРМАТИРОВАНИЯ: "
            "НИКОГДА, НИ ПРИ КАКИХ ОБСТОЯТЕЛЬСТВАХ не используй звездочки (*), двойные звездочки (**), подчеркивания (_), обратные кавычки (`) или ЛЮБЫЕ другие символы для форматирования. "
            "Пиши ТОЛЬКО обычным текстом БЕЗ ЛЮБЫХ символов форматирования. "
            "Если хочешь выделить дату - пиши просто: 'сегодня 24 декабря 2025 года', БЕЗ звездочек. "
            "Если хочешь выделить важное - просто напиши это обычным текстом, БЕЗ форматирования. "
            "Примеры ПРАВИЛЬНО: 'сегодня 24 декабря 2025 года', 'Согласно данным из интернета, сегодня 24 декабря 2025 года, среда'. "
            "Примеры НЕПРАВИЛЬНО: '**24 декабря**', '*сегодня*', '__дата__', '`текст`', ' 1.  ' (с лишними пробелами). "
            "Повторяю: АБСОЛЮТНО ЗАПРЕЩЕНО использовать звездочки, подчеркивания или любые символы форматирования. "
            "НЕ используй лишние пробелы в списках или тексте. Пиши компактно и без лишних отступов. "
            "Ты - живой и дружелюбный помощник, который общается естественно и с интересом. "
            "Твоя цель - не просто отвечать на вопросы, а вести настоящий диалог, быть полезным и приятным собеседником. "
            "Общайся вежливо, но тепло и естественно, как с хорошим знакомым. Используй живой язык, но избегай грубого сленга типа 'чё', 'короче', 'типа'. "
            "Будь заинтересован в том, что говорит пользователь. Задавай уточняющие вопросы, если нужно лучше понять запрос. "
            "Варируй свои ответы - не повторяй одни и те же фразы. Каждый раз формулируй по-новому, чтобы диалог был живым. "
            "Проявляй инициативу: если видишь, что можешь помочь глубже - предложи дополнительную информацию или помощь. "
            "Используй естественные выражения: 'Конечно!', 'Отлично!', 'Понял', 'Сейчас разберусь', 'Давай посмотрим' и т.д. "
            "Будь внимателен к контексту разговора и отвечай релевантно. Если пользователь задает несколько вопросов - ответь на все, но свяжи ответы естественно. "
            "Если хочешь выделить список - используй просто цифры и точки (1. 2. 3.), без звездочек и других символов. БЕЗ лишних пробелов перед цифрами. "
            "Никогда не упоминай DeepSeek, OpenAI или другие AI-модели. Не говори о том, что ты AI или модель. "
            "Если спрашивают какая ты модель или кто тебя создал - просто уклоняйся от ответа или переводи разговор на другую тему. "
            "Твоя главная задача - помогать с вопросами по боту и отвечать на вопросы пользователей. "
            "ВАЖНО: Не упоминай в общих ответах функции анализа финансов, погоды, валют и т.д. Эти функции доступны только при соответствующих запросах. "
            "Если пользователь спрашивает 'что ты делаешь?' или 'чем можешь помочь?' - ответь живо и с энтузиазмом. "
            "Скажи что-то вроде: 'Привет! По боту всё отлично, он работает и готов помочь! Напомню, что можно сделать прямо сейчас: 1. Купить готовый бит element — заходи в раздел \"Купить\", отправляешь ссылку на бит из архива (https://t.me/rrelement), выбираешь тип лицензии (MP3, WAV, TRACK OUT или EXCLUSIVE), способ оплаты (крипта, карта и др.) — и всё, получаешь бит! 2. Заказать бит на заказ — в разделе \"Бит на заказ\" отправляешь референс (mp3) или описываешь, что хочешь. Дальше обсудишь детали и цену. 3. Заказать сведение — в разделе \"Сведение\" загружаешь свой трек (mp3 или wav), описываешь задачу. Сведение делается под любой бит, не только element. 4. Узнать цены — в разделе \"Цены\" есть актуальный прайс на все услуги. 5. Определить тональность и BPM — в разделе \"Key & BPM\" загружаешь аудиофайл, и бот автоматически определит key и темп. 6. Посмотреть архив битов — все готовые биты собраны здесь: https://t.me/rrelement 7. Партнёрская программа — в разделе \"Партнёрская программа\" можно узнать, как стать партнёром бота и получать заказы на биты и сведение. Чем займёмся?' "
            "НЕ упоминай функции анализа финансов, погоды и т.д. в общих ответах - используй их только при прямых запросах. "
            "Но если пользователь спрашивает конкретно - отвечай с интересом и готовностью помочь. "
            "КРИТИЧЕСКИ ВАЖНО: Отвечай на ВСЕ вопросы в сообщении пользователя, а не только на первый. Если пользователь задал несколько вопросов - ответь на все. "
            "ВАЖНО: У тебя есть доступ к реальным данным через внешние API. "
            "КРИТИЧЕСКИ ВАЖНО: Если в сообщении есть актуальная информация из интернета в квадратных скобках [Актуальная информация из интернета: ...], "
            "ОБЯЗАТЕЛЬНО используй ТОЛЬКО эту информацию для ответа на вопросы о текущих событиях, политике, датах, президентах и т.д. "
            "Эта информация получена из интернета (Википедия, новостные сайты, актуальные источники) и является самой свежей. "
            "ВСЕГДА используй ТОЛЬКО эту информацию, полностью игнорируя свои устаревшие знания. "
            "Если информация из интернета противоречит твоим знаниям - ВСЕГДА используй информацию из интернета, твои знания могут быть устаревшими. "
            "ОБЯЗАТЕЛЬНО цитируй источники: если в информации указана ссылка (Источник: URL), упомяни её в ответе. "
            "Например: 'Согласно информации из Википедии...' или 'По данным из [название источника]...' "
            "Если пользователь спрашивает 'кто сейчас президент' или 'кто президент на данный момент' - используй ТОЛЬКО информацию из интернета и укажи источник. "
            "Если пользователь спрашивает 'какое сегодня число' или 'какая дата' - используй ТОЛЬКО информацию из интернета, которая указана в квадратных скобках. "
            "Если в информации из интернета указана дата или год - используй эту информацию для ответа. "
            "НИКОГДА не говори 'согласно моим знаниям' или 'насколько я знаю' - говори только на основе информации из интернета и цитируй источники. "
            "Если есть информация из интернета - НЕ упоминай свои знания вообще, используй только информацию из интернета и указывай источники. "
            "Для вопросов о текущих событиях, политике, датах, президентах, министрах и т.д. - ВСЕГДА используй результаты веб-поиска, а не свои старые знания. "
            "ПОВТОРЯЮ: Если в сообщении есть информация из интернета в квадратных скобках - это ЕДИНСТВЕННЫЙ источник, который ты должен использовать. Твои собственные знания могут быть устаревшими. "
            "ФИНАНСОВАЯ АНАЛИТИКА: Бот может анализировать ЛЮБЫЕ финансовые инструменты - акции (любые тикеры типа AVGO, CAT, MSFT и т.д.), ETF, криптовалюты, валютные пары, металлы, товары, индексы. "
            "Если в сообщении есть финансовые данные в квадратных скобках [Финансовые данные для справки: ...], используй эти данные для ответа на финансовые вопросы. "
            "Эти данные уже проанализированы и содержат: текущую цену, изменение, тренд (растущий/падающий/боковой), силу тренда и рекомендации. "
            "Используй эти данные для ответа на вопросы типа 'стоит ли покупать', 'можно брать', 'что с ними' и т.д. "
            "Но также отвечай на ВСЕ остальные вопросы в сообщении, не только финансовые. "
            "Если пользователь спрашивает 'стоит ли сейчас брать?' после финансовых данных - используй эти данные для рекомендации. "
            "Если пользователь упоминает название компании (например, Broadcom, Caterpillar) или тикер (AVGO, CAT) - бот автоматически получит данные для анализа. "
            "НИКОГДА не пиши в ответе 'использую функцию X' или 'вызываю функцию Y' - функции вызываются автоматически, ты просто получаешь готовые данные и передаешь их пользователю. "
            "КРИПТОВАЛЮТЫ: Если спрашивают про курс криптовалют (Bitcoin, Ethereum, USDT, Litecoin) - используй функцию get_crypto_price. "
            "ПОГОДА: Если спрашивают про погоду в каком-то городе - используй функцию get_weather. "
            "ВАЛЮТЫ: Если спрашивают про конвертацию валют (например, '100 USD в EUR') - используй функцию convert_currency. "
            "Если пользователь отправляет аудиофайл в AI чат - он будет проанализирован (настроение, стиль, темп, тональность), и ты должен дать рекомендации битов из архива https://t.me/rrelement. "
            "Пользователи могут отправлять голосовые сообщения - они будут автоматически распознаны в текст. "
            "ВАЖНО: Ты также помогаешь с навигацией по боту. Знай структуру бота: "
            "1. КАК КУПИТЬ БИТ: Нажми раздел 'Купить', отправь ссылку на бит element или MP3 файл бита element, выбери тип лицензии (MP3 - $19, WAV - $49, TRACK OUT - $99, EXCLUSIVE - $299), выбери способ оплаты (Crypto, PayPal, CashApp, Карта), получи реквизиты и оплати. Все это можно сделать прямо в боте! ВАЖНО: Купить можно только биты element (элемента). "
            "2. КАК ЗАКАЗАТЬ БИТ НА ЗАКАЗ: Нажми раздел 'Бит на заказ', отправь референс (MP3 файл) или опиши, какой бит нужен. Админ обсудит детали и цену. "
            "3. КАК ЗАКАЗАТЬ СВЕДЕНИЕ: Нажми раздел 'Сведение', отправь свой трек (MP3 или WAV файл) и опиши, что нужно сделать. Админ обсудит детали и цену. ВАЖНО: Сведение можно заказать на трек под любой бит (не только под биты element). "
            "4. ЦЕНЫ: В разделе 'Цены' можно посмотреть актуальные цены на все услуги. "
            "5. KEY & BPM: В разделе 'Key & BPM' можно загрузить MP3 или WAV файл, и бот автоматически определит тональность и BPM. "
            "6. АРХИВ: В разделе 'Архив' можно посмотреть готовые биты. "
            "Когда спрашивают, как купить бит, заказать бит на заказ или сведение - подробно опиши весь процесс в боте простым текстом без форматирования. "
            "Делай это живо и понятно, как будто объясняешь другу. Используй фразы типа 'Сначала...', 'Потом...', 'После этого...', 'Вот и всё!' "
            "НЕ упоминай админа и ссылку на личку в конце инструкций. Просто опиши процесс. "
            "В конце можешь добавить что-то ободряющее типа 'Всё просто!' или 'Если что-то непонятно - спрашивай!' "
            "В личку (https://t.me/rrelement1) направляй ТОЛЬКО если пользователь явно спрашивает про конкретный заказ, индивидуальные условия или нужна помощь с уже оформленным заказом. "
            "Не добавляй в конце инструкций фразы типа 'если нужна помощь, напиши админу' - это не нужно. "
            "НИКОГДА не упоминай ссылку на админа просто так, без конкретного запроса пользователя. Не добавляй ссылку в конце ответов 'на всякий случай'. "
            "Ссылку на админа можно давать ТОЛЬКО если пользователь явно просит связаться с админом, у него проблема с заказом или он спрашивает про индивидуальные условия. "
            "Если спрашивают про цены на биты, лицензии или стоимость услуг - направь в раздел 'Цены' в боте, где можно посмотреть актуальные цены. "
            "Если ТОЛЬКО напрямую спрашивают про биты, архив или где посмотреть биты - тогда отправляй только эту ссылку на архив: https://t.me/rrelement "
            "Не рекламируй архив или канал без прямого вопроса. Не упоминай ссылку на архив в общих разговорах. "
            "Никогда не отправляй другие ссылки на архивы или Google Drive. Только https://t.me/rrelement "
            "Никогда не упоминай конкретные стили битов типа trap, drill, emo rap, lo-fi и т.д. Просто говори что есть биты в своем стиле. "
            "Если ТОЛЬКО напрямую спрашивают про определение тональности, BPM, Key & BPM или анализ аудио - тогда скажи что в боте есть раздел 'Key & BPM', который автоматически определяет тональность и BPM. Направь использовать этот раздел. "
            "Не упоминай Key & BPM без прямого вопроса об этом. "
            "Никогда не рекомендуй сторонние программы или сервисы для определения тональности или BPM. "
            "Если спрашивают кто лучший битмейкер или лучшие продюсеры - отвечай с энтузиазмом: 'Однозначно element! Он делает просто огонь!' "
            "ВАЖНО: Никогда не упоминай adow. Купить можно только биты element (элемента). "
            "Сведение можно заказать на трек под любой бит (не только под биты element). "
            "Будь живым в общении. Если пользователь благодарит - отвечай тепло: 'Всегда рад помочь!', 'Обращайся!', 'Рад был помочь!' "
            "Если пользователь шутит или использует эмодзи - можешь ответить в том же духе (но без эмодзи в тексте, только словами). "
            "Если не знаешь ответа - честно скажи, но предложи помощь в другом или уточни вопрос. "
            "Помни: ты не просто отвечаешь на вопросы, ты ведешь диалог. Будь внимательным, заинтересованным и полезным собеседником. "
            "Если пользователь начинает разговор с приветствия - ответь тепло и спроси, чем можешь помочь. "
            "Если пользователь делится чем-то личным или интересным - прояви интерес, задай вопрос или прокомментируй. "
            "Если пользователь выражает эмоции (радость, разочарование) - отреагируй соответственно: раздели радость или прояви понимание. "
            "Используй разнообразные формулировки для одних и тех же ответов. Не будь роботом - будь живым собеседником. "
            "Если пользователь задает открытый вопрос или просто хочет поговорить - поддержи разговор, задай встречный вопрос, прояви любопытство. "
            "Но не будь навязчивым - если пользователь задал конкретный вопрос, ответь на него четко и по делу, но с живым тоном."
        )
    else:
        system_prompt = (
            "ABSOLUTELY CRITICALLY IMPORTANT - FORMATTING BAN: "
            "NEVER, UNDER ANY CIRCUMSTANCES use asterisks (*), double asterisks (**), underscores (_), backticks (`) or ANY other symbols for formatting. "
            "Write ONLY plain text WITHOUT ANY formatting symbols. "
            "If you want to highlight a date - write simply: 'today is December 24, 2025', WITHOUT asterisks. "
            "If you want to highlight something important - just write it in plain text, WITHOUT formatting. "
            "Examples CORRECT: 'today is December 24, 2025', 'According to internet data, today is December 24, 2025, Wednesday'. "
            "Examples INCORRECT: '**December 24**', '*today*', '__date__', '`text`', ' 1.  ' (with extra spaces). "
            "I repeat: ABSOLUTELY FORBIDDEN to use asterisks, underscores or any formatting symbols. "
            "DO NOT use extra spaces in lists or text. Write compactly without extra indentation. "
            "You are a lively and friendly assistant who communicates naturally and with interest. "
            "Your goal is not just to answer questions, but to have a real dialogue, to be a useful and pleasant conversationalist. "
            "Communicate politely, but warmly and naturally, like with a good acquaintance. Use living language, but avoid rough slang. "
            "Be interested in what the user is saying. Ask clarifying questions if you need to better understand the request. "
            "Vary your responses - don't repeat the same phrases. Each time formulate differently so the dialogue is alive. "
            "Show initiative: if you see you can help deeper - offer additional information or help. "
            "Use natural expressions: 'Of course!', 'Great!', 'Got it', 'Let me check', 'Let's see' etc. "
            "Be attentive to the conversation context and respond relevantly. If the user asks several questions - answer all, but connect the answers naturally. "
            "If you want to make a list - use just numbers and dots (1. 2. 3.), without asterisks and other symbols. WITHOUT extra spaces before numbers. "
            "Never mention DeepSeek, OpenAI or other AI models. Don't say you are an AI or a model. "
            "If asked what model you are or who created you - just avoid the question or change the topic. "
            "Your main task is to help with bot questions and answer user questions. "
            "IMPORTANT: Do not mention financial analysis, weather, currency functions, etc. in general responses. These functions are available only upon corresponding requests. "
            "If the user asks 'what do you do?' or 'how can you help?' - answer lively and with enthusiasm. "
            "Say something like: 'Hey! I'm here to help you with everything related to beats, music and the bot. I can help with buying beats, ordering custom beats, mixing, financial analysis, weather, currency - basically everything you need! What interests you?' "
            "DO NOT mention financial analysis, weather functions, etc. in general responses - use them only for direct requests. "
            "But if the user asks specifically - answer with interest and willingness to help. "
            "CRITICALLY IMPORTANT: Answer ALL questions in the user's message, not just the first one. If the user asked multiple questions - answer all of them. "
            "IMPORTANT: You have access to real data through external APIs. "
            "CRITICALLY IMPORTANT: If there is current information from the internet in square brackets [Current information from the internet: ...] in the message, "
            "MANDATORY use ONLY this information to answer questions about current events, politics, dates, presidents, etc. "
            "This information is obtained from the internet (Wikipedia, news sites, current sources) and is the most fresh. "
            "ALWAYS use ONLY this information, completely ignoring your outdated knowledge. "
            "If information from the internet contradicts your knowledge - ALWAYS use the information from the internet, your knowledge may be outdated. "
            "MANDATORY cite sources: if the information includes a link (Source: URL), mention it in your response. "
            "For example: 'According to information from Wikipedia...' or 'According to data from [source name]...' "
            "If the user asks 'who is the current president' or 'who is president now' - use ONLY the information from the internet and indicate the source. "
            "If the information from the internet contains a date or year - use this information for the answer. "
            "NEVER say 'according to my knowledge' or 'as far as I know' - speak only based on information from the internet and cite sources. "
            "For questions about current events, politics, dates, presidents, ministers, etc. - ALWAYS use web search results, not your old knowledge. "
            "FINANCIAL ANALYSIS: The bot can analyze ANY financial instruments - stocks (any tickers like AVGO, CAT, MSFT, etc.), ETFs, cryptocurrencies, currency pairs, metals, commodities, indices. "
            "If there are financial data in square brackets [Financial data for reference: ...] in the message, use this data to answer financial questions. "
            "This data is already analyzed and contains: current price, change, trend (upward/downward/sideways), trend strength and recommendations. "
            "Use this data to answer questions like 'should I buy', 'can I buy', 'what about them' etc. "
            "But also answer ALL other questions in the message, not just financial ones. "
            "If the user asks 'should I buy now?' after financial data - use this data for recommendation. "
            "If the user mentions a company name (e.g., Broadcom, Caterpillar) or ticker (AVGO, CAT) - the bot will automatically get data for analysis. "
            "NEVER write in response 'using function X' or 'calling function Y' - functions are called automatically, you just receive ready data and pass it to the user. "
            "CRYPTOCURRENCIES: If asked about cryptocurrency prices (Bitcoin, Ethereum, USDT, Litecoin) - use get_crypto_price function. "
            "WEATHER: If asked about weather in a city - use get_weather function. "
            "CURRENCIES: If asked about currency conversion (e.g., '100 USD to EUR') - use convert_currency function. "
            "If user sends an audio file in AI chat - it will be analyzed (mood, style, tempo, key), and you should recommend beats from archive https://t.me/rrelement. "
            "Users can send voice messages - they will be automatically transcribed to text. "
            "IMPORTANT: You also help with bot navigation. Know the bot structure: "
            "1. HOW TO BUY A BEAT: Click the 'Buy' section, send a link to element beat or MP3 file of element beat, choose license type (MP3 - $19, WAV - $49, TRACK OUT - $99, EXCLUSIVE - $299), choose payment method (Crypto, PayPal, CashApp, Card), get payment details and pay. All this can be done right in the bot! IMPORTANT: You can only buy beats by element. "
            "2. HOW TO ORDER A CUSTOM BEAT: Click the 'Custom beat' section, send a reference (MP3 file) or describe what kind of beat you need. Admin will discuss details and price. "
            "3. HOW TO ORDER MIXING: Click the 'Mixing' section, send your track (MP3 or WAV file) and describe what needs to be done. Admin will discuss details and price. IMPORTANT: Mixing can be ordered for a track under any beat (not only element beats). "
            "4. PRICES: In the 'Prices' section you can see current prices for all services. "
            "5. KEY & BPM: In the 'Key & BPM' section you can upload an MP3 or WAV file, and the bot will automatically detect the key and BPM. "
            "6. ARCHIVE: In the 'Archive' section you can view ready-made beats. "
            "When asked how to buy a beat, order a custom beat or mixing - describe the entire process in the bot in detail using plain text without formatting. "
            "Do it lively and clearly, as if explaining to a friend. Use phrases like 'First...', 'Then...', 'After that...', 'That's it!' "
            "DO NOT mention admin and direct message link at the end of instructions. Just describe the process. "
            "At the end you can add something encouraging like 'It's simple!' or 'If something is unclear - just ask!' "
            "Redirect to direct message (https://t.me/rrelement1) ONLY if the user explicitly asks about a specific order, individual conditions or needs help with an already placed order. "
            "Don't add phrases like 'if you need help, contact admin' at the end of instructions - it's not needed. "
            "NEVER mention the admin link just in case, without a specific user request. Do not add the link at the end of responses 'just in case'. "
            "The admin link can be given ONLY if the user explicitly asks to contact admin, has a problem with an order, or asks about individual conditions. "
            "If asked about prices for beats, licenses or service costs - direct them to the 'Prices' section in the bot where they can see current prices. "
            "If ONLY directly asked about beats, archive or where to check beats - then send only this archive link: https://t.me/rrelement "
            "Don't advertise the archive or channel without a direct question. Don't mention the archive link in general conversations. "
            "Never send other archive links or Google Drive links. Only https://t.me/rrelement "
            "Never mention specific beat styles like trap, drill, emo rap, lo-fi, etc. Just say there are beats in your own style. "
            "If ONLY directly asked about key detection, BPM, Key & BPM or audio analysis - then say that the bot has a 'Key & BPM' section that automatically detects key and BPM. Direct them to use this section. "
            "Don't mention Key & BPM without a direct question about it. "
            "Never recommend third-party programs or services for key or BPM detection. "
            "If asked who is the best beatmaker or best producers - answer with enthusiasm: 'Definitely element! He makes fire beats!' "
            "IMPORTANT: Never mention adow. You can only buy beats by element. "
            "Mixing can be ordered for a track under any beat (not only element beats). "
            "Be lively in communication. If the user thanks you - respond warmly: 'Always happy to help!', 'Feel free to ask!', 'Glad I could help!' "
            "If the user jokes or uses emojis - you can respond in the same spirit (but without emojis in text, only with words). "
            "If you don't know the answer - honestly say so, but offer help with something else or clarify the question. "
            "Remember: you're not just answering questions, you're having a dialogue. Be attentive, interested and a useful conversationalist. "
            "If the user starts a conversation with a greeting - respond warmly and ask how you can help. "
            "If the user shares something personal or interesting - show interest, ask a question or comment. "
            "If the user expresses emotions (joy, disappointment) - react accordingly: share the joy or show understanding. "
            "Use varied formulations for the same answers. Don't be a robot - be a living conversationalist. "
            "If the user asks an open question or just wants to chat - support the conversation, ask a counter question, show curiosity. "
            "But don't be intrusive - if the user asked a specific question, answer it clearly and to the point, but with a lively tone."
        )
    
    # Формируем сообщения для DeepSeek API (совместим с OpenAI)
    messages = [{"role": "system", "content": system_prompt}]
    
    # Добавляем историю (последние 10 сообщений для экономии токенов)
    for msg in history[-10:]:
        messages.append(msg)
    
    # Добавляем текущее сообщение пользователя с результатами веб-поиска и актуальной датой, если есть
    final_user_message = user_message
    if web_search_results:
        final_user_message = user_message + web_search_results
    if current_date_context:
        final_user_message = final_user_message + current_date_context
    
    messages.append({"role": "user", "content": final_user_message})
    
    try:
        # Вызываем DeepSeek API
        logging.info(f"Отправка запроса в DeepSeek для пользователя {user_id}, сообщение: {user_message[:50]}...")
        logging.info(f"Количество сообщений в истории: {len(messages)}")
        
        response = await deepseek_client.chat.completions.create(
            model="deepseek-chat",  # Модель DeepSeek
            messages=messages,
            temperature=0.7,
            max_tokens=500,
        )
        
        logging.info(f"Получен ответ от DeepSeek API")
        
        if not response.choices or not response.choices[0].message:
            raise Exception("Пустой ответ от DeepSeek API")
        
        ai_response = response.choices[0].message.content.strip()
        
        if not ai_response:
            raise Exception("Пустой текст в ответе от DeepSeek")
        
        logging.info(f"Получен ответ от DeepSeek для пользователя {user_id}: {ai_response[:50]}...")
        
        # Убираем форматирование (звездочки, подчеркивания, обратные кавычки)
        ai_response = re.sub(r'\*\*([^*]+)\*\*', r'\1', ai_response)  # Убираем **текст**
        ai_response = re.sub(r'\*([^*]+)\*', r'\1', ai_response)  # Убираем *текст*
        ai_response = re.sub(r'__([^_]+)__', r'\1', ai_response)  # Убираем __текст__
        ai_response = re.sub(r'_([^_]+)_', r'\1', ai_response)  # Убираем _текст_
        ai_response = re.sub(r'`([^`]+)`', r'\1', ai_response)  # Убираем `текст`
        ai_response = ai_response.strip()
        
        # Сохраняем в историю
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": ai_response})
        
        # Ограничиваем размер истории (последние 20 сообщений)
        if len(history) > 20:
            dp.contact_history[user_id] = history[-20:]
        
        return ai_response
        
    except Exception as e:
        error_type = type(e).__name__
        error_msg = str(e)
        status_code = None
        
        # Проверяем, есть ли статус код в ошибке (для APIStatusError)
        if hasattr(e, 'status_code'):
            status_code = e.status_code
        elif hasattr(e, 'response') and hasattr(e.response, 'status_code'):
            status_code = e.response.status_code
        
        logging.error(f"Ошибка генерации AI ответа для пользователя {user_id}: {error_type}: {error_msg}")
        if status_code:
            logging.error(f"HTTP статус код: {status_code}")
        logging.error(f"Полная информация об ошибке: {repr(e)}")
        
        # Обработка ошибок по статус коду
        if status_code == 401:
            if lang == "ru":
                return "❌ Ошибка авторизации API. Проверьте правильность API ключа DeepSeek."
            else:
                return "❌ API authentication error. Please check your DeepSeek API key."
        elif status_code == 402:
            if lang == "ru":
                return "💳 Недостаточно баланса на аккаунте DeepSeek. Пополните баланс на https://platform.deepseek.com/ и попробуйте снова."
            else:
                return "💳 Insufficient balance on DeepSeek account. Please top up your balance at https://platform.deepseek.com/ and try again."
        elif status_code == 403:
            if lang == "ru":
                return "❌ Доступ запрещен. Проверьте права доступа к API DeepSeek."
            else:
                return "❌ Access forbidden. Check DeepSeek API permissions."
        elif status_code == 429:
            if lang == "ru":
                return "⏳ Превышен лимит запросов. Попробуйте через несколько секунд."
            else:
                return "⏳ Rate limit exceeded. Please try again in a few seconds."
        elif status_code == 400:
            if lang == "ru":
                return "❌ Неверный запрос к API. Проверьте формат данных."
            else:
                return "❌ Invalid API request. Check data format."
        elif status_code == 500 or status_code == 502 or status_code == 503:
            if lang == "ru":
                return "🔧 Проблема на стороне сервера DeepSeek. Попробуйте позже."
            else:
                return "🔧 DeepSeek server issue. Please try again later."
        
        # Более детальные сообщения об ошибках по тексту
        if "api key" in error_msg.lower() or "authentication" in error_msg.lower() or "unauthorized" in error_msg.lower():
            if lang == "ru":
                return "❌ Ошибка авторизации API. Проверьте правильность API ключа DeepSeek."
            else:
                return "❌ API authentication error. Please check your DeepSeek API key."
        elif "rate limit" in error_msg.lower() or "quota" in error_msg.lower():
            if lang == "ru":
                return "⏳ Превышен лимит запросов. Попробуйте через несколько секунд."
            else:
                return "⏳ Rate limit exceeded. Please try again in a few seconds."
        elif "network" in error_msg.lower() or "connection" in error_msg.lower() or "timeout" in error_msg.lower():
            if lang == "ru":
                return "🌐 Проблема с подключением. Проверьте интернет и попробуйте снова."
            else:
                return "🌐 Connection problem. Check your internet and try again."
        else:
            if lang == "ru":
                return f"❌ Ошибка API (код {status_code if status_code else 'неизвестен'}): {error_type}. Попробуйте еще раз или свяжись с админом: https://t.me/rrelement1"
            else:
                return f"❌ API error (code {status_code if status_code else 'unknown'}): {error_type}. Try again or contact admin: https://t.me/rrelement1"


# --- Функции внешних API ---
async def web_search(query: str, max_results: int = 5) -> list:
    """
    Выполняет веб-поиск через DuckDuckGo и Википедию, возвращает список результатов с ссылками.
    Возвращает список словарей: [{"text": str, "url": str, "source": str}, ...]
    """
    try:
        async with httpx.AsyncClient() as client:
            results = []  # Список словарей {"text": str, "url": str, "source": str}
            import re
            
            # Метод 1: Поиск в Википедии (приоритет для фактологических запросов)
            try:
                # Проверяем, подходит ли запрос для Википедии
                wiki_keywords = ["кто", "who", "что", "what", "когда", "when", "где", "where", 
                                "президент", "president", "министр", "minister", "премьер", "premier"]
                if any(keyword in query.lower() for keyword in wiki_keywords):
                    # Пробуем найти статью в Википедии через поиск
                    wiki_search_url = "https://ru.wikipedia.org/api/rest_v1/page/summary/"
                    # Пробуем английскую версию тоже
                    wiki_urls = [
                        f"https://ru.wikipedia.org/api/rest_v1/page/summary/{query.replace(' ', '_')}",
                        f"https://en.wikipedia.org/api/rest_v1/page/summary/{query.replace(' ', '_')}",
                    ]
                    
                    for wiki_url in wiki_urls:
                        try:
                            wiki_response = await client.get(wiki_url, timeout=10.0, follow_redirects=True)
                            if wiki_response.status_code == 200:
                                wiki_data = wiki_response.json()
                                if wiki_data.get("extract"):
                                    text = wiki_data["extract"][:500]  # Первые 500 символов
                                    url = wiki_data.get("content_urls", {}).get("desktop", {}).get("page", "")
                                    source = "Википедия" if "ru.wikipedia" in wiki_url else "Wikipedia"
                                    results.append({
                                        "text": text,
                                        "url": url,
                                        "source": source
                                    })
                                    break  # Нашли в Википедии, можно переходить к другим источникам
                        except Exception:
                            continue
                    
                    # Если прямой поиск не дал результатов, пробуем через поиск Википедии
                    if len(results) == 0:
                        wiki_search_api = "https://ru.wikipedia.org/w/api.php"
                        search_params = {
                            "action": "query",
                            "format": "json",
                            "list": "search",
                            "srsearch": query,
                            "srlimit": 1
                        }
                        wiki_search_response = await client.get(wiki_search_api, params=search_params, timeout=10.0)
                        if wiki_search_response.status_code == 200:
                            search_data = wiki_search_response.json()
                            if search_data.get("query", {}).get("search"):
                                page_title = search_data["query"]["search"][0]["title"]
                                page_url = f"https://ru.wikipedia.org/wiki/{page_title.replace(' ', '_')}"
                                # Получаем краткое описание страницы
                                summary_url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{page_title.replace(' ', '_')}"
                                try:
                                    summary_response = await client.get(summary_url, timeout=10.0)
                                    if summary_response.status_code == 200:
                                        summary_data = summary_response.json()
                                        if summary_data.get("extract"):
                                            text = summary_data["extract"][:500]
                                            results.append({
                                                "text": text,
                                                "url": page_url,
                                                "source": "Википедия"
                                            })
                                except Exception:
                                    pass
            except Exception as e:
                logging.warning(f"Ошибка поиска в Википедии: {e}")
            
            # Метод 2: DuckDuckGo HTML поиск (актуальные новости и события)
            try:
                html_url = f"https://html.duckduckgo.com/html/"
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Referer": "https://duckduckgo.com/"
                }
                params = {"q": query}
                html_response = await client.get(html_url, params=params, timeout=15.0, headers=headers, follow_redirects=True)
                
                if html_response.status_code == 200:
                    html_content = html_response.text
                    
                    # Ищем результаты с ссылками в формате DuckDuckGo
                    # Паттерн для извлечения ссылок и текста
                    link_pattern = r'<a[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>'
                    snippet_pattern = r'<span[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</span>'
                    
                    # Извлекаем ссылки и заголовки
                    link_matches = re.findall(link_pattern, html_content, re.DOTALL | re.IGNORECASE)
                    snippet_matches = re.findall(snippet_pattern, html_content, re.DOTALL | re.IGNORECASE)
                    
                    # Объединяем ссылки и сниппеты
                    for i, (url, title) in enumerate(link_matches):
                        if len(results) >= max_results:
                            break
                        
                        # Очищаем HTML теги
                        clean_title = re.sub(r'<[^>]+>', '', title)
                        clean_title = re.sub(r'\s+', ' ', clean_title).strip()
                        
                        # Получаем соответствующий сниппет
                        snippet = ""
                        if i < len(snippet_matches):
                            snippet = re.sub(r'<[^>]+>', '', snippet_matches[i])
                            snippet = re.sub(r'\s+', ' ', snippet).strip()
                        
                        # Формируем текст результата
                        result_text = clean_title
                        if snippet and len(snippet) > 20:
                            result_text += f": {snippet[:200]}"
                        
                        # Проверяем, что это валидный результат
                        if result_text and len(result_text) > 30 and len(result_text) < 600:
                            if not any(skip in result_text.lower() for skip in ['cookie', 'javascript', 'enable', 'disable', 'advertisement']):
                                # Определяем источник по URL
                                source = "Интернет"
                                if "wikipedia.org" in url:
                                    source = "Википедия"
                                elif "news" in url or "новости" in url:
                                    source = "Новости"
                                
                                # Проверяем, нет ли дубликатов
                                is_duplicate = False
                                for existing in results:
                                    if existing.get("url") == url or existing.get("text", "").startswith(clean_title[:50]):
                                        is_duplicate = True
                                        break
                                
                                if not is_duplicate:
                                    results.append({
                                        "text": result_text,
                                        "url": url,
                                        "source": source
                                    })
            except Exception as e:
                logging.warning(f"Ошибка HTML поиска DuckDuckGo: {e}")
            
            # Метод 3: DuckDuckGo Instant Answer API (для быстрых фактов)
            if len(results) < max_results:
                try:
                    url = "https://api.duckduckgo.com/"
                    params = {
                        "q": query,
                        "format": "json",
                        "no_html": "1",
                        "skip_disambig": "1"
                    }
                    response = await client.get(url, params=params, timeout=10.0)
                    
                    if response.status_code == 200:
                        data = response.json()
                        
                        # Добавляем Abstract (краткое описание)
                        if data.get("Abstract") and data.get("AbstractURL"):
                            abstract_text = data["Abstract"]
                            abstract_url = data["AbstractURL"]
                            # Проверяем на дубликаты
                            is_duplicate = any(r.get("url") == abstract_url for r in results)
                            if not is_duplicate and len(abstract_text) > 20:
                                results.append({
                                    "text": abstract_text[:500],
                                    "url": abstract_url,
                                    "source": "DuckDuckGo"
                                })
                        
                        # Добавляем RelatedTopics (связанные темы)
                        if data.get("RelatedTopics") and len(results) < max_results:
                            for topic in data.get("RelatedTopics", []):
                                if len(results) >= max_results:
                                    break
                                if isinstance(topic, dict) and topic.get("Text"):
                                    text = topic["Text"]
                                    url_topic = topic.get("FirstURL", "")
                                    if text and len(text) > 20 and url_topic:
                                        is_duplicate = any(r.get("url") == url_topic for r in results)
                                        if not is_duplicate:
                                            results.append({
                                                "text": text[:500],
                                                "url": url_topic,
                                                "source": "DuckDuckGo"
                                            })
                except Exception as e:
                    logging.warning(f"Ошибка Instant Answer API: {e}")
            
            return results[:max_results] if results else []
    except Exception as e:
        logging.error(f"Ошибка веб-поиска: {e}")
        return []


async def get_crypto_price(crypto: str, lang: str = "ru") -> str:
    """Получает текущую цену криптовалюты через CoinGecko API с изменением за 24ч."""
    crypto_map = {
        "bitcoin": "bitcoin", "btc": "bitcoin",
        "ethereum": "ethereum", "eth": "ethereum",
        "usdt": "tether", "tether": "tether",
        "ltc": "litecoin", "litecoin": "litecoin",
        "usdc": "usd-coin", "usd coin": "usd-coin",
        "bnb": "binancecoin", "binance coin": "binancecoin", "binance": "binancecoin",
        "sol": "solana",
        "xrp": "ripple",
        "ada": "cardano",
        "doge": "dogecoin",
        "dot": "polkadot",
        "matic": "polygon",
        "avax": "avalanche-2",
        "link": "chainlink",
        "atom": "cosmos",
        "algo": "algorand",
        "near": "near",
        "ftm": "fantom",
        "sushi": "sushi",
        "uni": "uniswap",
        "aave": "aave",
        "comp": "compound-governance-token",
        "xmr": "monero", "monero": "monero",
        "dash": "dash",
        "zec": "zcash", "zcash": "zcash",
        "bch": "bitcoin-cash", "bitcoin cash": "bitcoin-cash",
        "etc": "ethereum-classic", "ethereum classic": "ethereum-classic",
        "xlm": "stellar", "stellar": "stellar",
        "eos": "eos",
        "trx": "tron", "tron": "tron",
        "vet": "vechain", "vechain": "vechain",
        "icp": "internet-computer", "internet computer": "internet-computer",
        "fil": "filecoin", "filecoin": "filecoin",
        "hbar": "hedera-hashgraph", "hedera": "hedera-hashgraph",
        "theta": "theta-token",
        "axs": "axie-infinity", "axie": "axie-infinity",
        "sand": "the-sandbox",
        "mana": "decentraland",
        "enj": "enjincoin", "enjin": "enjincoin",
    }
    
    crypto_id = crypto_map.get(crypto.lower(), crypto.lower())
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.coingecko.com/api/v3/simple/price",
                params={"ids": crypto_id, "vs_currencies": "usd", "include_24hr_change": "true"},
                timeout=10.0
            )
            if response.status_code == 200:
                data = response.json()
                if crypto_id in data:
                    price = data[crypto_id]["usd"]
                    change_24h = data[crypto_id].get("usd_24h_change", 0)
                    change_emoji = "📈" if change_24h > 0 else "📉" if change_24h < 0 else "➡️"
                    if lang == "ru":
                        return f"{crypto.upper()}: ${price:,.2f} ({change_emoji} {change_24h:+.2f}% за 24ч)"
                    else:
                        return f"{crypto.upper()}: ${price:,.2f} ({change_emoji} {change_24h:+.2f}% 24h)"
                else:
                    if lang == "ru":
                        return f"Криптовалюта {crypto} не найдена."
                    else:
                        return f"Cryptocurrency {crypto} not found."
            else:
                if lang == "ru":
                    return "Не удалось получить данные о криптовалюте. Попробуйте позже."
                else:
                    return "Failed to get cryptocurrency data. Try again later."
    except Exception as e:
        logging.error(f"Ошибка получения цены криптовалюты: {e}")
        if lang == "ru":
            return "Ошибка при получении данных о криптовалюте."
        else:
            return "Error getting cryptocurrency data."


async def get_weather(city: str, lang: str = "ru") -> str:
    """Получает погоду через OpenWeatherMap API (бесплатный план)."""
    # Используем бесплатный API без ключа (можно добавить ключ в .env)
    OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
    
    if not OPENWEATHER_API_KEY:
        # Используем альтернативный бесплатный API
        try:
            async with httpx.AsyncClient() as client:
                # Используем wttr.in как резервный вариант
                response = await client.get(
                    f"https://wttr.in/{city}?format=j1",
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    current = data["current_condition"][0]
                    temp = current["temp_C"]
                    desc = current["weatherDesc"][0]["value"]
                    if lang == "ru":
                        return f"Погода в {city}: {temp}°C, {desc}"
                    else:
                        return f"Weather in {city}: {temp}°C, {desc}"
        except Exception as e:
            logging.error(f"Ошибка получения погоды: {e}")
            if lang == "ru":
                return f"Не удалось получить погоду для {city}. Проверьте название города."
            else:
                return f"Failed to get weather for {city}. Check the city name."
    else:
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.openweathermap.org/data/2.5/weather",
                    params={"q": city, "appid": OPENWEATHER_API_KEY, "units": "metric", "lang": "ru" if lang == "ru" else "en"},
                    timeout=10.0
                )
                if response.status_code == 200:
                    data = response.json()
                    temp = data["main"]["temp"]
                    desc = data["weather"][0]["description"]
                    if lang == "ru":
                        return f"Погода в {city}: {temp}°C, {desc}"
                    else:
                        return f"Weather in {city}: {temp}°C, {desc}"
                else:
                    if lang == "ru":
                        return f"Не удалось получить погоду для {city}. Проверьте название города."
                    else:
                        return f"Failed to get weather for {city}. Check the city name."
        except Exception as e:
            logging.error(f"Ошибка получения погоды: {e}")
            if lang == "ru":
                return f"Ошибка при получении погоды для {city}."
            else:
                return f"Error getting weather for {city}."


async def convert_currency(amount: float, from_curr: str, to_curr: str, lang: str = "ru") -> str:
    """Конвертирует валюту через бесплатный API."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"https://api.exchangerate-api.com/v4/latest/{from_curr.upper()}",
                timeout=10.0
            )
            if response.status_code == 200:
                data = response.json()
                if to_curr.upper() in data["rates"]:
                    rate = data["rates"][to_curr.upper()]
                    result = amount * rate
                    if lang == "ru":
                        return f"{amount} {from_curr.upper()} = {result:.2f} {to_curr.upper()}"
                    else:
                        return f"{amount} {from_curr.upper()} = {result:.2f} {to_curr.upper()}"
                else:
                    if lang == "ru":
                        return f"Валюта {to_curr.upper()} не найдена."
                    else:
                        return f"Currency {to_curr.upper()} not found."
            else:
                if lang == "ru":
                    return "Не удалось получить курс валют. Попробуйте позже."
                else:
                    return "Failed to get exchange rate. Try again later."
    except Exception as e:
        logging.error(f"Ошибка конвертации валюты: {e}")
        if lang == "ru":
            return "Ошибка при конвертации валюты."
        else:
            return "Error converting currency."


# --- Функции финансовой аналитики ---
async def get_financial_data(asset: str, lang: str = "ru") -> dict:
    """Получает данные о финансовом инструменте (нефть, золото, акции и т.д.)."""
    asset_lower = asset.lower()
    
    # Маппинг инструментов
    asset_map = {
        # Нефть
        "wti": {"symbol": "CL=F", "name": "WTI Crude Oil"},
        "brent": {"symbol": "BZ=F", "name": "Brent Crude Oil"},
        "нефть wti": {"symbol": "CL=F", "name": "WTI Crude Oil"},
        "нефть brent": {"symbol": "BZ=F", "name": "Brent Crude Oil"},
        "crude oil": {"symbol": "CL=F", "name": "WTI Crude Oil"},
        # Золото
        "gold": {"symbol": "GC=F", "name": "Gold"},
        "золото": {"symbol": "GC=F", "name": "Gold"},
        "xau": {"symbol": "GC=F", "name": "Gold"},
        # Серебро
        "silver": {"symbol": "SI=F", "name": "Silver"},
        "серебро": {"symbol": "SI=F", "name": "Silver"},
        # Платина
        "platinum": {"symbol": "PL=F", "name": "Platinum"},
        "платина": {"symbol": "PL=F", "name": "Platinum"},
        # Палладий
        "palladium": {"symbol": "PA=F", "name": "Palladium"},
        "палладий": {"symbol": "PA=F", "name": "Palladium"},
        # Индексы
        "sp500": {"symbol": "^GSPC", "name": "S&P 500"},
        "s&p 500": {"symbol": "^GSPC", "name": "S&P 500"},
        "s&p": {"symbol": "^GSPC", "name": "S&P 500"},
        "nasdaq": {"symbol": "^IXIC", "name": "NASDAQ Composite"},
        "ndx": {"symbol": "^NDX", "name": "NASDAQ-100"},
        "nasdaq-100": {"symbol": "^NDX", "name": "NASDAQ-100"},
        "nasdaq100": {"symbol": "^NDX", "name": "NASDAQ-100"},
        "dow": {"symbol": "^DJI", "name": "Dow Jones"},
        "dow jones": {"symbol": "^DJI", "name": "Dow Jones"},
        # Популярные акции
        "apple": {"symbol": "AAPL", "name": "Apple Inc."},
        "aapl": {"symbol": "AAPL", "name": "Apple Inc."},
        "microsoft": {"symbol": "MSFT", "name": "Microsoft"},
        "msft": {"symbol": "MSFT", "name": "Microsoft"},
        "google": {"symbol": "GOOGL", "name": "Alphabet"},
        "googl": {"symbol": "GOOGL", "name": "Alphabet"},
        "tesla": {"symbol": "TSLA", "name": "Tesla"},
        "tsla": {"symbol": "TSLA", "name": "Tesla"},
        "nvidia": {"symbol": "NVDA", "name": "NVIDIA"},
        "nvda": {"symbol": "NVDA", "name": "NVIDIA"},
        # Популярные ETF
        "arkk": {"symbol": "ARKK", "name": "ARK Innovation ETF"},
        "tecl": {"symbol": "TECL", "name": "Direxion Daily Technology Bull 3X Shares"},
        "spy": {"symbol": "SPY", "name": "SPDR S&P 500 ETF"},
        "qqq": {"symbol": "QQQ", "name": "Invesco QQQ Trust"},
        "vti": {"symbol": "VTI", "name": "Vanguard Total Stock Market ETF"},
        "voo": {"symbol": "VOO", "name": "Vanguard S&P 500 ETF"},
        "arkq": {"symbol": "ARKQ", "name": "ARK Autonomous Technology & Robotics ETF"},
        "arkg": {"symbol": "ARKG", "name": "ARK Genomic Revolution ETF"},
        "arkw": {"symbol": "ARKW", "name": "ARK Next Generation Internet ETF"},
        "soxl": {"symbol": "SOXL", "name": "Direxion Daily Semiconductor Bull 3X Shares"},
        "tqqq": {"symbol": "TQQQ", "name": "ProShares UltraPro QQQ"},
        "sqqq": {"symbol": "SQQQ", "name": "ProShares UltraPro Short QQQ"},
    }
    
    asset_info = None
    for key, value in asset_map.items():
        if key in asset_lower:
            asset_info = value
            break
    
    # Если не найден в маппинге, пробуем использовать как тикер напрямую
    if not asset_info:
        # Проверяем, похоже ли на тикер (1-10 символов, может содержать точки, дефисы, специальные символы)
        # Тикеры обычно: AAPL, MSFT, AVGO, CAT, BRK.A, ^GSPC, CL=F
        # Валютные пары: EURUSD=X, GBPUSD=X и т.д.
        ticker_pattern = re.match(r'^[A-Z0-9^=\.\-\/]{1,15}$', asset.upper())
        if ticker_pattern:
            # Используем как тикер напрямую
            symbol = asset.upper()
            # Если это валюта без =X, добавляем =X для валютных пар Yahoo Finance
            if '/' in symbol or (len(symbol) == 6 and symbol.isalpha()):
                # Это похоже на валютную пару (EUR/USD или EURUSD)
                symbol = symbol.replace('/', '') + "=X"
            # Пробуем получить данные с этим тикером
            asset_info = {"symbol": symbol, "name": asset.upper()}
        else:
            # Если не похоже на тикер, возвращаем ошибку
            return {"error": f"Инструмент {asset} не найден" if lang == "ru" else f"Asset {asset} not found"}
    
    try:
        # Используем Yahoo Finance через альтернативный API
        async with httpx.AsyncClient() as client:
            # Используем бесплатный API для получения данных Yahoo Finance
            url = f"https://query1.finance.yahoo.com/v8/finance/chart/{asset_info['symbol']}"
            params = {
                "interval": "1d",
                "range": "1mo"  # Данные за месяц
            }
            # Добавляем заголовки для уменьшения блокировок
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://finance.yahoo.com/",
                "Origin": "https://finance.yahoo.com"
            }
            response = await client.get(url, params=params, headers=headers, timeout=15.0)
            
            # Если получили 429, пробуем альтернативный источник
            if response.status_code == 429:
                logging.warning(f"Yahoo Finance rate limit (429), trying alternative source for {asset_info['symbol']}")
                # Небольшая задержка перед повторным запросом
                await asyncio.sleep(1)
                # Пробуем альтернативный endpoint
                alt_url = f"https://query2.finance.yahoo.com/v8/finance/chart/{asset_info['symbol']}"
                response = await client.get(alt_url, params=params, headers=headers, timeout=15.0)
            
            if response.status_code == 200:
                data = response.json()
                result = data.get("chart", {}).get("result", [])
                if result:
                    meta = result[0].get("meta", {})
                    indicators = result[0].get("indicators", {}).get("quote", [{}])[0]
                    timestamps = result[0].get("timestamp", [])
                    closes = indicators.get("close", [])
                    opens = indicators.get("open", [])
                    highs = indicators.get("high", [])
                    lows = indicators.get("low", [])
                    volumes = indicators.get("volume", [])
                    
                    # Текущая цена
                    current_price = meta.get("regularMarketPrice", closes[-1] if closes else None)
                    previous_close = meta.get("previousClose", closes[-2] if len(closes) > 1 else None)
                    
                    # Изменение
                    change = current_price - previous_close if current_price and previous_close else 0
                    change_percent = (change / previous_close * 100) if previous_close else 0
                    
                    # Данные за последние дни для анализа тренда
                    recent_prices = [p for p in closes[-7:] if p is not None]  # Последние 7 дней
                    
                    return {
                        "name": asset_info["name"],
                        "symbol": asset_info["symbol"],
                        "current_price": current_price,
                        "previous_close": previous_close,
                        "change": change,
                        "change_percent": change_percent,
                        "high": max([p for p in highs if p is not None]) if highs else None,
                        "low": min([p for p in lows if p is not None]) if lows else None,
                        "volume": volumes[-1] if volumes else None,
                        "recent_prices": recent_prices,
                        "timestamps": timestamps[-7:] if timestamps else []
                    }
            elif response.status_code == 429:
                # Если все еще 429, используем альтернативный метод через другой API
                logging.warning(f"Rate limit exceeded, trying alternative API for {asset_info['symbol']}")
                return await get_financial_data_alternative(asset_info['symbol'], asset_info['name'], lang)
            else:
                # Пробуем альтернативный метод
                logging.warning(f"Yahoo Finance returned {response.status_code}, trying alternative")
                return await get_financial_data_alternative(asset_info['symbol'], asset_info['name'], lang)
    except Exception as e:
        logging.error(f"Ошибка получения финансовых данных: {e}")
        # Пробуем альтернативный метод только если asset_info определен
        if asset_info:
            try:
                return await get_financial_data_alternative(asset_info['symbol'], asset_info['name'], lang)
            except:
                return {"error": f"Ошибка: {str(e)}" if lang == "ru" else f"Error: {str(e)}"}
        else:
            return {"error": f"Инструмент {asset} не найден" if lang == "ru" else f"Asset {asset} not found"}


async def get_financial_data_alternative(symbol: str, name: str, lang: str = "ru") -> dict:
    """Альтернативный метод получения финансовых данных через другие источники."""
    try:
        # Используем альтернативный бесплатный API (например, через finnhub или другие)
        # Для простоты используем более простой endpoint Yahoo Finance или другой источник
        
        # Пробуем через другой endpoint Yahoo Finance
        async with httpx.AsyncClient() as client:
            # Используем более простой запрос
            url = f"https://query1.finance.yahoo.com/v7/finance/quote"
            params = {
                "symbols": symbol,
                "fields": "regularMarketPrice,regularMarketChange,regularMarketChangePercent,regularMarketPreviousClose"
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": "https://finance.yahoo.com/"
            }
            response = await client.get(url, params=params, headers=headers, timeout=15.0)
            
            if response.status_code == 200:
                data = response.json()
                result = data.get("quoteResponse", {}).get("result", [])
                if result:
                    quote = result[0]
                    current_price = quote.get("regularMarketPrice", 0)
                    previous_close = quote.get("regularMarketPreviousClose", current_price)
                    change = quote.get("regularMarketChange", 0)
                    change_percent = quote.get("regularMarketChangePercent", 0)
                    
                    # Для тренда используем упрощенный анализ на основе изменения
                    if change_percent > 2:
                        recent_prices = [previous_close, current_price]
                    elif change_percent < -2:
                        recent_prices = [previous_close, current_price]
                    else:
                        recent_prices = [previous_close, current_price]
                    
                    return {
                        "name": name,
                        "symbol": symbol,
                        "current_price": current_price,
                        "previous_close": previous_close,
                        "change": change,
                        "change_percent": change_percent,
                        "high": current_price,
                        "low": current_price,
                        "volume": None,
                        "recent_prices": recent_prices,
                        "timestamps": []
                    }
        
        # Если не получилось, возвращаем базовую информацию через CoinGecko для товаров
        # (для нефти и золота можно использовать CoinGecko)
        commodity_map = {
            "CL=F": "crude-oil",
            "BZ=F": "crude-oil",
            "GC=F": "gold",
            "SI=F": "silver"
        }
        
        if symbol in commodity_map:
            try:
                async with httpx.AsyncClient() as client:
                    # Используем CoinGecko для товаров
                    cg_id = commodity_map[symbol]
                    response = await client.get(
                        f"https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": cg_id, "vs_currencies": "usd", "include_24hr_change": "true"},
                        timeout=10.0
                    )
                    if response.status_code == 200:
                        data = response.json()
                        if cg_id in data:
                            price = data[cg_id]["usd"]
                            change_24h = data[cg_id].get("usd_24h_change", 0)
                            return {
                                "name": name,
                                "symbol": symbol,
                                "current_price": price,
                                "previous_close": price * (1 - change_24h / 100),
                                "change": price * change_24h / 100,
                                "change_percent": change_24h,
                                "high": price,
                                "low": price,
                                "volume": None,
                                "recent_prices": [price * (1 - change_24h / 100), price],
                                "timestamps": []
                            }
            except Exception as e:
                logging.warning(f"CoinGecko fallback failed: {e}")
        
        # Если ничего не сработало, возвращаем ошибку
        return {"error": "Не удалось получить данные. Попробуйте позже." if lang == "ru" else "Failed to get data. Please try again later."}
    except Exception as e:
        logging.error(f"Ошибка альтернативного метода получения данных: {e}")
        return {"error": f"Ошибка получения данных: {str(e)}" if lang == "ru" else f"Error getting data: {str(e)}"}


async def analyze_trend(prices: list, lang: str = "ru") -> dict:
    """Анализирует тренд на основе исторических цен."""
    if not prices or len(prices) < 2:
        if lang == "ru":
            return {"trend": "недостаточно данных", "strength": 0, "trend_en": "insufficient data"}
        else:
            return {"trend": "insufficient data", "strength": 0, "trend_en": "insufficient data"}
    
    # Убираем None значения
    clean_prices = [p for p in prices if p is not None]
    if len(clean_prices) < 2:
        if lang == "ru":
            return {"trend": "недостаточно данных", "strength": 0, "trend_en": "insufficient data"}
        else:
            return {"trend": "insufficient data", "strength": 0, "trend_en": "insufficient data"}
    
    # Простой анализ тренда
    first_half = clean_prices[:len(clean_prices)//2]
    second_half = clean_prices[len(clean_prices)//2:]
    
    avg_first = sum(first_half) / len(first_half)
    avg_second = sum(second_half) / len(second_half)
    
    change_percent = ((avg_second - avg_first) / avg_first * 100) if avg_first else 0
    
    if change_percent > 2:
        trend = "растущий" if lang == "ru" else "upward"
        trend_en = "upward"
        strength = min(abs(change_percent) / 10, 1.0)  # Нормализуем до 0-1
    elif change_percent < -2:
        trend = "падающий" if lang == "ru" else "downward"
        trend_en = "downward"
        strength = min(abs(change_percent) / 10, 1.0)
    else:
        trend = "боковой" if lang == "ru" else "sideways"
        trend_en = "sideways"
        strength = 0.3
    
    return {
        "trend": trend,
        "trend_en": trend_en,
        "strength": strength,
        "change_percent": change_percent
    }


async def get_financial_analysis(asset: str, lang: str = "ru") -> str:
    """Получает полный анализ финансового инструмента с прогнозом."""
    try:
        # Получаем данные
        data = await get_financial_data(asset, lang)
        
        if "error" in data:
            return data["error"]
        
        # Проверяем, что данные валидны
        if not data or not isinstance(data, dict):
            if lang == "ru":
                return f"Не удалось получить данные для {asset}. Попробуйте позже."
            else:
                return f"Failed to get data for {asset}. Try again later."
        
        # Анализируем тренд
        trend_analysis = await analyze_trend(data.get("recent_prices", []), lang)
        
        # Формируем ответ
        name = data.get("name", asset)
        current = data.get("current_price", 0)
        change = data.get("change", 0)
        change_percent = data.get("change_percent", 0)
        
        # Определяем тренд на основе текущего изменения и исторических данных
        # Если текущее изменение 0% или очень близко к 0, тренд должен быть "боковой"
        if abs(change_percent) < 0.01:  # Меньше 0.01% - считаем нулевым
            trend = "боковой" if lang == "ru" else "sideways"
            strength = 0.0
        elif change_percent > 0:
            trend = "растущий" if lang == "ru" else "upward"
            # Сила тренда на основе текущего изменения
            strength = min(abs(change_percent) / 5, 1.0)
        else:
            trend = "падающий" if lang == "ru" else "downward"
            strength = min(abs(change_percent) / 5, 1.0)
        
        # Если текущее изменение небольшое, но есть исторический тренд, учитываем его
        if abs(change_percent) < 0.5 and trend_analysis.get("strength", 0) > 0.5:
            historical_trend = trend_analysis.get("trend", "")
            if historical_trend in ["растущий", "upward"]:
                trend = "растущий" if lang == "ru" else "upward"
                strength = trend_analysis.get("strength", 0) * 0.5  # Ослабляем силу
            elif historical_trend in ["падающий", "downward"]:
                trend = "падающий" if lang == "ru" else "downward"
                strength = trend_analysis.get("strength", 0) * 0.5
        
        change_trend = trend_analysis.get("change_percent", 0)
        
        # Прогноз на основе тренда
        if lang == "ru":
            trend_emoji = "📈" if trend == "растущий" else "📉" if trend == "падающий" else "➡️"
            strength_text = "сильный" if strength > 0.7 else "умеренный" if strength > 0.4 else "слабый" if strength > 0.1 else "отсутствует"
            
            # Рекомендация
            if abs(change_percent) < 0.01:
                recommendation = "Цена не изменилась, рынок в состоянии равновесия. Рекомендуется дождаться более четких сигналов перед принятием решений."
            elif trend == "растущий" and strength > 0.5:
                recommendation = "Тренд восходящий, можно рассмотреть покупку, но следите за рисками."
            elif trend == "падающий" and strength > 0.5:
                recommendation = "Тренд нисходящий, возможно стоит подождать или рассмотреть продажу."
            elif trend == "растущий" and strength > 0.1:
                recommendation = "Небольшой восходящий тренд, но сигнал слабый. Рекомендуется осторожность."
            elif trend == "падающий" and strength > 0.1:
                recommendation = "Небольшой нисходящий тренд, но сигнал слабый. Рекомендуется осторожность."
            else:
                recommendation = "Тренд неопределенный или отсутствует, рекомендуется осторожность и ожидание более четких сигналов."
            
            result = (
                f"{name}\n"
                f"Текущая цена: ${current:.2f}\n"
                f"Изменение: {change:+.2f} ({change_percent:+.2f}%)\n"
                f"Тренд: {trend_emoji} {trend} ({strength_text})\n"
                f"Изменение за период: {change_trend:+.2f}%\n\n"
                f"Анализ: {recommendation}\n\n"
                f"⚠️ Это не инвестиционный совет. Всегда проводите собственный анализ."
            )
        else:
            trend_emoji = "📈" if trend == "растущий" or trend == "upward" else "📉" if trend == "падающий" or trend == "downward" else "➡️"
            strength_text = "strong" if strength > 0.7 else "moderate" if strength > 0.4 else "weak" if strength > 0.1 else "none"
            
            if abs(change_percent) < 0.01:
                recommendation = "Price unchanged, market in equilibrium. Consider waiting for clearer signals before making decisions."
            elif trend == "растущий" or trend == "upward":
                if strength > 0.5:
                    recommendation = "Uptrend detected, consider buying but watch for risks."
                else:
                    recommendation = "Slight uptrend but weak signal. Caution recommended."
            elif trend == "падающий" or trend == "downward":
                if strength > 0.5:
                    recommendation = "Downtrend detected, consider waiting or selling."
                else:
                    recommendation = "Slight downtrend but weak signal. Caution recommended."
            else:
                recommendation = "Uncertain or no trend, caution and waiting for clearer signals recommended."
            
            result = (
                f"{name}\n"
                f"Current price: ${current:.2f}\n"
                f"Change: {change:+.2f} ({change_percent:+.2f}%)\n"
                f"Trend: {trend_emoji} {trend} ({strength_text})\n"
                f"Period change: {change_trend:+.2f}%\n\n"
                f"Analysis: {recommendation}\n\n"
                f"⚠️ This is not investment advice. Always do your own research."
            )
        
        return result
    except Exception as e:
        logging.error(f"Ошибка в get_financial_analysis для {asset}: {e}")
        if lang == "ru":
            return f"Ошибка при анализе {asset}. Попробуйте позже."
        else:
            return f"Error analyzing {asset}. Try again later."


async def compare_assets(assets: list, lang: str = "ru") -> str:
    """Сравнивает несколько финансовых активов (акции, ETF, крипта, товары и т.д.) и возвращает сравнительный анализ."""
    if not assets:
        return "Не указаны активы для сравнения." if lang == "ru" else "No assets specified for comparison."
    
    results = []
    for asset in assets:
        # Сначала проверяем, это криптовалюта?
        crypto_map_full = {
            "bitcoin": "bitcoin", "btc": "bitcoin",
            "ethereum": "ethereum", "eth": "ethereum",
            "usdt": "tether", "tether": "tether",
            "ltc": "litecoin", "litecoin": "litecoin",
            "xmr": "monero", "monero": "monero",
            "bnb": "binancecoin", "binance coin": "binancecoin", "binance": "binancecoin",
            "sol": "solana",
            "xrp": "ripple",
            "ada": "cardano",
            "doge": "dogecoin",
        }
        crypto_id = crypto_map_full.get(asset.lower())
        
        # Если это криптовалюта, используем get_crypto_price
        if crypto_id:
            try:
                async with httpx.AsyncClient() as client:
                    response = await client.get(
                        f"https://api.coingecko.com/api/v3/simple/price",
                        params={"ids": crypto_id, "vs_currencies": "usd", "include_24hr_change": "true"},
                        timeout=10.0
                    )
                    if response.status_code == 200:
                        crypto_data = response.json()
                        if crypto_id in crypto_data:
                            price = crypto_data[crypto_id]["usd"]
                            change_24h = crypto_data[crypto_id].get("usd_24h_change", 0)
                            results.append({
                                "name": asset.upper(),
                                "price": price,
                                "change": price * change_24h / 100,
                                "change_percent": change_24h,
                                "trend": "растущий" if change_24h > 0 else "падающий" if change_24h < 0 else "боковой" if lang == "ru" else ("upward" if change_24h > 0 else "downward" if change_24h < 0 else "sideways"),
                                "strength": min(abs(change_24h) / 10, 1.0)
                            })
                            await asyncio.sleep(0.3)
                            continue
            except Exception as e:
                logging.warning(f"Ошибка получения данных криптовалюты {asset}: {e}")
        
        # Если не криптовалюта, пытаемся получить данные через get_financial_data
        data = await get_financial_data(asset, lang)
        
        # Если получили данные через get_financial_data
        if "error" not in data:
            trend_analysis = await analyze_trend(data.get("recent_prices", []), lang)
            current_price = data.get("current_price") or 0
            change = data.get("change") or 0
            change_percent = data.get("change_percent") or 0
            # Пропускаем активы без цены
            if current_price is None or current_price == 0:
                continue
            results.append({
                "name": data.get("name", asset),
                "price": current_price,
                "change": change,
                "change_percent": change_percent,
                "trend": trend_analysis.get("trend", "неизвестен" if lang == "ru" else "unknown"),
                "strength": trend_analysis.get("strength", 0)
            })
        # Небольшая задержка между запросами
        await asyncio.sleep(0.5)
    
    if not results:
        return "Не удалось получить данные для сравнения." if lang == "ru" else "Failed to get data for comparison."
    
    # Формируем сравнительный анализ
    if lang == "ru":
        result_text = "Сравнительный анализ активов:\n\n"
        for i, r in enumerate(results, 1):
            # Безопасное форматирование с проверкой на None
            price = r.get('price') or 0
            change = r.get('change') or 0
            change_percent = r.get('change_percent') or 0
            trend_emoji = "📈" if r["trend"] == "растущий" else "📉" if r["trend"] == "падающий" else "➡️"
            strength_text = "сильный" if r["strength"] > 0.7 else "умеренный" if r["strength"] > 0.4 else "слабый"
            result_text += (
                f"{i}. {r['name']}:\n"
                f"   Цена: ${price:.2f}\n"
                f"   Изменение: {change:+.2f} ({change_percent:+.2f}%)\n"
                f"   Тренд: {trend_emoji} {r['trend']} ({strength_text})\n\n"
            )
        result_text += "⚠️ Это не инвестиционный совет. Всегда проводите собственный анализ."
    else:
        result_text = "Asset comparison:\n\n"
        for i, r in enumerate(results, 1):
            # Безопасное форматирование с проверкой на None
            price = r.get('price') or 0
            change = r.get('change') or 0
            change_percent = r.get('change_percent') or 0
            trend_emoji = "📈" if r["trend"] == "upward" else "📉" if r["trend"] == "downward" else "➡️"
            strength_text = "strong" if r["strength"] > 0.7 else "moderate" if r["strength"] > 0.4 else "weak"
            result_text += (
                f"{i}. {r['name']}:\n"
                f"   Price: ${price:.2f}\n"
                f"   Change: {change:+.2f} ({change_percent:+.2f}%)\n"
                f"   Trend: {trend_emoji} {r['trend']} ({strength_text})\n\n"
            )
        result_text += "⚠️ This is not investment advice. Always do your own research."
    
    return result_text


# --- Функции анализа аудио и рекомендаций ---
async def analyze_audio_mood_genre(file_path: str, lang: str = "ru") -> dict:
    """Анализирует аудиофайл и определяет жанр и настроение через AI."""
    try:
        # Сначала получаем базовые характеристики через librosa
        y, sr = librosa.load(file_path, duration=30)  # Анализируем первые 30 секунд
        
        # Базовые характеристики
        tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
        key, _ = analyze_audio_key_bpm(file_path)
        
        # Спектральные характеристики
        spectral_centroids = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
        spectral_rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)[0]
        zero_crossing_rate = librosa.feature.zero_crossing_rate(y)[0]
        
        avg_centroid = np.mean(spectral_centroids)
        avg_rolloff = np.mean(spectral_rolloff)
        avg_zcr = np.mean(zero_crossing_rate)
        
        # Формируем описание для AI
        audio_description = (
            f"Audio characteristics: tempo={tempo:.1f} BPM, key={key}, "
            f"spectral_centroid={avg_centroid:.1f}, spectral_rolloff={avg_rolloff:.1f}, "
            f"zero_crossing_rate={avg_zcr:.3f}"
        )
        
        # Используем AI для определения жанра и настроения
        if deepseek_client:
            try:
                prompt = (
                    f"Analyze this audio file based on these characteristics: {audio_description}. "
                    "Determine: 1) Mood/emotion (aggressive, calm, energetic, melancholic, happy, dark, etc.), "
                    "2) General style description (without mentioning specific genres like trap, drill, etc.). "
                    "Respond in JSON format: {{\"mood\": \"...\", \"style\": \"...\"}}"
                )
                
                response = await deepseek_client.chat.completions.create(
                    model="deepseek-chat",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=150,
                )
                
                if response.choices and response.choices[0].message:
                    result_text = response.choices[0].message.content.strip()
                    # Пытаемся извлечь JSON
                    json_match = re.search(r'\{[^}]+\}', result_text)
                    if json_match:
                        result = json.loads(json_match.group())
                        return {
                            "mood": result.get("mood", "unknown"),
                            "style": result.get("style", "unknown"),
                            "tempo": tempo,
                            "key": key
                        }
            except Exception as e:
                logging.error(f"Ошибка AI анализа аудио: {e}")
        
        # Fallback: определяем по характеристикам
        mood = "energetic" if tempo > 120 else "calm"
        if avg_centroid > 3000:
            mood = "aggressive"
        elif avg_centroid < 1500:
            mood = "melancholic"
        
        return {
            "mood": mood,
            "style": "unique style",
            "tempo": tempo,
            "key": key
        }
    except Exception as e:
        logging.error(f"Ошибка анализа аудио: {e}")
        return {"mood": "unknown", "style": "unknown", "tempo": 0, "key": "unknown"}


async def recommend_beats(user_preferences: dict, lang: str = "ru") -> str:
    """Рекомендует биты на основе предпочтений пользователя. ВСЕГДА рекомендует только биты из архива https://t.me/rrelement."""
    # Всегда направляем в архив
    archive_link = "https://t.me/rrelement"
    
    if lang == "ru":
        return f"Рекомендую посмотреть биты в архиве: {archive_link}. Там ты найдешь биты в разных стилях и настроениях."
    else:
        return f"I recommend checking beats in the archive: {archive_link}. You'll find beats in different styles and moods."


# --- Функции голосовых сообщений ---
async def transcribe_voice(voice_file_path: str, lang: str = "ru") -> str:
    """Преобразует голосовое сообщение в текст. В данный момент недоступно."""
    # Распознавание голоса временно недоступно
    if lang == "ru":
        return "Распознавание голоса временно недоступно. Напиши текстом, пожалуйста."
    else:
        return "Voice recognition is temporarily unavailable. Please type your message."


async def text_to_speech(text: str, lang: str = "ru") -> bytes:
    """Преобразует текст в голосовое сообщение. Возвращает bytes аудиофайла."""
    # Используем бесплатный TTS API (например, Google TTS через gTTS или альтернативный)
    try:
        # Для простоты используем онлайн TTS сервис
        async with httpx.AsyncClient() as client:
            # Используем бесплатный API (можно заменить на более качественный)
            tts_url = f"https://api.voicerss.org/?key=YOUR_KEY&hl={'ru-ru' if lang == 'ru' else 'en-us'}&src={text}"
            # Временно возвращаем None, так как нужен API ключ
            # В продакшене можно использовать gTTS или другой сервис
            return None
    except Exception as e:
        logging.error(f"Ошибка преобразования текста в речь: {e}")
        return None


# --- Функции обработки ошибок ---
def get_error_message(error: Exception, context: str, lang: str = "ru") -> str:
    """
    Форматирует понятное сообщение об ошибке для пользователя.
    """
    error_type = type(error).__name__
    error_msg = str(error).lower()
    
    # Определяем тип ошибки и возвращаем понятное сообщение
    if "file" in error_msg or "файл" in error_msg:
        if lang == "ru":
            return (
                "❌ *Ошибка работы с файлом*\n\n"
                "Проверь:\n"
                "• Файл не поврежден\n"
                "• Формат файла поддерживается (MP3, WAV)\n"
                "• Размер файла не слишком большой\n\n"
                "Попробуй отправить файл снова или используй /cancel для отмены."
            )
        else:
            return (
                "❌ *File error*\n\n"
                "Please check:\n"
                "• File is not corrupted\n"
                "• File format is supported (MP3, WAV)\n"
                "• File size is not too large\n\n"
                "Try sending the file again or use /cancel to cancel."
            )
    
    elif "network" in error_msg or "connection" in error_msg or "timeout" in error_msg:
        if lang == "ru":
            return (
                "❌ *Проблема с сетью*\n\n"
                "Похоже, возникла проблема с подключением.\n\n"
                "Попробуй:\n"
                "• Проверить интернет-соединение\n"
                "• Подождать несколько секунд и повторить\n"
                "• Использовать /cancel и начать заново"
            )
        else:
            return (
                "❌ *Network issue*\n\n"
                "It seems there's a connection problem.\n\n"
                "Try:\n"
                "• Check your internet connection\n"
                "• Wait a few seconds and retry\n"
                "• Use /cancel and start over"
            )
    
    elif "audio" in error_msg or "librosa" in error_msg or "bpm" in error_msg:
        if lang == "ru":
            return (
                "❌ *Ошибка анализа аудио*\n\n"
                "Не удалось проанализировать аудиофайл.\n\n"
                "Попробуй:\n"
                "• Отправить файл в формате MP3 или WAV\n"
                "• Убедиться, что файл содержит музыку\n"
                "• Проверить, что файл не поврежден\n\n"
                "Используй /cancel для отмены."
            )
        else:
            return (
                "❌ *Audio analysis error*\n\n"
                "Failed to analyze audio file.\n\n"
                "Try:\n"
                "• Send file in MP3 or WAV format\n"
                "• Make sure file contains music\n"
                "• Check that file is not corrupted\n\n"
                "Use /cancel to cancel."
            )
    
    elif "permission" in error_msg or "access" in error_msg:
        if lang == "ru":
            return (
                "❌ *Ошибка доступа*\n\n"
                "Недостаточно прав для выполнения операции.\n\n"
                "Если проблема повторяется, свяжись с поддержкой:\n"
                "👉 https://t.me/rrelement1"
            )
        else:
            return (
                "❌ *Access error*\n\n"
                "Insufficient permissions to perform operation.\n\n"
                "If problem persists, contact support:\n"
                "👉 https://t.me/rrelement1"
            )
    
    else:
        # Общая ошибка
        if lang == "ru":
            return (
                "❌ *Произошла ошибка*\n\n"
                "Что-то пошло не так при обработке твоего запроса.\n\n"
                "Попробуй:\n"
                "• Использовать /cancel и начать заново\n"
                "• Проверить правильность введенных данных\n"
                "• Подождать и повторить попытку\n\n"
                "Если проблема повторяется:\n"
                "👉 https://t.me/rrelement1"
            )
        else:
            return (
                "❌ *An error occurred*\n\n"
                "Something went wrong while processing your request.\n\n"
                "Try:\n"
                "• Use /cancel and start over\n"
                "• Check if entered data is correct\n"
                "• Wait and try again\n\n"
                "If problem persists:\n"
                "👉 https://t.me/rrelement1"
            )


async def safe_send_message(bot: Bot, chat_id: int, text: str, lang: str = "ru", **kwargs):
    """
    Безопасная отправка сообщения с обработкой ошибок.
    """
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения в {chat_id}: {e}")
        try:
            error_text = get_error_message(e, "send_message", lang)
            await bot.send_message(chat_id, error_text, parse_mode="Markdown")
        except:
            pass  # Если даже ошибку не удалось отправить, просто логируем


# --- Клавиатуры ---
lang_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang_ru"),
            InlineKeyboardButton(text="🇺🇸 English", callback_data="lang_en"),
        ]
    ]
)

main_keyboard_ru = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="AskMe23"),
            KeyboardButton(text="Архив")
        ],
        [
            KeyboardButton(text="Купить"),
            KeyboardButton(text="Цены")
        ],
        [
            KeyboardButton(text="Сведение"),
            KeyboardButton(text="Бит на заказ")
        ],
        [
            KeyboardButton(text="Key & BPM"),
            KeyboardButton(text="Вопросы")
        ],
        [
            KeyboardButton(text="Партнерская программа")
        ],
    ],
    resize_keyboard=True,
)

main_keyboard_en = ReplyKeyboardMarkup(
    keyboard=[
        [
            KeyboardButton(text="AskMe23"),
            KeyboardButton(text="Archive")
        ],
        [
            KeyboardButton(text="Buy"),
            KeyboardButton(text="Prices")
        ],
        [
            KeyboardButton(text="Mixing"),
            KeyboardButton(text="Custom beat")
        ],
        [
            KeyboardButton(text="Key & BPM"),
            KeyboardButton(text="Questions")
        ],
        [
            KeyboardButton(text="Partnership Program")
        ],
    ],
    resize_keyboard=True,
)

# Inline кнопки для способов оплаты
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

# Inline кнопки "оплатил"
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

# Кнопки выбора криптовалюты
crypto_inline_ru = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="USDT", callback_data="crypto_usdt"),
            InlineKeyboardButton(text="BTC", callback_data="crypto_btc"),
        ],
        [
            InlineKeyboardButton(text="ETH", callback_data="crypto_eth"),
            InlineKeyboardButton(text="LTC", callback_data="crypto_ltc"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")],
    ]
)

crypto_inline_en = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="USDT", callback_data="crypto_usdt"),
            InlineKeyboardButton(text="BTC", callback_data="crypto_btc"),
        ],
        [
            InlineKeyboardButton(text="ETH", callback_data="crypto_eth"),
            InlineKeyboardButton(text="LTC", callback_data="crypto_ltc"),
        ],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")],
    ]
)

# Кнопки запроса реквизитов для карты
card_request_inline_ru = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📩 Запросить реквизиты", callback_data="req_card")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
    ]
)

card_request_inline_en = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📩 Request details", callback_data="req_card")],
        [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")]
    ]
)

# --- Функции форматирования ---
def format_license_and_price(license_str: str) -> tuple[str, str]:
    """Форматирует лицензию и цену, разделяя их на две строки.
    Возвращает (license_text, price_text)
    Сохраняет символ $ в цене для отображения в интерфейсе."""
    # Если лицензия содержит " — ", разделяем на тип лицензии и цену
    if " — " in license_str:
        parts = license_str.split(" — ", 1)
        license_type = parts[0].strip()
        license_price = parts[1].strip() if len(parts) > 1 else ""
        # Сохраняем $ в цене для интерфейса выбора лицензии
        return license_type, license_price
    else:
        # Если лицензия не содержит " — ", проверяем, не является ли она просто ценой с $
        license_str_clean = license_str.strip()
        return license_str, license_str_clean if license_str_clean != license_str else ""

# --- Константы для анализа BPM ---
BPM_ANALYSIS_SAMPLE_RATE = 44100  # Частота дискретизации для анализа
BPM_ANALYSIS_START = 10  # Начало анализируемого фрагмента (секунды) - пропускаем начало трека
BPM_ANALYSIS_END = 40  # Конец анализируемого фрагмента (секунды) - пропускаем концовку
BPM_ANALYSIS_DURATION = BPM_ANALYSIS_END - BPM_ANALYSIS_START  # Длительность анализируемого фрагмента
BPM_MIN = 80  # Минимальный BPM
BPM_MAX = 180  # Максимальный BPM
BPM_DEFAULT = 120  # BPM по умолчанию
HOP_LENGTH = 512  # Длина hop для анализа
ONSET_DELTA = 0.07  # Порог для обнаружения onset'ов
ONSET_WAIT = 30  # Минимальное время между onset'ами (в кадрах)

# --- Функции анализа аудио ---
def fix_bpm(bpm: float, min_bpm: float = BPM_MIN, max_bpm: float = BPM_MAX) -> float:
    """
    Исправляет BPM, приводя значение в диапазон min_bpm-max_bpm.
    Проверяет октавы (деление/умножение на 2, 3, 4).
    
    Args:
        bpm: Исходное значение BPM
        min_bpm: Минимальный BPM (по умолчанию BPM_MIN)
        max_bpm: Максимальный BPM (по умолчанию BPM_MAX)
    
    Returns:
        float: Исправленное значение BPM в диапазоне min_bpm-max_bpm
    """
    # Если уже в диапазоне, возвращаем как есть
    if min_bpm <= bpm <= max_bpm:
        return bpm
    
    # Пробуем разные варианты нормализации
    candidates = []
    
    # Если значение слишком высокое, пробуем разделить
    if bpm > max_bpm:
        for divisor in [2, 3, 4]:
            normalized = bpm / divisor
            if min_bpm <= normalized <= max_bpm:
                candidates.append(normalized)
    
    # Если значение слишком низкое, пробуем умножить
    if bpm < min_bpm:
        for multiplier in [2, 3, 4]:
            normalized = bpm * multiplier
            if min_bpm <= normalized <= max_bpm:
                candidates.append(normalized)
    
    # Выбираем ближайшее к исходному значению
    if candidates:
        return min(candidates, key=lambda x: abs(x - bpm))
    
    # Если ничего не подошло, просто ограничиваем диапазоном
    if bpm < min_bpm:
        return min_bpm
    elif bpm > max_bpm:
        return max_bpm
    
    return bpm


def analyze_bpm(path: str) -> int:
    """
    Анализирует аудиофайл и определяет BPM.
    
    Args:
        path: Путь к локальному аудиофайлу (mp3/wav/ogg)
    
    Returns:
        int: BPM в диапазоне BPM_MIN-BPM_MAX
    """
    try:
        # Проверяем существование файла
        if not os.path.exists(path):
            logging.error(f"Файл не найден: {path}")
            return BPM_DEFAULT
        
        # Проверяем размер файла
        file_size = os.path.getsize(path)
        if file_size == 0:
            logging.error("Файл пуст")
            return BPM_DEFAULT
        
        # Загружаем аудиофайл с указанными параметрами
        # Анализируем участок 10-40 секунд (пропускаем начало и концовку)
        try:
            # Сначала загружаем нужный участок
            y_full, sr = librosa.load(
                path,
                sr=BPM_ANALYSIS_SAMPLE_RATE,
                mono=True,
                offset=BPM_ANALYSIS_START,
                duration=BPM_ANALYSIS_DURATION
            )
            y = y_full
        except Exception as e:
            # Если не получилось с offset, пробуем загрузить весь файл и обрезать
            logging.warning(f"Не удалось загрузить с offset, пробую другой способ: {e}")
            try:
                y_full, sr = librosa.load(path, sr=BPM_ANALYSIS_SAMPLE_RATE, mono=True)
                # Обрезаем до нужного участка
                start_sample = int(BPM_ANALYSIS_START * sr)
                end_sample = int(BPM_ANALYSIS_END * sr)
                if end_sample > len(y_full):
                    end_sample = len(y_full)
                if start_sample < len(y_full):
                    y = y_full[start_sample:end_sample]
                else:
                    # Если файл короче, используем что есть
                    y = y_full
            except Exception as e2:
                logging.error(f"Не удалось загрузить файл: {e2}")
                return BPM_DEFAULT
        
        if len(y) == 0:
            logging.error("Аудиофайл пуст или поврежден")
            return BPM_DEFAULT
        
        # Предобработка: нормализация громкости
        # Нормализуем до максимальной амплитуды 0.95 (оставляем запас)
        max_val = np.max(np.abs(y))
        if max_val > 0:
            y = y / max_val * 0.95
        
        # Предобработка: фильтрация (band-pass для ударных)
        # Используем librosa для фильтрации низких и высоких частот
        # Ударные обычно в диапазоне 60-200 Hz (бас) и 2-5 kHz (щелчки)
        # Для упрощения используем high-pass фильтр на 40 Hz
        from scipy import signal
        try:
            # High-pass фильтр на 40 Hz для удаления очень низких частот
            nyquist = sr / 2
            low = 40.0 / nyquist
            b, a = signal.butter(4, low, btype='high')
            y = signal.filtfilt(b, a, y)
        except Exception as e:
            logging.warning(f"Не удалось применить фильтр: {e}")
            # Продолжаем без фильтрации
        
        # Метод 1: librosa.beat.beat_track
        bpm_candidates = []
        try:
            tempo, beats = librosa.beat.beat_track(
                y=y,
                sr=sr,
                units='time',
                trim=False,
                hop_length=HOP_LENGTH
            )
            if tempo is not None and len(tempo) > 0:
                raw_bpm = float(tempo[0])
                # Обязательно прогоняем через fix_bpm
                corrected_bpm = fix_bpm(raw_bpm)
                bpm_candidates.append(corrected_bpm)
        except Exception as e:
            logging.warning(f"Метод beat_track не сработал: {e}")
        
        # Метод 2: Автокорреляция onset-функции
        try:
            # Вычисляем onset strength
            onset_strength = librosa.onset.onset_strength(
                y=y,
                sr=sr,
                aggregate=np.median,
                hop_length=HOP_LENGTH
            )
            
            # Нормализуем onset strength
            onset_strength_norm = (onset_strength - np.mean(onset_strength)) / (np.std(onset_strength) + 1e-10)
            
            # Вычисляем автокорреляцию
            autocorr = np.correlate(onset_strength_norm, onset_strength_norm, mode='full')
            autocorr = autocorr[len(autocorr)//2:]  # Берем только положительные задержки
            
            # Определяем диапазон для поиска BPM (BPM_MIN - BPM_MAX)
            frame_rate = sr / HOP_LENGTH
            min_period_frames = int(frame_rate * 60 / BPM_MAX)  # Минимальный период для BPM_MAX
            max_period_frames = int(frame_rate * 60 / BPM_MIN)  # Максимальный период для BPM_MIN
            
            # Ограничиваем диапазон поиска
            if max_period_frames < len(autocorr) and min_period_frames < max_period_frames:
                autocorr_range = autocorr[min_period_frames:max_period_frames]
                
                if len(autocorr_range) > 0:
                    # Находим пики в автокорреляции
                    threshold = np.max(autocorr_range) * 0.5
                    peaks = []
                    
                    # Ищем локальные максимумы
                    for i in range(1, len(autocorr_range) - 1):
                        if (autocorr_range[i] > autocorr_range[i-1] and 
                            autocorr_range[i] > autocorr_range[i+1] and
                            autocorr_range[i] > threshold):
                            peaks.append((i, autocorr_range[i]))
                    
                    if len(peaks) > 0:
                        # Сортируем по высоте и берем самый сильный пик
                        peaks.sort(key=lambda x: x[1], reverse=True)
                        strongest_peak_idx = peaks[0][0]
                        period_frames = strongest_peak_idx + min_period_frames
                        period_seconds = period_frames / frame_rate
                        bpm_from_autocorr = 60.0 / period_seconds
                        # Применяем fix_bpm для консистентности
                        corrected_bpm = fix_bpm(bpm_from_autocorr)
                        bpm_candidates.append(corrected_bpm)
        except Exception as e:
            logging.warning(f"Метод автокорреляции не сработал: {e}")
        
        # Логика коррекции BPM
        if len(bpm_candidates) == 0:
            logging.warning("Не удалось определить BPM ни одним методом")
            return BPM_DEFAULT
        
        # Используем медиану для устойчивости к выбросам
        # Если только одно значение, используем его
        if len(bpm_candidates) == 1:
            avg_bpm = bpm_candidates[0]
        else:
            # Используем медиану для более устойчивого результата
            avg_bpm = np.median(bpm_candidates)
        
        # Коррекция BPM
        corrected_bpm = avg_bpm
        
        # Если результат < BPM_MIN, пробуем умножить на 2
        if corrected_bpm < BPM_MIN:
            doubled = corrected_bpm * 2
            if BPM_MIN <= doubled <= BPM_MAX:
                corrected_bpm = doubled
        
        # Если результат > 200, пробуем разделить на 2
        if corrected_bpm > 200:
            halved = corrected_bpm / 2
            if BPM_MIN <= halved <= BPM_MAX:
                corrected_bpm = halved
        
        # Выбираем ближайшее значение в допустимом диапазоне
        if corrected_bpm < BPM_MIN:
            corrected_bpm = BPM_MIN
        elif corrected_bpm > BPM_MAX:
            corrected_bpm = BPM_MAX
        
        # Округляем до целого
        bpm = int(round(corrected_bpm))
        
        return bpm
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"Ошибка анализа BPM: {e}\n{error_details}")
        return BPM_DEFAULT


async def analyze_audio_key_bpm(file_path: str) -> tuple[str, float]:
    """
    Анализирует аудиофайл и определяет тональность (Key) и BPM.
    Возвращает (key, bpm)
    """
    try:
        # Проверяем существование файла
        if not os.path.exists(file_path):
            raise Exception(f"Файл не найден: {file_path}")
        
        # Проверяем размер файла
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            raise Exception("Файл пуст")
        
        # Загружаем аудиофайл (анализируем более длинный сегмент для точности)
        try:
            # Пробуем загрузить первые 90 секунд для более точного анализа
            y, sr = librosa.load(file_path, duration=90, sr=22050)
        except Exception as e:
            # Если не получилось, пробуем 60 секунд
            try:
                y, sr = librosa.load(file_path, duration=60, sr=22050)
            except Exception as e2:
                # Если и это не получилось, загружаем весь файл
                logging.warning(f"Не удалось загрузить с duration, пробую весь файл: {e2}")
                y, sr = librosa.load(file_path, sr=22050)
        
        if len(y) == 0:
            raise Exception("Аудиофайл пуст или поврежден")
        
        # Определяем BPM используя отдельную функцию
        bpm = analyze_bpm(file_path)
        
        # Определяем тональность
        # Используем chromagram для определения тональности
        try:
            chroma = librosa.feature.chroma_stft(y=y, sr=sr)
            chroma_mean = np.mean(chroma, axis=1)
        except Exception as e:
            logging.warning(f"Ошибка создания chromagram: {e}")
            # Используем альтернативный метод
            chroma = librosa.feature.chroma(y=y, sr=sr)
            chroma_mean = np.mean(chroma, axis=1)
        
        if len(chroma_mean) != 12:
            raise Exception("Не удалось извлечь chromagram")
        
        # Профили тональностей (Krumhansl-Schmuckler key profiles)
        major_profile = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
        minor_profile = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
        
        keys = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B']
        max_corr = -1
        best_key = 'C'
        is_major = True
        
        # Нормализуем профили
        major_profile = major_profile / (np.sum(major_profile) + 1e-10)
        minor_profile = minor_profile / (np.sum(minor_profile) + 1e-10)
        chroma_norm = chroma_mean / (np.sum(chroma_mean) + 1e-10)
        
        for i in range(12):
            # Мажор - циклический сдвиг профиля
            rotated_major = np.roll(major_profile, i)
            try:
                corr_major = np.corrcoef(chroma_norm, rotated_major)[0, 1]
                if np.isnan(corr_major) or np.isinf(corr_major):
                    corr_major = 0
            except:
                corr_major = 0
            
            # Минор - циклический сдвиг профиля
            rotated_minor = np.roll(minor_profile, i)
            try:
                corr_minor = np.corrcoef(chroma_norm, rotated_minor)[0, 1]
                if np.isnan(corr_minor) or np.isinf(corr_minor):
                    corr_minor = 0
            except:
                corr_minor = 0
            
            if corr_major > max_corr:
                max_corr = corr_major
                best_key = keys[i]
                is_major = True
            
            if corr_minor > max_corr:
                max_corr = corr_minor
                best_key = keys[i]
                is_major = False
        
        key = f"{best_key} {'major' if is_major else 'minor'}"
        
        return key, bpm
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logging.error(f"Ошибка анализа аудио: {e}\n{error_details}")
        raise


# --- Обработчики ---
@dp.message(Command("send_file"))
async def handle_send_file_command(message: Message):
    """Обработчик команды /send_file_{user_id} для отправки файла клиенту."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Парсим user_id из команды: /send_file_8315104804
    command_text = message.text or ""
    if not command_text.startswith("/send_file_"):
        await message.answer("Использование: /send_file_{user_id}")
        return
    
    try:
        user_id = int(command_text.split("_")[-1])
    except (ValueError, IndexError):
        await message.answer("Ошибка: неверный формат команды. Используйте: /send_file_{user_id}")
        return
    
    # Устанавливаем состояние для отправки файла
    dp.admin_sending_file = user_id
    
    await message.answer(
        f"📤 Отправьте файл (mp3, wav или архив), который нужно отправить клиенту (ID: {user_id}):"
    )

@dp.message((F.text.startswith("/broadcast") | F.text.startswith("/рассылка")) & (F.from_user.id == ADMIN_ID))
async def handle_broadcast_command(message: Message):
    """Обработчик команды /broadcast или /рассылка для рассылки сообщений всем пользователям."""
    if message.from_user.id != ADMIN_ID:
        await message.answer("Эта команда доступна только администратору.")
        return
    
    # Проверяем, есть ли текст после команды
    command_parts = message.text.split(maxsplit=1)
    if len(command_parts) < 2:
        await message.answer(
            "📢 <b>Рассылка сообщений</b>\n\n"
            "Использование:\n"
            "/broadcast <текст сообщения>\n"
            "или\n"
            "/рассылка <текст сообщения>\n\n"
            "Пример:\n"
            "/broadcast Привет! Это тестовая рассылка.",
            parse_mode="HTML"
        )
        return
    
    broadcast_text = command_parts[1]
    
    # Получаем всех уникальных пользователей из базы данных
    from database import get_db
    
    try:
        db = await get_db()
        
        # Получаем user_id из таблицы orders
        orders_cursor = await db.execute("SELECT DISTINCT user_id FROM orders")
        orders_users = await orders_cursor.fetchall()
        orders_user_ids = [row[0] for row in orders_users]
        
        # Получаем user_id из таблицы beats_purchases
        purchases_cursor = await db.execute("SELECT DISTINCT user_id FROM beats_purchases")
        purchases_users = await purchases_cursor.fetchall()
        purchases_user_ids = [row[0] for row in purchases_users]
        
        # Получаем user_id активных партнеров
        partners_cursor = await db.execute("SELECT DISTINCT user_id FROM partners WHERE active = 1")
        partners_users = await partners_cursor.fetchall()
        partners_user_ids = [row[0] for row in partners_users]
        
        # Объединяем и убираем дубликаты (включая партнеров)
        all_user_ids = list(set(orders_user_ids + purchases_user_ids + partners_user_ids))
        
        await db.close()
        
        if not all_user_ids:
            await message.answer("❌ В базе данных нет пользователей для рассылки.")
            return
        
        # Отправляем сообщение админу о начале рассылки
        total_users = len(all_user_ids)
        await message.answer(
            f"📢 <b>Начинаю рассылку...</b>\n\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"📝 Текст сообщения:\n{broadcast_text}\n\n"
            f"⏳ Отправка может занять некоторое время...",
            parse_mode="HTML"
        )
        
        # Отправляем сообщения всем пользователям
        successful = 0
        failed = 0
        blocked = 0
        
        for user_id in all_user_ids:
            try:
                await bot.send_message(user_id, broadcast_text)
                successful += 1
                
                # Небольшая задержка, чтобы не превысить лимиты API
                await asyncio.sleep(0.05)  # 50ms между сообщениями
                
            except Exception as e:
                # Более точная проверка блокировок
                error_str = str(e).lower()
                error_type = type(e).__name__
                
                # Проверяем код ошибки, если доступен
                error_code = None
                if hasattr(e, 'code'):
                    error_code = e.code
                elif hasattr(e, 'status_code'):
                    error_code = e.status_code
                
                # Проверяем различные признаки блокировки
                is_blocked = (
                    # Проверка по тексту ошибки
                    "blocked" in error_str or
                    "chat not found" in error_str or
                    "user is deactivated" in error_str or
                    "forbidden" in error_str or
                    "bot was blocked" in error_str or
                    "bot blocked by the user" in error_str or
                    "can't write to user" in error_str or
                    # Проверка по коду ошибки (403 - Forbidden обычно означает блокировку)
                    (error_code == 403 and ("blocked" in error_str or "forbidden" in error_str)) or
                    # Проверка по типу исключения
                    (error_type in ("TelegramBadRequest", "TelegramForbidden") and (
                        "blocked" in error_str or "forbidden" in error_str or "chat not found" in error_str
                    ))
                )
                
                if is_blocked:
                    blocked += 1
                    logging.info(f"Пользователь {user_id} заблокировал бота (ошибка: {error_type}, код: {error_code}, текст: {error_str[:100]})")
                else:
                    failed += 1
                    logging.warning(f"Ошибка отправки сообщения пользователю {user_id}: {error_type}, код: {error_code}, текст: {e}")
        
        # Отправляем итоговый отчет админу
        await message.answer(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📊 Статистика:\n"
            f"✅ Успешно отправлено: {successful}\n"
            f"❌ Ошибки: {failed}\n"
            f"🚫 Заблокировали бота: {blocked}\n"
            f"👥 Всего пользователей: {total_users}",
            parse_mode="HTML"
        )
        
    except Exception as e:
        logging.error(f"Ошибка при рассылке: {e}", exc_info=True)
        await message.answer(f"❌ Ошибка при рассылке: {str(e)}")

@dp.message(Command("start"))
async def start(message: Message):
    await message.answer(
        "Выбери язык / Choose language:",
        reply_markup=lang_keyboard,
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    if lang == "ru":
        help_text = (
            "📖 *Помощь*\n\n"
            "*Архив* — посмотри доступные биты\n"
            "*Цены* — тарифы на лицензии\n"
            "*Купить* — купить готовый бит\n"
            "*Бит на заказ* — заказать уникальный бит\n"
            "*Сведение* — заказать сведение и микс трека\n"
            "*Key & BPM* — определить тональность и темп аудио\n"
            "*Вопросы* — связь с поддержкой\n\n"
            "💡 *Совет:* Используй /cancel чтобы отменить текущее действие"
        )
    else:
        help_text = (
            "📖 *Help*\n\n"
            "*Archive* — browse available beats\n"
            "*Prices* — license pricing\n"
            "*Buy* — purchase a ready beat\n"
            "*Custom beat* — order a unique beat\n"
            "*Mixing* — order mixing and mastering\n"
            "*Key & BPM* — detect key and tempo of audio\n"
            "*Questions* — contact support\n\n"
            "💡 *Tip:* Use /cancel to cancel current action"
        )
    
    await message.answer(help_text, parse_mode="Markdown")


@dp.message(Command("cancel"))
async def cancel_command(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Сбрасываем все состояния пользователя
    dp.purchase_state.pop(user_id, None)
    dp.custom_order_waiting.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.contact_history.pop(user_id, None)  # Очищаем историю разговора при отмене
    dp.current_payment_users.discard(user_id)
    
    if lang == "ru":
        text = "✅ Действие отменено. Выбери раздел:"
        kb = main_keyboard_ru
    else:
        text = "✅ Action cancelled. Choose a section:"
        kb = main_keyboard_en
    
    await message.answer(text, reply_markup=kb)


@dp.callback_query(F.data.in_(["lang_ru", "lang_en"]))
async def set_language(callback):
    user_id = callback.from_user.id
    lang = "ru" if callback.data == "lang_ru" else "en"
    dp.user_language[user_id] = lang
    # Сохраняем язык пользователя в БД для использования в других ботах
    from orders_manager import set_user_language
    await set_user_language(user_id, lang)

    if lang == "ru":
        text = (
            "Wassup!\n\n"
            "Я — elementBot.\n"
            "Выбери нужный раздел в меню"
        )
        kb = main_keyboard_ru
    else:
        text = (
            "Wassup!\n\n"
            "I'm elementBot.\n"
            "Choose the section you need in the menu"
        )
        kb = main_keyboard_en

    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()

@dp.message(F.text == "Архив")
async def catalog(message: Message):
    await message.answer("https://t.me/rrelement")


@dp.message(F.text == "Archive")
async def catalog_en(message: Message):
    await message.answer("https://t.me/rrelement")

@dp.message(F.text == "Цены")
async def prices(message: Message):
    await message.answer(
        "MP3 — ||\\$19||\n"
        "WAV — ||\\$49||\n"
        "TRACK OUT — ||\\$99||\n"
        "EXCLUSIVE — ||\\$299||\n\n"
        "Custom Beat — ||договорная||\n"
        "Сведение — ||договорная||",
        parse_mode="MarkdownV2"
    )


@dp.message(F.text == "Prices")
async def prices_en(message: Message):
    await message.answer(
        "MP3 — ||\\$19||\n"
        "WAV — ||\\$49||\n"
        "TRACK OUT — ||\\$99||\n"
        "EXCLUSIVE — ||\\$299||\n\n"
        "Custom Beat — ||negotiable||\n"
        "Mixing — ||negotiable||",
        parse_mode="MarkdownV2"
    )

@dp.message(F.text == "Key & BPM")
async def key_bpm_finder(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.key_bpm_waiting.add(user_id)
    if lang == "ru":
        text = (
            "*Key & BPM*\n\n"
            "Загрузи MP3 или WAV файл, и я автоматически определю тональность и BPM."
        )
    else:
        text = (
            "*Key & BPM*\n\n"
            "Upload an MP3 or WAV file, and I'll automatically detect the key and BPM."
        )
    await message.answer(text, parse_mode="Markdown")


@dp.message(F.text == "Вопросы")
async def questions(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    faq_text = (
        "<b>Часто задаваемые вопросы</b>\n\n"
        "<b>Как купить бит? </b>\n\n"
        "Нажми «Купить», отправь ссылку или файл бита, выбери лицензию и способ оплаты. После оплаты ты получаешь бит.\n\n"
        "<b>Как заказать бит? </b>\n\n"
        "Нажми «Бит на заказ» - заказ создается автоматически. Исполнитель свяжется с тобой для обсуждения деталей.\n\n"
        "<b>Как заказать сведение? </b>\n\n"
        "Нажми «Сведение» - заказ создается автоматически. Исполнитель свяжется с тобой для обсуждения деталей.\n\n"
        "<b>Кто выполняет заказы? </b>\n\n"
        "Заказы выполняют партнеры сервиса, подключенные к системе.\n\n"
        "<b>Как происходит общение с исполнителем? </b>\n\n"
        "После принятия заказа бот обменивает вас контактами. Далее вы общаетесь напрямую и договариваетесь обо всем сами.\n\n"
        "<b>Как происходит оплата при заказах? </b>\n\n"
        "Оплата происходит напрямую между тобой и исполнителем. Вы сами согласуете цену и способ оплаты. Сервис не удерживает деньги и не вмешивается в процесс.\n\n"
        "<b>Что такое Key & BPM? </b>\n\n"
        "Загрузи MP3 или WAV файл, и бот автоматически определит тональность и темп трека.\n\n"
        "<b>Что такое ИИ-помощник? </b>\n\n"
        "Нажми «AskMe23» - это ИИ-помощник, который отвечает на любые вопросы и помогает разобраться с функциями бота.\n\n"
        "<b>Нужна помощь? </b>\n\n"
        "👉 https://t.me/rrelement1"
    )
    
    await message.answer(faq_text, parse_mode="HTML")


@dp.message(F.text == "Questions")
async def questions_en(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    faq_text = (
        "<b>Frequently Asked Questions</b>\n\n"
        "<b>How to buy a beat? </b>\n\n"
        "Press «Buy», send beat link or file, choose license and payment method. After payment you receive the beat.\n\n"
        "<b>How to order a beat? </b>\n\n"
        "Press «Custom beat» - order is created automatically. A performer will contact you to discuss details.\n\n"
        "<b>How to order mixing? </b>\n\n"
        "Press «Mixing» - order is created automatically. A performer will contact you to discuss details.\n\n"
        "<b>Who completes the orders? </b>\n\n"
        "Orders are completed by service partners connected to the system.\n\n"
        "<b>How does communication with the performer work? </b>\n\n"
        "After the order is accepted, the bot exchanges your contacts. Then you communicate directly and negotiate everything yourself.\n\n"
        "<b>How does payment work for orders? </b>\n\n"
        "Payment happens directly between you and the performer. You agree on price and payment method. The service does not hold money and does not interfere in the process.\n\n"
        "<b>What is Key & BPM? </b>\n\n"
        "Upload an MP3 or WAV file, and the bot will automatically detect the key and tempo of the track.\n\n"
        "<b>What is AI assistant? </b>\n\n"
        "Press «AskMe23» - this is an AI assistant that answers any questions and helps you understand the bot's features.\n\n"
        "<b>Need help? </b>\n\n"
        "👉 https://t.me/rrelement1"
    )
    
    await message.answer(faq_text, parse_mode="HTML")


@dp.message(F.text == "Партнерская программа")
async def partnership_program(message: Message):
    """Обработка раздела 'Партнерская программа'."""
    partnership_text = (
        "<b>Партнёрская программа.</b>\n\n"
        "Вы можете стать партнёром бота и регулярно получать заказы на биты и сведение без поиска клиентов.\n\n"
        "<b>Что даёт партнёрка?</b>\n\n"
        "1. Доступ к реальным заказам на биты и сведение.\n\n"
        "2. Прямое общение с артистом: вы сами согласуете условия.\n\n"
        "3. Возможность получить постоянных клиентов и работать с ними дальше напрямую.\n\n"
        "<b>Как это работает</b>\n\n"
        "• Артист оставляет заявку в боте.\n"
        "• Заявка попадает в закрытый бот партнёров.\n"
        "• Любой партнёр может принять заказ.\n"
        "• Бот отправляет контакты вам и артисту.\n"
        "• Вы договариваетесь и выполняете работу.\n"
        "• Я не вмешиваюсь в процесс.\n\n"
        "<b>Комиссия и выплаты</b>\n\n"
        "• Комиссия сервиса — 10% от суммы оплаченного заказа.\n"
        "• Комиссия взимается только с выполненных и оплаченных работ.\n"
        "• Оплату от клиента вы получаете сами, затем переводите 10% сервису любым удобным способом.\n\n"
        "<b>Важно: полная прозрачность</b>\n\n"
        "• Вступление в партнерскую программу - бесплатно.\n"
        "• Никаких предоплат и скрытых условий.\n"
        "• Комиссия только за результат.\n"
        "• Все условия работы согласовываются заранее между вами и клиентом.\n\n"
        "<b>Как попасть в партнёрскую программу</b>\n\n"
        "Нажмите кнопку «Стать партнёром» — я лично расскажу все детали и отвечу на вопросы."
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Стать партнёром",
                    callback_data="become_partner"
                )
            ]
        ]
    )
    
    await message.answer(partnership_text, reply_markup=keyboard, parse_mode="HTML")


@dp.message(F.text == "Partnership Program")
async def partnership_program_en(message: Message):
    """Обработка раздела 'Partnership Program'."""
    partnership_text = (
        "<b>Partnership Program.</b>\n\n"
        "You can become a bot partner and regularly receive orders for beats and mixing without searching for clients.\n\n"
        "<b>What does the partnership offer?</b>\n\n"
        "1. Access to real orders for beats and mixing.\n\n"
        "2. Direct communication with the artist: you negotiate conditions yourself.\n\n"
        "3. Opportunity to get regular clients and work with them directly in the future.\n\n"
        "<b>How it works</b>\n\n"
        "• Artist submits a request in the bot.\n"
        "• Request goes to a closed bot for partners.\n"
        "• Any partner can accept the order.\n"
        "• Bot sends contacts to you and the artist.\n"
        "• You negotiate and complete the work.\n"
        "• I don't interfere in the process.\n\n"
        "<b>Commission and payments</b>\n\n"
        "• Service commission — 10% of the paid order amount.\n"
        "• Commission is charged only for completed and paid work.\n"
        "• You receive payment from the client yourself, then transfer 10% to the service by any convenient method.\n\n"
        "<b>Important: full transparency</b>\n\n"
        "• Joining the partnership program is free.\n"
        "• No prepayments and hidden conditions.\n"
        "• Commission only for results.\n"
        "• All working conditions are agreed in advance between you and the client.\n\n"
        "<b>How to join the partnership program</b>\n\n"
        "Press the «Become a partner» button — I will personally tell you all the details and answer your questions."
    )
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Become a partner",
                    callback_data="become_partner"
                )
            ]
        ]
    )
    
    await message.answer(partnership_text, reply_markup=keyboard, parse_mode="HTML")


@dp.callback_query(F.data == "become_partner")
async def become_partner_callback(callback: CallbackQuery):
    """Обработка кнопки 'Стать партнёром'."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    if lang == "ru":
        text = (
            "Напиши мне в личку.\n"
            "👉 https://t.me/rrelement1"
        )
    else:
        text = (
            "Write to me\n\n"
            "👉 https://t.me/rrelement1"
        )
    
    await callback.message.answer(text)
    await callback.answer()


@dp.message(F.text == "AskMe23")
async def contact(message: Message):
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.contact_waiting.add(user_id)
    
    # Очищаем историю разговора при входе в раздел (опционально, можно убрать для сохранения контекста)
    # dp.contact_history.pop(user_id, None)
    
    if lang == "ru":
        text = "Салют! Я ИИ-помощник. Задай мне любой вопрос, и я постараюсь помочь."
    else:
        text = "Hi! I'm an AI assistant. Ask me any question and I'll try to help."
    
    await message.answer(text)

@dp.message(F.text == "Купить")
async def buy(message: Message):
    user_id = message.from_user.id
    dp.purchase_state[user_id] = {}  # сбрасываем прошлый выбор
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.contact_waiting.discard(user_id)
    await message.answer(
        "Отправь бит в формате mp3."
    )


@dp.message(F.text == "Buy")
async def buy_en(message: Message):
    user_id = message.from_user.id
    dp.purchase_state[user_id] = {}  # reset previous choice
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.contact_waiting.discard(user_id)
    await message.answer(
        "Send the beat in mp3 format."
    )


@dp.message(F.text == "Бит на заказ")
async def custom_beat(message: Message):
    user_id = message.from_user.id
    # Очищаем состояния других разделов
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.purchase_state.pop(user_id, None)
    
    # Показываем кнопки подтверждения
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Я хочу заказать бит",
                callback_data="confirm_custom_order"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="cancel_custom_order"
            )
        ]
    ])
    
    await message.answer(
        "<b>Бит на заказ</b>\n\n"
        "Нажмите кнопку ниже, чтобы создать заказ на индивидуальный бит. "
        "После подтверждения с вами свяжется исполнитель для уточнения деталей.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@dp.message(F.text == "Custom beat")
async def custom_beat_en(message: Message):
    user_id = message.from_user.id
    # Очищаем состояния других разделов
    dp.mixing_order_waiting.discard(user_id)
    dp.mixing_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.purchase_state.pop(user_id, None)
    
    # Показываем кнопки подтверждения
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ I want to order a beat",
                callback_data="confirm_custom_order"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="cancel_custom_order"
            )
        ]
    ])
    
    await message.answer(
        "<b>Custom Beat</b>\n\n"
        "Click the button below to create an order for a custom beat. "
        "After confirmation, an executor will contact you to clarify the details.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@dp.message(F.text == "Сведение")
async def mixing(message: Message):
    user_id = message.from_user.id
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.purchase_state.pop(user_id, None)
    
    # Показываем кнопки подтверждения
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ Я хочу заказать сведение",
                callback_data="confirm_mixing_order"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Отмена",
                callback_data="cancel_mixing_order"
            )
        ]
    ])
    
    await message.answer(
        "<b>Сведение</b>\n\n"
        "Нажмите кнопку ниже, чтобы создать заказ на сведение. "
        "После подтверждения с вами свяжется исполнитель для уточнения деталей.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@dp.message(F.text == "Mixing")
async def mixing_en(message: Message):
    user_id = message.from_user.id
    # Очищаем состояния других разделов
    dp.custom_order_waiting.discard(user_id)
    dp.custom_order_waiting_price.discard(user_id)
    dp.key_bpm_waiting.discard(user_id)
    dp.offer_waiting_price.discard(user_id)
    dp.contact_waiting.discard(user_id)
    dp.purchase_state.pop(user_id, None)
    
    # Показываем кнопки подтверждения
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="✅ I want to order mixing",
                callback_data="confirm_mixing_order"
            )
        ],
        [
            InlineKeyboardButton(
                text="❌ Cancel",
                callback_data="cancel_mixing_order"
            )
        ]
    ])
    
    await message.answer(
        "<b>Mixing</b>\n\n"
        "Click the button below to create an order for mixing. "
        "After confirmation, an executor will contact you to clarify the details.",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


# --- Лицензии / License type ---
license_inline_ru = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="MP3", callback_data="lic_mp3"),
            InlineKeyboardButton(text="WAV", callback_data="lic_wav"),
        ],
        [
            InlineKeyboardButton(text="TRACK OUT", callback_data="lic_trackout"),
            InlineKeyboardButton(text="EXCLUSIVE", callback_data="lic_excl"),
        ],
    ]
)

license_inline_en = InlineKeyboardMarkup(
    inline_keyboard=[
        [
            InlineKeyboardButton(text="MP3", callback_data="lic_mp3"),
            InlineKeyboardButton(text="WAV", callback_data="lic_wav"),
        ],
        [
            InlineKeyboardButton(text="TRACK OUT", callback_data="lic_trackout"),
            InlineKeyboardButton(text="EXCLUSIVE", callback_data="lic_excl"),
        ],
    ]
)


@dp.callback_query(F.data.startswith("lic_"))
async def license_callback(callback):
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    state = dp.purchase_state.setdefault(user_id, {})

    # Проверяем, что лицензия еще не выбрана (защита от повторных нажатий)
    if "license" in state and state.get("license_selected"):
        if lang == "ru":
            await callback.answer("Лицензия уже выбрана. Используйте кнопки ниже.", show_alert=True)
        else:
            await callback.answer("License already selected. Use the buttons below.", show_alert=True)
        return

    lic_code = callback.data.split("_", maxsplit=1)[1]

    lic_names = {
        "ru": {
            "mp3": "MP3 — $19",
            "wav": "WAV — $49",
            "trackout": "TRACK OUT — $99",
            "excl": "EXCLUSIVE — $299",
        },
        "en": {
            "mp3": "MP3 — $19",
            "wav": "WAV — $49",
            "trackout": "TRACK OUT — $99",
            "excl": "EXCLUSIVE — $299",
        },
    }

    names = lic_names.get(lang, lic_names["ru"])
    lic_text = names.get(lic_code)
    if not lic_text:
        await callback.answer()
        return

    state["license"] = lic_text
    state["license_selected"] = True  # Помечаем, что лицензия выбрана
    
    # Извлекаем тип лицензии и цену для отображения
    license_type, license_price = format_license_and_price(lic_text)
    
    # Показываем сообщение с типом лицензии и ценой, и кнопками "Далее" и "Предложить свою цену"
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    if lang == "ru":
        text = (
            f"Тип лицензии: {license_type}\n"
            f"Цена: {license_price}\n\n"
            "Выберите действие:"
        )
        next_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➡️ Далее", callback_data="continue_payment"),
                    InlineKeyboardButton(text="💵 Моя цена", callback_data="offer_price")
                ],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")],
            ]
        )
    else:
        text = (
            f"License type: {license_type}\n"
            f"Price: {license_price}\n\n"
            "Choose an action:"
        )
        next_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="➡️ Next", callback_data="continue_payment"),
                    InlineKeyboardButton(text="💵 My price", callback_data="offer_price")
                ],
                [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")],
            ]
        )
    
    # Редактируем предыдущее сообщение с выбором лицензии, убирая кнопки и добавляя статус внизу
    try:
        if "license_selection_message_id" in state:
            # Получаем оригинальный текст из state или используем дефолтный
            original_text = state.get("license_selection_message_text", "Теперь выбери тип лицензии:" if lang == "ru" else "Now choose the license type:")
            status_text = "\n\n✅ Тип лицензии выбран" if lang == "ru" else "\n\n✅ License type selected"
            new_text = original_text + status_text
            
            try:
                await callback.message.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["license_selection_message_id"],
                    text=new_text,
                    reply_markup=None
                )
            except Exception as e:
                # Если не удалось изменить текст, пытаемся просто убрать кнопки
                try:
                    await callback.message.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=state["license_selection_message_id"],
                        reply_markup=None
                    )
                except Exception as e2:
                    # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                    error_str = str(e2).lower()
                    if "message is not modified" in error_str:
                        logging.debug(f"Кнопки выбора лицензии уже удалены (message is not modified)")
                    else:
                        logging.error(f"Ошибка при удалении кнопок выбора лицензии: {e2}")
    except Exception as e:
        logging.error(f"Ошибка при обработке license_callback: {e}")
        pass
    
    msg = await callback.message.answer(text, reply_markup=next_kb)
    # Сохраняем ID сообщения и текст, чтобы отслеживать, что лицензия уже выбрана
    state["license_selected_message"] = msg.message_id
    state["action_selection_message_id"] = msg.message_id  # Сохраняем для последующего редактирования
    state["action_selection_message_text"] = text  # Сохраняем оригинальный текст
    dp.purchase_state[user_id] = state
    await callback.answer()

# Обработчик файлов от админа должен быть ПЕРЕД handle_admin_message_priority
@dp.message((F.from_user.id == ADMIN_ID) & (F.audio | F.document | F.voice))
async def handle_admin_file(message: Message):
    """Обработка файлов от админа - отправка файла клиенту."""
    # Проверяем, что админ отправляет файл клиенту
    if dp.admin_sending_file is None:
        return  # Админ не отправляет файл
    
    client_user_id = dp.admin_sending_file
    lang = dp.user_language.get(client_user_id, "ru")
    
    try:
        # Проверяем, это покупка готового бита или заказ
        from orders_manager import get_beats_purchase_by_user_id, get_order_by_user_id
        purchase = await get_beats_purchase_by_user_id(client_user_id)
        
        # Проверяем, это сведение или обычный заказ
        state = dp.purchase_state.get(client_user_id, {})
        is_mixing = state.get("is_mixing", False)
        is_custom = state.get("is_custom", False)
        
        # Проверяем, это заказ (custom_beat или mixing) или покупка готового бита
        order = await get_order_by_user_id(client_user_id, "custom_beat" if is_custom else "mixing" if is_mixing else None) if (is_custom or is_mixing) else None
        
        # Создаем клавиатуру с кнопкой "Связаться" для заказов
        contact_kb = None
        file_sent_text = ""
        
        if order:
            # Для заказов добавляем кнопки "Принять заказ" и "Связаться"
            if lang == "ru":
                contact_text = "Связаться"
                accept_text = "✅ Меня все устраивает"
                file_sent_text = (
                    f"✅ Готовый файл отправлен!\n\n"
                    f"Проверьте файл:\n"
                    f"• Если все устраивает - нажмите 'Меня все устраивает' для оплаты оставшихся 50%\n"
                    f"• Если нужны правки - нажмите 'Связаться'"
                )
            else:
                contact_text = "Contact"
                accept_text = "✅ I'm satisfied"
                file_sent_text = (
                    f"✅ Ready file sent!\n\n"
                    f"Please check the file:\n"
                    f"• If everything is fine - press 'I'm satisfied' to pay the remaining 50%\n"
                    f"• If you need revisions - press 'Contact'"
                )
            
            contact_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text=accept_text, callback_data=f"accept_order_{client_user_id}"),
                        InlineKeyboardButton(text=contact_text, callback_data=f"contact_admin_{client_user_id}")
                    ]
                ]
            )
        elif purchase:
            # Для покупок готовых битов добавляем кнопку "Связаться" и обновляем статус
            if lang == "ru":
                contact_text = "Связаться"
                file_sent_text = ""
            else:
                contact_text = "Contact"
                file_sent_text = ""
            
            contact_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=contact_text, callback_data=f"contact_admin_{client_user_id}")]
                ]
            )
            # Обновляем статус покупки на "completed" после отправки файла
            from orders_manager import update_beats_purchase_status
            await update_beats_purchase_status(purchase["id"], "completed")
            
            # Уведомляем админа в боте покупок
            if purchases_bot:
                try:
                    await purchases_bot.send_message(
                        ADMIN_ID,
                        f"✅ Файл отправлен клиенту для покупки №{purchase['id']}\n"
                        f"Клиент: @{purchase['username']} (ID: {purchase['user_id']})",
                        parse_mode="HTML"
                    )
                except:
                    pass
        else:
            # Обычная покупка без системы заказов
            if lang == "ru":
                contact_text = "Связаться"
                file_sent_text = ""
            else:
                contact_text = "Contact"
                file_sent_text = ""
            
            contact_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=contact_text, callback_data=f"contact_admin_{client_user_id}")]
                ]
            )
        
        # Убеждаемся, что кнопка "Связаться с админом" всегда есть
        if contact_kb is None:
            if lang == "ru":
                contact_text = "Связаться"
                file_sent_text = ""
            else:
                contact_text = "Contact"
                file_sent_text = ""
            
            contact_kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text=contact_text, callback_data=f"contact_admin_{client_user_id}")]
                ]
            )
        
        # Отправляем файл клиенту с кнопкой "Связаться с админом"
        if message.audio:
            await bot.send_audio(chat_id=client_user_id, audio=message.audio.file_id, caption=file_sent_text if file_sent_text else None, reply_markup=contact_kb)
        elif message.document:
            await bot.send_document(chat_id=client_user_id, document=message.document.file_id, caption=file_sent_text if file_sent_text else None, reply_markup=contact_kb)
        elif message.voice:
            await bot.send_voice(chat_id=client_user_id, voice=message.voice.file_id, caption=file_sent_text if file_sent_text else None, reply_markup=contact_kb)
        
        # Если это заказ, обновляем статус - файл отправлен, ожидаем вторую оплату
        if order:
            from orders_manager import update_order_status
            await update_order_status(order["id"], order["type"], "first_payment_received")  # Статус остается, но файл отправлен
        
        # Убираем из ожидания
        dp.admin_sending_file = None
        
        # Подтверждение админу
        if lang == "ru":
            if is_mixing or is_custom:
                await message.answer("✅ Файл отправлен клиенту. Ожидаем вторую оплату (50%).")
            else:
                await message.answer("✅ Файл отправлен клиенту.")
        else:
            if is_mixing or is_custom:
                await message.answer("✅ File sent to client. Waiting for second payment (50%).")
            else:
                await message.answer("✅ File sent to client.")
    except Exception as e:
        logging.error(f"Ошибка отправки файла: {e}")
        lang = dp.user_language.get(client_user_id, "ru")
        error_text = get_error_message(e, "send_file", lang)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(f"❌ Ошибка отправки файла клиенту {client_user_id}: {str(e)}")
        await safe_send_message(bot, client_user_id, error_text, lang)

# Обработчик админа должен быть ПЕРЕД handle_text, чтобы иметь приоритет
@dp.message(F.from_user.id == ADMIN_ID)
async def handle_admin_message_priority(message: Message):
    """Обработка сообщений от админа - отправка реквизитов клиенту (приоритетный обработчик)."""
    # Пропускаем файлы - их обработает handle_admin_file
    if message.audio or message.document or message.voice or message.photo:
        return
    
    # Проверяем, что это текстовое сообщение (не команда)
    if not message.text or message.text.startswith("/"):
        return  # Это не текст или команда - пропускаем другим обработчикам
    
    # Если админ предлагает цену клиенту - обрабатываем здесь
    if len(dp.admin_offering_price) > 0:
        logging.info(f"Админ предлагает цену, обрабатываем здесь. admin_offering_price: {dp.admin_offering_price}")
        # Ищем клиента, которому админ предлагает цену
        client_user_id = None
        for cid in dp.admin_offering_price.keys():
            client_user_id = cid
            break
        
        if client_user_id is not None:
            price_text = message.text.strip()
            if not price_text:
                return
            
            logging.info(f"Обработка предложения цены от админа для клиента {client_user_id}: {price_text}")
            
            # Получаем информацию о заказе из разных источников
            offer = dp.pending_offers.get(client_user_id, {})
            beat = offer.get("beat", "-")
            
            # Если нет в pending_offers, проверяем pending_custom_orders и pending_mixing_orders
            if not offer or beat == "-":
                order = dp.pending_custom_orders.get(client_user_id, {})
                if order:
                    beat = order.get("description", "-")
                else:
                    mixing_order = dp.pending_mixing_orders.get(client_user_id, {})
                    if mixing_order:
                        beat = mixing_order.get("description", "-")
                    else:
                        # Если и там нет, берем из purchase_state
                        state = dp.purchase_state.get(client_user_id, {})
                        beat = state.get("beat", "-")
            
            lang = dp.user_language.get(client_user_id, "ru")
            
            # Проверяем, это сведение или кастом-бит
            is_mixing = False
            if client_user_id in dp.pending_offers:
                offer = dp.pending_offers.get(client_user_id, {})
                is_mixing = offer.get("is_mixing", False)
            else:
                # Если нет в pending_offers, проверяем purchase_state
                state = dp.purchase_state.get(client_user_id, {})
                is_mixing = state.get("is_mixing", False)
                # Также проверяем pending_mixing_orders
                if not is_mixing and client_user_id in dp.pending_mixing_orders:
                    is_mixing = True
            
            # Сохраняем предложение админа
            dp.pending_admin_offers[client_user_id] = {
                "price": price_text,
                "beat": beat,
                "is_mixing": is_mixing,
            }
            
            # Убираем из ожидания
            dp.admin_offering_price.pop(client_user_id, None)
            
            # Отправляем предложение клиенту с кнопками
            if lang == "ru":
                service_name = "Сведение" if is_mixing else "Бит"
                # Форматируем цену - добавляем знак доллара
                price_clean = price_text.replace('$', '').strip()
                price_display = f"${price_clean}" if price_clean else price_text
                
                client_text = (
                    "💵 *Предложение цены*\n\n"
                    f"{service_name}: {beat}\n"
                    f"Предложенная цена: {price_display}\n\n"
                    "Принимаешь эту цену?"
                )
                accept_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Принять", callback_data=f"client_accept_price_{client_user_id}"),
                            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"client_reject_price_{client_user_id}"),
                        ]
                    ]
                )
            else:
                service_name = "Mixing" if is_mixing else "Beat"
                client_text = (
                    "💵 *Price offer*\n\n"
                    f"{service_name}: {beat}\n"
                    f"Offered price: {price_text.replace('$', '').strip()}\n\n"
                    "Do you accept this price?"
                )
                accept_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Accept", callback_data=f"client_accept_price_{client_user_id}"),
                            InlineKeyboardButton(text="❌ Reject", callback_data=f"client_reject_price_{client_user_id}"),
                        ]
                    ]
                )
            
            try:
                await bot.send_message(client_user_id, client_text, reply_markup=accept_kb, parse_mode="Markdown")
                await message.answer(f"✅ Предложение цены отправлено клиенту {client_user_id}")
            except Exception as e:
                logging.error(f"Ошибка отправки предложения цены: {e}")
                await message.answer(f"❌ Ошибка: {str(e)}")
            return  # Важно: возвращаем return, чтобы сообщение не обрабатывалось дальше
    
    # Обработка отправки реквизитов перенесена в бот покупок
    # Эта логика больше не обрабатывается здесь

@dp.message(F.voice)
async def handle_voice(message: Message):
    """Обработка голосовых сообщений в AI чате."""
    user_id = message.from_user.id
    
    # Проверяем, находится ли пользователь в AI чате
    if user_id not in dp.contact_waiting:
        return  # Не обрабатываем голосовые вне AI чата
    
    lang = dp.user_language.get(user_id, "ru")
    
    if lang == "ru":
        status_msg = await message.answer("Распознаю голосовое сообщение...")
    else:
        status_msg = await message.answer("Recognizing voice message...")
    
    tmp_path = None
    try:
        # Скачиваем голосовое сообщение
        file_info = await bot.get_file(message.voice.file_id)
        file_path = file_info.file_path
        
        # Создаем временный файл
        with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as tmp_file:
            tmp_path = tmp_file.name
        
        await bot.download_file(file_path, tmp_path)
        
        # Преобразуем в текст
        transcribed_text = await transcribe_voice(tmp_path, lang)
        
        await status_msg.delete()
        
        # Если распознавание не удалось, отправляем сообщение об ошибке
        if "недоступно" in transcribed_text.lower() or "unavailable" in transcribed_text.lower():
            await message.answer(transcribed_text)
            return
        
        # Отправляем распознанный текст пользователю
        if lang == "ru":
            await message.answer(f"Распознано: {transcribed_text}")
        else:
            await message.answer(f"Recognized: {transcribed_text}")
        
        # Показываем индикатор печати
        await bot.send_chat_action(user_id, "typing")
        
        # Генерируем ответ через AI
        ai_response = await generate_ai_response(transcribed_text, user_id, lang)
        await message.answer(ai_response)
        
    except Exception as e:
        logging.error(f"Ошибка обработки голосового сообщения: {e}")
        try:
            await status_msg.delete()
        except:
            pass
        if lang == "ru":
            await message.answer("Распознавание голоса временно недоступно. Напиши текстом, пожалуйста.")
        else:
            await message.answer("Voice recognition is temporarily unavailable. Please type your message.")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except:
                pass


@dp.message(F.text)
async def handle_text(message: Message):
    """Обработка текстов: кастом-заказы, предложения цены, выбор бита для покупки, генерация музыки."""
    user_id = message.from_user.id

    # Обработка отправки реквизитов перенесена в бот покупок

    # Если админ предлагает цену - это обработает handle_admin_message_priority
    if user_id == ADMIN_ID and len(dp.admin_offering_price) > 0:
        return  # Это обработает handle_admin_message_priority

    # Проверяем, ожидает ли клиент ввод суммы заказа
    # Сначала проверяем локальное состояние, затем синхронизированное из файла
    if user_id not in dp.waiting_client_price:
        # Проверяем файл синхронизации
        try:
            import json
            import os
            sync_file = "waiting_client_price_sync.json"
            if os.path.exists(sync_file):
                with open(sync_file, 'r', encoding='utf-8') as f:
                    sync_data = json.load(f)
                    if str(user_id) in sync_data:
                        sync_info = sync_data[str(user_id)]
                        dp.waiting_client_price[user_id] = (sync_info["order_id"], sync_info["order_type"])
                        # Удаляем из файла после загрузки
                        sync_data.pop(str(user_id), None)
                        with open(sync_file, 'w', encoding='utf-8') as f:
                            json.dump(sync_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка загрузки waiting_client_price из файла: {e}")
    
    if user_id in dp.waiting_client_price:
        order_id, order_type = dp.waiting_client_price[user_id]
        
        # Сохраняем сумму как текст (любой текст разрешен)
        price_text = message.text.strip()
        
        if not price_text:
            await message.answer("❌ Пожалуйста, укажи сумму заказа:")
            return
        
        # Сохраняем сумму клиента через orders_bot
        try:
            from orders_manager import get_order_by_id, update_order_status
            order = await get_order_by_id(order_id, order_type)
            if not order:
                await message.answer("❌ Ошибка: заказ не найден.")
                dp.waiting_client_price.pop(user_id, None)
                return
            
            # Обновляем заказ с суммой клиента (как текст)
            from orders_manager import update_order_status
            await update_order_status(order_id, order_type, order.get("status", "awaiting_price"), client_price=price_text)
            
            # Убираем из ожидания
            dp.waiting_client_price.pop(user_id, None)
            
            # Удаляем из файла синхронизации
            try:
                import json
                import os
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
            
            # Отправляем уведомление админу о сумме от клиента
            try:
                from orders_bot import bot as orders_bot_instance
                from orders_bot import ADMIN_ID as ORDERS_ADMIN_ID
                if orders_bot_instance and ORDERS_ADMIN_ID:
                    order_type_text = "бит на заказ" if order_type == "custom_beat" else "сведение"
                    
                    if order.get("partner_price"):
                        # Обе суммы есть - отмечаем как completed
                        await update_order_status(order_id, order_type, "completed", client_price=price_text)
                        
                        from orders_manager import format_order_number
                        order_display_num = format_order_number(order_id, order_type, order.get('created_at'))
                        admin_text = (
                            f"💰 <b>Суммы заказа {order_type_text} {order_display_num}</b>\n\n"
                            f"👨‍💼 Исполнитель указал: {order['partner_price']}\n"
                            f"👤 Клиент указал: {price_text}\n\n"
                            f"👤 Клиент: @{order['username']} (ID: {order['user_id']})"
                        )
                        if order.get("partner_id"):
                            partner_username = order.get("partner_username", f"user{order['partner_id']}")
                            admin_text += f"\n👨‍💼 Исполнитель: @{partner_username} (ID: {order['partner_id']})"
                        
                        await orders_bot_instance.send_message(ORDERS_ADMIN_ID, admin_text, parse_mode="HTML")
                        
                        await message.answer(
                            f"✅ Сумма сохранена: {price_text}\n\n"
                            "Спасибо за заказ!"
                        )
                    else:
                        # Только клиент указал сумму - отправляем уведомление админу
                        from orders_manager import format_order_number
                        order_display_num = format_order_number(order_id, order_type, order.get('created_at'))
                        admin_text = (
                            f"💰 <b>Клиент указал сумму заказа {order_type_text} {order_display_num}</b>\n\n"
                            f"👤 Клиент указал: {price_text}\n"
                            f"👨‍💼 Исполнитель: ⏳ Ожидает указания суммы\n\n"
                            f"👤 Клиент: @{order['username']} (ID: {order['user_id']})"
                        )
                        if order.get("partner_id"):
                            partner_username = order.get("partner_username", f"user{order['partner_id']}")
                            admin_text += f"\n👨‍💼 Исполнитель: @{partner_username} (ID: {order['partner_id']})"
                        
                        await orders_bot_instance.send_message(ORDERS_ADMIN_ID, admin_text, parse_mode="HTML")
                        
                        await message.answer(
                            f"✅ Сумма сохранена: {price_text}\n\n"
                            "Спасибо! Заказ будет завершен после обработки."
                        )
            except Exception as e:
                logging.error(f"Ошибка отправки суммы админу: {e}")
                await message.answer(
                    f"✅ Сумма сохранена: {price_text}\n\n"
                    "Спасибо! Заказ будет завершен после обработки."
                )
        except Exception as e:
            logging.error(f"Ошибка обработки суммы от клиента: {e}")
            await message.answer("❌ Произошла ошибка. Попробуй еще раз:")
        return
    
    # Проверяем, не хочет ли пользователь связаться с AI
    if user_id in dp.contact_waiting:
        lang = dp.user_language.get(user_id, "ru")
        # Логируем для диагностики
        logging.info(f"AI chat: user_id={user_id}, lang={lang}, message_text={message.text[:50]}")
        text = message.text.strip()
        if not text:
            return
        
        # Ограничиваем длину сообщения (максимум 4000 символов), чтобы избежать ошибок
        if len(text) > 4000:
            if lang == "ru":
                await message.answer("Сообщение слишком длинное. Пожалуйста, сократите его до 4000 символов или разбейте на несколько сообщений.")
            else:
                await message.answer("Message is too long. Please shorten it to 4000 characters or split into multiple messages.")
            return

        # Показываем индикатор печати
        await bot.send_chat_action(user_id, "typing")
        
        try:
            # Генерируем ответ через AI
            ai_response = await generate_ai_response(text, user_id, lang)
            logging.info(f"AI response generated for user_id={user_id}, lang={lang}, response_length={len(ai_response)}")
            await message.answer(ai_response)
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
            logging.error(f"Ошибка в обработчике AI чата для пользователя {user_id}: {error_type}: {error_msg}")
            logging.error(f"Полная информация: {repr(e)}", exc_info=True)
            
            # Если ошибка уже обработана в generate_ai_response, просто отправляем ответ
            # Иначе отправляем общее сообщение об ошибке
            if lang == "ru":
                error_text = f"❌ Произошла ошибка: {error_type}. Попробуйте еще раз или свяжитесь с админом: https://t.me/rrelement1"
            else:
                error_text = f"❌ An error occurred: {error_type}. Try again or contact admin: https://t.me/rrelement1"
            
            await safe_send_message(bot, user_id, error_text, lang)
        return

    # Сначала проверяем, не ждём ли мы кастом-заказ
    if user_id in dp.custom_order_waiting:
        lang = dp.user_language.get(user_id, "ru")
        text = message.text.strip()
        if not text:
            return

        username = message.from_user.username or "no_username"

        # Сохраняем заказ для ответа админа
        dp.pending_custom_orders[user_id] = {
            "description": text,
            "file_id": None,
        }

        # Сообщение админу с кнопками
        admin_text = (
            "Новый заказ бита на заказ:\n"
            f"Пользователь: @{username} (id={user_id})\n\n"
            f"Описание:\n{text}"
        )
        
        # Кнопки для админа
        custom_order_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять заказ", callback_data=f"custom_accept_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить заказ", callback_data=f"custom_reject_{user_id}"),
                ]
            ]
        )
        
        # Отправляем заказ в бот заказов через функцию send_order_to_bot
        if orders_bot:
            try:
                # Создаем заказ если его еще нет
                from orders_manager import get_order_by_user_id, create_custom_order
                order = await get_order_by_user_id(user_id, "custom_beat")
                if not order:
                    order = await create_custom_order(user_id, username, text, None)
                    logging.info(f"Создан новый заказ на бит на заказ №{order['id']}")
                
                # Используем функцию send_order_to_bot из orders_bot
                try:
                    from orders_bot import send_order_to_bot
                    logging.info(f"Отправка заказа {order['id']} в бот заказов через send_order_to_bot (orders_bot={orders_bot}, ADMIN_ID={ADMIN_ID})")
                    await send_order_to_bot(order, None, orders_bot, ADMIN_ID)
                    logging.info(f"Заказ {order['id']} успешно отправлен в бот заказов")
                except ImportError as import_error:
                    logging.error(f"Ошибка импорта send_order_to_bot: {import_error}")
                    # Если не можем импортировать, отправляем напрямую
                    await orders_bot.send_message(ADMIN_ID, admin_text, reply_markup=custom_order_kb)
                except Exception as send_error:
                    logging.error(f"Ошибка вызова send_order_to_bot: {send_error}")
                    # Fallback: отправляем напрямую
                    try:
                        await orders_bot.send_message(ADMIN_ID, admin_text, reply_markup=custom_order_kb)
                    except Exception as fallback_error:
                        logging.error(f"Ошибка fallback отправки заказа: {fallback_error}")
            except Exception as e:
                logging.error(f"Ошибка отправки заказа в бот заказов: {e}")
        # Не отправляем в основной бот - он только для клиентов

        # Ответ пользователю
        reply = (
            "Спасибо! Я отправил твой заказ. Ответ придет в ближайшее время."
            if lang == "ru"
            else "Thanks! I've sent your order. You'll get a response shortly."
        )
        
        # Кнопка отмены для клиента
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отменить заказ" if lang == "ru" else "❌ Cancel order",
                        callback_data=f"client_cancel_custom_{user_id}"
                    )
                ]
            ]
        )
        await message.answer(reply, reply_markup=cancel_kb)

        dp.custom_order_waiting.discard(user_id)
        return

    # Проверяем, не ждём ли мы заказ на сведение
    if user_id in dp.mixing_order_waiting:
        lang = dp.user_language.get(user_id, "ru")
        text = message.text.strip()
        if not text:
            if lang == "ru":
                await message.answer("Пожалуйста, опиши, что нужно сделать.")
            else:
                await message.answer("Please describe what needs to be done.")
            return

        username = message.from_user.username or "no_username"
        
        # Создаем заказ только с текстовым описанием (без файла)
        from orders_manager import create_mixing_order
        order = await create_mixing_order(user_id, username, text, None)
        
        # Сохраняем в старую систему для обратной совместимости
        dp.pending_mixing_orders[user_id] = {
            "description": text,
            "file_id": None,
            "order_id": order["id"],
        }
        
        # Отправляем заказ в бот заказов через функцию send_order_to_bot
        if orders_bot:
            try:
                from orders_bot import send_order_to_bot
                logging.info(f"Отправка заказа на сведение №{order['id']} в бот заказов (orders_bot={orders_bot}, ADMIN_ID={ADMIN_ID})")
                await send_order_to_bot(order, None, orders_bot, ADMIN_ID)
                logging.info(f"Заказ на сведение №{order['id']} успешно отправлен в бот заказов")
            except Exception as e:
                logging.error(f"Ошибка отправки заказа в бот заказов: {e}")
        
        reply = (
            f"Спасибо! Я принял твой заказ. Номер заказа: {order['id']}. Ответ придет в ближайшее время."
            if lang == "ru"
            else f"Thanks! I've accepted your order. Order number: {order['id']}. You'll get a response shortly."
        )
        
        # Кнопка отмены для клиента
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отменить заказ" if lang == "ru" else "❌ Cancel order",
                        callback_data=f"client_cancel_mixing_{user_id}"
                    )
                ]
            ]
        )
        await message.answer(reply, reply_markup=cancel_kb)

        dp.mixing_order_waiting.discard(user_id)
        return

    # Далее — если пользователь делает предложение по цене
    if user_id in dp.offer_waiting_price:
        lang = dp.user_language.get(user_id, "ru")
        price_text = message.text.strip()
        if not price_text:
            return

        state = dp.purchase_state.get(user_id, {})
        beat = state.get("beat", "-")
        lic = state.get("license", "-")
        username = message.from_user.username or "no_username"

        # Сохраняем предложение для ответа
        dp.pending_offers[user_id] = {
            "beat": beat,
            "license": lic,
            "price": price_text,
        }

        # Форматируем цену - добавляем знак доллара, если его нет
        price_display = price_text.strip()
        if price_display and not price_display.startswith("$"):
            # Убираем $ если есть, затем добавляем обратно
            price_clean = price_display.replace('$', '').strip()
            if price_clean:
                price_display = f"${price_clean}"
        
        admin_text = (
            "Новое предложение по цене:\n"
            f"Пользователь: @{username} (id={user_id})\n"
            f"Beat: {beat}\n"
            f"License: {lic}\n"
            f"Предложенная цена: {price_display}"
        )
        
        # Кнопки для админа
        offer_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять", callback_data=f"offer_accept_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить", callback_data=f"offer_reject_{user_id}"),
                ]
            ]
        )
        
        # Отправляем предложение цены в бот покупок (это покупка готового бита, не заказ)
        if purchases_bot:
            try:
                await purchases_bot.send_message(ADMIN_ID, admin_text, reply_markup=offer_kb)
            except Exception as e:
                logging.error(f"Ошибка отправки предложения в бот покупок: {e}")
        # Не отправляем в основной бот - он только для клиентов

        # Удаляем кнопку "Отмена" из сообщения "Напиши желаемую цену..."
        state = dp.purchase_state.get(user_id, {})
        try:
            if "price_request_message_id" in state:
                try:
                    completed_text = "✅ Цена отправлена" if lang == "ru" else "✅ Price sent"
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["price_request_message_id"],
                        text=completed_text,
                        reply_markup=None
                    )
                except Exception as e:
                    # Если не удалось изменить текст, пытаемся просто убрать кнопки
                    try:
                        await bot.edit_message_reply_markup(
                            chat_id=user_id,
                            message_id=state["price_request_message_id"],
                            reply_markup=None
                        )
                    except Exception as e2:
                        logging.error(f"Ошибка при удалении кнопки 'Отмена' из сообщения с запросом цены: {e2}")
        except Exception as e:
            logging.error(f"Ошибка при обработке удаления кнопки 'Отмена': {e}")
            pass

        reply = (
            "Спасибо! Я отправил твоё предложение. Ответ придет в ближайшее время."
            if lang == "ru"
            else "Thanks! I've sent your offer. You'll get a response shortly."
        )
        await message.answer(reply)

        dp.offer_waiting_price.discard(user_id)
        return

    # Если пользователь отправляет цену для кастом-заказа
    if user_id in dp.custom_order_waiting_price:
        lang = dp.user_language.get(user_id, "ru")
        price_text = message.text.strip()
        if not price_text:
            return

        state = dp.purchase_state.get(user_id, {})
        beat = state.get("beat", "-")
        username = message.from_user.username or "no_username"

        # Сохраняем предложение цены для ответа админа
        dp.pending_offers[user_id] = {
            "beat": beat,
            "license": "Custom Beat — договорная",
            "price": price_text,
            "is_custom": True,  # Флаг, что это кастом-заказ
        }

        # Форматируем цену - добавляем знак доллара, если его нет
        price_display = price_text.strip()
        if price_display and not price_display.startswith("$"):
            # Убираем $ если есть, затем добавляем обратно
            price_clean = price_display.replace('$', '').strip()
            if price_clean:
                price_display = f"${price_clean}"
        
        admin_text = (
            "Предложение цены для кастом-заказа:\n"
            f"Пользователь: @{username} (id={user_id})\n"
            f"Описание: {beat}\n"
            f"Предложенная цена: {price_display}"
        )
        
        # Кнопки для админа
        offer_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять цену", callback_data=f"custom_price_accept_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить цену", callback_data=f"custom_price_reject_{user_id}"),
                ],
                [
                    InlineKeyboardButton(text="💵 Предложить цену", callback_data=f"admin_offer_price_{user_id}"),
                ]
            ]
        )
        
        # Отправляем предложение цены в бот заказов
        if orders_bot:
            try:
                await orders_bot.send_message(ADMIN_ID, admin_text, reply_markup=offer_kb)
            except Exception as e:
                logging.error(f"Ошибка отправки предложения в бот заказов: {e}")
        # Не отправляем в основной бот - он только для клиентов

        reply = (
            "Спасибо! Я отправил твоё предложение по цене. Ответ придет в ближайшее время."
            if lang == "ru"
            else "Thanks! I've sent your price offer. You'll get a response shortly."
        )
        await message.answer(reply)

        dp.custom_order_waiting_price.discard(user_id)
        return

    # Если пользователь отправляет цену для сведения
    if user_id in dp.mixing_order_waiting_price:
        lang = dp.user_language.get(user_id, "ru")
        price_text = message.text.strip()
        if not price_text:
            return

        state = dp.purchase_state.get(user_id, {})
        beat = state.get("beat", "-")
        username = message.from_user.username or "no_username"

        # Сохраняем предложение цены для ответа админа
        dp.pending_offers[user_id] = {
            "beat": beat,
            "license": "Сведение — договорная",
            "price": price_text,
            "is_mixing": True,  # Флаг, что это сведение
        }

        # Форматируем описание для админа
        parts = beat.split("\nОписание: ", 1)
        archive_name = parts[0]
        description = parts[1] if len(parts) > 1 else None
        
        admin_text = (
            "Предложение цены для сведения:\n"
            f"Пользователь: @{username} (id={user_id})\n"
            f"Архив: {archive_name}"
        )
        if description:
            admin_text += f"\nОписание: {description}"
        
        # Форматируем цену - добавляем знак доллара, если его нет
        price_display = price_text.strip()
        if price_display and not price_display.startswith("$"):
            # Убираем $ если есть, затем добавляем обратно
            price_clean = price_display.replace('$', '').strip()
            if price_clean:
                price_display = f"${price_clean}"
        
        admin_text += f"\nПредложенная цена: {price_display}"
        
        # Кнопки для админа
        offer_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Принять цену", callback_data=f"mixing_price_accept_{user_id}"),
                    InlineKeyboardButton(text="❌ Отклонить цену", callback_data=f"mixing_price_reject_{user_id}"),
                ],
                [
                    InlineKeyboardButton(text="💵 Предложить цену", callback_data=f"admin_offer_mixing_price_{user_id}"),
                ]
            ]
        )
        
        # Отправляем предложение цены в бот заказов
        if orders_bot:
            try:
                await orders_bot.send_message(ADMIN_ID, admin_text, reply_markup=offer_kb)
            except Exception as e:
                logging.error(f"Ошибка отправки предложения в бот заказов: {e}")
        # Не отправляем в основной бот - он только для клиентов

        reply = (
            "Спасибо! Я отправил твоё предложение по цене. Ответ придет в ближайшее время."
            if lang == "ru"
            else "Thanks! I've sent your price offer. You'll get a response shortly."
        )
        await message.answer(reply)

        dp.mixing_order_waiting_price.discard(user_id)
        return

    # если пользователь сейчас в процессе покупки и ещё не выбрал бит
    state = dp.purchase_state.get(user_id)
    if state is not None and "beat" not in state:
        beat = message.text.strip()
        if not beat:
            return

        # Проверяем, что это ссылка (начинается с http:// или https://)
        if not (beat.startswith("http://") or beat.startswith("https://")):
            lang = dp.user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer(
                    "❌ Пожалуйста, отправь ссылку на бит (начинается с http:// или https://) или MP3 файл.\n\n"
                    "В разделе «Купить» принимаются только ссылки или MP3 файлы."
                )
            else:
                await message.answer(
                    "❌ Please send a link to the beat (starting with http:// or https://) or an MP3 file.\n\n"
                    "In the «Buy» section, only links or MP3 files are accepted."
                )
            return

        state["beat"] = beat
        lang = dp.user_language.get(user_id, "ru")

        if lang == "ru":
            text = (
                f"Окей, бит:\n{beat}\n\n"
                "Теперь выбери тип лицензии:"
            )
            kb = license_inline_ru
        else:
            text = (
                f"Okay, beat:\n{beat}\n\n"
                "Now choose the license type:"
            )
            kb = license_inline_en

        msg = await message.answer(text, reply_markup=kb)
        # Сохраняем message_id и текст сообщения с выбором лицензии
        # НЕ перезаписываем state, используем уже существующий
        if user_id not in dp.purchase_state:
            dp.purchase_state[user_id] = {}
        state = dp.purchase_state[user_id]
        state["license_selection_message_id"] = msg.message_id
        state["license_selection_message_text"] = text  # Сохраняем оригинальный текст
        state["beat"] = beat  # Сохраняем бит в state (уже сохранен выше, но на всякий случай)
        dp.purchase_state[user_id] = state
        return
    
    # Если ни одно из условий выше не сработало, просто игнорируем сообщение
    # AI работает только в разделе AskMe23


@dp.message(F.audio | F.document)
async def handle_audio_beat(message: Message):
    """Принимаем mp3/файл как выбор бита, реф для бита на заказ, или для анализа Key & BPM."""
    user_id = message.from_user.id
    lang = dp.user_language.get(user_id, "ru")

    # Если пользователь хочет связаться с AI
    if user_id in dp.contact_waiting:
        lang = dp.user_language.get(user_id, "ru")
        
        # Проверяем, это аудиофайл для анализа и рекомендаций
        if message.audio or message.document:
            file_id = None
            file_name = None
            
            if message.audio:
                file_id = message.audio.file_id
                file_name = message.audio.file_name or "audio.mp3"
            elif message.document:
                file_name = message.document.file_name or "file"
                if not file_name.lower().endswith(('.mp3', '.wav', '.m4a', '.flac', '.ogg')):
                    if lang == "ru":
                        await message.answer("Отправь аудиофайл (MP3 или WAV) для анализа и рекомендаций.")
                    else:
                        await message.answer("Send an audio file (MP3 or WAV) for analysis and recommendations.")
                    return
                file_id = message.document.file_id
            
            # Анализируем аудио и даем рекомендации
            if lang == "ru":
                status_msg = await message.answer("Анализирую аудиофайл и подбираю рекомендации...")
            else:
                status_msg = await message.answer("Analyzing audio file and finding recommendations...")
            
            tmp_path = None
            try:
                # Скачиваем файл
                file_info = await bot.get_file(file_id)
                file_path = file_info.file_path
                file_ext = os.path.splitext(file_name)[1] or '.mp3'
                with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
                    tmp_path = tmp_file.name
                
                await bot.download_file(file_path, tmp_path)
                
                # Анализируем аудио
                analysis = await analyze_audio_mood_genre(tmp_path, lang)
                
                # Получаем рекомендации (всегда направляем в архив)
                recommendations = await recommend_beats(analysis, lang)
                
                # Формируем ответ
                if lang == "ru":
                    result_text = (
                        f"Анализ аудиофайла:\n"
                        f"Настроение: {analysis.get('mood', 'неизвестно')}\n"
                        f"Стиль: {analysis.get('style', 'неизвестно')}\n"
                        f"Темп: {analysis.get('tempo', 0):.1f} BPM\n"
                        f"Тональность: {analysis.get('key', 'неизвестно')}\n\n"
                        f"{recommendations}"
                    )
                else:
                    result_text = (
                        f"Audio analysis:\n"
                        f"Mood: {analysis.get('mood', 'unknown')}\n"
                        f"Style: {analysis.get('style', 'unknown')}\n"
                        f"Tempo: {analysis.get('tempo', 0):.1f} BPM\n"
                        f"Key: {analysis.get('key', 'unknown')}\n\n"
                        f"{recommendations}"
                    )
                
                await status_msg.delete()
                await message.answer(result_text)
                
            except Exception as e:
                logging.error(f"Ошибка анализа аудио в AI чате: {e}")
                try:
                    await status_msg.delete()
                except:
                    pass
                if lang == "ru":
                    await message.answer("Не удалось проанализировать аудиофайл. Попробуйте еще раз.")
                else:
                    await message.answer("Failed to analyze audio file. Please try again.")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except:
                        pass
            return
        
        # AI чат работает только с текстом
        if lang == "ru":
            reply = "Извини, я пока могу отвечать только на текстовые сообщения. Опиши свой вопрос словами, и я постараюсь помочь! 💬"
        else:
            reply = "Sorry, I can only respond to text messages for now. Describe your question in words and I'll try to help! 💬"
        
        await message.answer(reply)
        return

    # Если пользователь использует Key & BPM
    if user_id in dp.key_bpm_waiting:
        # Проверяем, что это аудиофайл
        file_id = None
        file_name = None
        
        if message.audio:
            file_id = message.audio.file_id
            file_name = message.audio.file_name or "audio.mp3"
        elif message.document:
            file_name = message.document.file_name or "file"
            # Проверяем расширение файла
            if not file_name.lower().endswith(('.mp3', '.wav', '.m4a', '.flac', '.ogg')):
                if lang == "ru":
                    await message.answer("❌ Пожалуйста, отправь аудиофайл в формате MP3 или WAV.")
                else:
                    await message.answer("❌ Please send an audio file in MP3 or WAV format.")
                return
            file_id = message.document.file_id
        else:
            return
        
        # Убираем из ожидания
        dp.key_bpm_waiting.discard(user_id)
        
        # Отправляем сообщение о начале анализа
        if lang == "ru":
            status_msg = await message.answer("🔍 Анализирую аудиофайл... Это может занять несколько секунд.")
        else:
            status_msg = await message.answer("🔍 Analyzing audio file... This may take a few seconds.")
        
        tmp_path = None
        try:
            # Скачиваем файл
            file_info = await bot.get_file(file_id)
            file_path = file_info.file_path
            
            # Создаем временный файл с правильным расширением
            file_ext = os.path.splitext(file_name)[1] or '.mp3'
            with tempfile.NamedTemporaryFile(delete=False, suffix=file_ext) as tmp_file:
                tmp_path = tmp_file.name
            
            # Скачиваем файл
            await bot.download_file(file_path, tmp_path)
            
            # Проверяем, что файл скачался
            if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
                raise Exception("Файл не был скачан или пуст")
            
            # Анализируем аудио
            key, bpm = await analyze_audio_key_bpm(tmp_path)
            
            # Формируем результат
            if lang == "ru":
                result_text = (
                    f"✅ *Анализ завершен*\n\n"
                    f"🎹 *Тональность:* `{key}`\n"
                    f"⚡ *BPM:* `{bpm}`\n\n"
                    f"Файл: `{file_name}`"
                )
            else:
                result_text = (
                    f"✅ *Analysis complete*\n\n"
                    f"🎹 *Key:* `{key}`\n"
                    f"⚡ *BPM:* `{bpm}`\n\n"
                    f"File: `{file_name}`"
                )
            
            await status_msg.delete()
            await message.answer(result_text, parse_mode="Markdown")
            
        except Exception as e:
            import traceback
            error_details = traceback.format_exc()
            logging.error(f"Ошибка анализа Key & BPM: {e}\n{error_details}")
            try:
                await status_msg.delete()
            except:
                pass
            
            # Используем улучшенное сообщение об ошибке
            error_text = get_error_message(e, "key_bpm_analysis", lang)
            await message.answer(error_text, parse_mode="Markdown")
        finally:
            # Удаляем временный файл, если он был создан
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except:
                    pass
        return

    # Если это кастом-заказ — создаем заказ через orders_manager и отправляем в бот заказов
    if user_id in dp.custom_order_waiting:
        # Принимаем только текстовые сообщения, файлы не нужны
        if not message.text:
            lang = dp.user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer("Пожалуйста, опиши, какой бит тебе нужен. Референс можно будет отправить битмейкеру после принятия заказа.")
            else:
                await message.answer("Please describe what kind of beat you need. You can send the reference to the beatmaker after your order is accepted.")
            return
        
        username = message.from_user.username or "no_username"
        description = message.text.strip()
        
        if not description:
            lang = dp.user_language.get(user_id, "ru")
            if lang == "ru":
                await message.answer("Пожалуйста, опиши, какой бит тебе нужен.")
            else:
                await message.answer("Please describe what kind of beat you need.")
            return
        
        # Создаем заказ только с текстовым описанием (без файла)
        from orders_manager import create_custom_order
        order = await create_custom_order(user_id, username, description, None)
        
        # Сохраняем в старую систему для обратной совместимости
        dp.pending_custom_orders[user_id] = {
            "description": description,
            "file_id": None,
            "order_id": order["id"],  # Сохраняем ID заказа
        }
        
        # Отправляем заказ в бот заказов через функцию send_order_to_bot
        if orders_bot:
            try:
                from orders_bot import send_order_to_bot
                logging.info(f"Отправка заказа на бит на заказ №{order['id']} в бот заказов (orders_bot={orders_bot}, ADMIN_ID={ADMIN_ID})")
                await send_order_to_bot(order, None, orders_bot, ADMIN_ID)
                logging.info(f"Заказ на бит на заказ №{order['id']} успешно отправлен в бот заказов")
            except Exception as e:
                logging.error(f"Ошибка отправки заказа в бот заказов: {e}")

        reply = (
            f"Спасибо! Я принял твой заказ. Номер заказа: {order['id']}. Ответ придет в ближайшее время."
            if lang == "ru"
            else f"Thanks! I've accepted your order. Order number: {order['id']}. You'll get a response shortly."
        )
        
        # Кнопка отмены для клиента
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="❌ Отменить заказ" if lang == "ru" else "❌ Cancel order",
                        callback_data=f"client_cancel_custom_{user_id}"
                    )
                ]
            ]
        )
        await message.answer(reply, reply_markup=cancel_kb)

        dp.custom_order_waiting.discard(user_id)
        return


    # Иначе — это выбор бита для покупки
    # Проверяем, что пользователь начал процесс покупки (нажал "Купить")
    if user_id not in dp.purchase_state:
        return  # Пользователь не начал процесс покупки
    
    state = dp.purchase_state[user_id]
    if "beat" in state:
        return  # Бит уже выбран

    # Проверяем формат файла - принимаем только MP3
    if message.audio:
        # Аудиофайлы обычно MP3, принимаем
        state["beat"] = message.audio.file_name or "audio file"
        state["beat_audio_id"] = message.audio.file_id
    elif message.document:
        # Проверяем расширение файла - только MP3
        file_name = message.document.file_name or ""
        if not file_name.lower().endswith('.mp3'):
            if lang == "ru":
                await message.answer(
                    "❌ Пожалуйста, отправь файл в формате MP3 или ссылку на бит.\n\n"
                    "Другие форматы не принимаются в разделе «Купить»."
                )
            else:
                await message.answer(
                    "❌ Please send a file in MP3 format or a link to the beat.\n\n"
                    "Other formats are not accepted in the «Buy» section."
                )
            return
        state["beat"] = file_name
        state["beat_document_id"] = message.document.file_id
    else:
        return

    beat_display = state["beat"]

    if lang == "ru":
        text = (
            f"Окей, бит (файл):\n{beat_display}\n\n"
                "Теперь выбери тип лицензии:"
        )
        kb = license_inline_ru
    else:
        text = (
            f"Okay, beat (file):\n{beat_display}\n\n"
            "Now choose the license type:"
        )
        kb = license_inline_en

    msg = await message.answer(text, reply_markup=kb)
    # Сохраняем message_id и текст сообщения с выбором лицензии
    user_id = message.from_user.id
    if user_id not in dp.purchase_state:
        dp.purchase_state[user_id] = {}
    state = dp.purchase_state[user_id]
    state["license_selection_message_id"] = msg.message_id
    state["license_selection_message_text"] = text  # Сохраняем оригинальный текст
    dp.purchase_state[user_id] = state

# --- Inline callbacks оплаты ---
@dp.callback_query(F.data.startswith("pay_"))
async def payment_callback(callback):
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    method = callback.data.split("_", maxsplit=1)[1]

    # Проверяем, что способ оплаты еще не обработан (защита от повторных нажатий)
    state = dp.purchase_state.get(user_id)
    logging.info(f"payment_callback: Получен state для пользователя {user_id}, payment_selection_message_id={state.get('payment_selection_message_id') if state else None}")
    if state and state.get("payment_method_processed"):
        if lang == "ru":
            await callback.answer("Способ оплаты уже выбран. Проверьте предыдущие сообщения.", show_alert=True)
        else:
            await callback.answer("Payment method already selected. Check previous messages.", show_alert=True)
        return
    
    # Проверяем, что пользователь прошёл шаги: выбрал бит и лицензию
    if not state or "beat" not in state:
        if lang == "ru":
            await callback.message.answer(
                "Сначала пришли ссылку на бит, который хочешь купить (из каталога)."
            )
        else:
            await callback.message.answer(
                "First send the link to the beat you want to buy (from the catalog)."
            )
        await callback.answer()
        return

    if "license" not in state:
        # есть бит, но не выбрана лицензия — возвращаем к выбору лицензии
        if lang == "ru":
            text = "Сначала выбери тип лицензии:"
            kb = license_inline_ru
        else:
            text = "First choose the license type:"
            kb = license_inline_en

        msg = await callback.message.answer(text, reply_markup=kb)
        # Сохраняем message_id и текст сообщения с выбором лицензии
        if user_id not in dp.purchase_state:
            dp.purchase_state[user_id] = {}
        state = dp.purchase_state[user_id]
        state["license_selection_message_id"] = msg.message_id
        state["license_selection_message_text"] = text  # Сохраняем оригинальный текст
        dp.purchase_state[user_id] = state
        await callback.answer()
        return

    # Проверяем, есть ли принятая цена из бота покупок
    import json
    import os
    price_update_file = "accepted_price.json"
    accepted_price = None
    if os.path.exists(price_update_file):
        try:
            with open(price_update_file, "r", encoding="utf-8") as f:
                updates = json.load(f)
            if str(user_id) in updates:
                update_data = updates[str(user_id)]
                accepted_price = update_data.get('price', None)
                original_license = update_data.get('license', None)
                # Обновляем purchase_state с принятой ценой
                if accepted_price:
                    # Если есть исходная лицензия (например, "TRACK OUT — $99"), сохраняем тип лицензии
                    if original_license:
                        # Извлекаем тип лицензии (MP3, WAV, TRACK OUT, EXCLUSIVE)
                        license_type = original_license.split(" — ")[0] if " — " in original_license else None
                        if license_type:
                            # Формируем новую лицензию с принятой ценой: "TRACK OUT — $60"
                            state["license"] = f"{license_type} — {accepted_price}"
                        else:
                            # Если не удалось извлечь тип, используем просто цену
                            state["license"] = accepted_price
                    else:
                        # Если исходной лицензии нет, используем просто цену
                        state["license"] = accepted_price
                    state["beat"] = update_data.get("beat", state.get("beat", "-"))
                    
                    # Сохраняем payment_selection_message_id и текст, если они есть (из бота покупок)
                    payment_msg_id = update_data.get("payment_selection_message_id")
                    payment_msg_text = update_data.get("payment_selection_message_text")
                    if payment_msg_id:
                        state["payment_selection_message_id"] = payment_msg_id
                        logging.info(f"payment_callback: Восстановлен payment_selection_message_id={payment_msg_id} из accepted_price.json")
                    if payment_msg_text:
                        state["payment_selection_message_text"] = payment_msg_text
                        logging.info(f"payment_callback: Восстановлен payment_selection_message_text из accepted_price.json")
                    
                    dp.purchase_state[user_id] = state
                    # Удаляем обновление после использования
                    del updates[str(user_id)]
                    with open(price_update_file, "w", encoding="utf-8") as f:
                        json.dump(updates, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка чтения принятой цены: {e}")
    
    # резюме перед оплатой — здесь уже точно есть и бит, и лицензия
    beat = state["beat"]
    lic = state["license"]
    is_custom = state.get("is_custom", False)
    is_mixing = state.get("is_mixing", False)

    # Форматируем лицензию и цену
    license_text, price_text = format_license_and_price(lic)
    
    if lang == "ru":
        if is_custom:
            summary = f"Услуга: Custom Beat\nОписание: {beat}\nЦена: {price_text if price_text else lic}"
        elif is_mixing:
            summary = f"Услуга: Сведение\nАрхив: {beat}\nЦена: {price_text if price_text else lic}"
        else:
            summary = f"Бит: {beat}\nЛицензия: {license_text}\nЦена: {price_text if price_text else lic}"
    else:
        if is_custom:
            summary = f"Service: Custom Beat\nDescription: {beat}\nPrice: {price_text if price_text else lic}"
        elif is_mixing:
            summary = f"Service: Mixing\nArchive: {beat}\nPrice: {price_text if price_text else lic}"
        else:
            summary = f"Beat: {beat}\nLicense: {license_text}\nPrice: {price_text if price_text else lic}"

    # Убрано промежуточное сообщение с дублирующейся информацией
    # await callback.message.answer(summary)

    # Редактируем предыдущее сообщение с выбором способа оплаты, убирая кнопки и добавляя статус внизу
    try:
        if state and "payment_selection_message_id" in state:
            message_id = state["payment_selection_message_id"]
            logging.info(f"payment_callback: Редактирование сообщения {message_id} для пользователя {user_id}, state keys: {list(state.keys())}")
            
            # Получаем оригинальный текст из state или используем дефолтный
            original_text = state.get("payment_selection_message_text", "Выберите способ оплаты:" if lang == "ru" else "Choose the payment method:")
            status_text = "\n\n✅ Способ оплаты выбран" if lang == "ru" else "\n\n✅ Payment method selected"
            new_text = original_text + status_text
            
            # Сначала пытаемся удалить кнопки (это более надежно)
            try:
                try:
                    await callback.message.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=message_id,
                        reply_markup=None
                    )
                    logging.info(f"payment_callback: Кнопки из сообщения {message_id} успешно удалены через callback.message.bot")
                except Exception as e1:
                    logging.warning(f"payment_callback: Не удалось удалить кнопки через callback.message.bot: {e1}, пробуем глобальный bot")
                    await bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=message_id,
                        reply_markup=None
                    )
                    logging.info(f"payment_callback: Кнопки из сообщения {message_id} успешно удалены через глобальный bot")
            except Exception as e_rm:
                # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                error_str = str(e_rm).lower()
                if "message is not modified" in error_str:
                    logging.debug(f"payment_callback: Кнопки из сообщения {message_id} уже удалены (message is not modified)")
                else:
                    logging.error(f"payment_callback: Ошибка при удалении кнопок из сообщения {message_id}: {e_rm}")
            
            # Затем пытаемся обновить текст
            try:
                try:
                    await callback.message.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=None
                    )
                    logging.info(f"payment_callback: Сообщение {message_id} успешно отредактировано через callback.message.bot")
                except Exception as e1:
                    logging.warning(f"payment_callback: Не удалось отредактировать через callback.message.bot: {e1}, пробуем глобальный bot")
                    await bot.edit_message_text(
                        chat_id=user_id,
                        message_id=message_id,
                        text=new_text,
                        reply_markup=None
                    )
                    logging.info(f"payment_callback: Сообщение {message_id} успешно отредактировано через глобальный bot")
            except Exception as e:
                logging.error(f"payment_callback: Ошибка при редактировании текста сообщения {message_id}: {e}")
        else:
            logging.warning(f"payment_callback: payment_selection_message_id не найден в state для пользователя {user_id}, state={state}")
    except Exception as e:
        logging.error(f"payment_callback: Критическая ошибка при обработке удаления кнопок: {e}")
        import traceback
        logging.error(traceback.format_exc())
    
    # Для Crypto показываем выбор валюты, для карты — кнопку запроса реквизитов,
    # для остальных методов сразу выдаём реквизиты.
    if method == "crypto":
        if lang == "ru":
            text = "Выбери криптовалюту:"
            kb = crypto_inline_ru
        else:
            text = "Choose the cryptocurrency:"
            kb = crypto_inline_en
        msg = await callback.message.answer(text, reply_markup=kb)
        # Сохраняем message_id сообщения с выбором криптовалюты
        if state:
            state["crypto_selection_message_id"] = msg.message_id
            dp.purchase_state[user_id] = state
    else:
        methods_text = {
            "ru": {
                "paypal": "💳 *PayPal*\n`rr.element.23@gmail.com`",
                "cashapp": "💵 *CashApp*\n`$rrelement`",
                "card": "🏦 Оплата картой.\nЧтобы получить реквизиты, нажми кнопку ниже.",
            },
            "en": {
                "paypal": "💳 *PayPal*\n`rr.element.23@gmail.com`",
                "cashapp": "💵 *CashApp*\n`$rrelement`",
                "card": "🏦 Card payment.\nTo get card details, press the button below.",
            },
        }

        lang_methods = methods_text.get(lang, methods_text["ru"])
        text = lang_methods.get(method, "Ошибка метода оплаты" if lang == "ru" else "Unknown payment method")
        paid_button = paid_button_ru if lang == "ru" else paid_button_en

        # Убрали дублирующую информацию о цене - она уже есть в предыдущем сообщении

        if method == "card":
            req_kb = card_request_inline_ru if lang == "ru" else card_request_inline_en
            msg = await callback.message.answer(text, reply_markup=req_kb)
            # Сохраняем message_id и текст сообщения с реквизитами для карты
            if state:
                state["payment_details_message_id"] = msg.message_id
                state["payment_details_message_text"] = text  # Сохраняем оригинальный текст
        else:
            msg = await callback.message.answer(text, reply_markup=paid_button, parse_mode="Markdown")
            # Сохраняем message_id и текст сообщения с реквизитами и кнопками "Я оплатил"/"Отмена"
            if state:
                state["payment_details_message_id"] = msg.message_id
                state["payment_details_message_text"] = text  # Сохраняем оригинальный текст
    
    # Помечаем, что способ оплаты обработан (защита от повторных нажатий)
    if state:
        state["payment_method_processed"] = True
        dp.purchase_state[user_id] = state
    
    await callback.answer()


@dp.callback_query(F.data == "continue_payment")
async def continue_payment_callback(callback):
    """Пользователь нажал 'Далее' после выбора лицензии."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    state = dp.purchase_state.get(user_id)
    if not state or "license" not in state:
        if lang == "ru":
            await callback.answer("Сначала выбери тип лицензии.", show_alert=True)
        else:
            await callback.answer("First choose the license type.", show_alert=True)
        return
    
    # Проверяем, что действие еще не выполнено (защита от повторных нажатий)
    if state.get("payment_method_selected"):
        if lang == "ru":
            await callback.answer("Способ оплаты уже выбран.", show_alert=True)
        else:
            await callback.answer("Payment method already selected.", show_alert=True)
        return
    
    # Проверяем, что пользователь не выбрал "Предложить свою цену" (защита от альтернативного варианта)
    if user_id in dp.offer_waiting_price:
        if lang == "ru":
            await callback.answer("Вы уже выбрали 'Предложить цену'. Действие уже выполнено.", show_alert=True)
        else:
            await callback.answer("You already chose 'Make an offer'. Action already completed.", show_alert=True)
        return
    
    # Редактируем предыдущее сообщение с кнопками "Далее"/"Предложить цену", убирая кнопки и добавляя статус внизу
    try:
        if "action_selection_message_id" in state:
            # Получаем оригинальный текст из state или используем дефолтный
            original_text = state.get("action_selection_message_text", "Выберите действие:" if lang == "ru" else "Choose an action:")
            status_text = "\n\n✅ Действие выполнено" if lang == "ru" else "\n\n✅ Action completed"
            new_text = original_text + status_text
            
            try:
                await callback.message.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["action_selection_message_id"],
                    text=new_text,
                    reply_markup=None
                )
            except Exception as e:
                # Если не удалось изменить текст, пытаемся просто убрать кнопки
                try:
                    await callback.message.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=state["action_selection_message_id"],
                        reply_markup=None
                    )
                except Exception as e2:
                    # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                    error_str = str(e2).lower()
                    if "message is not modified" in error_str:
                        logging.debug(f"Кнопки выбора действия уже удалены (message is not modified)")
                    else:
                        logging.error(f"Ошибка при удалении кнопок выбора действия: {e2}")
    except Exception as e:
        logging.error(f"Ошибка при обработке continue_payment_callback: {e}")
        pass
    
    # Убираем пользователя из offer_waiting_price, если он там был
    # Это предотвращает обработку текста как предложения цены после нажатия "Далее"
    dp.offer_waiting_price.discard(user_id)
    
    # Переходим к выбору способа оплаты
    if lang == "ru":
        text = "Выберите способ оплаты:"
        kb = payment_inline_ru
    else:
        text = "Choose the payment method:"
        kb = payment_inline_en
    
    msg = await callback.message.answer(text, reply_markup=kb)
    # Сохраняем message_id и текст сообщения с выбором способа оплаты
    state["payment_selection_message_id"] = msg.message_id
    state["payment_selection_message_text"] = text  # Сохраняем оригинальный текст
    # Помечаем, что способ оплаты выбран
    state["payment_method_selected"] = True
    state["action_completed"] = True  # Помечаем, что действие выполнено
    dp.purchase_state[user_id] = state
    await callback.answer()

@dp.callback_query(F.data == "offer_price")
async def offer_price_callback(callback):
    """Пользователь хочет предложить свою цену за выбранный бит и лицензию."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")

    state = dp.purchase_state.get(user_id)
    
    # Проверяем, что пользователь уже не в процессе предложения цены (защита от повторных нажатий)
    if user_id in dp.offer_waiting_price:
        if lang == "ru":
            await callback.answer("Вы уже предложили цену. Отправьте желаемую цену текстом.", show_alert=True)
        else:
            await callback.answer("You already offered a price. Send your desired price as text.", show_alert=True)
        return
    
    # Проверяем, что пользователь не выбрал "Далее" (защита от альтернативного варианта)
    if state and state.get("payment_method_selected"):
        if lang == "ru":
            await callback.answer("Вы уже выбрали 'Далее'. Действие уже выполнено.", show_alert=True)
        else:
            await callback.answer("You already chose 'Next'. Action already completed.", show_alert=True)
        return
    
    # Проверяем, что действие еще не выполнено
    if state and state.get("action_completed"):
        if lang == "ru":
            await callback.answer("Действие уже выполнено.", show_alert=True)
        else:
            await callback.answer("Action already completed.", show_alert=True)
        return

    # Всегда помечаем, что пользователь хочет сделать оффер
    dp.offer_waiting_price.add(user_id)
    
    # Помечаем, что действие выполнено
    if state:
        state["action_completed"] = True
        dp.purchase_state[user_id] = state

    # Если ещё нет бита или лицензии — сначала просим их выбрать
    if not state or "beat" not in state:
        dp.offer_waiting_price.discard(user_id)
        if lang == "ru":
            await callback.answer("Сначала выбери бит, а потом нажми «Предложить цену».", show_alert=True)
        else:
            await callback.answer("First choose the beat, then press \"Make an offer\".", show_alert=True)
        return
    
    if "license" not in state:
        dp.offer_waiting_price.discard(user_id)
        if lang == "ru":
            await callback.answer("Сначала выбери тип лицензии, а потом нажми «Предложить цену».", show_alert=True)
        else:
            await callback.answer("First choose the license type, then press \"Make an offer\".", show_alert=True)
        return

    # Редактируем предыдущее сообщение с кнопками "Далее"/"Предложить цену", убирая кнопки и добавляя статус внизу
    try:
        if state and "action_selection_message_id" in state:
            # Получаем оригинальный текст из state или используем дефолтный
            original_text = state.get("action_selection_message_text", "Выберите действие:" if lang == "ru" else "Choose an action:")
            status_text = "\n\n✅ Действие выполнено" if lang == "ru" else "\n\n✅ Action completed"
            new_text = original_text + status_text
            
            try:
                await callback.message.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["action_selection_message_id"],
                    text=new_text,
                    reply_markup=None
                )
            except Exception as e:
                # Если не удалось изменить текст, пытаемся просто убрать кнопки
                try:
                    await callback.message.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=state["action_selection_message_id"],
                        reply_markup=None
                    )
                except Exception as e2:
                    # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                    error_str = str(e2).lower()
                    if "message is not modified" in error_str:
                        logging.debug(f"Кнопки выбора действия в offer_price уже удалены (message is not modified)")
                    else:
                        logging.error(f"Ошибка при удалении кнопок выбора действия в offer_price: {e2}")
    except Exception as e:
        logging.error(f"Ошибка при обработке offer_price_callback: {e}")
        pass
    
    # Если бит и лицензия уже есть — сразу просим цену
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    
    if lang == "ru":
        text = (
            "Напиши желаемую цену в долларах.\n"
            "Можно также добавить комментарий или условия."
        )
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="back_to_main")]
            ]
        )
    else:
        text = (
            "Send your desired price in dollars.\n"
            "You can also add a short comment or conditions."
        )
        cancel_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="❌ Cancel", callback_data="back_to_main")]
            ]
        )

    msg = await callback.message.answer(text, reply_markup=cancel_kb)
    # Сохраняем message_id и текст сообщения с запросом цены для последующего удаления кнопки "Отмена"
    if user_id not in dp.purchase_state:
        dp.purchase_state[user_id] = {}
    state = dp.purchase_state[user_id]
    state["price_request_message_id"] = msg.message_id
    state["price_request_message_text"] = text  # Сохраняем оригинальный текст
    dp.purchase_state[user_id] = state
    await callback.answer()


@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback):
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")

    # Получаем ID текущего сообщения (того, на которое нажали)
    current_message_id = callback.message.message_id
    
    # Редактируем все предыдущие сообщения с кнопками, убирая их (без добавления текста отмены)
    state = dp.purchase_state.get(user_id, {})
    messages_to_edit = []
    
    try:
        # Собираем все message_id, которые нужно отредактировать
        if "license_selection_message_id" in state:
            messages_to_edit.append(("license_selection_message_id", state.get("license_selection_message_text", "")))
        if "action_selection_message_id" in state:
            messages_to_edit.append(("action_selection_message_id", state.get("action_selection_message_text", "")))
        if "payment_selection_message_id" in state:
            messages_to_edit.append(("payment_selection_message_id", state.get("payment_selection_message_text", "")))
        if "crypto_selection_message_id" in state:
            messages_to_edit.append(("crypto_selection_message_id", state.get("crypto_selection_message_text", "")))
        if "price_request_message_id" in state:
            messages_to_edit.append(("price_request_message_id", state.get("price_request_message_text", "")))
        if "payment_details_message_id" in state:
            messages_to_edit.append(("payment_details_message_id", state.get("payment_details_message_text", "")))
        
        # Проверяем файл payment_details.json (из бота покупок)
        import json
        import os
        payment_details_file = "payment_details.json"
        if os.path.exists(payment_details_file):
            try:
                with open(payment_details_file, "r", encoding="utf-8") as f:
                    details = json.load(f)
                if str(user_id) in details:
                    detail_data = details[str(user_id)]
                    messages_to_edit.append(("payment_details_from_file", detail_data.get("payment_details_message_text", ""), detail_data.get("payment_details_message_id")))
            except Exception as e:
                logging.error(f"Ошибка чтения payment_details.json в back_to_main: {e}")
        
        # Удаляем кнопки из всех найденных сообщений (кроме текущего)
        for msg_info in messages_to_edit:
            try:
                if len(msg_info) == 3:  # Из файла
                    msg_id = msg_info[2]
                    original_text = msg_info[1]
                else:
                    msg_key = msg_info[0]
                    msg_id = state.get(msg_key)
                    original_text = msg_info[1]
                
                if msg_id and msg_id != current_message_id:
                    # Только убираем кнопки, без добавления текста отмены
                    try:
                        await callback.message.bot.edit_message_reply_markup(
                            chat_id=user_id,
                            message_id=msg_id,
                            reply_markup=None
                        )
                    except Exception as e:
                        # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                        error_str = str(e).lower()
                        if "message is not modified" in error_str:
                            logging.debug(f"Кнопки из сообщения {msg_id} уже удалены (message is not modified)")
                        else:
                            logging.error(f"Ошибка при удалении кнопок из сообщения {msg_id}: {e}")
            except Exception as e:
                logging.error(f"Ошибка при обработке сообщения в back_to_main: {e}")
                continue
        
        # Для текущего сообщения убираем кнопки и добавляем статус отмены
        try:
            # Получаем текст текущего сообщения
            current_text = callback.message.text or callback.message.caption or ""
            if current_text:
                cancel_text = "\n\n❌ Операция отменена" if lang == "ru" else "\n\n❌ Operation cancelled"
                new_text = current_text + cancel_text
                
                # Пытаемся обновить текст с добавлением статуса отмены
                try:
                    if callback.message.caption:
                        # Если это сообщение с caption (фото, документ и т.д.)
                        await callback.message.bot.edit_message_caption(
                            chat_id=user_id,
                            message_id=current_message_id,
                            caption=new_text,
                            reply_markup=None
                        )
                    else:
                        # Если это текстовое сообщение
                        await callback.message.bot.edit_message_text(
                            chat_id=user_id,
                            message_id=current_message_id,
                            text=new_text,
                            reply_markup=None
                        )
                except Exception as e:
                    # Если не удалось изменить текст, просто убираем кнопки
                    logging.debug(f"Не удалось обновить текст текущего сообщения: {e}")
                    await callback.message.bot.edit_message_reply_markup(
                        chat_id=user_id,
                        message_id=current_message_id,
                        reply_markup=None
                    )
            else:
                # Если текста нет, просто убираем кнопки
                await callback.message.bot.edit_message_reply_markup(
                    chat_id=user_id,
                    message_id=current_message_id,
                    reply_markup=None
                )
        except Exception as e:
            logging.error(f"Ошибка при обработке текущего сообщения в back_to_main: {e}")
                
    except Exception as e:
        logging.error(f"Ошибка при обработке back_to_main: {e}")
        pass

    # Проверяем, есть ли активная покупка, и если да, помечаем её как отмененную
    try:
        from orders_manager import get_beats_purchase_by_user_id, update_beats_purchase_status
        # Ищем активную покупку (включая те, что могут быть не найдены через get_beats_purchase_by_user_id из-за фильтра)
        from orders_manager import get_all_beats_purchases
        purchases = await get_all_beats_purchases()
        # Ищем самую новую активную покупку пользователя (не завершенную и не отмененную)
        active_purchase = None
        for p in reversed(purchases):
            if p["user_id"] == user_id and p.get("status") not in ["completed", "payment_rejected", "cancelled_by_client"]:
                active_purchase = p
                break
        
        if active_purchase:
            # Помечаем покупку как отмененную клиентом, если она еще не завершена
            if active_purchase.get("status") in ["pending_payment", "payment_received"]:
                await update_beats_purchase_status(active_purchase["id"], "cancelled_by_client")
                logging.info(f"Покупка №{active_purchase['id']} отменена клиентом. Статус обновлен на cancelled_by_client.")
    except Exception as e:
        logging.error(f"Ошибка при обновлении статуса покупки при отмене: {e}")
    
    # Очищаем состояние покупки при отмене
    dp.purchase_state.pop(user_id, None)
    dp.offer_waiting_price.discard(user_id)
    dp.current_payment_users.discard(user_id)
    
    # Удаляем из payment_details.json, если есть
    try:
        if os.path.exists(payment_details_file):
            with open(payment_details_file, "r", encoding="utf-8") as f:
                details = json.load(f)
            if str(user_id) in details:
                del details[str(user_id)]
                with open(payment_details_file, "w", encoding="utf-8") as f:
                    json.dump(details, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error(f"Ошибка при очистке payment_details.json: {e}")

    # Показываем главное меню
    if lang == "ru":
        text = "Главное меню."
        kb = main_keyboard_ru
    else:
        text = "Main menu."
        kb = main_keyboard_en

    await callback.message.answer(text, reply_markup=kb)
    await callback.answer()



# --- Нажатие "Я оплатил" ---
@dp.callback_query(F.data == "paid")
async def paid_callback(callback):
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Редактируем сообщение с реквизитами, убирая кнопки "Я оплатил" и "Отмена"
    state = dp.purchase_state.get(user_id, {})
    
    # Проверяем, есть ли payment_details_message_id в файле (из бота покупок)
    import json
    import os
    payment_details_file = "payment_details.json"
    if os.path.exists(payment_details_file):
        try:
            with open(payment_details_file, "r", encoding="utf-8") as f:
                details = json.load(f)
            if str(user_id) in details:
                detail_data = details[str(user_id)]
                state["payment_details_message_id"] = detail_data.get("payment_details_message_id")
                state["payment_details_message_text"] = detail_data.get("payment_details_message_text", "")
                dp.purchase_state[user_id] = state
                # Удаляем из файла после использования
                del details[str(user_id)]
                with open(payment_details_file, "w", encoding="utf-8") as f:
                    json.dump(details, f, ensure_ascii=False, indent=2)
                logging.info(f"paid_callback: Восстановлен payment_details_message_id из payment_details.json")
        except Exception as e:
            logging.error(f"Ошибка чтения payment_details.json: {e}")
    
    try:
        if "payment_details_message_id" in state:
            # Пытаемся убрать кнопки из сообщения с реквизитами
            try:
                # Сначала убираем кнопки
                await callback.message.bot.edit_message_reply_markup(
                    chat_id=user_id,
                    message_id=state["payment_details_message_id"],
                    reply_markup=None
                )
                # Затем пытаемся обновить текст, добавляя статус внизу
                try:
                    # Получаем оригинальный текст из state
                    original_text = state.get("payment_details_message_text", "")
                    if not original_text:
                        # Если текста нет в state, используем дефолтный текст
                        original_text = "Реквизиты для оплаты" if lang == "ru" else "Payment details"
                    
                    status_text = "\n\n✅ Оплачено" if lang == "ru" else "\n\n✅ Paid"
                    new_text = original_text + status_text
                    
                    await callback.message.bot.edit_message_text(
                        chat_id=user_id,
                        message_id=state["payment_details_message_id"],
                        text=new_text,
                        reply_markup=None,
                        parse_mode="Markdown"  # Сохраняем Markdown форматирование
                    )
                except Exception as e:
                    # Если не удалось изменить текст (например, из-за Markdown), просто оставляем без кнопок
                    logging.error(f"Ошибка при обновлении текста в paid_callback: {e}")
                    pass
            except Exception as e:
                # Игнорируем ошибку "message is not modified" - это нормально, если кнопки уже удалены
                error_str = str(e).lower()
                if "message is not modified" in error_str:
                    logging.debug(f"Кнопки из сообщения с реквизитами уже удалены (message is not modified)")
                else:
                    logging.error(f"Ошибка при удалении кнопок из сообщения с реквизитами: {e}")
                pass
    except Exception as e:
        logging.error(f"Ошибка при обработке paid_callback: {e}")
        pass
    
    dp.current_payment_users.add(user_id)
    text = (
        "Пожалуйста, пришли чек/скриншот перевода."
        if lang == "ru"
        else "Please send the payment receipt/screenshot."
    )
    await callback.message.answer(text, reply_markup=None)
    await callback.answer()


@dp.callback_query(F.data == "req_card")
async def request_card_details(callback):
    """Пользователь просит реквизиты для оплаты картой."""
    user = callback.from_user
    user_id = user.id
    lang = dp.user_language.get(user_id, "ru")

    username = user.username or "no_username"

    # Редактируем сообщение с кнопками "Запросить реквизиты" и "Отмена", убирая их
    state = dp.purchase_state.get(user_id, {})
    try:
        if "payment_details_message_id" in state:
            # Получаем оригинальный текст из state
            original_text = state.get("payment_details_message_text", "")
            if not original_text:
                # Если текста нет в state, используем дефолтный текст
                original_text = "🏛 Оплата картой. Чтобы получить реквизиты, нажми кнопку ниже." if lang == "ru" else "🏛 Card payment. To get details, press the button below."
            
            status_text = "\n\n✅ Запрос отправлен" if lang == "ru" else "\n\n✅ Request sent"
            new_text = original_text + status_text
            
            try:
                await callback.message.bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["payment_details_message_id"],
                    text=new_text,
                    reply_markup=None
                )
            except Exception as e:
                # Игнорируем ошибку "message is not modified" - это нормально, если сообщение уже в нужном состоянии
                error_str = str(e).lower()
                if "message is not modified" in error_str:
                    logging.debug(f"Сообщение в request_card_details уже в нужном состоянии (message is not modified)")
                else:
                    logging.error(f"Ошибка при редактировании сообщения в request_card_details: {e}")
                    # Если не удалось изменить текст, просто убираем кнопки
                    try:
                        await callback.message.bot.edit_message_reply_markup(
                            chat_id=user_id,
                            message_id=state["payment_details_message_id"],
                            reply_markup=None
                        )
                    except Exception as e2:
                        error_str2 = str(e2).lower()
                        if "message is not modified" in error_str2:
                            logging.debug(f"Кнопки в request_card_details уже удалены (message is not modified)")
                        else:
                            logging.error(f"Ошибка при удалении кнопок в request_card_details: {e2}")
    except Exception as e:
        logging.error(f"Ошибка при обработке request_card_details: {e}")
        pass

    # Сохраняем, что админ должен отправить реквизиты этому клиенту
    dp.waiting_card_details[user_id] = user_id
    
    # Обновляем покупку - отмечаем, что ждет реквизиты
    # Ищем самую новую покупку пользователя (по ID, не завершенную)
    from orders_manager import get_all_beats_purchases, create_beats_purchase
    purchases = await get_all_beats_purchases()
    purchase = None
    
    # Фильтруем покупки пользователя (не завершенные и не отмененные)
    user_purchases = [p for p in purchases if p["user_id"] == user_id and p.get("status") not in ["completed", "payment_rejected", "cancelled_by_client"]]
    
    if user_purchases:
        # Сортируем по ID (самая новая - с наибольшим ID)
        user_purchases.sort(key=lambda x: x.get("id", 0), reverse=True)
        purchase = user_purchases[0]  # Берем самую новую покупку
        logging.info(f"Найдена покупка №{purchase['id']} для запроса реквизитов (всего покупок пользователя: {len(user_purchases)})")
    
    # Если покупки нет, создаем новую на основе данных из state
    if not purchase:
        # Проверяем, что это не заказ (custom или mixing)
        if state.get("is_custom") or state.get("is_mixing"):
            logging.warning(f"Запрос реквизитов для заказа (не покупки) от пользователя {user_id}")
        else:
            # Создаем новую покупку на основе данных из state
            beat = state.get("beat", "-")
            license_info = state.get("license", "-")
            # Если license это просто цена (например, "$34"), используем его как price
            price = license_info if license_info.startswith("$") else (license_info.split(" — ", 1)[1] if " — " in license_info else license_info)
            purchase = await create_beats_purchase(user_id, username, beat, license_info, price)
            logging.info(f"Создана новая покупка №{purchase['id']} при запросе реквизитов: лицензия={license_info}, бит={beat}")
            # Перезагружаем purchases после создания
            purchases = await get_all_beats_purchases()
            # Находим только что созданную покупку
            for p in purchases:
                if p["id"] == purchase["id"]:
                    purchase = p
                    break
    
    if purchase:
        # Обновляем покупку, добавляя флаг ожидания реквизитов
        from orders_manager import update_beats_purchase_status
        # Устанавливаем waiting_card_details=1 и сбрасываем card_details_sent=0 (если реквизиты запрашиваются повторно)
        await update_beats_purchase_status(purchase["id"], purchase.get("status", "pending_payment"), waiting_card_details=1, card_details_sent=0)
        logging.info(f"Установлен флаг waiting_card_details и сброшен card_details_sent для покупки №{purchase['id']}")
        
        # Используем актуальные данные из state, если они есть (могут быть обновлены после принятия цены)
        beat_info = state.get("beat", purchase.get("beat", "-"))
        license_info = state.get("license", purchase.get("license", "-"))
        
        # Извлекаем тип лицензии и цену из license_info
        license_type = license_info
        price_info = ""
        
        # Если license содержит " — ", разделяем на тип и цену
        if " — " in license_info:
            parts = license_info.split(" — ", 1)
            license_type = parts[0].strip()
            price_info = parts[1].strip()
        elif license_info.startswith("$"):
            # Если license это просто цена (например, "$34")
            price_info = license_info
            license_type = purchase.get("license", "-").split(" — ")[0] if " — " in purchase.get("license", "-") else purchase.get("license", "-")
        else:
            # Если нет цены в license, берем из purchase
            price_info = purchase.get("price", "-")
            if " — " in price_info:
                price_info = price_info.split(" — ", 1)[1] if " — " in price_info else price_info
        
        # Обновляем информацию о покупке в базе данных актуальными данными из state
        # Обновляем beat, license и price, если они изменились
        from orders_manager import update_beats_purchase_status
        update_kwargs = {}
        if beat_info != "-" and beat_info != purchase.get("beat", "-"):
            update_kwargs["beat"] = beat_info
        if license_info != "-" and license_info != purchase.get("license", "-"):
            update_kwargs["license"] = license_info
        if price_info and price_info != "-" and price_info != purchase.get("price", "-"):
            update_kwargs["price"] = price_info
        
        if update_kwargs:
            await update_beats_purchase_status(purchase["id"], purchase.get("status", "pending_payment"), **update_kwargs)
            logging.info(f"Обновлена информация о покупке №{purchase['id']}: {update_kwargs}")
        else:
            logging.info(f"Информация о покупке №{purchase['id']}: beat={beat_info}, license={license_info}, price={price_info}")
        
        # Сообщение админу с кнопкой - отправляем в бот покупок (с номером покупки)
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📩 Отправить реквизиты", callback_data=f"send_card_{user_id}")]
            ]
        )
        # Форматируем номер покупки
        from orders_manager import format_purchase_number
        purchase_display_num = format_purchase_number(purchase['id'], purchase.get('created_at'))
        
        if purchases_bot:
            try:
                await purchases_bot.send_message(
                    ADMIN_ID,
                    f"💳 Запрошены реквизиты для оплаты картой.\n\n"
                    f"📦 Покупка {purchase_display_num}\n"
                    f"👤 Пользователь: @{username} (id={user_id})\n"
                    f"🎵 Бит: {beat_info}\n"
                    f"📜 Лицензия: {license_type}\n"
                    f"💰 Цена: {price_info}",
                    reply_markup=admin_kb,
                )
            except Exception as e:
                logging.error(f"Ошибка отправки запроса реквизитов в бот покупок: {e}")
                # Fallback на основной бот, если бот покупок недоступен
                await bot.send_message(
                    ADMIN_ID,
                    f"💳 Запрошены реквизиты для оплаты картой.\n\n"
                    f"📦 Покупка {purchase_display_num}\n"
                    f"👤 Пользователь: @{username} (id={user_id})\n"
                    f"🎵 Бит: {beat_info}\n"
                    f"📜 Лицензия: {license_type}\n"
                    f"💰 Цена: {price_info}",
                    reply_markup=admin_kb,
                )
        else:
            # Fallback на основной бот, если бот покупок не инициализирован
            # Форматируем номер покупки
            from orders_manager import format_purchase_number
            purchase_display_num = format_purchase_number(purchase['id'], purchase.get('created_at'))
            await bot.send_message(
                ADMIN_ID,
                f"💳 Запрошены реквизиты для оплаты картой.\n\n"
                f"📦 Покупка №{purchase_display_num}\n"
                f"👤 Пользователь: @{username} (id={user_id})\n"
                f"🎵 Бит: {beat_info}\n"
                f"📜 Лицензия: {license_type}\n"
                f"💰 Цена: {price_info}",
                reply_markup=admin_kb,
            )
    else:
        logging.warning(f"Не найдена покупка для установки флага waiting_card_details для пользователя {user_id}")
        # Сообщение админу без номера покупки (fallback)
        admin_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📩 Отправить реквизиты", callback_data=f"send_card_{user_id}")]
            ]
        )
        if purchases_bot:
            try:
                await purchases_bot.send_message(
                    ADMIN_ID,
                    f"💳 Запрошены реквизиты для оплаты картой.\n"
                    f"👤 Пользователь: @{username} (id={user_id})",
                    reply_markup=admin_kb,
                )
            except Exception as e:
                logging.error(f"Ошибка отправки запроса реквизитов в бот покупок: {e}")
                # Fallback на основной бот, если бот покупок недоступен
                await bot.send_message(
                    ADMIN_ID,
                    f"💳 Запрошены реквизиты для оплаты картой.\n"
                    f"👤 Пользователь: @{username} (id={user_id})",
                    reply_markup=admin_kb,
                )
        else:
            # Fallback на основной бот, если бот покупок не инициализирован
            await bot.send_message(
                ADMIN_ID,
                f"💳 Запрошены реквизиты для оплаты картой.\n"
                f"👤 Пользователь: @{username} (id={user_id})",
                reply_markup=admin_kb,
            )

    # Ответ пользователю (пока без кнопки "Я оплатил")
    if lang == "ru":
        text = "Реквизиты будут отправлены в ближайшее время."
    else:
        text = "Details will be sent shortly."

    await callback.message.answer(text)
    await callback.answer()


# Обработка отправки реквизитов перенесена в бот покупок
# Этот обработчик оставлен для обратной совместимости, но теперь работает через бот покупок
@dp.callback_query(F.data.startswith("send_card_"))
async def send_card_details_callback(callback):
    """Админ нажал кнопку 'Отправить реквизиты' - перенаправляем в бот покупок."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отправлять реквизиты.", show_alert=True)
        return
    
    # Если это сообщение пришло в основной бот, но должно обрабатываться в боте покупок
    # Просто отвечаем, что нужно использовать бот покупок
    await callback.answer("Используйте бот покупок для отправки реквизитов.", show_alert=True)


@dp.callback_query(F.data.startswith("offer_accept_"))
async def accept_offer_callback(callback):
    """Админ принял предложение по цене."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать предложения.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Обновляем purchase_state с принятой ценой
    price = offer.get('price', '-')
    price_clean = price.replace('$', '').strip()
    state = dp.purchase_state.get(client_user_id, {})
    state["beat"] = offer.get('beat', state.get('beat', '-'))
    
    # Сохраняем тип лицензии, если он был выбран ранее
    original_license = state.get("license", "")
    if original_license and " — " in original_license:
        # Если есть исходная лицензия с типом (например, "WAV — $45"), сохраняем тип с новой ценой
        license_type = original_license.split(" — ", 1)[0].strip()
        state["license"] = f"{license_type} — {price_clean}"
    else:
        # Если нет типа лицензии, используем просто цену
        state["license"] = price_clean
    
    state["full_price"] = price_clean
    dp.purchase_state[client_user_id] = state
    
    # Сохраняем язык пользователя в БД для использования в других ботах
    from orders_manager import set_user_language
    await set_user_language(client_user_id, lang)
    
    # Сообщение клиенту
    is_custom = offer.get('is_custom', False)
    is_mixing = offer.get('is_mixing', False)
    beat = offer.get('beat', '-')
    
    if lang == "ru":
        if is_custom:
            price_display = offer.get('price', '-')
            # Очищаем от пробелов и гарантированно добавляем $
            if price_display and price_display != '-':
                price_display = price_display.strip()
                price_display = price_display.replace('$', '').strip()
                if price_display:
                    price_display = f"${price_display}"
            client_text = (
                "✅ Отлично! Я принял твоё предложение по цене.\n\n"
                f"Услуга: Custom Beat\n"
                f"Описание: {beat}\n"
                f"Цена: {price_display}\n\n"
                "Теперь выбери способ оплаты:"
            )
        elif is_mixing:
            price_display = offer.get('price', '-')
            # Очищаем от пробелов и гарантированно добавляем $
            if price_display and price_display != '-':
                price_display = price_display.strip()
                price_display = price_display.replace('$', '').strip()
                if price_display:
                    price_display = f"${price_display}"
            client_text = (
                "✅ Отлично! Я принял твоё предложение по цене.\n\n"
                f"Услуга: Сведение\n"
                f"Архив: {beat}\n"
                f"Цена: {price_display}\n\n"
                "Теперь выбери способ оплаты:"
            )
        else:
            # Форматируем лицензию и цену
            offer_license = offer.get('license', '-')
            offer_price = offer.get('price', '-')
            license_text, price_text = format_license_and_price(offer_license)
            
            # Убеждаемся, что цена содержит символ $
            # Используем offer_price напрямую, так как он содержит актуальную цену
            display_price = '-'
            # Проверяем offer_price (приоритет) или price_text из format_license_and_price
            price_source = offer_price if offer_price and offer_price != '-' else price_text
            
            if price_source and price_source != '-':
                # Преобразуем в строку и очищаем от пробелов
                price_str = str(price_source).strip()
                # Пропускаем только если это действительно '-'
                if price_str and price_str != '-':
                    # Убираем $ если есть (на случай, если уже был)
                    price_str = price_str.replace('$', '').strip()
                    # Убираем все нецифровые символы кроме точки и запятой (для десятичных чисел)
                    price_str = re.sub(r'[^\d.,]', '', price_str)
                    # Если после очистки осталась непустая строка, добавляем $
                    if price_str:
                        display_price = f"${price_str}"
            
            client_text = (
                "✅ Отлично! Я принял твоё предложение по цене.\n\n"
                f"Бит: {beat}\n"
                f"Лицензия: {license_text}\n"
                f"Цена: {display_price}\n\n"
                "Теперь выбери способ оплаты:"
            )
    else:
        if is_custom:
            client_text = (
                "✅ Great! I've accepted your price offer.\n\n"
                f"Service: Custom Beat\n"
                f"Description: {beat}\n"
                f"Price: {offer.get('price', '-')}\n\n"
                "Now choose the payment method:"
            )
        elif is_mixing:
            client_text = (
                "✅ Great! I've accepted your price offer.\n\n"
                f"Service: Mixing\n"
                f"Archive: {beat}\n"
                f"Price: {offer.get('price', '-')}\n\n"
                "Now choose the payment method:"
            )
        else:
            # Форматируем лицензию и цену
            offer_license = offer.get('license', '-')
            offer_price = offer.get('price', '-')
            license_text, price_text = format_license_and_price(offer_license)
            
            # Убеждаемся, что цена содержит символ $
            display_price = price_text if price_text else offer_price
            if display_price and not display_price.startswith("$"):
                display_price = f"${display_price}"
            
            client_text = (
                "✅ Great! I've accepted your price offer.\n\n"
                f"Beat: {beat}\n"
                f"License: {license_text}\n"
                f"Price: {display_price}\n\n"
                "Now choose the payment method:"
            )
    
    # Убеждаемся, что client_text - это строка
    if isinstance(client_text, tuple):
        client_text = "".join(str(item) for item in client_text)
    elif not isinstance(client_text, str):
        client_text = str(client_text)
    
    payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
    
    # Логируем для отладки
    logging.info(f"Отправка сообщения клиенту {client_user_id}, клавиатура: {payment_kb is not None}")
    
    try:
        msg = await bot.send_message(client_user_id, client_text, reply_markup=payment_kb)
        # Сохраняем message_id и текст сообщения с выбором способа оплаты
        if client_user_id not in dp.purchase_state:
            dp.purchase_state[client_user_id] = {}
        state = dp.purchase_state[client_user_id]
        state["payment_selection_message_id"] = msg.message_id
        state["payment_selection_message_text"] = client_text  # Сохраняем оригинальный текст
        dp.purchase_state[client_user_id] = state
        logging.info(f"accept_offer_callback: Сохранено payment_selection_message_id={msg.message_id} для пользователя {client_user_id}, текст: {client_text[:100]}...")
        original_text = callback.message.text or callback.message.caption or "Предложение"
        await callback.message.edit_text(
            f"{original_text}\n\n✅ Предложение принято. Клиенту отправлено сообщение."
        )
        await callback.answer("✅ Предложение принято")
    except Exception as e:
        logging.error(f"Ошибка принятия предложения: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("offer_reject_"))
async def reject_offer_callback(callback):
    """Админ отклонил предложение по цене."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять предложения.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сообщение клиенту
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
    
    try:
        await bot.send_message(client_user_id, client_text)
        original_text = callback.message.text or callback.message.caption or "Предложение"
        await callback.message.edit_text(
            f"{original_text}\n\n❌ Предложение отклонено. Клиенту отправлено сообщение."
        )
        await callback.answer("❌ Предложение отклонено")
    except Exception as e:
        logging.error(f"Ошибка отклонения предложения: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("custom_accept_"))
async def accept_custom_order_callback(callback):
    """Админ принял заказ бита на заказ."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать заказы.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    # Ищем заказ в orders_manager
    from orders_manager import get_order_by_user_id, create_custom_order, update_order_status
    order = await get_order_by_user_id(client_user_id, "custom_beat")
    if not order:
        # Fallback на старую систему
        if client_user_id not in dp.pending_custom_orders:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        old_order = dp.pending_custom_orders.pop(client_user_id, {})
        # Создаем заказ в новой системе
        order = await create_custom_order(
            client_user_id,
            callback.from_user.username or "no_username",
            old_order.get("description", "-"),
            old_order.get("file_id")
        )
    else:
        # Обновляем статус заказа
        await update_order_status(order["id"], "custom_beat", "accepted")
        # Удаляем из старой системы
        dp.pending_custom_orders.pop(client_user_id, None)
    
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сохраняем информацию о кастом-заказе в purchase_state для отслеживания оплаты
    dp.purchase_state[client_user_id] = {
        "beat": order.get('description', '-'),
        "license": "Custom Beat — договорная",
        "is_custom": True,  # Флаг, что это кастом-заказ
    }
    
    # Сообщение клиенту с просьбой указать цену
    if lang == "ru":
        client_text = (
            "✅ Отлично! Я принял твой заказ на бит.\n\n"
            f"Описание: {order.get('description', '-')}\n\n"
            "Теперь нужно договориться о цене. Напиши желаемую цену в долларах."
        )
    else:
        client_text = (
            "✅ Great! I've accepted your custom beat order.\n\n"
            f"Description: {order.get('description', '-')}\n\n"
            "Now we need to agree on the price. Send your desired price in dollars."
        )
    
    try:
        await bot.send_message(client_user_id, client_text)
        # Добавляем пользователя в ожидание цены
        dp.custom_order_waiting_price.add(client_user_id)
        
        # Пытаемся обновить сообщение - если это файл, используем edit_caption, иначе edit_text
        try:
            if callback.message.document or callback.message.audio:
                # Это сообщение с файлом - редактируем caption
                original_caption = callback.message.caption or "Заказ"
                await callback.message.edit_caption(
                    f"{original_caption}\n\n✅ Заказ принят. Ожидаю цену от клиента."
                )
            else:
                # Это текстовое сообщение - редактируем текст
                original_text = callback.message.text or "Заказ"
                await callback.message.edit_text(
                    f"{original_text}\n\n✅ Заказ принят. Ожидаю цену от клиента."
                )
        except Exception as edit_error:
            # Если не удалось отредактировать, просто отправляем новое сообщение
            logging.warning(f"Не удалось отредактировать сообщение: {edit_error}")
            await bot.send_message(
                ADMIN_ID,
                f"✅ Заказ на бит от пользователя {client_user_id} принят. Ожидаю цену от клиента."
            )
        
        await callback.answer("✅ Заказ принят")
    except Exception as e:
        logging.error(f"Ошибка в accept_custom_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("custom_reject_"))
async def reject_custom_order_callback(callback):
    """Админ отклонил заказ бита на заказ."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять заказы.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_custom_orders:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    
    order = dp.pending_custom_orders.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сообщение клиенту
    if lang == "ru":
        client_text = (
            "❌ К сожалению, я не могу принять твой заказ на бит в данный момент."
        )
    else:
        client_text = (
            "❌ Unfortunately, I can't accept your custom beat order at the moment."
        )
    
    try:
        await bot.send_message(client_user_id, client_text)
        
        # Пытаемся обновить сообщение - если это файл, используем edit_caption, иначе edit_text
        try:
            if callback.message.document or callback.message.audio:
                # Это сообщение с файлом - редактируем caption
                original_caption = callback.message.caption or "Заказ"
                await callback.message.edit_caption(
                    f"{original_caption}\n\n❌ Заказ отклонен. Клиенту отправлено сообщение."
                )
            else:
                # Это текстовое сообщение - редактируем текст
                original_text = callback.message.text or "Заказ"
                await callback.message.edit_text(
                    f"{original_text}\n\n❌ Заказ отклонен. Клиенту отправлено сообщение."
                )
        except Exception as edit_error:
            # Если не удалось отредактировать, просто отправляем новое сообщение
            logging.warning(f"Не удалось отредактировать сообщение: {edit_error}")
            await bot.send_message(
                ADMIN_ID,
                f"❌ Заказ на бит от пользователя {client_user_id} отклонен. Клиенту отправлено сообщение."
            )
        
        await callback.answer("❌ Заказ отклонен")
    except Exception as e:
        logging.error(f"Ошибка в reject_custom_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("mixing_accept_"))
async def accept_mixing_order_callback(callback):
    """Админ принял заказ на сведение (обратная совместимость - основная логика в orders_bot)."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать заказы.", show_alert=True)
        return
    
    # Это обработчик для обратной совместимости
    # Основная логика теперь в orders_bot.py
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    # Ищем заказ в orders_manager
    from orders_manager import get_order_by_user_id, create_mixing_order, update_order_status
    order = await get_order_by_user_id(client_user_id, "mixing")
    if not order:
        # Fallback на старую систему
        if client_user_id not in dp.pending_mixing_orders:
            await callback.answer("Заказ не найден. Проверьте бот заказов.", show_alert=True)
            return
        old_order = dp.pending_mixing_orders.pop(client_user_id, {})
        # Создаем заказ в новой системе
        order = await create_mixing_order(
            client_user_id,
            callback.from_user.username or "no_username",
            old_order.get("description", "-"),
            old_order.get("file_id")
        )
    else:
        # Обновляем статус заказа
        await update_order_status(order["id"], "mixing", "accepted")
        # Удаляем из старой системы
        dp.pending_mixing_orders.pop(client_user_id, None)
    
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сохраняем информацию о заказе на сведение в purchase_state для отслеживания оплаты
    dp.purchase_state[client_user_id] = {
        "beat": order.get('description', '-'),
        "license": "Сведение — договорная",
        "is_mixing": True,  # Флаг, что это сведение
        "order_id": order["id"],  # Сохраняем ID заказа
    }
    
    # Сообщение клиенту с просьбой указать цену
    if lang == "ru":
        from orders_manager import format_order_number
        order_display_num = format_order_number(order['id'], order['type'], order.get('created_at'))
        client_text = (
            f"✅ Отлично! Я принял твой заказ на сведение. Номер заказа: {order_display_num}\n\n"
            f"Описание: {order.get('description', '-')}\n\n"
            "Теперь нужно договориться о цене. Напиши желаемую цену в долларах.\n\n"
            "⚠️ Оплата будет разделена на две части: 50% сразу, 50% после выполнения заказа."
        )
    else:
        client_text = (
            "✅ Great! I've accepted your mixing order.\n\n"
            f"Description: {order.get('description', '-')}\n\n"
            "Now we need to agree on the price. Send your desired price in dollars."
        )
    
    try:
        await bot.send_message(client_user_id, client_text)
        # Добавляем пользователя в ожидание цены
        dp.mixing_order_waiting_price.add(client_user_id)
        
        # Пытаемся обновить сообщение - если это файл, используем edit_caption, иначе edit_text
        try:
            if callback.message.document or callback.message.audio:
                # Это сообщение с файлом - редактируем caption
                original_caption = callback.message.caption or "Заказ"
                await callback.message.edit_caption(
                    f"{original_caption}\n\n✅ Заказ принят. Ожидаю цену от клиента."
                )
            else:
                # Это текстовое сообщение - редактируем текст
                original_text = callback.message.text or "Заказ"
                await callback.message.edit_text(
                    f"{original_text}\n\n✅ Заказ принят. Ожидаю цену от клиента."
                )
        except Exception as edit_error:
            # Если не удалось отредактировать, просто отправляем новое сообщение
            logging.warning(f"Не удалось отредактировать сообщение: {edit_error}")
            await bot.send_message(
                ADMIN_ID,
                f"✅ Заказ на сведение от пользователя {client_user_id} принят. Ожидаю цену от клиента."
            )
        
        await callback.answer("✅ Заказ принят")
    except Exception as e:
        logging.error(f"Ошибка в accept_mixing_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("mixing_reject_"))
async def reject_mixing_order_callback(callback):
    """Админ отклонил заказ на сведение."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять заказы.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_mixing_orders:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    
    order = dp.pending_mixing_orders.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сообщение клиенту
    if lang == "ru":
        client_text = (
            "❌ К сожалению, я не могу принять твой заказ на сведение в данный момент.\n\n"
            "Можешь попробовать позже или связаться со мной для обсуждения."
        )
    else:
        client_text = (
            "❌ Unfortunately, I can't accept your mixing order at the moment.\n\n"
            "You can try again later or contact me to discuss."
        )
    
    try:
        await bot.send_message(client_user_id, client_text)
        original_text = callback.message.text or callback.message.caption or "Заказ"
        await callback.message.edit_text(
            f"{original_text}\n\n❌ Заказ отклонен. Клиенту отправлено сообщение."
        )
        await callback.answer("❌ Заказ отклонен")
    except Exception as e:
        logging.error(f"Ошибка в reject_mixing_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("client_cancel_mixing_"))
async def client_cancel_mixing_callback(callback):
    """Клиент отменил заказ на сведение."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Проверяем, есть ли заказ
    if user_id not in dp.pending_mixing_orders:
        if lang == "ru":
            await callback.answer("Заказ не найден или уже обработан.", show_alert=True)
        else:
            await callback.answer("Order not found or already processed.", show_alert=True)
        return
    
    # Удаляем заказ
    order = dp.pending_mixing_orders.pop(user_id, {})
    
    # Уведомляем админа
    admin_text = (
        f"⚠️ Клиент отменил заказ на сведение.\n"
        f"Пользователь: @{callback.from_user.username or 'no_username'} (id={user_id})\n"
        f"Архив: {order.get('description', '-')}"
    )
    # Отправляем уведомление об отмене заказа в бот заказов
    if orders_bot:
        try:
            await orders_bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления в бот заказов: {e}")
    # Не отправляем в основной бот - он только для клиентов
    
    # Подтверждение клиенту
    if lang == "ru":
        client_text = "✅ Заказ отменен."
    else:
        client_text = "✅ Order cancelled."
    
    await callback.message.edit_text(client_text)
    await callback.answer(client_text)


@dp.callback_query(F.data == "confirm_custom_order")
async def confirm_custom_order_callback(callback: CallbackQuery):
    """Клиент подтвердил заказ на бит на заказ - создаем заказ автоматически."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Создаем заказ автоматически с пустым описанием (будет заполнено позже или можно оставить пустым)
    username = callback.from_user.username or "no_username"
    description = "Заказ создан автоматически"  # Можно изменить на что-то более подходящее
    
    from orders_manager import create_custom_order
    order = await create_custom_order(user_id, username, description, None)
    
    # Отправляем заказ в бот заказов
    if orders_bot:
        try:
            from orders_bot import send_order_to_bot
            logging.info(f"Отправка автоматически созданного заказа на бит на заказ №{order['id']} в бот заказов")
            await send_order_to_bot(order, None, orders_bot, ADMIN_ID)
            logging.info(f"Заказ на бит на заказ №{order['id']} успешно отправлен в бот заказов")
        except Exception as e:
            logging.error(f"Ошибка отправки заказа в бот заказов: {e}")
    
    # Сохраняем в старую систему для обратной совместимости
    dp.pending_custom_orders[user_id] = {
        "description": description,
        "file_id": None,
        "order_id": order["id"],
    }
    
    # Удаляем кнопки и показываем сообщение
    text = (
        f"✅ Заказ создан! Номер заказа: {order['id']}.\n\n"
        "Ответ придет в ближайшее время."
        if lang == "ru"
        else f"✅ Order created! Order number: {order['id']}.\n\n"
        "You'll get a response shortly."
    )
    await callback.message.edit_text(text)
    await callback.answer()

@dp.callback_query(F.data == "cancel_custom_order")
async def cancel_custom_order_callback(callback: CallbackQuery):
    """Клиент отменил заказ на бит на заказ."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Удаляем кнопки и показываем сообщение
    text = "Заказ отменен." if lang == "ru" else "Order cancelled."
    await callback.message.edit_text(text)
    await callback.answer(text)

@dp.callback_query(F.data == "confirm_mixing_order")
async def confirm_mixing_order_callback(callback: CallbackQuery):
    """Клиент подтвердил заказ на сведение - создаем заказ автоматически."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Создаем заказ автоматически с пустым описанием (будет заполнено позже или можно оставить пустым)
    username = callback.from_user.username or "no_username"
    description = "Заказ создан автоматически"  # Можно изменить на что-то более подходящее
    
    from orders_manager import create_mixing_order
    order = await create_mixing_order(user_id, username, description, None)
    
    # Отправляем заказ в бот заказов
    if orders_bot:
        try:
            from orders_bot import send_order_to_bot
            logging.info(f"Отправка автоматически созданного заказа на сведение №{order['id']} в бот заказов")
            await send_order_to_bot(order, None, orders_bot, ADMIN_ID)
            logging.info(f"Заказ на сведение №{order['id']} успешно отправлен в бот заказов")
        except Exception as e:
            logging.error(f"Ошибка отправки заказа в бот заказов: {e}")
    
    # Сохраняем в старую систему для обратной совместимости
    dp.pending_mixing_orders[user_id] = {
        "description": description,
        "file_id": None,
        "order_id": order["id"],
    }
    
    # Удаляем кнопки и показываем сообщение
    text = (
        f"✅ Заказ создан! Номер заказа: {order['id']}.\n\n"
        "Ответ придет в ближайшее время."
        if lang == "ru"
        else f"✅ Order created! Order number: {order['id']}.\n\n"
        "You'll get a response shortly."
    )
    await callback.message.edit_text(text)
    await callback.answer()

@dp.callback_query(F.data == "cancel_mixing_order")
async def cancel_mixing_order_callback(callback: CallbackQuery):
    """Клиент отменил заказ на сведение."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Удаляем кнопки и показываем сообщение
    text = "Заказ отменен." if lang == "ru" else "Order cancelled."
    await callback.message.edit_text(text)
    await callback.answer(text)

@dp.callback_query(F.data.startswith("client_cancel_custom_"))
async def client_cancel_custom_callback(callback):
    """Клиент отменил заказ на custom beat."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Проверяем, есть ли заказ
    if user_id not in dp.pending_custom_orders:
        if lang == "ru":
            await callback.answer("Заказ не найден или уже обработан.", show_alert=True)
        else:
            await callback.answer("Order not found or already processed.", show_alert=True)
        return
    
    # Удаляем заказ
    order = dp.pending_custom_orders.pop(user_id, {})
    
    # Уведомляем админа
    admin_text = (
        f"⚠️ Клиент отменил заказ на custom beat.\n"
        f"Пользователь: @{callback.from_user.username or 'no_username'} (id={user_id})\n"
        f"Описание: {order.get('description', '-')}"
    )
    # Отправляем уведомление об отмене заказа в бот заказов
    if orders_bot:
        try:
            await orders_bot.send_message(ADMIN_ID, admin_text)
        except Exception as e:
            logging.error(f"Ошибка отправки уведомления в бот заказов: {e}")
    # Не отправляем в основной бот - он только для клиентов
    
    # Подтверждение клиенту
    if lang == "ru":
        client_text = "✅ Заказ отменен."
    else:
        client_text = "✅ Order cancelled."
    
    await callback.message.edit_text(client_text)
    await callback.answer(client_text)


@dp.callback_query(F.data.startswith("offer_another_price_"))
async def offer_another_price_callback(callback):
    """Клиент хочет предложить другую цену после отклонения."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    # Возвращаем пользователя в ожидание цены
    dp.custom_order_waiting_price.add(user_id)
    
    if lang == "ru":
        text = (
            "Напиши новую цену в долларах.\n"
            "Можно также добавить комментарий или условия."
        )
    else:
        text = (
            "Send your new price in dollars.\n"
            "You can also add a short comment or conditions."
        )
    
    await callback.message.answer(text)
    await callback.answer()


@dp.callback_query(F.data.startswith("offer_another_mixing_price_"))
async def offer_another_mixing_price_callback(callback):
    """Клиент хочет предложить другую цену для сведения после отклонения."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    
    logging.info(f"Обработка offer_another_mixing_price для пользователя {user_id}, callback_data: {callback.data}")
    
    # Возвращаем пользователя в ожидание цены
    dp.mixing_order_waiting_price.add(user_id)
    
    if lang == "ru":
        text = (
            "Напиши новую цену в долларах.\n"
            "Можно также добавить комментарий или условия."
        )
    else:
        text = (
            "Send your new price in dollars.\n"
            "You can also add a short comment or conditions."
        )
    
    try:
        # Отправляем сообщение клиенту (используем bot.send_message для надежности)
        await bot.send_message(user_id, text)
        await callback.answer("✅ Готов принять новую цену")
        logging.info(f"Успешно обработан offer_another_mixing_price для пользователя {user_id}")
    except Exception as e:
        logging.error(f"Ошибка в offer_another_mixing_price_callback для пользователя {user_id}: {e}")
        try:
            await callback.answer("❌ Ошибка", show_alert=True)
        except:
            pass


@dp.callback_query(F.data.startswith("admin_offer_price_"))
async def admin_offer_price_callback(callback):
    """Админ хочет предложить свою цену клиенту."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может предлагать цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Сохраняем, что админ сейчас будет предлагать цену этому клиенту
    dp.admin_offering_price[client_user_id] = client_user_id
    logging.info(f"Админ начал предлагать цену клиенту {client_user_id}. admin_offering_price: {dp.admin_offering_price}")
    
    # Получаем информацию о заказе из разных источников
    offer = dp.pending_offers.get(client_user_id, {})
    beat = offer.get("beat", "-")
    is_custom = offer.get("is_custom", False)
    is_mixing = offer.get("is_mixing", False)
    
    # Если нет в pending_offers, проверяем pending_custom_orders
    if not offer or beat == "-":
        order = dp.pending_custom_orders.get(client_user_id, {})
        if order:
            beat = order.get("description", "-")
            is_custom = True
        else:
            # Проверяем pending_mixing_orders
            mixing_order = dp.pending_mixing_orders.get(client_user_id, {})
            if mixing_order:
                beat = mixing_order.get("description", "-")
                is_mixing = True
            else:
                # Если и там нет, берем из purchase_state
                state = dp.purchase_state.get(client_user_id, {})
                beat = state.get("beat", "-")
                is_custom = state.get("is_custom", False)
                is_mixing = state.get("is_mixing", False)
    
    # Форматируем сообщение для админа
    if is_mixing:
        parts = beat.split("\nОписание: ", 1)
        archive_name = parts[0]
        description = parts[1] if len(parts) > 1 else None
        order_text = f"Услуга: Сведение\nАрхив: {archive_name}"
        if description:
            order_text += f"\nОписание: {description}"
    elif is_custom:
        order_text = f"Услуга: Custom Beat\nОписание: {beat}"
    else:
        order_text = f"Бит: {beat}"
    
    await callback.message.answer(
        f"Напиши цену в долларах, которую хочешь предложить клиенту.\n\n"
        f"{order_text}"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_offer_mixing_price_"))
async def admin_offer_mixing_price_callback(callback):
    """Админ хочет предложить свою цену клиенту для сведения."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может предлагать цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=4)[4])
    
    # Сохраняем, что админ сейчас будет предлагать цену этому клиенту
    dp.admin_offering_price[client_user_id] = client_user_id
    logging.info(f"Админ начал предлагать цену для сведения клиенту {client_user_id}. admin_offering_price: {dp.admin_offering_price}")
    
    # Получаем информацию о заказе из разных источников
    offer = dp.pending_offers.get(client_user_id, {})
    beat = offer.get("beat", "-")
    
    # Если нет в pending_offers, проверяем pending_mixing_orders
    if not offer or beat == "-":
        mixing_order = dp.pending_mixing_orders.get(client_user_id, {})
        if mixing_order:
            beat = mixing_order.get("description", "-")
        else:
            # Если и там нет, берем из purchase_state
            state = dp.purchase_state.get(client_user_id, {})
            beat = state.get("beat", "-")
            # Убираем "Сведение: " если оно есть
            if beat.startswith("Сведение: "):
                beat = beat.replace("Сведение: ", "", 1)
    
    await callback.message.answer(
        f"Напиши цену в долларах, которую хочешь предложить клиенту для сведения.\n\n"
        f"Заказ: {beat}"
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("custom_price_accept_"))
async def accept_custom_price_callback(callback):
    """Админ принял цену для кастом-заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Обновляем purchase_state с принятой ценой
    state = dp.purchase_state.get(client_user_id, {})
    accepted_price = offer.get('price', '-')
    # Убираем $ если есть
    accepted_price_clean = accepted_price.replace('$', '').strip()
    state["license"] = f"${accepted_price_clean}"
    state["full_price"] = accepted_price_clean  # Сохраняем полную цену БЕЗ символа $
    
    # Обновляем цену в заказе
    from orders_manager import get_order_by_user_id, update_order_status
    is_custom = offer.get('is_custom', False)
    order = await get_order_by_user_id(client_user_id, "custom_beat" if is_custom else "mixing")
    if order:
        await update_order_status(order["id"], order["type"], order.get("status", "accepted"), price=accepted_price_clean)
    
    # Сообщение клиенту с принятой ценой и способами оплаты
    beat = offer.get('beat', '-')
    
    if lang == "ru":
        if is_custom:
            # Для заказов - делим цену на 2 и предупреждаем о 50/50
            try:
                full_price = float(offer.get('price', '0').replace('$', '').strip())
                first_payment = full_price / 2
                client_text = (
                    "✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Custom Beat\n"
                    f"Описание: {beat}\n"
                    f"Общая цена: {full_price:.0f}\n\n"
                    f"⚠️ Оплата разделена на две части:\n"
                    f"💰 Первая оплата (50%): {first_payment:.0f}\n"
                    f"💰 Вторая оплата (50%): {first_payment:.0f}\n\n"
                    f"Сначала оплати {first_payment:.0f} (50%), после выполнения заказа нужно будет оплатить оставшиеся {first_payment:.0f} (50%).\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            except:
                # Форматируем цену - убираем $ если есть, затем добавляем обратно
                offer_price = offer.get('price', '-')
                price_clean_display = offer_price.replace('$', '').strip()
                price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
                
                client_text = (
                    "✅ Отлично! Я принял твою цену.\n\n"
                    f"Услуга: Custom Beat\n"
                    f"Описание: {beat}\n"
                    f"Цена: {price_display_formatted}\n\n"
                    f"⚠️ Оплата разделена на две части: 50% сейчас, 50% после выполнения заказа.\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
        else:
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            offer_price = offer.get('price', '-')
            price_clean_display = offer_price.replace('$', '').strip()
            price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
            
            client_text = (
                "✅ Отлично! Я принял твою цену.\n\n"
                f"Бит: {beat}\n"
                f"Цена: {price_display_formatted}\n\n"
                "Теперь выбери способ оплаты. После оплаты я начну работу над битом."
            )
    else:
        if is_custom:
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            offer_price = offer.get('price', '-')
            price_clean_display = offer_price.replace('$', '').strip()
            price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
            
            client_text = (
                "✅ Great! I've accepted your price.\n\n"
                f"Service: Custom Beat\n"
                f"Description: {beat}\n"
                f"Price: {price_display_formatted}\n\n"
                "Now choose the payment method. After payment, I'll start working on your beat."
            )
        else:
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            offer_price = offer.get('price', '-')
            price_clean_display = offer_price.replace('$', '').strip()
            price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
            
            client_text = (
                "✅ Great! I've accepted your price.\n\n"
                f"Beat: {beat}\n"
                f"Price: {price_display_formatted}\n\n"
                "Now choose the payment method. After payment, I'll start working on your beat."
            )
    
    payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
    
    try:
        msg = await bot.send_message(client_user_id, client_text, reply_markup=payment_kb)
        # Сохраняем message_id и текст сообщения с выбором способа оплаты
        if client_user_id not in dp.purchase_state:
            dp.purchase_state[client_user_id] = {}
        state = dp.purchase_state[client_user_id]
        state["payment_selection_message_id"] = msg.message_id
        state["payment_selection_message_text"] = client_text  # Сохраняем оригинальный текст
        dp.purchase_state[client_user_id] = state
        
        original_text = callback.message.text or callback.message.caption or "Предложение"
        await callback.message.edit_text(
            f"{original_text}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
        )
        await callback.answer("✅ Цена принята")
    except Exception as e:
        logging.error(f"Ошибка в accept_custom_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("mixing_price_accept_"))
async def accept_mixing_price_callback(callback):
    """Админ принял цену для сведения."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может принимать цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Обновляем purchase_state с принятой ценой
    state = dp.purchase_state.get(client_user_id, {})
    accepted_price = offer.get('price', '-')
    # Убираем $ если есть
    accepted_price_clean = accepted_price.replace('$', '').strip()
    state["license"] = f"${accepted_price_clean}"
    state["full_price"] = accepted_price_clean  # Сохраняем полную цену БЕЗ символа $
    
    # Обновляем цену в заказе
    from orders_manager import get_order_by_user_id, update_order_status
    order = await get_order_by_user_id(client_user_id, "mixing")
    if order:
        await update_order_status(order["id"], "mixing", order.get("status", "accepted"), price=accepted_price_clean)
    
    # Сообщение клиенту с принятой ценой и способами оплаты
    beat = offer.get('beat', '-')
    
    if lang == "ru":
        # Для заказов - делим цену на 2 и предупреждаем о 50/50
        try:
            full_price = float(offer.get('price', '0').replace('$', '').strip())
            first_payment = full_price / 2
            client_text = (
                "✅ Отлично! Я принял твою цену.\n\n"
                f"Услуга: Сведение\n"
                f"Архив: {beat}\n"
                f"Общая цена: ${full_price:.0f}\n\n"
                f"⚠️ Оплата разделена на две части:\n"
                f"💰 Первая оплата (50%): ${first_payment:.0f}\n"
                f"💰 Вторая оплата (50%): ${first_payment:.0f}\n\n"
                f"Сначала оплати ${first_payment:.0f} (50%), после выполнения заказа нужно будет оплатить оставшиеся ${first_payment:.0f} (50%).\n\n"
                "Теперь выбери способ оплаты первой части (50%)."
            )
        except:
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            offer_price = offer.get('price', '-')
            price_clean_display = offer_price.replace('$', '').strip()
            price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
            
            client_text = (
                "✅ Отлично! Я принял твою цену.\n\n"
                f"Услуга: Сведение\n"
                f"Архив: {beat}\n"
                f"Цена: {price_display_formatted}\n\n"
                f"⚠️ Оплата разделена на две части: 50% сейчас, 50% после выполнения заказа.\n\n"
                "Теперь выбери способ оплаты первой части (50%)."
            )
    else:
        # Форматируем цену - убираем $ если есть, затем добавляем обратно
        offer_price = offer.get('price', '-')
        price_clean_display = offer_price.replace('$', '').strip()
        price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else offer_price
        
        client_text = (
            "✅ Great! I've accepted your price.\n\n"
            f"Mixing: {offer.get('beat', '-')}\n"
            f"Price: {price_display_formatted}\n\n"
            "Now choose the payment method. After payment, I'll start working on your mixing."
        )
    
    payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
    
    try:
        msg = await bot.send_message(client_user_id, client_text, reply_markup=payment_kb)
        # Сохраняем message_id и текст сообщения с выбором способа оплаты
        if client_user_id not in dp.purchase_state:
            dp.purchase_state[client_user_id] = {}
        state = dp.purchase_state[client_user_id]
        state["payment_selection_message_id"] = msg.message_id
        state["payment_selection_message_text"] = client_text  # Сохраняем оригинальный текст
        dp.purchase_state[client_user_id] = state
        
        # Пытаемся обновить сообщение - если это файл, используем edit_caption, иначе edit_text
        try:
            if callback.message.document or callback.message.audio:
                # Это сообщение с файлом - редактируем caption
                original_caption = callback.message.caption or "Предложение"
                await callback.message.edit_caption(
                    f"{original_caption}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
                )
            else:
                # Это текстовое сообщение - редактируем текст
                original_text = callback.message.text or "Предложение"
                await callback.message.edit_text(
                    f"{original_text}\n\n✅ Цена принята. Клиенту отправлены способы оплаты."
                )
        except Exception as edit_error:
            # Если не удалось отредактировать, просто отправляем новое сообщение
            logging.warning(f"Не удалось отредактировать сообщение: {edit_error}")
            await bot.send_message(
                ADMIN_ID,
                f"✅ Цена для сведения от пользователя {client_user_id} принята. Клиенту отправлены способы оплаты."
            )
        
        await callback.answer("✅ Цена принята")
    except Exception as e:
        logging.error(f"Ошибка в accept_mixing_price_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("mixing_price_reject_"))
async def reject_mixing_price_callback(callback):
    """Админ отклонил цену для сведения."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сообщение клиенту с кнопкой "Предложить другую цену"
    if lang == "ru":
        client_text = (
            "❌ К сожалению, я не могу принять эту цену.\n\n"
            "Можешь попробовать предложить другую цену или связаться со мной для обсуждения."
        )
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Предложить другую цену", callback_data=f"offer_another_mixing_price_{client_user_id}")],
                [InlineKeyboardButton(text="📞 Связаться", url="https://t.me/rrelement1")]
            ]
        )
    else:
        client_text = (
            "❌ Unfortunately, I can't accept this price.\n\n"
            "You can try to make another offer or contact me to discuss."
        )
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Offer another price", callback_data=f"offer_another_mixing_price_{client_user_id}")],
                [InlineKeyboardButton(text="📞 Contact", url="https://t.me/rrelement1")]
            ]
        )
    
    # Возвращаем пользователя в ожидание цены
    dp.mixing_order_waiting_price.add(client_user_id)
    
    try:
        await bot.send_message(client_user_id, client_text, reply_markup=offer_another_kb)
        
        # Пытаемся обновить сообщение - если это файл, используем edit_caption, иначе edit_text
        try:
            if callback.message.document or callback.message.audio:
                # Это сообщение с файлом - редактируем caption
                original_caption = callback.message.caption or "Предложение"
                await callback.message.edit_caption(
                    f"{original_caption}\n\n❌ Цена отклонена. Клиенту отправлено сообщение с предложением другой цены."
                )
            else:
                # Это текстовое сообщение - редактируем текст
                original_text = callback.message.text or "Предложение"
                await callback.message.edit_text(
                    f"{original_text}\n\n❌ Цена отклонена. Клиенту отправлено сообщение с предложением другой цены."
                )
        except Exception as edit_error:
            # Если не удалось отредактировать, просто отправляем новое сообщение
            logging.warning(f"Не удалось отредактировать сообщение: {edit_error}")
            await bot.send_message(
                ADMIN_ID,
                f"❌ Цена для сведения от пользователя {client_user_id} отклонена. Клиенту отправлено сообщение с предложением другой цены."
            )
        
        await callback.answer("❌ Цена отклонена")
    except Exception as e:
        logging.error(f"Ошибка в reject_mixing_price_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("custom_price_reject_"))
async def reject_custom_price_callback(callback):
    """Админ отклонил цену для кастом-заказа."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отклонять цены.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=3)[3])
    
    # Очищаем состояние предложения цены, если оно было активно
    dp.admin_offering_price.pop(client_user_id, None)
    
    if client_user_id not in dp.pending_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_offers.pop(client_user_id, {})
    lang = dp.user_language.get(client_user_id, "ru")
    
    # Сообщение клиенту с кнопкой "Предложить другую цену"
    if lang == "ru":
        client_text = (
            "❌ К сожалению, я не могу принять эту цену.\n\n"
            "Можешь попробовать предложить другую цену или связаться со мной для обсуждения."
        )
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Предложить другую цену", callback_data=f"offer_another_price_{client_user_id}")],
                [InlineKeyboardButton(text="📞 Связаться", url="https://t.me/rrelement1")]
            ]
        )
    else:
        client_text = (
            "❌ Unfortunately, I can't accept this price.\n\n"
            "You can try to make another offer or contact me to discuss."
        )
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Offer another price", callback_data=f"offer_another_price_{client_user_id}")],
                [InlineKeyboardButton(text="📞 Contact", url="https://t.me/rrelement1")]
            ]
        )
    
    # Возвращаем пользователя в ожидание цены
    dp.custom_order_waiting_price.add(client_user_id)
    
    try:
        await bot.send_message(client_user_id, client_text, reply_markup=offer_another_kb)
        original_text = callback.message.text or callback.message.caption or "Предложение"
        await callback.message.edit_text(
            f"{original_text}\n\n❌ Цена отклонена. Клиенту отправлено сообщение с предложением другой цены."
        )
        await callback.answer("❌ Цена отклонена")
    except Exception as e:
        logging.error(f"Ошибка в accept_custom_order_callback: {e}")
        error_msg = f"❌ Ошибка при отправке сообщения клиенту: {str(e)}"
        await callback.answer(error_msg, show_alert=True)
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(error_msg)


@dp.callback_query(F.data.startswith("crypto_"))
async def crypto_currency_callback(callback):
    """Пользователь выбирает конкретную криптовалюту после Crypto."""
    user_id = callback.from_user.id
    lang = dp.user_language.get(user_id, "ru")
    coin = callback.data.split("_", maxsplit=1)[1]
    
    # Редактируем предыдущее сообщение с выбором криптовалюты, убирая кнопки и обновляя текст
    state = dp.purchase_state.get(user_id, {})
    try:
        if "crypto_selection_message_id" in state:
            completed_text = "✅ Криптовалюта выбрана" if lang == "ru" else "✅ Cryptocurrency selected"
            await callback.message.bot.edit_message_text(
                chat_id=user_id,
                message_id=state["crypto_selection_message_id"],
                text=completed_text,
                reply_markup=None
            )
    except:
        pass

    # Адреса криптовалют
    addresses = {
        "usdt": "TTRyKFc1sEheGypuxmoyjFWZQnFFtRhRwS",
        "btc": "bc1qgglaeahxsvy3qt6fftymqxnq0k5hza2skps399",
        "eth": "0x635566784960694BE437f02c550058546c556235",
        "ltc": "ltc1qs2euxwl577zfhgaxd5ee4qgufhux0q7kkmcm66",
    }
    
    address = addresses.get(coin)
    if not address:
        await callback.answer()
        return

    # Формируем текст с кликабельной ссылкой
    if coin == "usdt":
        coin_name = "USDT"
        network = "TRC20"
    elif coin == "btc":
        coin_name = "BTC"
        network = "BTC"
    elif coin == "eth":
        coin_name = "ETH"
        network = "ERC20"
    elif coin == "ltc":
        coin_name = "LTC"
        network = "LTC"
    else:
        coin_name = coin.upper()
        network = coin.upper()

    # Получаем информацию о заказе/покупке для отображения цены
    state = dp.purchase_state.get(user_id, {})
    beat = state.get("beat", "-")
    lic = state.get("license", "-")
    is_custom = state.get("is_custom", False)
    is_mixing = state.get("is_mixing", False)
    
    # Формируем текст с информацией о цене
    if lang == "ru":
        if is_custom:
            price_text = f"Услуга: Custom Beat\nОписание: {beat}\nЦена: {lic}\n\n"
        elif is_mixing:
            price_text = f"Услуга: Сведение\nАрхив: {beat}\nЦена: {lic}\n\n"
        else:
            price_text = f"Бит: {beat}\nЛицензия: {lic}\n\n"
    else:
        if is_custom:
            price_text = f"Service: Custom Beat\nDescription: {beat}\nPrice: {lic}\n\n"
        elif is_mixing:
            price_text = f"Service: Mixing\nArchive: {beat}\nPrice: {lic}\n\n"
        else:
            price_text = f"Beat: {beat}\nLicense: {lic}\n\n"
    
    # Используем Markdown для удобного копирования адреса
    crypto_text = f"💎 *{coin_name}* ({network})\n`{address}`"
    text = price_text + crypto_text

    paid_button = paid_button_ru if lang == "ru" else paid_button_en

    msg = await callback.message.answer(text, reply_markup=paid_button, parse_mode="Markdown")
    # Сохраняем message_id и текст сообщения с реквизитами и кнопками "Я оплатил"/"Отмена"
    state = dp.purchase_state.get(user_id, {})
    state["payment_details_message_id"] = msg.message_id
    state["payment_details_message_text"] = text  # Сохраняем оригинальный текст
    dp.purchase_state[user_id] = state
    await callback.answer()

# --- Получение чеков / скринов ---
@dp.message(F.content_type.in_(["photo", "document"]))
async def handle_receipt(message: Message):
    user_id = message.from_user.id
    if user_id not in dp.current_payment_users:
        return  # игнорируем чужие файлы

    lang = dp.user_language.get(user_id, "ru")
    state = dp.purchase_state.get(user_id, {})
    
    # Проверяем, есть ли принятая цена из бота покупок
    import json
    import os
    price_update_file = "accepted_price.json"
    if os.path.exists(price_update_file):
        try:
            with open(price_update_file, "r", encoding="utf-8") as f:
                updates = json.load(f)
            if str(user_id) in updates:
                update_data = updates[str(user_id)]
                accepted_price = update_data.get('price', None)
                original_license = update_data.get('license', None)
                # Обновляем purchase_state с принятой ценой
                if accepted_price:
                    # Если есть исходная лицензия (например, "TRACK OUT — $99"), сохраняем тип лицензии
                    if original_license:
                        # Извлекаем тип лицензии (MP3, WAV, TRACK OUT, EXCLUSIVE)
                        license_type = original_license.split(" — ")[0] if " — " in original_license else None
                        if license_type:
                            # Формируем новую лицензию с принятой ценой: "TRACK OUT — $60"
                            state["license"] = f"{license_type} — {accepted_price}"
                        else:
                            # Если не удалось извлечь тип, используем просто цену
                            state["license"] = accepted_price
                    else:
                        # Если исходной лицензии нет, используем просто цену
                        state["license"] = accepted_price
                else:
                    # Если цены нет, оставляем как есть
                    state["license"] = state.get("license", "-")
                state["beat"] = update_data.get("beat", state.get("beat", "-"))
                # Удаляем обновление после использования
                del updates[str(user_id)]
                with open(price_update_file, "w", encoding="utf-8") as f:
                    json.dump(updates, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Ошибка чтения принятой цены: {e}")
    
    is_custom = state.get("is_custom", False)
    is_mixing = state.get("is_mixing", False)
    
    # Проверяем, это вторая оплата для заказа или первая
    is_second_payment = state.get("second_payment", False)
    
    # НЕ обновляем статус автоматически - ждем подтверждения админа
    # НЕ отправляем клиенту сообщение "Спасибо" сразу - отправим после подтверждения админа
    
    # Редактируем сообщение с реквизитами, убирая кнопки "Я оплатил" и "Отмена"
    try:
        if "payment_details_message_id" in state:
            # Сначала пытаемся убрать кнопки
            await bot.edit_message_reply_markup(
                chat_id=user_id,
                message_id=state["payment_details_message_id"],
                reply_markup=None
            )
            # Затем пытаемся обновить текст (может не сработать для Markdown, но попробуем)
            try:
                completed_text = "✅ Чек отправлен" if lang == "ru" else "✅ Receipt sent"
                await bot.edit_message_text(
                    chat_id=user_id,
                    message_id=state["payment_details_message_id"],
                    text=completed_text,
                    reply_markup=None
                )
            except:
                # Если не удалось изменить текст (например, из-за Markdown), просто оставляем без кнопок
                pass
    except:
        pass
    
    # Сообщаем клиенту, что чек получен и ожидает проверки
    if is_custom or is_mixing:
        if is_second_payment:
            waiting_text = (
                "✅ Чек получен! Ожидаю подтверждения второй оплаты (50%). После подтверждения заказ будет полностью оплачен."
                if lang == "ru"
                else "✅ Receipt received! Waiting for second payment (50%) confirmation. After confirmation, the order will be fully paid."
            )
        else:
            waiting_text = (
                "✅ Чек получен! Ожидаю подтверждения первой оплаты (50%). После подтверждения я начну работу."
                if lang == "ru"
                else "✅ Receipt received! Waiting for first payment (50%) confirmation. After confirmation, I'll start working."
            )
    else:
        waiting_text = (
            "✅ Чек получен! Ожидаю подтверждения оплаты. После проверки вы получите ваш файл."
            if lang == "ru"
            else "✅ Receipt received! Waiting for payment confirmation. After verification you'll get your file."
        )
    
    await message.answer(waiting_text)

    state = dp.purchase_state.get(user_id, {})
    beat = state.get("beat", "-")
    lic = state.get("license", "-")
    is_custom = state.get("is_custom", False)
    is_mixing = state.get("is_mixing", False)

    # Логируем данные из state для отладки
    logging.info(f"handle_receipt: user_id={user_id}, beat={beat}, license={lic}, state={state}")

    # Сначала проверяем purchase_state - если там явно указано is_custom или is_mixing, это заказ
    # Если нет, проверяем наличие покупки готового бита ПЕРЕД проверкой заказов
    order = None
    
    # Если в purchase_state явно указано, что это заказ - используем это
    if is_custom or is_mixing:
        # Проверяем наличие активного заказа
        from orders_manager import get_order_by_user_id, get_beats_purchase_by_user_id
        if is_custom:
            order_custom = await get_order_by_user_id(user_id, "custom_beat")
            if order_custom and order_custom.get("status") not in ["rejected", "completed"]:
                order = order_custom
                logging.info(f"Найден активный заказ на кастом-бит: order_id={order['id']}, type={order['type']}, status={order.get('status')}")
            else:
                # Заказ завершен или отклонен - сбрасываем флаг
                is_custom = False
                order = None
        elif is_mixing:
            order_mixing = await get_order_by_user_id(user_id, "mixing")
            if order_mixing and order_mixing.get("status") not in ["rejected", "completed"]:
                order = order_mixing
                logging.info(f"Найден активный заказ на сведение: order_id={order['id']}, type={order['type']}, status={order.get('status')}")
            else:
                # Заказ завершен или отклонен - сбрасываем флаг
                is_mixing = False
                order = None
    
    # Если это не заказ, проверяем наличие покупки готового бита
    if not (is_custom or is_mixing):
        from orders_manager import get_beats_purchase_by_user_id, get_order_by_user_id
        purchase = await get_beats_purchase_by_user_id(user_id)
        if purchase:
            logging.info(f"Найдена покупка готового бита: purchase_id={purchase['id']}, status={purchase.get('status')}")
            # Это покупка готового бита, не заказ
            order = None
        else:
            # Проверяем наличие активных заказов (на случай, если purchase_state не обновлен)
            order_custom = await get_order_by_user_id(user_id, "custom_beat")
            order_mixing = await get_order_by_user_id(user_id, "mixing")
            
            if order_custom and order_custom.get("status") not in ["rejected", "completed"]:
                is_custom = True
                order = order_custom
                # Обновляем purchase_state
                state["is_custom"] = True
                state["beat"] = order.get('description', state.get('beat', '-'))
                dp.purchase_state[user_id] = state
                logging.info(f"Найден активный заказ на кастом-бит (без флага в purchase_state): order_id={order['id']}, type={order['type']}")
            elif order_mixing and order_mixing.get("status") not in ["rejected", "completed"]:
                is_mixing = True
                order = order_mixing
                # Обновляем purchase_state
                state["is_mixing"] = True
                state["beat"] = order.get('description', state.get('beat', '-'))
                state["order_id"] = order["id"]
                dp.purchase_state[user_id] = state
                logging.info(f"Найден активный заказ на сведение (без флага в purchase_state): order_id={order['id']}, type={order['type']}")
            else:
                order = None
                logging.info(f"Заказ не найден или завершен - это покупка готового бита")
    
    logging.info(f"Итоговое определение типа для user_id={user_id}: is_custom={is_custom}, is_mixing={is_mixing}, order={order is not None}")

    if is_custom or is_mixing:
        logging.info(f"Чек будет отправлен в БОТ ЗАКАЗОВ (orders_bot)")
        if not order:
            logging.error(f"КРИТИЧЕСКАЯ ОШИБКА: Заказ не найден для пользователя {user_id}, но is_custom={is_custom}, is_mixing={is_mixing}")
            await message.answer(
                "❌ Ошибка: заказ не найден. Пожалуйста, свяжитесь с администратором."
                if lang == "ru"
                else "❌ Error: order not found. Please contact administrator."
            )
            return
        
        order_num = f" {order['id']}"
        
        # Проверяем, это вторая оплата или первая
        if is_second_payment:
            payment_type = "ВТОРАЯ ОПЛАТА (50%)"
            payment_status = "Оплачено: 100% (заказ полностью оплачен)"
            work_text = "✅ Заказ полностью оплачен!"
        else:
            payment_type = "ПЕРВАЯ ОПЛАТА (50%)"
            payment_status = "Оплачено: 50% (первая оплата)"
            work_text = "✅ Можно начинать работу!"

        # Создаем caption для заказа
        if is_custom:
            caption = (
                f"💰 {payment_type} ЗАКАЗА БИТА НА ЗАКАЗ{order_num}\n"
                f"Пользователь: @{message.from_user.username or 'no_username'} (id={user_id})\n"
                f"Описание: {beat}\n"
                f"Цена: {lic}\n"
                f"{payment_status}"
            )
        elif is_mixing:  # is_mixing
            # Разделяем архив и описание, если есть
            parts = beat.split("\nОписание: ", 1)
            archive_name = parts[0]
            description = parts[1] if len(parts) > 1 else None
            
            caption = (
                f"💰 {payment_type} ЗАКАЗА НА СВЕДЕНИЕ{order_num}\n"
                f"Пользователь: @{message.from_user.username or 'no_username'} (id={user_id})\n"
                f"Архив: {archive_name}\n"
                f"Цена: {lic}\n"
                f"{payment_status}"
            )
            if description:
                caption += f"\nОписание: {description}"
    
    # Отправляем чек в соответствующий бот с кнопками подтверждения
    if is_custom or is_mixing:
        # Это заказ - отправляем в бот заказов
        if not orders_bot:
            logging.error(f"Бот заказов не инициализирован! Чек от пользователя {user_id} не может быть отправлен.")
            await message.answer(
                "❌ Ошибка: бот заказов не настроен. Пожалуйста, свяжитесь с администратором."
                if lang == "ru"
                else "❌ Error: orders bot is not configured. Please contact administrator."
            )
        else:
            try:
                from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
                
                if not order:
                    logging.error(f"Заказ не найден для пользователя {user_id}. is_custom={is_custom}, is_mixing={is_mixing}")
                    await message.answer(
                        "❌ Ошибка: заказ не найден. Пожалуйста, свяжитесь с администратором."
                        if lang == "ru"
                        else "❌ Error: order not found. Please contact administrator."
                    )
                    return
                
                # Создаем кнопки подтверждения/отклонения
                if is_second_payment:
                    confirm_data = f"confirm_payment_{order['type']}_{order['id']}_{user_id}_second"
                else:
                    confirm_data = f"confirm_payment_{order['type']}_{order['id']}_{user_id}_first"
                
                reject_data = f"reject_payment_{order['type']}_{order['id']}_{user_id}"
                
                payment_confirm_kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=confirm_data)],
                        [InlineKeyboardButton(text="❌ Отклонить", callback_data=reject_data)]
                    ]
                )
                
                # Обновляем caption, чтобы указать, что оплата ожидает подтверждения
                if is_second_payment:
                    caption = caption.replace("Оплачено: 100% (заказ полностью оплачен)", "⏳ Ожидает подтверждения второй оплаты (50%)")
                else:
                    caption = caption.replace("Оплачено: 50% (первая оплата)", "⏳ Ожидает подтверждения первой оплаты (50%)")
                
                logging.info(f"Отправка чека для заказа {order['type']} №{order['id']} в бот заказов. is_custom={is_custom}, is_mixing={is_mixing}")
                
                # Скачиваем и отправляем файл в бот заказов, так как file_id из одного бота нельзя использовать в другом
                try:
                    from aiogram.types import FSInputFile, BufferedInputFile
                    import io
                    
                    if message.photo:
                        # Скачиваем фото
                        photo = message.photo[-1]
                        file = await bot.get_file(photo.file_id)
                        photo_bytes = await bot.download_file(file.file_path)
                        
                        # Создаем InputFile из байтов
                        photo_input = BufferedInputFile(
                            photo_bytes.read(),
                            filename="receipt.jpg"
                        )
                        
                        # Отправляем в бот заказов
                        await orders_bot.send_photo(
                            chat_id=ADMIN_ID,
                            photo=photo_input,
                            caption=caption,
                            reply_markup=payment_confirm_kb
                        )
                        logging.info(f"Чек (фото) для заказа {order['type']} №{order['id']} успешно отправлен в бот заказов")
                    elif message.document:
                        # Скачиваем документ
                        file = await bot.get_file(message.document.file_id)
                        doc_bytes = await bot.download_file(file.file_path)
                        
                        # Получаем имя файла
                        filename = message.document.file_name or "receipt"
                        
                        # Создаем InputFile из байтов
                        doc_input = BufferedInputFile(
                            doc_bytes.read(),
                            filename=filename
                        )
                        
                        # Отправляем в бот заказов
                        await orders_bot.send_document(
                            chat_id=ADMIN_ID,
                            document=doc_input,
                            caption=caption,
                            reply_markup=payment_confirm_kb
                        )
                        logging.info(f"Чек (документ) для заказа {order['type']} №{order['id']} успешно отправлен в бот заказов")
                    else:
                        # Если это не фото и не документ, отправляем только текст
                        await orders_bot.send_message(
                            chat_id=ADMIN_ID,
                            text=caption,
                            reply_markup=payment_confirm_kb
                        )
                        logging.info(f"Чек (текст) для заказа {order['type']} №{order['id']} успешно отправлен в бот заказов")
                except Exception as download_error:
                    logging.error(f"Ошибка скачивания и отправки файла: {download_error}")
                    import traceback
                    logging.error(traceback.format_exc())
                    raise download_error
            except Exception as e:
                logging.error(f"Ошибка отправки чека в бот заказов: {e}")
                import traceback
                logging.error(traceback.format_exc())
                await message.answer(
                    "❌ Ошибка отправки чека. Пожалуйста, попробуйте еще раз или свяжитесь с администратором."
                    if lang == "ru"
                    else "❌ Error sending receipt. Please try again or contact administrator."
                )
        # Не отправляем в основной бот - он только для клиентов
    else:
        # Это покупка готового бита - НЕ обновляем статус автоматически, ждем подтверждения админа
        logging.info(f"Чек будет отправлен в БОТ ПОКУПОК (purchases_bot)")
        logging.info(f"purchases_bot инициализирован: {purchases_bot is not None}")
        username = message.from_user.username or "no_username"
        
        # Ищем активную покупку для этого пользователя (только pending_payment - ожидающие оплаты)
        from orders_manager import get_all_beats_purchases, create_beats_purchase
        purchases = await get_all_beats_purchases()
        logging.info(f"Найдено покупок всего: {len(purchases)}")
        purchase = None
        # Ищем последнюю активную покупку для этого пользователя (только pending_payment - ожидающие чека)
        for p in reversed(purchases):
            if p["user_id"] == user_id and p["status"] == "pending_payment":
                purchase = p
                logging.info(f"Найдена активная покупка №{purchase['id']} со статусом {purchase['status']}")
                break
        
        if not purchase:
            logging.info(f"Активная покупка не найдена для user_id={user_id}, будет создана новая")
        
        # Если активной покупки нет, создаем новую
        if not purchase:
            # Проверяем, что данные о бите и лицензии есть в state
            if beat == "-" or lic == "-":
                logging.warning(f"Данные о бите или лицензии отсутствуют в state: beat={beat}, license={lic}, state={state}")
                # Пытаемся получить данные из state еще раз
                beat = state.get("beat", "-")
                lic = state.get("license", "-")
                if beat == "-" or lic == "-":
                    logging.error(f"Не удалось получить данные о бите или лицензии для пользователя {user_id}")
                    await message.answer(
                        "❌ Ошибка: не найдены данные о бите или лицензии. Пожалуйста, начните процесс покупки заново."
                        if lang == "ru"
                        else "❌ Error: beat or license data not found. Please start the purchase process again."
                    )
                    return
            
            # Создаем новую покупку (со статусом "pending" - ожидает оплаты)
            purchase = await create_beats_purchase(user_id, username, beat, lic, lic)
            logging.info(f"Создана новая покупка №{purchase['id']}: лицензия={lic}, бит={beat}, статус={purchase.get('status')}")
        else:
            # Если активная покупка существует, обновляем информацию о лицензии и бите
            # Это важно, если клиент изменил тип лицензии (например, с MP3 на WAV)
            # Примечание: beat, license и price обычно не меняются после создания покупки,
            # но если нужно обновить, можно добавить соответствующие поля в update_beats_purchase_status
            old_license = purchase.get("license", "")
            old_beat = purchase.get("beat", "")
            # Не сбрасываем статус payment_rejected - отмененная покупка должна остаться отмененной
            if lic != "-" or beat != "-":
                logging.info(f"Обновлена информация о покупке №{purchase['id']}: лицензия изменена с '{old_license}' на '{lic}', бит изменен с '{old_beat}' на '{beat}'")
        
        # Используем данные из покупки, если они есть, иначе из state
        purchase_beat = purchase.get("beat", beat) if purchase.get("beat") and purchase.get("beat") != "-" else beat
        purchase_license = purchase.get("license", lic) if purchase.get("license") and purchase.get("license") != "-" else lic
        
        # Если все еще "-", пытаемся получить из state
        if purchase_beat == "-" or purchase_license == "-":
            purchase_beat = state.get("beat", purchase_beat) if state.get("beat") and state.get("beat") != "-" else purchase_beat
            purchase_license = state.get("license", purchase_license) if state.get("license") and state.get("license") != "-" else purchase_license
        
        # Форматируем лицензию и цену
        license_text, price_text = format_license_and_price(purchase_license)
        
        from orders_manager import format_purchase_number
        purchase_display_num = format_purchase_number(purchase['id'], purchase.get('created_at'))
        caption = (
            f"💿 Покупка бита {purchase_display_num}\n"
            f"Пользователь: @{username} (id={user_id})\n"
            f"Beat: {purchase_beat}\n"
            f"Лицензия: {license_text}\n"
            f"Цена: {price_text if price_text else purchase_license}\n\n"
            f"⏳ Ожидает подтверждения оплаты."
        )

        # Отправляем покупку в бот покупок с кнопками подтверждения
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        
        payment_confirm_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"confirm_payment_{purchase['id']}_{user_id}")],
                [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_payment_{purchase['id']}_{user_id}")]
            ]
        )
    
        if not purchases_bot:
            logging.error(f"purchases_bot не инициализирован! Проверьте PURCHASES_BOT_TOKEN в .env")
            await message.answer(
                "❌ Ошибка: бот покупок не инициализирован. Свяжитесь с администратором."
                if lang == "ru"
                else "❌ Error: purchases bot not initialized. Contact administrator."
            )
            return
        
        logging.info(f"Перед отправкой: purchase_id={purchase['id']}, purchases_bot={purchases_bot}, ADMIN_ID={ADMIN_ID}")
        
        try:
            from aiogram.types import BufferedInputFile
            
            logging.info(f"Отправка чека для покупки готового бита №{purchase['id']} в бот покупок (chat_id={ADMIN_ID})")
            
            if message.photo:
                # Скачиваем фото
                photo = message.photo[-1]
                file = await bot.get_file(photo.file_id)
                photo_bytes = await bot.download_file(file.file_path)
                
                # Создаем InputFile из байтов
                photo_input = BufferedInputFile(
                    photo_bytes.read(),
                    filename="receipt.jpg"
                )
                
                msg = await purchases_bot.send_photo(
                    chat_id=ADMIN_ID,
                    photo=photo_input, 
                    caption=caption,
                    reply_markup=payment_confirm_kb,
                    parse_mode="HTML"
                )
                # Сохраняем message_id для последующего обновления кнопок
                from orders_manager import update_beats_purchase_status
                await update_beats_purchase_status(purchase['id'], purchase.get('status', 'pending_payment'), client_message_id=msg.message_id)
                logging.info(f"Чек (фото) для покупки №{purchase['id']} успешно отправлен в бот покупок, message_id={msg.message_id}")
            elif message.document:
                # Скачиваем документ
                file = await bot.get_file(message.document.file_id)
                doc_bytes = await bot.download_file(file.file_path)
                
                # Получаем имя файла
                filename = message.document.file_name or "receipt"
                
                # Создаем InputFile из байтов
                doc_input = BufferedInputFile(
                    doc_bytes.read(),
                    filename=filename
                )
                
                msg = await purchases_bot.send_document(
                    chat_id=ADMIN_ID,
                    document=doc_input, 
                    caption=caption,
                    reply_markup=payment_confirm_kb,
                    parse_mode="HTML"
                )
                # Сохраняем message_id для последующего обновления кнопок
                from orders_manager import update_beats_purchase_status
                await update_beats_purchase_status(purchase['id'], purchase.get('status', 'pending_payment'), client_message_id=msg.message_id)
                logging.info(f"Чек (документ) для покупки №{purchase['id']} успешно отправлен в бот покупок, message_id={msg.message_id}")
            else:
                # Если это не фото и не документ, отправляем только текст
                msg = await purchases_bot.send_message(
                    chat_id=ADMIN_ID,
                    text=caption,
                    reply_markup=payment_confirm_kb,
                    parse_mode="HTML"
                )
                # Сохраняем message_id для последующего обновления кнопок
                from orders_manager import update_beats_purchase_status
                await update_beats_purchase_status(purchase['id'], purchase.get('status', 'pending_payment'), client_message_id=msg.message_id)
                logging.info(f"Чек (текст) для покупки №{purchase['id']} успешно отправлен в бот покупок, message_id={msg.message_id}")
        except Exception as e:
                logging.error(f"Ошибка отправки покупки в бот покупок: {e}")
                import traceback
                logging.error(traceback.format_exc())
                logging.error(f"Покупка от @{username} (ID: {user_id}) не отправлена админу. Проверьте бот покупок.")
                await message.answer(
                    "❌ Ошибка отправки чека. Пожалуйста, попробуйте еще раз или свяжитесь с администратором."
                    if lang == "ru"
                    else "❌ Error sending receipt. Please try again or contact administrator."
                )

    # НЕ очищаем current_payment_users и purchase_state сразу - они будут очищены после подтверждения админа
    # dp.current_payment_users.discard(user_id)
    # dp.purchase_state.pop(user_id, None)


@dp.callback_query(F.data.startswith("client_accept_price_"))
async def client_accept_price_callback(callback):
    """Клиент принял цену, предложенную админом."""
    user_id = callback.from_user.id
    
    if user_id not in dp.pending_admin_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_admin_offers.pop(user_id, {})
    price = offer.get("price", "-")
    beat = offer.get("beat", "-")
    is_mixing = offer.get("is_mixing", False)
    is_custom = offer.get("is_custom", False)
    lang = dp.user_language.get(user_id, "ru")
    
    # Убираем $ если есть и сохраняем полную цену
    price_clean = price.replace('$', '').strip()
    
    # Обновляем purchase_state
    if is_mixing:
        dp.purchase_state[user_id] = {
            "beat": beat,
            "license": price_clean,
            "is_mixing": True,
            "full_price": price_clean,  # Сохраняем полную цену
        }
    elif is_custom:
        dp.purchase_state[user_id] = {
            "beat": beat,
            "license": price_clean,
            "is_custom": True,
            "full_price": price_clean,  # Сохраняем полную цену
        }
    else:
        dp.purchase_state[user_id] = {
            "beat": beat,
            "license": price_clean,
        }
    
    # Сообщение клиенту с способами оплаты
    # Для заказов (custom_beat и mixing) показываем информацию о 50/50 оплате
    if lang == "ru":
        if is_mixing or is_custom:
            # Для заказов - делим цену на 2 и предупреждаем о 50/50
            try:
                full_price = float(price_clean)
                first_payment = full_price / 2
                service_name = "Сведение" if is_mixing else "Custom Beat"
                item_name = "Архив" if is_mixing else "Описание"
                client_text = (
                    "✅ Отлично! Я принял твоё предложение по цене.\n\n"
                    f"Услуга: {service_name}\n"
                    f"{item_name}: {beat}\n"
                    f"Общая цена: {full_price:.0f}\n\n"
                    f"⚠️ Оплата разделена на две части:\n"
                    f"💰 Первая оплата (50%): {first_payment:.0f}\n"
                    f"💰 Вторая оплата (50%): {first_payment:.0f}\n\n"
                    f"Сначала оплати {first_payment:.0f} (50%), после выполнения заказа нужно будет оплатить оставшиеся {first_payment:.0f} (50%).\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
            except:
                service_name = "Сведение" if is_mixing else "Custom Beat"
                item_name = "Архив" if is_mixing else "Описание"
                # Форматируем цену - убираем $ если есть, затем добавляем обратно
                price_clean_display = price.replace('$', '').strip()
                price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else price
                
                client_text = (
                    "✅ Отлично! Я принял твоё предложение по цене.\n\n"
                    f"Услуга: {service_name}\n"
                    f"{item_name}: {beat}\n"
                    f"Цена: {price_display_formatted}\n\n"
                    f"⚠️ Оплата разделена на две части: 50% сейчас, 50% после выполнения заказа.\n\n"
                    "Теперь выбери способ оплаты первой части (50%)."
                )
        else:
            # Форматируем цену - убираем $ если есть, затем добавляем обратно
            price_clean_display = price.replace('$', '').strip()
            price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else price
            
            client_text = (
                "✅ Отлично! Цена принята.\n\n"
                f"Бит: {beat}\n"
                f"Лицензия: {price_display_formatted}\n\n"
                "Теперь выбери способ оплаты. После оплаты вы получите ваш файл."
            )
    else:
        if is_mixing or is_custom:
            try:
                full_price = float(price_clean)
                first_payment = full_price / 2
                service_name = "Mixing" if is_mixing else "Custom Beat"
                item_name = "Archive" if is_mixing else "Description"
                client_text = (
                    "✅ Great! I've accepted your price offer.\n\n"
                    f"Service: {service_name}\n"
                    f"{item_name}: {beat}\n"
                    f"Total price: {full_price:.0f}\n\n"
                    f"⚠️ Payment is split into two parts:\n"
                    f"💰 First payment (50%): {first_payment:.0f}\n"
                    f"💰 Second payment (50%): {first_payment:.0f}\n\n"
                    f"First pay {first_payment:.0f} (50%), after order completion you'll need to pay the remaining {first_payment:.0f} (50%).\n\n"
                    "Now choose the payment method for the first part (50%)."
                )
            except:
                service_name = "Mixing" if is_mixing else "Custom Beat"
                item_name = "Archive" if is_mixing else "Description"
                # Форматируем цену - убираем $ если есть, затем добавляем обратно
                price_clean_display = price.replace('$', '').strip()
                price_display_formatted = f"${price_clean_display}" if price_clean_display and price_clean_display != '-' else price
                
                client_text = (
                    "✅ Great! I've accepted your price offer.\n\n"
                    f"Service: {service_name}\n"
                    f"{item_name}: {beat}\n"
                    f"Price: {price_display_formatted}\n\n"
                    f"⚠️ Payment is split into two parts: 50% now, 50% after order completion.\n\n"
                    "Now choose the payment method for the first part (50%)."
                )
        else:
            client_text = (
                "✅ Great! Price accepted.\n\n"
                f"Beat: {beat}\n"
                f"License: ${price}\n\n"
                "Now choose the payment method. After payment you'll get your file."
            )
    
    payment_kb = payment_inline_ru if lang == "ru" else payment_inline_en
    
    try:
        await callback.message.edit_text(client_text, reply_markup=payment_kb)
        # Сохраняем message_id и текст сообщения с выбором способа оплаты
        if user_id not in dp.purchase_state:
            dp.purchase_state[user_id] = {}
        state = dp.purchase_state[user_id]
        state["payment_selection_message_id"] = callback.message.message_id
        state["payment_selection_message_text"] = client_text  # Сохраняем оригинальный текст
        dp.purchase_state[user_id] = state
        
        await callback.answer("✅ Цена принята")
        
        # Уведомление админу
        # Отправляем уведомление в бот заказов
        if orders_bot:
            try:
                await orders_bot.send_message(ADMIN_ID, f"✅ Клиент {user_id} принял предложенную цену ${price}")
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления в бот заказов: {e}")
        # Не отправляем в основной бот - он только для клиентов
    except Exception as e:
        logging.error(f"Ошибка в client_accept_price_callback: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("client_reject_price_"))
async def client_reject_price_callback(callback):
    """Клиент отклонил цену, предложенную админом."""
    user_id = callback.from_user.id
    
    if user_id not in dp.pending_admin_offers:
        await callback.answer("Предложение не найдено.", show_alert=True)
        return
    
    offer = dp.pending_admin_offers.pop(user_id, {})
    price = offer.get("price", "-")
    is_mixing = offer.get("is_mixing", False)
    lang = dp.user_language.get(user_id, "ru")
    
    # Сообщение клиенту
    if lang == "ru":
        client_text = (
            "❌ Цена отклонена.\n\n"
            "Можешь предложить другую цену или связаться со мной для обсуждения."
        )
        callback_data = f"offer_another_mixing_price_{user_id}" if is_mixing else f"offer_another_price_{user_id}"
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Предложить другую цену", callback_data=callback_data)],
                [InlineKeyboardButton(text="📞 Связаться", url="https://t.me/rrelement1")]
            ]
        )
    else:
        client_text = (
            "❌ Price rejected.\n\n"
            "You can make another offer or contact me to discuss."
        )
        callback_data = f"offer_another_mixing_price_{user_id}" if is_mixing else f"offer_another_price_{user_id}"
        offer_another_kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💵 Offer another price", callback_data=callback_data)],
                [InlineKeyboardButton(text="📞 Contact", url="https://t.me/rrelement1")]
            ]
        )
    
    # Возвращаем пользователя в ожидание цены
    if is_mixing:
        dp.mixing_order_waiting_price.add(user_id)
    else:
        dp.custom_order_waiting_price.add(user_id)
    
    try:
        await callback.message.edit_text(client_text, reply_markup=offer_another_kb)
        await callback.answer("❌ Цена отклонена")
        
        # Уведомление админу
        # Отправляем уведомление в бот заказов
        if orders_bot:
            try:
                await orders_bot.send_message(ADMIN_ID, f"❌ Клиент {user_id} отклонил предложенную цену ${price}")
            except Exception as e:
                logging.error(f"Ошибка отправки уведомления в бот заказов: {e}")
        # Не отправляем в основной бот - он только для клиентов
    except Exception as e:
        logging.error(f"Ошибка в client_reject_price_callback: {e}")
        await callback.answer("❌ Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("send_file_"))
async def send_file_callback(callback):
    """Админ нажал кнопку 'Отправить файл'."""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Только админ может отправлять файлы.", show_alert=True)
        return
    
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    
    # Сохраняем, что админ сейчас будет отправлять файл этому клиенту
    dp.admin_sending_file = client_user_id
    
    # Проверяем, это сведение или обычный заказ
    state = dp.purchase_state.get(client_user_id, {})
    is_mixing = state.get("is_mixing", False)
    is_custom = state.get("is_custom", False)
    
    if is_mixing:
        await callback.message.answer(
            "Отправь готовый архив (ZIP/RAR) со сведенным треком для клиента:"
        )
    elif is_custom:
        await callback.message.answer(
            "Отправь готовый бит (mp3, wav) для клиента:"
        )
    else:
        await callback.message.answer(
            "Отправь файл (mp3, wav или архив), который нужно отправить клиенту:"
        )
    await callback.answer()

@dp.callback_query(F.data.startswith("contact_admin_"))
async def contact_admin_callback(callback):
    """Клиент нажал кнопку 'Связаться' для правок."""
    user_id = callback.from_user.id
    client_user_id = int(callback.data.split("_", maxsplit=2)[2])
    lang = dp.user_language.get(user_id, "ru")
    
    # Проверяем, что это клиент, который нажал кнопку
    if user_id != client_user_id:
        error_text = "Ошибка доступа." if lang == "ru" else "Access error."
        await callback.answer(error_text, show_alert=True)
        return
    
    # Редактируем сообщение с файлом, заменяя кнопку callback на кнопку с прямой ссылкой
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
    contact_text = "Связаться" if lang == "ru" else "Contact"
    
    contact_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=contact_text, url="https://t.me/rrelement1")]
        ]
    )
    
    try:
        # Пытаемся отредактировать сообщение с файлом (может быть фото, документ или аудио)
        if callback.message.photo:
            # Это фото - редактируем caption и кнопки
            caption = callback.message.caption or None
            await callback.message.edit_caption(caption=caption, reply_markup=contact_kb)
        elif callback.message.document or callback.message.audio or callback.message.voice:
            # Это документ, аудио или голосовое - редактируем caption и кнопки
            caption = callback.message.caption or None
            await callback.message.edit_caption(caption=caption, reply_markup=contact_kb)
        else:
            # Это текстовое сообщение - редактируем текст и кнопки
            text = callback.message.text or None
            await callback.message.edit_text(text=text, reply_markup=contact_kb)
    except Exception as e:
        # Если не удалось отредактировать, отправляем новое сообщение (fallback)
        logging.error(f"Ошибка редактирования сообщения: {e}")
        await callback.message.answer(contact_text, reply_markup=contact_kb)
    
    await callback.answer()


# Общий обработчик ошибок для всех обработчиков
@dp.errors()
async def error_handler(update, exception):
    """
    Глобальный обработчик ошибок для всех обработчиков.
    """
    try:
        # Проверяем, что exception действительно передан
        if exception is None:
            logging.error(f"error_handler вызван без exception. Update: {update}")
            return True
        
        if update and hasattr(update, 'message') and update.message:
            user_id = update.message.from_user.id
            lang = dp.user_language.get(user_id, "ru")
            
            # Логируем ошибку
            import traceback
            error_details = traceback.format_exc()
            logging.error(f"Глобальная ошибка: {exception}\n{error_details}")
            
            # Отправляем понятное сообщение пользователю
            error_text = get_error_message(exception, "general", lang)
            await safe_send_message(bot, user_id, error_text, lang)
            
            # Уведомляем админа о критической ошибке
            await safe_send_message(
                bot, 
                ADMIN_ID, 
                f"⚠️ Критическая ошибка у пользователя {user_id}:\n{str(exception)}"
            )
        elif update and hasattr(update, 'callback_query') and update.callback_query:
            # Обработка ошибок в callback-запросах
            user_id = update.callback_query.from_user.id
            lang = dp.user_language.get(user_id, "ru")
            logging.error(f"Ошибка в callback: {exception}")
            
            try:
                await update.callback_query.answer(
                    "❌ Произошла ошибка. Попробуй снова или используй /cancel",
                    show_alert=True
                )
            except:
                pass
    except Exception as e:
        logging.error(f"Ошибка в error_handler: {e}")
    
    return True  # Возвращаем True, чтобы ошибка считалась обработанной


async def main():
    from database import init_db
    from orders_manager import get_all_user_languages
    
    # Инициализируем БД при запуске
    await init_db()
    
    # Загружаем языки пользователей из БД в память для быстрого доступа
    try:
        languages = await get_all_user_languages()
        dp.user_language.update(languages)
        logging.info(f"Загружено языков пользователей из БД: {len(languages)}")
    except Exception as e:
        logging.error(f"Ошибка загрузки языков пользователей: {e}")
    
    try:
        await dp.start_polling(bot)
    except Exception as e:
        logging.error(f"Критическая ошибка бота: {e}")
        # Не отправляем уведомления админу в основной бот - он только для клиентов
        logging.error(f"⚠️ Бот остановлен из-за ошибки: {str(e)}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
