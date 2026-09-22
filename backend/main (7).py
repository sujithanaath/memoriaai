"""
main.py — MemoryBot Backend (Groq AI + Railway ready)
=====================================================
A ChatGPT-like AI bot with:
  1. Long-term memory (remembers facts, e.g. "my birthday is September 1")
  2. Memory UPDATE (if you correct yourself: "it's not Sept 1, it's Oct 1",
     the old memory is deleted and replaced automatically)
  3. Deadlines with smart reminders ("I have a project review on the 20th"
     -> bot asks "When should I remind you?" -> notifies you at that time)

Database : SQLite (auto-created: memory_bot.db)
Host     : Railway (auto-detects PORT)

NO .env FILE NEEDED — just fill in the 3 values below and deploy.
=====================================================================
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

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests

# ===========================================================================
#  CONFIGURATION — set these as environment variables in Railway
#  (Railway dashboard → your service → Variables tab)
#  Local fallback strings below are only used if the env var isn't set.
# ===========================================================================
GROQ_API_KEY       = os.environ.get("GROQ_API_KEY", "gsk_paste_your_groq_api_key_here")
GROQ_MODEL         = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")  # or "llama-3.1-8b-instant" (faster)
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")   # optional: for real push notifications
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")     # optional: your Telegram chat id

PORT = int(os.environ.get("PORT", 8000))   # Railway injects this automatically
DB_PATH = "memory_bot.db"
POLL_SECONDS = 30
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
# ===========================================================================

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("memorybot")

app = FastAPI(title="MemoryBot", version="1.0.0")

# CORS — lets ANY frontend (Vercel, localhost, etc.) call your Railway URL
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory conversation flow state:
# { user_id: {"step": "awaiting_reminder_time", "deadline_id": int, ...} }
flow_state: dict[str, dict] = {}

AI_ENABLED = GROQ_API_KEY.startswith("gsk_") and "paste_your" not in GROQ_API_KEY


# ---------------------------------------------------------------------------
# Database (SQLite = real SQL database, zero setup)
# ---------------------------------------------------------------------------
def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with closing(get_db()) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                key TEXT NOT NULL,              -- e.g. "birthday"
                value TEXT NOT NULL,            -- e.g. "September 1"
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(user_id, key)
            );
            CREATE TABLE IF NOT EXISTS deadlines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                task TEXT NOT NULL,
                due_date TEXT NOT NULL,         -- ISO date YYYY-MM-DD
                reminder_time TEXT,             -- ISO datetime, NULL until user answers
                notified INTEGER DEFAULT 0,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.commit()


init_db()


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class ChatRequest(BaseModel):
    user_id: str
    message: str


class ChatResponse(BaseModel):
    reply: str
    memories: Optional[list] = None
    deadlines: Optional[list] = None


# ---------------------------------------------------------------------------
# Groq AI layer (OpenAI-compatible). Falls back to built-in parser if no key.
# ---------------------------------------------------------------------------
def groq_chat(system_prompt: str, user_message: str, json_mode: bool = False) -> str:
    """Call Groq. Returns the assistant's reply text."""
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ],
        "temperature": 0,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    resp = requests.post(
        GROQ_URL,
        headers={
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def ai_understand(message: str, memories: list[sqlite3.Row]) -> dict:
    """Classify intent and extract structured data using Groq.

    Returns: {"action": "remember"|"update"|"deadline"|"recall"|"chat",
              "key":..., "value":..., "task":..., "due_date":"YYYY-MM-DD"}
    """
    if not AI_ENABLED:
        return fallback_understand(message, memories)

    mem_summary = "; ".join(f"{m['key']} = {m['value']}" for m in memories) or "none"
    today = datetime.now().strftime("%Y-%m-%d")
    system = f"""Today is {today}. You are the brain of a personal memory bot.
Classify the user's message and extract data. Rules:
- "remember my birthday is September 1" -> action "remember", key "birthday", value "September 1"
- "my birthday is not September 1, it is October 1" -> action "update" (old value gets replaced), key "birthday", value "October 1"
- "I have a project review on the 20th" -> action "deadline", task "project review", due_date "YYYY-MM-DD" (nearest upcoming date)
- "when is my birthday?" / "what do you remember?" -> action "recall"
- everything else -> action "chat"
Existing memories: {mem_summary}
Return ONLY minified JSON: {{"action":"...","key":"...","value":"...","task":"...","due_date":"..."}} (use "" for missing)."""

    try:
        return json.loads(groq_chat(system, message, json_mode=True))
    except Exception as e:
        log.warning("Groq call failed (%s); using fallback parser", e)
        return fallback_understand(message, memories)


def fallback_understand(message: str, memories: list[sqlite3.Row]) -> dict:
    """Built-in rule parser so the bot works even without a Groq key."""
    m = message.lower()

    # --- correction / update: "X is not A, it is B" ---
    corr = re.search(r"(?:my\s+)?([\w\s]+?)\s+is\s+not\s+(.+?)[,;]?\s*(?:it is|it's)\s+(.+)$", m)
    if corr:
        return {"action": "update", "key": corr.group(1).strip(),
                "value": corr.group(3).strip(), "task": "", "due_date": ""}

    # --- deadline: task + date words ---
    if re.search(r"\b(deadline|due|meeting|review|exam|submit|assignment|"
                 r"project|appointment|interview|presentation|pay|bill)\b", m) \
            and re.search(r"\b(\d{1,2}(?:st|nd|rd|th)?|tomorrow|today|next week)\b"
                          r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", m):
        task_m = re.search(r"(?:have|has|there is)\s+(?:a\s+|an\s+)?(.+?)(?:\s+on|\s+by|\s+due|$)", m)
        task = task_m.group(1).strip() if task_m else "reminder"
        return {"action": "deadline", "key": "", "value": "",
                "task": task, "due_date": parse_date_flexible(message)}

    # --- remember fact: "remember (that) my X is Y" ---
    rem = re.search(r"remember\s+(?:that\s+)?(?:my\s+)?([\w\s]+?)\s+is\s+(.+)$", m)
    if rem:
        return {"action": "remember", "key": rem.group(1).strip(),
                "value": rem.group(2).strip(), "task": "", "due_date": ""}

    # --- recall ---
    if re.search(r"\b(what do you remember|what do you know|my memories|"
                 r"what did i tell you|list memories)\b", m):
        return {"action": "recall", "key": "", "value": "", "task": "", "due_date": ""}
    ask = re.search(r"what(?:'s| is) my (.+?)\??$", m)
    if ask:
        return {"action": "recall", "key": ask.group(1).strip(),
                "value": "", "task": "", "due_date": ""}

    return {"action": "chat", "key": "", "value": "", "task": "", "due_date": ""}


MONTHS = {name: i for i, name in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def parse_date_flexible(text: str) -> Optional[str]:
    """Parse 'the 20th', 'September 1', 'oct 1', 'tomorrow' into YYYY-MM-DD."""
    t = text.lower()
    now = datetime.now()

    if "tomorrow" in t:
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if "today" in t:
        return now.strftime("%Y-%m-%d")
    if "next week" in t:
        return (now + timedelta(days=7)).strftime("%Y-%m-%d")

    m = re.search(r"\b(january|february|march|april|may|june|july|august|"
                  r"september|october|november|december|jan|feb|mar|apr|jun|"
                  r"jul|aug|sep|sept|oct|nov|dec)\w*\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        mon = MONTHS.get(m.group(1))
        if mon is None:
            for full, num in MONTHS.items():
                if full.startswith(m.group(1)):
                    mon = num
                    break
        day = int(m.group(2))
        try:
            d = datetime(now.year, mon, day)
        except ValueError:
            return None
        if d.date() < now.date():          # month passed -> next year
            d = datetime(now.year + 1, mon, day)
        return d.strftime("%Y-%m-%d")

    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m:
        day = int(m.group(1))
        year, month = now.year, now.month
        try:
            d = datetime(year, month, day)
        except ValueError:
            return None
        if d.date() < now.date():          # day passed this month -> next month
            month += 1
            if month > 12:
                month, year = 1, year + 1
            try:
                d = datetime(year, month, day)
            except ValueError:
                return None
        return d.strftime("%Y-%m-%d")

    try:  # ISO "2026-09-20"
        return datetime.strptime(t.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_reminder_offset(text: str, due_date: str) -> Optional[str]:
    """Parse reminder answers into an absolute ISO datetime.

    Accepts: '1 day before', '2 hours before', '30 minutes before',
             'on the 19th', 'september 19', 'at 9am', 'the morning of the 19th'
    """
    t = text.lower()
    due = datetime.strptime(due_date, "%Y-%m-%d")

    m = re.search(r"(\d+)\s*(day|days|hour|hours|hr|hrs|minute|minutes|min|mins)\s*before", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith(("hour", "hr")):
            return (due - timedelta(hours=n)).strftime("%Y-%m-%d %H:%M")
        if unit.startswith(("minute", "min")):
            return (due - timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M")
        return (due - timedelta(days=n)).strftime("%Y-%m-%d %H:%M")

    m = re.search(r"(\d+)\s*(day|days|hour|hours|minute|minutes)\s*(?:early|earlier)?", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith(("hour", "hr")):
            return (due - timedelta(hours=n)).strftime("%Y-%m-%d %H:%M")
        if unit.startswith(("minute", "min")):
            return (due - timedelta(minutes=n)).strftime("%Y-%m-%d %H:%M")
        return (due - timedelta(days=n)).strftime("%Y-%m-%d %H:%M")

    hour_m = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    time_part = None
    if hour_m:
        h = int(hour_m.group(1))
        minute = int(hour_m.group(2) or 0)
        mer = hour_m.group(3)
        if mer == "pm" and h < 12:
            h += 12
        if mer == "am" and h == 12:
            h = 0
        time_part = f"{h:02d}:{minute:02d}"
    elif re.search(r"\b(morning)\b", t):
        time_part = "09:00"
    elif re.search(r"\b(evening)\b", t):
        time_part = "18:00"
    elif re.search(r"\b(night)\b", t):
        time_part = "20:00"

    date_part = parse_date_flexible(t)
    if time_part and not date_part:
        date_part = due_date
    if date_part:
        return f"{date_part} {time_part or '09:00'}"
    return None


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------
def list_memories(user_id: str) -> list[sqlite3.Row]:
    with closing(get_db()) as conn:
        return conn.execute(
            "SELECT * FROM memories WHERE user_id=? ORDER BY key", (user_id,)
        ).fetchall()


def upsert_memory(user_id: str, key: str, value: str) -> bool:
    """Insert a fact, or UPDATE (replace) it if the key already exists.
    Returns True if an existing memory was replaced."""
    key = key.lower().strip()
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_db()) as conn:
        old = conn.execute(
            "SELECT id FROM memories WHERE user_id=? AND key=?", (user_id, key)
        ).fetchone()
        if old:
            conn.execute(
                "UPDATE memories SET value=?, updated_at=? WHERE id=?",
                (value, now, old["id"]),
            )
            conn.commit()
            return True
        conn.execute(
            "INSERT INTO memories (user_id, key, value, created_at, updated_at)"
            " VALUES (?,?,?,?,?)", (user_id, key, value, now, now),
        )
        conn.commit()
        return False


def get_memory(user_id: str, key: str) -> Optional[sqlite3.Row]:
    with closing(get_db()) as conn:
        return conn.execute(
            "SELECT * FROM memories WHERE user_id=? AND key=?",
            (user_id, key.lower().strip()),
        ).fetchone()


def add_deadline(user_id: str, task: str, due_date: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_db()) as conn:
        cur = conn.execute(
            "INSERT INTO deadlines (user_id, task, due_date, created_at)"
            " VALUES (?,?,?,?)", (user_id, task, due_date, now),
        )
        conn.commit()
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Notifications (Telegram if configured; always visible via /deadlines)
# ---------------------------------------------------------------------------
def send_notification(user_id: str, text: str) -> None:
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                timeout=10,
            )
            log.info("Telegram notification sent to %s", user_id)
            return
        except Exception as e:
            log.error("Telegram send failed: %s", e)
    log.info("[NOTIFY:%s] %s", user_id, text)


async def reminder_loop() -> None:
    """Background task: fire any reminders whose time has come."""
    while True:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            with closing(get_db()) as conn:
                due = conn.execute(
                    "SELECT * FROM deadlines WHERE notified=0 AND reminder_time IS NOT NULL"
                    " AND reminder_time <= ?", (now,),
                ).fetchall()
                for d in due:
                    send_notification(
                        d["user_id"],
                        f"⏰ Reminder: '{d['task']}' is due on {d['due_date']}.",
                    )
                    conn.execute(
                        "UPDATE deadlines SET notified=1 WHERE id=?", (d["id"],)
                    )
                conn.commit()
        except Exception as e:
            log.error("reminder_loop error: %s", e)
        await asyncio.sleep(POLL_SECONDS)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(reminder_loop())


# ---------------------------------------------------------------------------
# Chat endpoint — the brain of the bot
# ---------------------------------------------------------------------------
@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    user_id = req.user_id.strip()
    msg = req.message.strip()
    if not msg:
        raise HTTPException(400, "message is required")

    # --- step 2 of the deadline flow: user answered WHEN to notify ---
    state = flow_state.get(user_id)
    if state and state.get("step") == "awaiting_reminder_time":
        flow_state.pop(user_id, None)
        if re.search(r"\b(never|no reminder|don't remind|cancel)\b", msg.lower()):
            return ChatResponse(reply="Okay, I saved it without a reminder.")
        reminder_dt = parse_reminder_offset(msg, state["due_date"])
        if not reminder_dt:
            return ChatResponse(
                reply="Sorry, I didn't catch that. Try e.g. '1 day before', "
                      "'2 hours before', 'on the 19th at 9am', or 'no reminder'.")
        with closing(get_db()) as conn:
            conn.execute(
                "UPDATE deadlines SET reminder_time=? WHERE id=?",
                (reminder_dt, state["deadline_id"]),
            )
            conn.commit()
        return ChatResponse(
            reply=f"Got it! I'll notify you about '{state['task']}' on {reminder_dt}."
        )

    # --- normal understanding (Groq, or fallback parser) ---
    memories = list_memories(user_id)
    data = ai_understand(msg, memories)
    action = data.get("action", "chat")
    key = (data.get("key") or "").strip()
    value = (data.get("value") or "").strip()

    if action == "remember" and key and value:
        replaced = upsert_memory(user_id, key, value)
        reply = (f"Memory updated: your {key} is now {value}. (old value removed)"
                 if replaced else
                 f"Got it! I'll remember that your {key} is {value}.")
        return ChatResponse(reply=reply)

    if action == "update" and key and value:
        replaced = upsert_memory(user_id, key, value)
        reply = (f"Noted — I deleted the old memory. Your {key} is now {value}."
                 if replaced else
                 f"Okay, your {key} is {value}. (no previous memory to replace)")
        return ChatResponse(reply=reply)

    if action == "deadline":
        task = (data.get("task") or "reminder").strip()
        due_date = data.get("due_date") or parse_date_flexible(msg)
        if not due_date:
            return ChatResponse(
                reply=f"I captured '{task}', but which date is it due? "
                      "(e.g. 'on the 20th', 'September 20')")
        dl_id = add_deadline(user_id, task, due_date)
        flow_state[user_id] = {
            "step": "awaiting_reminder_time",
            "deadline_id": dl_id,
            "task": task,
            "due_date": due_date,
        }
        return ChatResponse(
            reply=f"I noted '{task}' due on {due_date}. "
                  f"When should I notify you? (e.g. '1 day before', '2 hours before', "
                  f"'on the 19th at 9am', or 'no reminder')")

    if action == "recall":
        if key:  # "what is my birthday?"
            row = get_memory(user_id, key)
            if row:
                return ChatResponse(reply=f"Your {row['key']} is {row['value']}.")
            return ChatResponse(reply=f"I don't have anything stored about your {key} yet.")
        if memories:
            lines = [f"• {m['key']}: {m['value']}" for m in memories]
            return ChatResponse(reply="Here's what I remember:\n" + "\n".join(lines))
        return ChatResponse(reply="I don't have any memories about you yet. "
                                  "Tell me something, e.g. 'remember my birthday is September 1'.")

    # --- general chat via Groq (AI), friendly hint otherwise ---
    if AI_ENABLED:
        try:
            mem_text = "; ".join(f"{m['key']}={m['value']}" for m in memories) or "none"
            reply = groq_chat(
                f"You are MemoryBot, a warm, helpful chatbot. "
                f"User's remembered facts: {mem_text}",
                msg,
            )
            return ChatResponse(reply=reply)
        except Exception:
            pass
    return ChatResponse(
        reply="Hmm, I couldn't understand that as a memory or deadline. "
              "Try: 'remember my birthday is September 1' or "
              "'I have a project review on the 20th'.")


# ---------------------------------------------------------------------------
# Utility endpoints (for your frontend)
# ---------------------------------------------------------------------------
@app.get("/memories/{user_id}")
def api_memories(user_id: str):
    return [{"id": m["id"], "key": m["key"], "value": m["value"],
             "updated_at": m["updated_at"]} for m in list_memories(user_id)]


@app.delete("/memories/{user_id}/{key}")
def api_delete_memory(user_id: str, key: str):
    with closing(get_db()) as conn:
        cur = conn.execute(
            "DELETE FROM memories WHERE user_id=? AND key=?",
            (user_id, key.lower().strip()),
        )
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "memory not found")
    return {"deleted": key}


@app.get("/deadlines/{user_id}")
def api_deadlines(user_id: str):
    with closing(get_db()) as conn:
        rows = conn.execute(
            "SELECT * FROM deadlines WHERE user_id=? ORDER BY due_date",
            (user_id,),
        ).fetchall()
    return [{"id": r["id"], "task": r["task"], "due_date": r["due_date"],
             "reminder_time": r["reminder_time"], "notified": bool(r["notified"])}
            for r in rows]


@app.get("/health")
def health():
    return {"status": "ok", "ai": "groq" if AI_ENABLED else "fallback",
            "model": GROQ_MODEL if AI_ENABLED else None,
            "telegram_enabled": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
