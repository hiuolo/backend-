from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import sqlite3
import requests
from datetime import datetime

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # или укажите конкретный сайт, если нужно
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Отладочный вывод всех маршрутов при старте
@app.on_event("startup")
def dump_routes():
    import logging
    logging.basicConfig(level=logging.INFO)
    for route in app.routes:
        logging.info(f"ROUTE: {route.path} METHODS: {route.methods}")


TELEGRAM_BOT_TOKEN = "8137013358:AAHTfWc-CK9aT9h_v3ekIld0DnFBVIXXusQ"  # замените на реальный токен

# Функция для подключения к базе данных SQLite
def get_db():
    conn = sqlite3.connect("db.sqlite3")
    return conn

# filepath: c:\Users\User\Desktop\src\backend.py
def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user TEXT,
        phone TEXT,
        email TEXT,
        organization TEXT,
        branch TEXT,
        device TEXT,  # добавлено поле device
        problem TEXT,
        comment TEXT,
        chat_id TEXT,
        created_at TEXT,
        deleted INTEGER DEFAULT 0
    )
    """)
    conn.commit()
    conn.close()

# Создайте таблицу answers, если её нет
def init_answers_table():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS answers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER,
        chat_id TEXT,
        reply TEXT,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()

init_db()
init_answers_table()

# Endpoint для получения сообщений от Telegram-бота (заявка пользователя)
@app.post("/api/message")
async def receive_message(request: Request):
    data = await request.json()
    print("Получена заявка:", data)
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO requests (user, phone, email, organization, branch, device, problem, comment, chat_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            data.get("user"),
            data.get("phone"),
            data.get("email"),
            data.get("organization"),
            data.get("branch"),
            data.get("device"),  # сохраняем устройство
            data.get("problem"),
            data.get("comment"),
            data.get("chat_id"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )
    conn.commit()
    conn.close()
    # После сохранения заявки отправляем уведомление пользователю
    chat_id = data.get("chat_id")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": (
            "Ваша заявка принята!\n"
            f"Организация: {data.get('organization')}\n"
            f"Филиал: {data.get('branch')}\n"
            f"Проблема: {data.get('problem')}\n"
            "Ожидайте ответа оператора."
        )
    }
    resp = requests.post(url, json=payload)
    # Можно добавить проверку resp.status_code и логировать ошибки
    return {"status": "получено"}

@app.get("/api/chats")
async def get_chats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, user, phone, email, organization, branch, device, problem, comment, chat_id, created_at FROM requests WHERE deleted IS NULL OR deleted = 0 ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0],
            "user": r[1],
            "phone": r[2],
            "email": r[3],
            "organization": r[4],
            "branch": r[5],
            "device": r[6],  # добавлено
            "problem": r[7],
            "comment": r[8],
            "chat_id": r[9],
            "created_at": r[10]
        }
        for r in rows
    ]

# Сохраняйте ответ оператора в таблицу answers при отправке ответа
@app.post("/api/operator_reply")
async def operator_reply(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM requests WHERE id = ?", (data["chat_id"],))
    row = cur.fetchone()
    if row and row[0]:
        chat_id = row[0]
    else:
        conn.close()
        return {"status": "chat_id не найден"}

    reply_text = data["reply"]
    cur.execute(
        "INSERT INTO answers (request_id, chat_id, reply, created_at) VALUES (?, ?, ?, ?)",
        (data["chat_id"], chat_id, reply_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    )
    conn.commit()
    conn.close()
    # Отправляем уведомление пользователю через Telegram Bot
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Сообщение с призывом открыть мини-приложение
    notify_text = (
        "Вам поступил ответ от оператора!\n"
        "Откройте мини-приложение, чтобы прочитать ответ.\n\n"
        f"Ответ: {reply_text}"
    )
    payload = {
        "chat_id": chat_id,
        "text": notify_text
    }
    resp = requests.post(url, json=payload)
    if resp.status_code == 200:
        return {"status": "ответ отправлен"}
    else:
        return {"status": "ошибка отправки", "details": resp.text}

# Новый эндпоинт для получения ответов пользователя
@app.get("/api/answers")
async def get_answers(chat_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT request_id, reply, created_at FROM answers WHERE chat_id = ? ORDER BY created_at DESC",
        (chat_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"request_id": r[0], "reply": r[1], "created_at": r[2]}
        for r in rows
    ]

@app.post("/api/delete_chat")
async def delete_chat(request: Request):
    data = await request.json()
    chat_id = data.get("chat_id")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE requests SET deleted = 1 WHERE id = ?", (chat_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    print("🔔 WEBHOOK UPDATE:", update)
    msg = update.get("message", {})
    web_data = msg.get("web_app_data", {}).get("data")
    if web_data:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if chat_id:
            resp = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "📬 Вам пришёл ответ от оператора! Откройте мини‑приложение, чтобы прочитать."}
            )
            print("sendMessage status:", resp.status_code, resp.text)
    return {"ok": True}


