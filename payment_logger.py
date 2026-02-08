"""
Модуль для логирования платежей между клиентами и партнерами.
"""
import json
import os
from datetime import datetime
from typing import Dict, List, Optional

PAYMENT_LOGS_FILE = "payment_logs.json"

def load_payment_logs() -> Dict:
    """Загружает логи платежей."""
    if os.path.exists(PAYMENT_LOGS_FILE):
        try:
            with open(PAYMENT_LOGS_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            return {"logs": []}
    return {"logs": []}

def save_payment_logs(logs: Dict):
    """Сохраняет логи платежей."""
    with open(PAYMENT_LOGS_FILE, 'w', encoding='utf-8') as f:
        json.dump(logs, f, ensure_ascii=False, indent=2)

def log_payment(
    order_id: int,
    order_type: str,
    client_id: int,
    partner_id: int,
    amount: float,
    payment_type: str,
    status: str = "pending",
    notes: str = None
) -> bool:
    """
    Логирует платеж.
    
    Args:
        order_id: ID заказа
        order_type: Тип заказа ("custom_beat" или "mixing")
        client_id: ID клиента
        partner_id: ID партнера
        amount: Сумма платежа
        payment_type: Тип платежа ("first_payment" (50%), "second_payment" (50%), "full_payment" (100%))
        status: Статус платежа ("pending", "confirmed", "rejected")
        notes: Дополнительные заметки
    
    Returns:
        True если успешно залогировано
    """
    logs_data = load_payment_logs()
    
    log_entry = {
        "order_id": order_id,
        "order_type": order_type,
        "client_id": client_id,
        "partner_id": partner_id,
        "amount": amount,
        "payment_type": payment_type,
        "status": status,
        "notes": notes or "",
        "created_at": datetime.now().isoformat(),
        "updated_at": datetime.now().isoformat()
    }
    
    logs_data["logs"].append(log_entry)
    save_payment_logs(logs_data)
    return True

def update_payment_log_status(
    order_id: int,
    order_type: str,
    payment_type: str,
    status: str,
    notes: str = None
) -> bool:
    """
    Обновляет статус платежа в логе.
    
    Returns:
        True если успешно обновлено
    """
    logs_data = load_payment_logs()
    
    for log in logs_data["logs"]:
        if (log["order_id"] == order_id and 
            log["order_type"] == order_type and 
            log["payment_type"] == payment_type):
            log["status"] = status
            log["updated_at"] = datetime.now().isoformat()
            if notes:
                log["notes"] = notes
            save_payment_logs(logs_data)
            return True
    
    return False

def get_payment_logs_by_order(order_id: int, order_type: str) -> List[Dict]:
    """Получает все логи платежей для заказа."""
    logs_data = load_payment_logs()
    return [
        log for log in logs_data["logs"]
        if log["order_id"] == order_id and log["order_type"] == order_type
    ]

def get_payment_logs_by_partner(partner_id: int) -> List[Dict]:
    """Получает все логи платежей партнера."""
    logs_data = load_payment_logs()
    return [log for log in logs_data["logs"] if log["partner_id"] == partner_id]

def get_payment_logs_by_client(client_id: int) -> List[Dict]:
    """Получает все логи платежей клиента."""
    logs_data = load_payment_logs()
    return [log for log in logs_data["logs"] if log["client_id"] == client_id]


















