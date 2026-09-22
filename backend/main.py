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
from zoneinfo import ZoneInfo
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

# Public base URL of this deployed backend (e.g. https://your-app.up.railway.app)
# Used to auto-register the Telegram webhook on startup.
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")

# Optional secret path segment so random internet traffic can't hit the webhook.
TELEGRAM_WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "memoria")

PORT = int(os.environ.get("PORT", 8000))

DB_PATH = os.environ.get("DB_PATH", "memory_bot.db")

POLL_SECONDS = 30

# Local timezone for reminders (e.g. "Asia/Karachi", "Asia/Kolkata", "Europe/London")
LOCAL_TZ = ZoneInfo(os.environ.get("APP_TIMEZONE", "Asia/Karachi"))

# Fire reminders N days before the due date, at this local hour
REMIND_DAYS_BEFORE = int(os.environ.get("REMIND_DAYS_BEFORE", "1"))
REMIND_HOUR = int(os.environ.get("REMIND_HOUR", "9"))

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(level=logging.INFO)

log = logging.getLogger("memoria")


# ============================================================
# FASTAPI
# ============================================================

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):

    task = asyncio.create_task(reminder_loop())

    register_telegram_webhook()

    log.info("Memoria backend started")

    yield

    task.cancel()


app = FastAPI(
    title="Memoria",
    version="2.0.1",
    lifespan=lifespan,
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
        "version": "2.1.0"
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

    # Never store a deadline we cannot schedule — a bad date silently
    # meant "never notify".
    try:
        datetime.fromisoformat(due_date)
    except ValueError:
        log.warning(f"skipping deadline with unparseable due_date: {due_date!r}")
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
# DEADLINE REPLACE / CANCEL (handles "postponed", "moved", "cancelled")
# ============================================================

import difflib


def _norm_task(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def delete_matching_deadlines(user_id: str, task: str, threshold: float = 0.75) -> int:
    """Delete deadlines for `user_id` whose task matches `task` (fuzzy).
    Returns how many rows were deleted."""

    norm = _norm_task(task)

    if not norm:
        return 0

    with closing(get_db()) as conn:

        rows = conn.execute(
            "SELECT id, task FROM deadlines WHERE user_id = ?",
            (user_id,),
        ).fetchall()

        to_delete = []

        for r in rows:

            existing = _norm_task(r["task"])

            ratio = difflib.SequenceMatcher(None, norm, existing).ratio()

            # substring match catches rephrasings like
            # "maths exam" vs "maths exam postponed" (ratio only 0.67)
            substring = (
                len(norm) >= 4
                and len(existing) >= 4
                and (norm in existing or existing in norm)
            )

            # strong token overlap catches word reordering
            new_tokens = set(norm.split())
            old_tokens = set(existing.split())
            shared = new_tokens & old_tokens
            # need >=2 shared words (or identical single-word tasks) —
            # so "physics exam" and "maths exam" stay separate
            overlap = (
                len(shared) >= 2
                or (new_tokens == old_tokens and len(new_tokens) == 1)
            )

            if existing == norm or ratio >= threshold or substring or overlap:
                to_delete.append(r["id"])

        for i in to_delete:
            conn.execute("DELETE FROM deadlines WHERE id = ?", (i,))

        conn.commit()

    return len(to_delete)


def replace_deadline(user_id: str, task: str, due_date: str, reminder_time: Optional[str] = None):
    """If this task already has a deadline (possibly with an old date),
    remove the old row first so 'postponed to 26 November' does not leave
    the 24 November row behind."""

    deleted = delete_matching_deadlines(user_id, task)

    if deleted:
        log.info(f"replaced {deleted} old deadline(s) for task: {task}")

    save_deadline(user_id, task, due_date, reminder_time)


# ============================================================
# CHAT
# ============================================================

EXTRACT_PROMPT = """You extract memories and deadlines from a user message.
Reply with ONLY a JSON object in this exact shape:
{
  "memories": {"<short lowercase key>": "<value>", ...},
  "deadlines": [{"task": "...", "due_date": "YYYY-MM-DD"}, ...],
  "cancelled": ["<task name>", ...]
}
Rules:
- Only include facts the user explicitly wants remembered (birthdays, preferences, password hints, facts about themselves).
- Only include deadlines/tasks with a clear due date. Convert dates to YYYY-MM-DD. Use year {year} unless the user says otherwise.
- IMPORTANT: if the user says a deadline moved, was postponed, or its date changed, put that task in "deadlines" with the NEW date. Old dates are removed automatically, so always give the latest date.
- If the user cancels or deletes a task/reminder, put its name in "cancelled".
- If nothing is worth remembering, return {"memories": {}, "deadlines": [], "cancelled": []}.
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

        # cancelled tasks: remove their deadlines
        for t in (data.get("cancelled") or []):
            n = delete_matching_deadlines(user_id, str(t))
            if n:
                log.info(f"cancelled deadline(s) for task: {t}")

        # new/updated deadlines: replace any existing row for the same task
        for dl in (data.get("deadlines") or []):
            due = dl.get("due_date", "")
            task = dl.get("task", "")
            parsed = parse_date(due)
            if task and parsed:
                replace_deadline(user_id, task, parsed, dl.get("reminder_time"))
            elif task:
                log.warning(f"AI returned bad due_date {due!r} for task {task!r} — not saved")

    except Exception as e:

        log.warning(f"extraction failed: {e}")


def generate_reply(user_id: str, message: str) -> tuple:
    """Shared chat pipeline used by both the web /chat endpoint and the
    Telegram webhook, so both surfaces behave identically."""

    memories, deadlines = fetch_user_context(user_id)

    # try to pull out anything new worth remembering
    extract_facts(user_id, message)

    # re-fetch in case extraction just added something
    memories, deadlines = fetch_user_context(user_id)

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

            reply = groq_chat(system_prompt, message)

        except Exception as e:

            log.error(f"groq error: {e}")

            reply = "My brain hiccuped for a second — could you say that again?"

    else:

        reply = (
            "I'm running without my AI brain right now (no GROQ_API_KEY set), "
            "but I heard you loud and clear."
        )

    return reply, memories, deadlines


@app.post("/chat")
def chat(req: ChatRequest):

    reply, memories, deadlines = generate_reply(req.user_id, req.message)

    return ChatResponse(
        reply=reply,
        memories=[{"key": k, "value": v} for k, v in memories.items()],
        deadlines=deadlines,
    )


# ============================================================
# TELEGRAM — BOT (incoming chat) + REMINDERS (outgoing)
# ============================================================

def send_telegram(text: str, chat_id: Optional[str] = None):
    """Send a message to a specific Telegram chat. Falls back to the
    TELEGRAM_CHAT_ID env var if no chat_id is given (kept for backward
    compatibility with older deployments that only used one fixed chat)."""

    target = chat_id or TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not target:
        log.warning("telegram not configured — message not sent")
        return

    try:

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",

            json={
                "chat_id": target,
                "text": text,
            },

            timeout=10,
        )

        if not resp.ok:
            log.error(f"telegram API error {resp.status_code}: {resp.text}")

    except Exception as e:

        log.warning(f"telegram send failed: {e}")


def register_telegram_webhook():
    """Point Telegram at our /telegram/webhook/{secret} endpoint so incoming
    messages actually reach this backend. Runs once at startup. No-op if
    TELEGRAM_BOT_TOKEN or PUBLIC_URL is not set."""

    if not TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot is disabled")
        return

    if not PUBLIC_URL:
        log.warning(
            "PUBLIC_URL not set — skipping Telegram webhook registration. "
            "Set PUBLIC_URL to this service's public HTTPS URL so the bot can receive messages."
        )
        return

    webhook_url = f"{PUBLIC_URL}/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}"

    try:

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": webhook_url},
            timeout=10,
        )

        if resp.ok and resp.json().get("ok"):
            log.info(f"telegram webhook registered at {webhook_url}")
        else:
            log.error(f"telegram setWebhook failed: {resp.status_code} {resp.text}")

    except Exception as e:

        log.warning(f"telegram webhook registration failed: {e}")


@app.get("/telegram/status")
def telegram_status():
    """Quick way to check what Telegram thinks our webhook is, and whether
    the bot token is valid — hit this in a browser to debug connectivity."""

    if not TELEGRAM_BOT_TOKEN:
        return {"configured": False, "reason": "TELEGRAM_BOT_TOKEN not set"}

    try:
        me = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getMe", timeout=10
        ).json()

        webhook_info = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getWebhookInfo",
            timeout=10,
        ).json()

    except Exception as e:
        return {"configured": True, "error": str(e)}

    return {
        "configured": True,
        "bot": me.get("result"),
        "webhook": webhook_info.get("result"),
        "expected_webhook_url": (
            f"{PUBLIC_URL}/telegram/webhook/{TELEGRAM_WEBHOOK_SECRET}" if PUBLIC_URL else None
        ),
    }


@app.post("/telegram/webhook/{secret}")
async def telegram_webhook(secret: str, update: dict):
    """Receives incoming messages from Telegram. Each Telegram chat_id is
    used directly as the Memoria user_id, so a person's Telegram chat
    carries its own memories/deadlines, and reminders for those deadlines
    go straight back to that same chat."""

    if secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=404, detail="not found")

    message = update.get("message") or update.get("edited_message")

    if not message:
        # non-message updates (reactions, etc.) — nothing to do
        return {"ok": True}

    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return {"ok": True}

    user_id = f"telegram:{chat_id}"

    if text in ("/start", "/help"):
        send_telegram(
            "Hi! I'm Memoria 🧠 — tell me things to remember, or deadlines "
            "like \"submit report due 26 November\", and I'll remind you here.",
            chat_id=chat_id,
        )
        return {"ok": True}

    try:
        reply, _memories, _deadlines = generate_reply(user_id, text)
    except Exception as e:
        log.error(f"telegram webhook chat error: {e}")
        reply = "My brain hiccuped for a second — could you say that again?"

    send_telegram(reply, chat_id=chat_id)

    return {"ok": True}


def check_due_reminders():

    now = datetime.now(LOCAL_TZ)

    with closing(get_db()) as conn:

        rows = conn.execute(
            """
            SELECT id, user_id, task, due_date, reminder_time
            FROM deadlines
            WHERE notified = 0
            """
        ).fetchall()

        for r in rows:

            # parse the due date
            try:
                due = datetime.fromisoformat(r["due_date"].strip()).date()
            except (ValueError, TypeError, AttributeError):
                log.warning(
                    f"deadline {r['id']} has bad due_date {r['due_date']!r} — skipped"
                )
                continue

            # default: remind REMIND_DAYS_BEFORE days early at REMIND_HOUR local time
            remind_day = due - timedelta(days=REMIND_DAYS_BEFORE)
            fire_at = datetime(
                remind_day.year, remind_day.month, remind_day.day,
                REMIND_HOUR, 0, tzinfo=LOCAL_TZ,
            )

            # explicit reminder_time from the user overrides the default
            if r["reminder_time"]:
                try:
                    rt = datetime.fromisoformat(r["reminder_time"].strip())
                    if rt.tzinfo is None:
                        rt = rt.replace(tzinfo=LOCAL_TZ)
                    fire_at = rt
                except (ValueError, TypeError, AttributeError):
                    log.warning(
                        f"deadline {r['id']} has bad reminder_time {r['reminder_time']!r} — using default"
                    )

            # also fire if we're already past the due date and still unnotified
            overdue = now.date() > due

            if now >= fire_at or overdue:

                # Deadlines created via Telegram carry the chat id in their
                # user_id ("telegram:<chat_id>") — route the reminder back
                # to that exact chat. Deadlines created via the web board
                # fall back to the fixed TELEGRAM_CHAT_ID env var, if set.
                if r["user_id"].startswith("telegram:"):
                    target_chat_id = r["user_id"].split(":", 1)[1]
                else:
                    target_chat_id = TELEGRAM_CHAT_ID

                send_telegram(
                    f"⏰ Reminder: {r['task']} — due {due.isoformat()}",
                    chat_id=target_chat_id,
                )

                conn.execute(
                    "UPDATE deadlines SET notified = 1 WHERE id = ?",
                    (r["id"],),
                )

                log.info(f"fired reminder for deadline {r['id']}: {r['task']}")

        conn.commit()


async def reminder_loop():

    while True:

        try:

            check_due_reminders()

        except Exception as e:

            log.error(f"reminder loop error: {e}")

        await asyncio.sleep(POLL_SECONDS)


# ============================================================
# RUN LOCALLY
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
