from fastapi import FastAPI, Request, HTTPException, Path, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import sqlite3
import requests
from datetime import datetime
import os

# ---------------- App & CORS ----------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- Config ----------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
DB_PATH = os.environ.get("DB_PATH", "db.sqlite3")

def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

# ---------------- DB init + safe migration ----------------
def table_columns(conn, table):
    cur = conn.execute(f"PRAGMA table_info({table})")
    return {row[1] if isinstance(row, tuple) else row["name"] for row in cur.fetchall()}

def add_column_if_missing(conn, table, coldef):
    name = coldef.split()[0]
    cols = table_columns(conn, table)
    if name not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")

def init_db():
    conn = get_db()
    cur = conn.cursor()
    # base tables
    cur.executescript('''
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
        CREATE TABLE IF NOT EXISTS answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT
        );
    ''')
    conn.commit()

    # ensure columns exist (idempotent)
    for coldef in [
        "user TEXT",
        "phone TEXT",
        "email TEXT",
        "organization TEXT",
        "branch TEXT",
        "device TEXT",
        "problem TEXT",
        "comment TEXT",
        "chat_id TEXT",
        "created_at TEXT",
        "deleted INTEGER DEFAULT 0"
    ]:
        add_column_if_missing(conn, "requests", coldef)

    for coldef in [
        "request_id INTEGER",
        "chat_id TEXT",
        "reply TEXT",
        "created_at TEXT"
    ]:
        add_column_if_missing(conn, "answers", coldef)

    # helpful indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_requests_deleted ON requests(deleted)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_answers_chat ON answers(chat_id)")
    conn.commit()
    conn.close()

init_db()

# ---------------- Schemas ----------------
class ReplyIn(BaseModel):
    text: str
    operator: str | None = None

# ---------------- Endpoints ----------------
@app.get("/api/health")
def health():
    return {"ok": True, "time": datetime.now().isoformat(timespec="seconds")}

@app.post("/api/message")
async def receive_message(request: Request):
    data = await request.json()
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO requests (user, phone, email, organization, branch, device, problem, comment, chat_id, created_at, deleted) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)',
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
    req_id = cur.lastrowid
    conn.commit()
    conn.close()

    chat_id = data.get("chat_id")
    if chat_id and TELEGRAM_BOT_TOKEN:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "Ваша заявка принята. Ожидайте ответ оператора."},
                timeout=8
            )
        except Exception:
            pass

    return {"status": "получено", "id": req_id}

@app.get("/api/chats")
async def get_chats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, user, phone, email, organization, branch, device, problem, comment, chat_id, created_at, deleted '
        'FROM requests '
        'WHERE COALESCE(deleted, 0) = 0 '
        'ORDER BY id DESC'
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
        (request_id, chat_id, body.text, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    conn.close()

    if TELEGRAM_BOT_TOKEN:
        try:
            notify = "Вам поступил ответ от оператора. Откройте мини-приложение, чтобы прочитать."
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": notify},
                timeout=8
            )
        except Exception:
            pass

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