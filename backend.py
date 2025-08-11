from fastapi import FastAPI, Request, HTTPException, Path, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import requests
from datetime import datetime
import os

# --- App & CORS ---
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],           # при необходимости ограничьте список
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Config ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8137013358:AAHTfWc-CK9aT9h_v3ekIld0DnFBVIXXusQ")

DB_PATH = os.environ.get("DB_PATH", "db.sqlite3")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    return conn

# --- DB init (без # комментариев, одним executescript) ---
def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript(
        """
        -- заявки пользователей
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user TEXT,
            phone TEXT,
            email TEXT,
            organization TEXT,
            branch TEXT,
            device TEXT,
            problem TEXT,
            comment TEXT,
            chat_id TEXT,
            created_at TEXT,
            deleted INTEGER DEFAULT 0
        );

        -- ответы операторов
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id INTEGER,
            chat_id TEXT,
            reply TEXT,
            created_at TEXT,
            FOREIGN KEY(request_id) REFERENCES requests(id)
        );

        CREATE INDEX IF NOT EXISTS idx_requests_deleted ON requests(deleted);
        CREATE INDEX IF NOT EXISTS idx_answers_chat ON answers(chat_id);
        """
    )
    conn.commit()
    conn.close()

init_db()

# --- Debug: print routes on startup ---
@app.on_event("startup")
def dump_routes():
    import logging
    logging.basicConfig(level=logging.INFO)
    for route in app.routes:
        logging.info(f"ROUTE: {route.path} METHODS: {route.methods}")

# --- Schemas ---
class ReplyIn(BaseModel):
    text: str
    operator: str | None = None

# --- Endpoints ---

@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}

# Заявка из мини-приложения/бота
@app.post("/api/message")
async def receive_message(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO requests (user, phone, email, organization, branch, device, problem, comment, chat_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data.get("user"),
            data.get("phone"),
            data.get("email"),
            data.get("organization"),
            data.get("branch"),
            data.get("device"),
            data.get("problem"),
            data.get("comment"),
            data.get("chat_id"),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        )
    )
    conn.commit()
    conn.close()

    # Уведомление пользователю
    chat_id = data.get("chat_id")
    if chat_id and TELEGRAM_BOT_TOKEN:
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
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception as e:
            # не прерываем основную логику
            pass

    return {"status": "получено"}

# Список активных заявок (для фронтенда)
@app.get("/api/chats")
async def get_chats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, user, phone, email, organization, branch, device, problem, comment, chat_id, created_at
        FROM requests
        WHERE COALESCE(deleted, 0) = 0
        ORDER BY id DESC
        """
    )
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
            "device": r[6],
            "problem": r[7],
            "comment": r[8],
            "chat_id": r[9],
            "created_at": r[10],
        }
        for r in rows
    ]

# НОВО: совместимый с фронтендом эндпоинт
# POST /api/chats/{id}/reply  body: {"text": "...", "operator": "..."}
@app.post("/api/chats/{request_id}/reply")
async def reply_via_chat_id(
    request_id: int = Path(..., alias="request_id"),
    body: ReplyIn = Body(...),
):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM requests WHERE id=? AND COALESCE(deleted,0)=0", (request_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        conn.close()
        raise HTTPException(status_code=404, detail="Chat not found")

    chat_id = row[0]
    # Сохраним ответ
    cur.execute(
        "INSERT INTO answers (request_id, chat_id, reply, created_at) VALUES (?, ?, ?, ?)",
        (request_id, chat_id, body.text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    # Отправка пользователю в Telegram
    if TELEGRAM_BOT_TOKEN:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"Ответ от оператора{(' '+body.operator) if body.operator else ''}:\n{body.text}"
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception:
            pass

    return {"ok": True}

# СТАРЫЙ эндпоинт (оставлен для совместимости), но исправлен параметр
# ожидает: {"request_id": <id в таблице requests>, "reply": "текст"}
@app.post("/api/operator_reply")
async def operator_reply_legacy(request: Request):
    data = await request.json()
    request_id = data.get("request_id")
    reply_text = data.get("reply")
    operator = data.get("operator")

    if not request_id or not reply_text:
        raise HTTPException(status_code=400, detail="request_id and reply are required")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM requests WHERE id=? AND COALESCE(deleted,0)=0", (request_id,))
    row = cur.fetchone()
    if not row or not row[0]:
        conn.close()
        raise HTTPException(status_code=404, detail="Chat not found")

    chat_id = row[0]
    cur.execute(
        "INSERT INTO answers (request_id, chat_id, reply, created_at) VALUES (?, ?, ?, ?)",
        (request_id, chat_id, reply_text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    if TELEGRAM_BOT_TOKEN:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": f"Ответ от оператора{(' '+operator) if operator else ''}:\n{reply_text}"
        }
        try:
            requests.post(url, json=payload, timeout=10)
        except Exception:
            pass

    return {"ok": True}

# Получение ответов для пользователя
@app.get("/api/answers")
async def get_answers(chat_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT request_id, reply, created_at FROM answers WHERE chat_id=? ORDER BY created_at DESC",
        (chat_id,)
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"request_id": r[0], "reply": r[1], "created_at": r[2]}
        for r in rows
    ]

# Пометка заявки как удалённой (по id из таблицы requests)
@app.post("/api/delete_chat")
async def delete_chat(request: Request):
    data = await request.json()
    # Поддержим как {"id":1}, так и {"chat_id":1}
    req_id = data.get("id")
    if req_id is None:
        req_id = data.get("chat_id")
    if req_id is None:
        raise HTTPException(status_code=400, detail="id is required")

    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE requests SET deleted=1 WHERE id=?", (req_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

# Telegram webhook (опционально)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    msg = update.get("message", {})
    web_data = msg.get("web_app_data", {}).get("data")
    if web_data:
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        if chat_id and TELEGRAM_BOT_TOKEN:
            try:
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": "📬 Вам пришёл ответ от оператора! Откройте мини‑приложение, чтобы прочитать."},
                    timeout=10
                )
            except Exception:
                pass
    return {"ok": True}
