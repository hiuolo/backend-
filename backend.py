from fastapi import FastAPI, Request, HTTPException, Path, Body, Response
from fastapi.responses import PlainTextResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import requests
from datetime import datetime
import os
import json
import platform

# ===== БАЗОВАЯ НАСТРОЙКА ПРИЛОЖЕНИЯ =====
app = FastAPI()

# 1) Пермишсивный CORS через встроенное middleware
#    (allow_credentials=False обязательно при "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2) Дополнительное "страхующее" middleware:
#    добавляет CORS-заголовки в ЛЮБОЙ ответ (в т.ч. при исключениях и 404)
@app.middleware("http")
async def force_cors_headers(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception as e:
        # чтобы даже при исключении был корректный CORS
        body = {"ok": False, "error": "internal", "detail": repr(e)}
        resp = JSONResponse(body, status_code=500)
    # расставляем заголовки (если не расставлены)
    h = resp.headers
    h.setdefault("Access-Control-Allow-Origin", "*")
    h.setdefault("Access-Control-Allow-Methods", "*")
    h.setdefault("Access-Control-Allow-Headers", "*")
    return resp

# 3) Обработчик preflight на любой путь
@app.options("/{full_path:path}")
def any_preflight(full_path: str, request: Request):
    # Echo заголовков из запроса — это лучший UX для браузера
    acrh = request.headers.get("Access-Control-Request-Headers", "*")
    acrm = request.headers.get("Access-Control-Request-Method", "*")
    resp = Response(status_code=204)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = acrh
    resp.headers["Access-Control-Allow-Methods"] = acrm
    return resp

# ===== КОНФИГ =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "db.sqlite3")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ===== МЯГКИЕ МИГРАЦИИ БД =====
def table_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return { (row[1] if isinstance(row, tuple) else row["name"]) for row in cur.fetchall() }

def add_column_if_missing(conn, table, coldef):
    name = coldef.split()[0]
    cols = table_columns(conn, table)
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT);
        CREATE TABLE IF NOT EXISTS answers  (id INTEGER PRIMARY KEY AUTOINCREMENT);
    """)
    conn.commit()

    for coldef in [
        "user TEXT","phone TEXT","email TEXT","organization TEXT","branch TEXT",
        "device TEXT","problem TEXT","comment TEXT","chat_id TEXT",
        "created_at TEXT","deleted INTEGER DEFAULT 0"
    ]:
        add_column_if_missing(conn, "requests", coldef)

    for coldef in ["request_id INTEGER","chat_id TEXT","reply TEXT","created_at TEXT"]:
        add_column_if_missing(conn, "answers", coldef)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_deleted ON requests(deleted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_answers_chat ON answers(chat_id)")
    conn.commit()
    conn.close()

init_db()

# ===== УТИЛИТЫ =====
def telegram_send(chat_id: str, text: str) -> dict:
    """Отправка сообщения в Telegram с логированием результата."""
    if not TELEGRAM_BOT_TOKEN:
        msg = {"ok": False, "error": "no token configured"}
        print("[telegram]", msg)
        return msg
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=8,
        )
        try:
            j = r.json()
        except Exception:
            j = {"ok": False, "raw": r.text[:200]}
        if not j.get("ok"):
            print("[telegram] sendMessage failed", r.status_code, json.dumps(j, ensure_ascii=False))
        return j
    except Exception as e:
        msg = {"ok": False, "exception": repr(e)}
        print("[telegram] exception", repr(e))
        return msg

class ReplyIn(BaseModel):
    text: str
    operator: str | None = None

# ===== ДИАГНОСТИКА =====
@app.get("/")
def root():
    return {"alive": True, "ts": datetime.utcnow().isoformat(timespec="seconds") + "Z"}

@app.get("/api/ping")
def ping():
    return {"pong": True}

@app.get("/api/health")
def health(request: Request):
    return {
        "ok": True,
        "time": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "python": platform.python_version(),
        "client_origin": request.headers.get("origin"),
        "telegram_token_set": bool(TELEGRAM_BOT_TOKEN),
        "cors": {"forced_headers": True, "allow_origins": "*", "allow_credentials": False},
    }

@app.get("/api/echo_headers")
def echo_headers(request: Request):
    # Возвращает все заголовки запроса (удобно видеть Origin/Host и т.п.)
    return {k.lower(): v for k, v in request.headers.items()}

@app.get("/api/notify_test")
def notify_test(chat_id: str, text: str = "Тестовое уведомление от сервера"):
    return telegram_send(chat_id, text)

# ===== ФУНКЦИОНАЛ =====
@app.post("/api/message")
async def receive_message(request: Request):
    # и JSON, и form-data
    data = {}
    try:
        data = await request.json()
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    if not data:
        try:
            form = await request.form()
            data = dict(form)
        except Exception:
            data = {}

    payload = {
        "user":         (data.get("user") or "").strip(),
        "phone":        (data.get("phone") or "").strip(),
        "email":        (data.get("email") or "").strip(),
        "organization": (data.get("organization") or "").strip(),
        "branch":       (data.get("branch") or "").strip(),
        "device":       (data.get("device") or "").strip(),
        "problem":      (data.get("problem") or data.get("issue") or data.get("message") or "").strip(),
        "comment":      (data.get("comment") or "").strip(),
        "chat_id":      (data.get("chat_id") or "").strip(),
    }

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO requests (user, phone, email, organization, branch, device, problem, comment, chat_id, created_at, deleted) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)',
        (
            payload["user"], payload["phone"], payload["email"], payload["organization"], payload["branch"],
            payload["device"], payload["problem"], payload["comment"], payload["chat_id"],
            datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        )
    )
    req_id = cur.lastrowid
    conn.commit()
    conn.close()

    if payload["chat_id"]:
        telegram_send(payload["chat_id"], "Ваша заявка принята. Ожидайте ответ оператора.")
    return {"status": "получено", "id": req_id}

@app.get("/api/chats")
async def get_chats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, user, phone, email, organization, branch, device, problem, comment, chat_id, created_at, deleted '
        'FROM requests WHERE COALESCE(deleted, 0) = 0 ORDER BY id DESC'
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@app.post("/api/chats/{request_id}/reply")
async def reply_via_chat_id(
    request_id: int = Path(..., alias="request_id"),
    body: ReplyIn = Body(...),
):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM requests WHERE id=? AND COALESCE(deleted,0)=0", (request_id,))
    row = cur.fetchone()
    if not row or not row["chat_id"]:
        conn.close()
        raise HTTPException(status_code=404, detail="Chat not found")

    chat_id = row["chat_id"]
    cur.execute(
        "INSERT INTO answers (request_id, chat_id, reply, created_at) VALUES (?, ?, ?, ?)",
        (request_id, chat_id, body.text, datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    telegram_send(chat_id, "Вам поступил ответ от оператора. Откройте мини-приложение, чтобы прочитать.")
    return {"ok": True}

@app.get("/api/answers")
async def get_answers(chat_id: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT request_id, reply, created_at FROM answers WHERE chat_id=? ORDER BY created_at DESC",
        (chat_id,)
    )
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

@app.post("/api/delete_chat")
async def delete_chat(request: Request):
    data = await request.json()
    req_id = data.get("id") or data.get("chat_id")
    if req_id is None:
        raise HTTPException(status_code=400, detail="id is required")
    conn = get_db()
    conn.execute("UPDATE requests SET deleted=1 WHERE id=?", (req_id,))
    conn.commit()
    conn.close()
    return {"status": "deleted"}
