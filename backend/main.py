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

DB_PATH = "memory_bot.db"

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
    "september": 9
    "september": 9
