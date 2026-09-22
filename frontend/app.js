(() => {
  "use strict";

  const API_BASE = "https://memoriaai-production.up.railway.app";
  const STORAGE_KEY = "memoria_user_id";

  // ---------- elements ----------
  const chatLog      = document.getElementById("chatLog");
  const chatForm     = document.getElementById("chatForm");
  const chatInput    = document.getElementById("chatInput");
  const chatSend     = document.getElementById("chatSend");

  const memoriesList  = document.getElementById("memoriesList");
  const deadlinesList = document.getElementById("deadlinesList");

  const statusDot   = document.getElementById("statusDot");
  const statusText  = document.getElementById("statusText");
  const statusBrain = document.getElementById("statusBrain");
  const statusModel = document.getElementById("statusModel");

  const userChip     = document.getElementById("userChip");
  const userChipName = document.getElementById("userChipName");

  // ---------- state ----------
  let userId = localStorage.getItem(STORAGE_KEY) || "";

  // ---------- helpers ----------
  function escapeHtml(str) {
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
  }

  async function api(path, options = {}) {
    const url = `${API_BASE}${path}`;
    let res;
    try {
      res = await fetch(url, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
    } catch (err) {
      // fetch() throws on CORS blocks, network errors, DNS failures, etc.
      // The browser never gives JS the real reason for a CORS block, but
      // it DOES print a detailed CORS error to the console automatically —
      // so log loudly here and point the dev at it.
      console.error(`[api] fetch() threw for ${url}`, err);
      console.error(
        "[api] If you see a message above starting with " +
          "\"Access to fetch at ... has been blocked by CORS policy\", " +
          "this is a CORS problem on the backend, not a bug in this file."
      );
      throw new Error(`Network/CORS error reaching ${url}: ${err.message}`);
    }

    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        detail = body.detail || JSON.stringify(body);
      } catch (_) {
        // response wasn't JSON — keep statusText
      }
      console.error(`[api] ${res.status} ${res.statusText} for ${url}`, detail);
      throw new Error(`${res.status} ${detail}`);
    }

    if (res.status === 204) return null;

    try {
      return await res.json();
    } catch (err) {
      console.error(`[api] response from ${url} was not valid JSON`, err);
      throw new Error(`Bad JSON from ${url}: ${err.message}`);
    }
  }

  // ---------- user identity ----------
  function ensureUser() {
    if (!userId) {
      const proposed = window.prompt(
        "What should Memoria call you? (used to keep your memories separate)",
        "friend"
      );
      userId = (proposed || `guest-${Math.random().toString(36).slice(2, 8)}`).trim();
      localStorage.setItem(STORAGE_KEY, userId);
    }
    userChipName.textContent = userId;
  }

  userChip.addEventListener("click", () => {
    const proposed = window.prompt("Switch identity — who is Memoria talking to?", userId);
    if (proposed && proposed.trim()) {
      userId = proposed.trim();
      localStorage.setItem(STORAGE_KEY, userId);
      userChipName.textContent = userId;
      chatLog.innerHTML = "";
      addBubble("bot", `Switched. Hi ${escapeHtml(userId)}, what should I remember for you?`);
      refreshMemories();
      refreshDeadlines();
    }
  });

  // ---------- chat ----------
  function addBubble(role, text, { pending = false, error = false } = {}) {
    const el = document.createElement("div");
    el.className = `bubble bubble--${role}${pending ? " bubble--pending" : ""}${error ? " bubble--error" : ""}`;
    const p = document.createElement("p");
    p.textContent = text;
    el.appendChild(p);
    chatLog.appendChild(el);
    chatLog.scrollTop = chatLog.scrollHeight;
    return el;
  }

  async function sendMessage(message) {
    addBubble("user", message);
    chatInput.value = "";
    chatSend.disabled = true;
    const pendingEl = addBubble("bot", "…thinking", { pending: true });

    try {
      const data = await api("/chat", {
        method: "POST",
        body: JSON.stringify({ user_id: userId, message }),
      });
      pendingEl.remove();
      addBubble("bot", data.reply || "(no reply)");
      // the backend doesn't always echo updated state on /chat, so re-pull it
      refreshMemories();
      refreshDeadlines();
    } catch (err) {
      pendingEl.remove();
      addBubble("bot", `Couldn't reach the backend: ${err.message}`, { error: true });
    } finally {
      chatSend.disabled = false;
      chatInput.focus();
    }
  }

  chatForm.addEventListener("submit", (e) => {
    e.preventDefault();
    const message = chatInput.value.trim();
    if (!message) return;
    sendMessage(message);
  });

  // ---------- memories ----------
  async function refreshMemories() {
    try {
      const memories = await api(`/memories/${encodeURIComponent(userId)}`);
      renderMemories(memories);
    } catch (err) {
      console.error("[refreshMemories] failed:", err);
      memoriesList.innerHTML = `<p class="empty">Couldn't load memories.</p>`;
    }
  }

  function renderMemories(memories) {
    if (!memories || memories.length === 0) {
      memoriesList.innerHTML = `<p class="empty">Nothing filed away yet.</p>`;
      return;
    }
    memoriesList.innerHTML = "";
    memories.forEach((m) => {
      const row = document.createElement("div");
      row.className = "mem-item";
      row.innerHTML = `
        <span>
          <span class="mem-item__key">${escapeHtml(m.key)}</span>
          <span class="mem-item__value">${escapeHtml(m.value)}</span>
        </span>
        <button class="mem-item__del" title="Forget this" aria-label="Forget ${escapeHtml(m.key)}">×</button>
      `;
      row.querySelector(".mem-item__del").addEventListener("click", () => deleteMemory(m.key));
      memoriesList.appendChild(row);
    });
  }

  async function deleteMemory(key) {
    try {
      await api(`/memories/${encodeURIComponent(userId)}/${encodeURIComponent(key)}`, {
        method: "DELETE",
      });
      refreshMemories();
    } catch (err) {
      addBubble("bot", `Couldn't forget "${key}": ${err.message}`, { error: true });
    }
  }

  // ---------- deadlines ----------
  async function refreshDeadlines() {
    try {
      const deadlines = await api(`/deadlines/${encodeURIComponent(userId)}`);
      renderDeadlines(deadlines);
    } catch (err) {
      console.error("[refreshDeadlines] failed:", err);
      deadlinesList.innerHTML = `<p class="empty">Couldn't load deadlines.</p>`;
    }
  }

  function renderDeadlines(deadlines) {
    if (!deadlines || deadlines.length === 0) {
      deadlinesList.innerHTML = `<p class="empty">No deadlines on the board.</p>`;
      return;
    }
    deadlinesList.innerHTML = "";
    deadlines.forEach((d) => {
      const row = document.createElement("div");
      row.className = "dl-item";
      const badge = d.notified
        ? `<span class="dl-item__badge dl-item__badge--done">notified</span>`
        : d.reminder_time
        ? `<span class="dl-item__badge">reminder set</span>`
        : `<span class="dl-item__badge">no reminder</span>`;
      row.innerHTML = `
        <span>
          <span class="dl-item__task">${escapeHtml(d.task)}</span>
          <span class="dl-item__due">due ${escapeHtml(d.due_date)}</span>
        </span>
        ${badge}
      `;
      deadlinesList.appendChild(row);
    });
  }

  // ---------- health / status ----------
  async function refreshStatus() {
    try {
      const health = await api("/health");
      statusDot.className = "status-dot status-dot--ok";
      statusText.textContent = "connected";
      statusBrain.textContent = health.ai || "—";
      statusModel.textContent = health.model || "rule-based";
    } catch (err) {
      // Previously this swallowed the error entirely, so "offline" gave no
      // clue why. Now the real cause (CORS, network, bad JSON, etc.) is
      // logged above by api(), and we surface a short reason in the UI too.
      console.error("[refreshStatus] failed:", err);
      statusDot.className = "status-dot status-dot--bad";
      statusText.textContent = "offline";
      statusBrain.textContent = "—";
      statusModel.textContent = "—";
      if (statusText.dataset) {
        statusText.title = err.message; // hover to see the reason
      }
    }
  }

  // ---------- init ----------
  ensureUser();
  refreshStatus();
  refreshMemories();
  refreshDeadlines();
  chatInput.focus();
})();
