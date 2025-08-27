from fastapi import FastAPI, Request, HTTPException, Path, Body, Response, Header
import hmac, hashlib, json
from urllib.parse import parse_qsl
from fastapi import Body
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
CLIENT_URL = os.environ.get("CLIENT_URL", "https://mobiso-servicecentre.netlify.app")
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")

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

def validate_twa_init_data(init_data: str, bot_token: str):
    if not init_data or not bot_token:
        return False, {"reason": "empty init_data or token"}

    from urllib.parse import parse_qsl
    import hmac, hashlib, json

    pairs = parse_qsl(init_data, keep_blank_values=True)
    data = dict(pairs)

    provided_hash = data.pop("hash", None)
    data.pop("signature", None)  # на всякий случай

    data_check_string = "\n".join(f"{k}={data[k]}" for k in sorted(data.keys()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    calc_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if provided_hash != calc_hash:
        return False, {"reason": "bad hash", "calc": calc_hash, "given": provided_hash}

    user_obj = None
    chat_obj = None
    if "user" in data:
        try: user_obj = json.loads(data["user"])
        except: pass
    if "chat" in data:
        try: chat_obj = json.loads(data["chat"])
        except: pass

    user_id = str(user_obj["id"]) if isinstance(user_obj, dict) and "id" in user_obj else None
    chat_id = str(chat_obj["id"]) if isinstance(chat_obj, dict) and "id" in chat_obj else None
    preferred = user_id or chat_id

    return True, {
        "preferred_chat_id": preferred,
        "user_id": user_id,
        "chat_id": chat_id,
        "user": user_obj,
        "chat": chat_obj
    }

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
def telegram_send(chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
    """Отправка сообщения в Telegram с логированием результата."""
    if not TELEGRAM_BOT_TOKEN:
        msg = {"ok": False, "error": "no token configured"}
        print("[telegram]", msg)
        return msg
    try:
        payload = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json=payload,
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

@app.get("/api/diag/getchat")
def diag_getchat(chat_id: str):
    """Проверить, «видит» ли бот этот чат (Telegram getChat)."""
    if not TELEGRAM_BOT_TOKEN:
        return {"ok": False, "error": "no token"}
    r = requests.get(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getChat",
        params={"chat_id": chat_id.strip()},
        timeout=8
    )
    try:
        return {"status": r.status_code, "json": r.json()}
    except Exception:
        return {"status": r.status_code, "text": r.text[:400]}

@app.get("/api/diag/sendtest")
def diag_sendtest(chat_id: str):
    """Попытка отправки тестового сообщения в указанный chat_id."""
    return telegram_send(chat_id.strip(), "Тестовое уведомление от бота")


@app.post("/api/twa/resolve")
async def twa_resolve(payload: dict = Body(...)):
    init_data = payload.get("init_data")
    ok, info = validate_twa_init_data(init_data, TELEGRAM_BOT_TOKEN)
    if not ok or not info.get("preferred_chat_id"):
        raise HTTPException(status_code=400, detail={"ok": False, "error": "invalid_init_data", "info": info})
    # лог — чтобы видеть, что реально вернули
    print("[twa_resolve]", info)
    return {"ok": True, **info}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None)
):
    # Проверка секрета (если задан)
    if WEBHOOK_SECRET and x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        return {"ok": True}

    try:
        upd = await request.json()
    except Exception:
        return {"ok": True}

    msg = upd.get("message") or upd.get("edited_message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    # Если пользователь прислал контакт (кнопка 'request_contact')
    contact = msg.get("contact") or {}
    if contact.get("user_id") and chat_id:
        link = f"{CLIENT_URL}/?chat_id={chat_id}"
        telegram_send(str(chat_id), f"Спасибо! Откройте панель: {link}")
        return {"ok": True}

    # При /start — отправляем ссылку и (опционально) кнопку для отправки контакта
    if text == "/start" and chat_id:
        link = f"{CLIENT_URL}/?chat_id={chat_id}"
        kb = {
            "keyboard": [[{"text": "Отправить мой контакт ☎️", "request_contact": True}]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
        telegram_send(str(chat_id),
                      "Чтобы связать чат с панелью, нажмите кнопку ниже "
                      "или откройте панель по ссылке:\n" + link,
                      reply_markup=kb)
        return {"ok": True}

    return {"ok": True}

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
    req_id = data.get("id")
    chat_id = data.get("chat_id")
    if req_id is None and not chat_id:
        raise HTTPException(status_code=400, detail="id or chat_id is required")

    conn = get_db()
    cur = conn.cursor()
    if req_id is not None:
        cur.execute("UPDATE requests SET deleted=1 WHERE id=?", (req_id,))
    else:
        cur.execute("UPDATE requests SET deleted=1 WHERE chat_id=?", (str(chat_id),))
    conn.commit()
    conn.close()
    return {"status": "deleted"}

