#!/bin/bash

# Скрипт для остановки всех ботов
# Использование: ./stop_all_bots.sh

echo "Stopping all bots..."
echo ""

if [ ! -d "pids" ]; then
    echo "❌ Директория pids не найдена. Боты могут быть не запущены."
    exit 1
fi

# Функция для остановки бота
stop_bot() {
    local bot_name=$1
    local pid_file="pids/${bot_name}.pid"
    
    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "Остановка $bot_name (PID: $pid)..."
            kill "$pid"
            rm "$pid_file"
            echo "✅ $bot_name остановлен"
        else
            echo "⚠️  $bot_name уже не запущен (PID: $pid)"
            rm "$pid_file"
        fi
    else
        echo "⚠️  PID файл для $bot_name не найден"
    fi
}

stop_bot "main_bot"
stop_bot "orders_bot"
stop_bot "purchases_bot"

echo ""
echo "✅ Все боты остановлены!"


