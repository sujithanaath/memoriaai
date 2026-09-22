"""
Memoria — Long-Term Memory AI Backend
FastAPI + SQLite + Groq
Railway Ready
"""

import os
import re
import json
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta
from contextlib import closing
from typing import Optional

import requests

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel


# ============================================================
# CONFIGURATION
# ============================================================

GROQ_API_KEY = os.environ.get(
    "GROQ_API_KEY",
    "gsk_paste_your_groq_api_key_here"
)

GROQ_MODEL = os.environ.get(
    "GROQ_MODEL",
    "llama-3.3-70b-versatile"
)

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN",
    ""
)

TELEGRAM_CHAT_ID = os.environ.get(
    "TELEGRAM_CHAT_ID",
    ""
)

PORT = int(os.environ.get("PORT", 8000))

DB_PATH = os.environ.get("DB_PATH", "memory_bot.db")

POLL_SECONDS = 30

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(level=logging.INFO)

log = logging.getLogger("memoria")


# ============================================================
# FASTAPI
# ============================================================

app = FastAPI(
    title="Memoria",
    version="2.0.0"
)


# ============================================================
# CORS
# ============================================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://hen-psi.vercel.app",
        "http://localhost:3000",
        "http://localhost:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================
# AI STATUS
# ============================================================

AI_ENABLED = (
    GROQ_API_KEY.startswith("gsk_")
    and "paste_your" not in GROQ_API_KEY
)


# ============================================================
# USER FLOW STATE
# ============================================================

flow_state = {}


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(
        DB_PATH,
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with closing(get_db()) as conn:

        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, key)
            );

            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                task TEXT NOT NULL,
                due_date TEXT NOT NULL,
                reminder_time TEXT,
                notified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )

        conn.commit()


init_db()


# ============================================================
# MODELS
# ============================================================

class ChatRequest(BaseModel):

    user_id: str

    message: str


class ChatResponse(BaseModel):

    reply: str

    memories: Optional[list] = None

    deadlines: Optional[list] = None


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
def root():

    return {
        "name": "Memoria",
        "status": "online",
        "version": "2.0.0"
    }


@app.get("/health")
def health():

    return {
        "status": "ok",
        "ai": "Groq" if AI_ENABLED else "Fallback",
        "model": GROQ_MODEL if AI_ENABLED else "rule-based"
    }


# ============================================================
# MEMORIES — read / delete (used by the web board)
# ============================================================

@app.get("/memories/{user_id}")
def get_memories(user_id: str):

    with closing(get_db()) as conn:

        rows = conn.execute(
            """
            SELECT key, value, created_at, updated_at
            FROM memories
            WHERE user_id = ?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()

    return [
        {
            "key": r["key"],
            "value": r["value"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


@app.delete("/memories/{user_id}/{key}")
def delete_memory(user_id: str, key: str):

    with closing(get_db()) as conn:

        cur = conn.execute(
            "DELETE FROM memories WHERE user_id = ? AND key = ?",
            (user_id, key),
        )

        conn.commit()

    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Memory not found")

    return {"ok": True}


# ============================================================
# DEADLINES — read (used by the web board)
# ============================================================

@app.get("/deadlines/{user_id}")
def get_deadlines(user_id: str):

    with closing(get_db()) as conn:

        rows = conn.execute(
            """
            SELECT id, task, due_date, reminder_time, notified, created_at
            FROM deadlines
            WHERE user_id = ?
            ORDER BY due_date ASC
            """,
            (user_id,),
        ).fetchall()

    return [
        {
            "id": r["id"],
            "task": r["task"],
            "due_date": r["due_date"],
            "reminder_time": r["reminder_time"],
            "notified": bool(r["notified"]),
        }
        for r in rows
    ]


# ============================================================
# GROQ AI
# ============================================================

def groq_chat(
    system_prompt: str,
    user_message: str,
    json_mode: bool = False
) -> str:

    payload = {
        "model": GROQ_MODEL,

        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_message
            }
        ],

        "temperature": 0
    }

    if json_mode:

        payload["response_format"] = {
            "type": "json_object"
        }

    response = requests.post(
        GROQ_URL,

        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json"
        },

        json=payload,

        timeout=30
    )

    response.raise_for_status()

    data = response.json()

    return data["choices"][0]["message"]["content"]


# ============================================================
# DATE PARSING
# ============================================================

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}


def parse_date(text: str) -> Optional[str]:
    """Try to turn free text into YYYY-MM-DD. Returns None if unsure."""

    text = text.lower().strip()

    today = datetime.now().date()

    # relative: today / tomorrow
    if text == "today":
        return today.isoformat()

    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    # relative: "in 3 days"
    m = re.search(r"in (\d+) days?", text)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()

    # ISO format: 2026-09-01 or 01-09-2026
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat()
        except ValueError:
            pass

    # "september 1" or "1 september" (optionally with year)
    month_num = None
    day_num = None
    year_num = today.year

    m = re.search(r"\b(20\d{2})\b", text)
    if m:
        year_num = int(m.group(1))

    for name, num in MONTHS.items():
        if name in text:
            month_num = num
            break

    if month_num:
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", text.replace(text.split()[0] if text.split()[0] in MONTHS else "", "", 1))
        # simpler: find a 1-2 digit number that is not the year
        for tok in re.findall(r"\b\d{1,2}\b", text):
            n = int(tok)
            if 1 <= n <= 31:
                day_num = n
                break

    if month_num and day_num:
        try:
            d = datetime(year_num, month_num, day_num).date()
            # if the date already passed this year, assume next year
            if d < today:
                d = datetime(year_num + 1, month_num, day_num).date()
            return d.isoformat()
        except ValueError:
            return None

    return None


# ============================================================
# MEMORY / DEADLINE STORAGE
# ============================================================

def save_memory(user_id: str, key: str, value: str):

    key = key.strip().lower()[:60]
    value = value.strip()[:500]

    if not key or not value:
        return

    now = datetime.now().isoformat()

    with closing(get_db()) as conn:

        conn.execute(
            """
            INSERT INTO memories (user_id, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id, key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (user_id, key, value, now, now),
        )

        conn.commit()


def save_deadline(user_id: str, task: str, due_date: str, reminder_time: Optional[str] = None):

    task = task.strip()[:200]
    due_date = due_date.strip()[:20]

    if not task or not due_date:
        return

    with closing(get_db()) as conn:

        conn.execute(
            """
            INSERT INTO deadlines (user_id, task, due_date, reminder_time, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, task, due_date, reminder_time, datetime.now().isoformat()),
        )

        conn.commit()


def fetch_user_context(user_id: str) -> tuple:

    with closing(get_db()) as conn:

        mem_rows = conn.execute(
            "SELECT key, value FROM memories WHERE user_id = ? ORDER BY updated_at DESC LIMIT 30",
            (user_id,),
        ).fetchall()

        dl_rows = conn.execute(
            "SELECT task, due_date, reminder_time, notified FROM deadlines WHERE user_id = ? ORDER BY due_date ASC LIMIT 20",
            (user_id,),
        ).fetchall()

    memories = {r["key"]: r["value"] for r in mem_rows}

    deadlines = [
        {
            "task": r["task"],
            "due_date": r["due_date"],
            "reminder_time": r["reminder_time"],
            "notified": bool(r["notified"]),
        }
        for r in dl_rows
    ]

    return memories, deadlines


# ============================================================
# CHAT
# ============================================================

EXTRACT_PROMPT = """You extract memories and deadlines from a user message.
Reply with ONLY a JSON object in this exact shape:
{
  "memories": {"<short lowercase key>": "<value>", ...},
  "deadlines": [{"task": "...", "due_date": "YYYY-MM-DD"}, ...]
}
Rules:
- Only include facts the user explicitly wants remembered (birthdays, preferences, passwords hints, facts about themselves).
- Only include deadlines/tasks with a clear due date. Convert dates to YYYY-MM-DD. Use year {year} unless the user says otherwise.
- If nothing is worth remembering, return {"memories": {}, "deadlines": []}.
- No markdown, no explanation, JSON only."""


def extract_facts(user_id: str, message: str):

    if not AI_ENABLED:
        return

    try:

        raw = groq_chat(
            EXTRACT_PROMPT.replace("{year}", str(datetime.now().year)),
            message,
            json_mode=True,
        )

        data = json.loads(raw)

        for key, value in (data.get("memories") or {}).items():
            save_memory(user_id, key, str(value))

        for dl in (data.get("deadlines") or []):
            due = dl.get("due_date", "")
            task = dl.get("task", "")
            parsed = parse_date(due) or due
            if task and parsed:
                save_deadline(user_id, task, parsed, dl.get("reminder_time"))

    except Exception as e:

        log.warning(f"extraction failed: {e}")


@app.post("/chat")
def chat(req: ChatRequest):

    memories, deadlines = fetch_user_context(req.user_id)

    # try to pull out anything new worth remembering
    extract_facts(req.user_id, req.message)

    # re-fetch in case extraction just added something
    memories, deadlines = fetch_user_context(req.user_id)

    if AI_ENABLED:

        mem_lines = "\n".join(f"- {k}: {v}" for k, v in memories.items()) or "- (none yet)"

        dl_lines = "\n".join(
            f"- {d['task']} (due {d['due_date']})" for d in deadlines
        ) or "- (none yet)"

        system_prompt = f"""You are Memoria, a bot with perfect long-term memory.
You are talking to the user with these stored memories:
{mem_lines}

And these upcoming deadlines:
{dl_lines}

Rules:
- Be warm, brief and a little witty. Short paragraphs.
- When the user tells you something new to remember, confirm it naturally.
- When they ask about their memories or deadlines, use the lists above.
- Never claim to remember something that is not in the lists."""

        try:

            reply = groq_chat(system_prompt, req.message)

        except Exception as e:

            log.error(f"groq error: {e}")

            reply = "My brain hiccuped for a second — could you say that again?"

    else:

        reply = (
            "I'm running without my AI brain right now (no GROQ_API_KEY set), "
            "but I heard you loud and clear."
        )

    return ChatResponse(
        reply=reply,
        memories=[{"key": k, "value": v} for k, v in memories.items()],
        deadlines=deadlines,
    )


# ============================================================
# TELEGRAM REMINDERS
# ============================================================

def send_telegram(text: str):

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    try:

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",

            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
            },

            timeout=10,
        )

    except Exception as e:

        log.warning(f"telegram send failed: {e}")


def check_due_reminders():

    now = datetime.now()

    with closing(get_db()) as conn:

        rows = conn.execute(
            """
            SELECT id, user_id, task, due_date, reminder_time
            FROM deadlines
            WHERE notified = 0
            """
        ).fetchall()

        for r in rows:

            # remind at reminder_time if set, otherwise at midnight of the due date
            if r["reminder_time"]:
                try:
                    remind_at = datetime.fromisoformat(r["reminder_time"])
                except ValueError:
                    remind_at = None
            else:
                remind_at = None

            due = None
            try:
                due = datetime.fromisoformat(r["due_date"])
            except ValueError:
                pass

            fire = False

            if remind_at and now >= remind_at:
                fire = True

            elif due and now.date() >= due.date():
                fire = True

            if fire:

                send_telegram(
                    f"Reminder for {r['user_id']}: {r['task']} (due {r['due_date']})"
                )

                conn.execute(
                    "UPDATE deadlines SET notified = 1 WHERE id = ?",
                    (r["id"],),
                )

        conn.commit()


async def reminder_loop():

    while True:

        try:

            check_due_reminders()

        except Exception as e:

            log.error(f"reminder loop error: {e}")

        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup():

    asyncio.create_task(reminder_loop())

    log.info("Memoria backend started")


# ============================================================
# RUN LOCALLY
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
