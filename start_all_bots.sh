#!/bin/bash

# Скрипт для запуска всех ботов на Linux/сервере
# Использование: ./start_all_bots.sh

echo "Starting all bots..."
echo ""

# Проверяем наличие .env файла
if [ ! -f .env ]; then
    echo "❌ Ошибка: файл .env не найден!"
    echo "Создайте файл .env на основе .env.example"
    exit 1
fi

# Проверяем наличие виртуального окружения (опционально)
if [ -d "venv" ]; then
    echo "Активируем виртуальное окружение..."
    source venv/bin/activate
fi

# Функция для запуска бота в фоне
start_bot() {
    local bot_name=$1
    local bot_file=$2
    
    echo "Запуск $bot_name..."
    nohup python "$bot_file" > "logs/${bot_name}.log" 2>&1 &
    echo $! > "pids/${bot_name}.pid"
    echo "✅ $bot_name запущен (PID: $(cat pids/${bot_name}.pid))"
    sleep 2
}

# Создаем директории для логов и PID файлов
mkdir -p logs pids

# Запускаем ботов
start_bot "main_bot" "elementBot.py"
start_bot "orders_bot" "orders_bot.py"
start_bot "purchases_bot" "beats_purchases_bot.py"

echo ""
echo "✅ Все боты запущены!"
echo "Логи находятся в директории logs/"
echo "PID файлы находятся в директории pids/"
echo ""
echo "Для остановки ботов используйте: ./stop_all_bots.sh"




