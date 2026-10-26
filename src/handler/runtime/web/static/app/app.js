"use strict";
const handlerBase = (window.HANDLER_CONFIG || {}).base || "";
const handlerInstance = (window.HANDLER_CONFIG || {}).instance || "";
// UI build this page booted with; the server stamps its own on every response.
const handlerBuild = (window.HANDLER_CONFIG || {}).build || "";
const handlerPath = (path) => handlerBase + path;
const handlerKey = (key) => handlerInstance ? "handler.instance." + handlerInstance + ":" + key : key;

// Handler web UI (phase 4). Vanilla JS, no build step, no CDN.

// ─── API client: every endpoint path the UI uses lives here ──────────────────
const enc = encodeURIComponent;
const encPath = (name) => String(name).split("/").map(enc).join("/");
const API = {
    me: "/api/me",
    threads: "/api/threads",
    thread: (k) => `/api/threads/${enc(k)}`,
    threadMessages: (k) => `/api/threads/${enc(k)}/messages`,
    threadRuns: (k) => `/api/threads/${enc(k)}/runs`,
    threadStream: (k) => `/api/threads/${enc(k)}/stream`,
    stream: "/api/stream",
    run: (id) => `/api/runs/${enc(id)}`,
    chat: "/api/chat",
    chatOptions: (k) => "/api/chat/options" + (k ? `?thread_key=${enc(k)}` : ""),
    prompt: (t) => `/api/settings/prompt?thread_type=${enc(t)}`,
    memory: "/api/memory",
    memoryItem: (n) => `/api/memory/${encPath(n)}`,
    automations: "/api/automations",
    automation: (id) => `/api/automations/${enc(id)}`,
    automationRun: (id) => `/api/automations/${enc(id)}/run`,
    usage: (days) => `/api/usage?days=${days}`,
    approvals: (status) => "/api/approvals" + (status ? `?status=${status}` : "?limit=200"),
    approve: (id) => `/api/approvals/${enc(id)}/approve`,
    deny: (id) => `/api/approvals/${enc(id)}/deny`,
    notifications: (unread) => "/api/notifications" + (unread ? "?unread_only=true" : ""),
    notificationRead: (id) => `/api/notifications/${enc(id)}/read`,
    settings: "/api/settings",
    setting: (s) => `/api/settings/${enc(s)}`,
    settingHistory: (s) => `/api/settings/${enc(s)}/history`,
    settingRollback: (s) => `/api/settings/${enc(s)}/rollback`,
    tools: "/api/tools",
    uploads: "/api/uploads",
    upload: (id) => `/api/uploads/${enc(id)}`,
    logs: (p) => "/api/logs?" + new URLSearchParams(p),
    connections: "/api/connections",
    users: "/api/users",
    user: (id) => `/api/users/${enc(id)}`,
    userTokens: (id) => `/api/users/${enc(id)}/tokens`,
    token: (id) => `/api/tokens/${enc(id)}`,
};

// ─── Basics ──────────────────────────────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
    (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const store = {
    get: (k) => { try { return sessionStorage.getItem(handlerKey(k)); } catch { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(handlerKey(k), v); } catch { /* ignore */ } },
    del: (k) => { try { sessionStorage.removeItem(handlerKey(k)); } catch { /* ignore */ } },
};
const pref = {
    get: (k) => { try { return localStorage.getItem(handlerKey(k)); } catch { return null; } },
    set: (k, v) => { try { localStorage.setItem(handlerKey(k), v); } catch { /* ignore */ } },
};
let me = null;
const isAdmin = () => me && me.role === "admin";

class ApiError extends Error {
    constructor(status, detail, path) {
        super(typeof detail === "string" ? detail : `${path} ${status}`);
        this.status = status; this.detail = detail; this.path = path;
    }
}

// crypto.randomUUID exists only in secure contexts (HTTPS or localhost);
// getRandomValues works over plain HTTP too (e.g. a LAN or tailnet host).
function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
    return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

function authHeaders(extra) {
    const h = {...(extra || {})};
    const token = store.get("handler.token");
    if (token) h.Authorization = "Bearer " + token;
    const actAs = store.get("handler.actAs");
    if (actAs) h["X-Handler-User"] = actAs;
    return h;
}

function goLogin() { location.replace(handlerPath("/login")); }
function logout() {
    store.del("handler.token"); store.del("handler.actAs");
    goLogin();
}

function detailText(d) {
    if (d == null) return "";
    if (typeof d === "string") return d;
    if (Array.isArray(d)) return d.map((e) => (e && e.msg)
        ? (e.loc ? e.loc.join(".") + ": " : "") + e.msg : JSON.stringify(e)).join("; ");
    if (d.detail !== undefined) return detailText(d.detail);
    return JSON.stringify(d);
}

// A tab left open across a deploy keeps running the old script against the new
// server (settings forms then mislabel or drop fields). Once a response comes
// from another build, the next tab switch reloads the page.
let staleUi = false;
function noteBuild(r) {
    const served = r && r.headers && typeof r.headers.get === "function" ? r.headers.get("X-Handler-Build") : null;
    if (staleUi || !served || !handlerBuild || served === handlerBuild) return;
    staleUi = true;
    toast("Handler was updated; it reloads when you switch tabs", false, 6000);
}
async function apiRaw(path, opts = {}) {
    const r = await fetch(handlerPath(path), {...opts, headers: authHeaders(opts.headers), cache: "no-store"});
    noteBuild(r);
    if (r.status === 401) { logout(); throw new ApiError(401, "unauthorized", path); }
    if (!r.ok) {
        let body = null;
        try { body = await r.json(); } catch { /* not json */ }
        throw new ApiError(r.status, detailText(body) || r.statusText, path);
    }
    return r;
}
async function api(path, opts = {}) {
    const r = await apiRaw(path, opts);
    if (r.status === 204) return null;
    const text = await r.text();
    return text ? JSON.parse(text) : null;
}
const sendJson = (method, path, body, headers) => api(path, {
    method, headers: {"Content-Type": "application/json", ...(headers || {})},
    body: JSON.stringify(body ?? {}),
});
// Returns fallback when the endpoint does not exist (yet) or is not allowed.
async function apiOpt(path, fallback = null) {
    try { return await api(path); }
    catch (e) {
        if (e instanceof ApiError && [404, 405, 501].includes(e.status)) return fallback;
        throw e;
    }
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
let toastTimer;
function toast(msg, isError = false, ms = 2500) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.toggle("error", isError);
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), ms);
}
const fail = (e) => toast(e.message || String(e), true, 4000);

const fmtTime = (s) => {
    if (!s) return "-";
    const d = new Date(s);
    return isNaN(d) ? String(s) : d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
};
function fmtAge(s) {
    if (!s) return "-";
    const sec = Math.max(0, (Date.now() - new Date(s).getTime()) / 1000);
    if (isNaN(sec)) return "-";
    if (sec < 60) return Math.floor(sec) + "s";
    if (sec < 3600) return Math.floor(sec / 60) + "m";
    if (sec < 86400) return Math.floor(sec / 3600) + "h";
    return Math.floor(sec / 86400) + "d";
}
const fmtCost = (c) => (c == null || c === "") ? "-" : "$" + Number(c).toFixed(4);
const fmtNum = (n) => n == null ? "-" : Number(n).toLocaleString();
const fmtSize = (n) => {
    if (n == null) return "-";
    if (n < 1024) return n + " B";
    if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
    return (n / 1048576).toFixed(1) + " MB";
};
const durMs = (a, b) => (a && b) ? new Date(b) - new Date(a) : null;
const fmtDur = (ms) => ms == null ? "-" : ms < 1000 ? ms + " ms" : (ms / 1000).toFixed(1) + " s";
const empty = (msg) => `<div class="empty-state">${esc(msg)}</div>`;
const badge = (text, kind = "gray") => `<span class="badge badge-${kind}">${esc(text)}</span>`;
const statusClass = (s) => "status-" + String(s || "").toLowerCase().replace(/[^a-z]/g, "");
const json = (v) => JSON.stringify(v, null, 2);

function confirmDialog(message, okLabel = "Delete") {
    return new Promise((resolve) => {
        const bg = document.createElement("div");
        bg.className = "modal-bg";
        bg.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-label="Confirm">
            <p>${esc(message)}</p><div class="inline-actions">
            <button class="btn-small btn-danger" data-ok>${esc(okLabel)}</button>
            <button class="btn-small" data-cancel>Cancel</button></div></div>`;
        const done = (v) => { bg.remove(); document.removeEventListener("keydown", onKey); resolve(v); };
        const onKey = (e) => { if (e.key === "Escape") done(false); };
        bg.addEventListener("click", (e) => {
            if (e.target === bg || e.target.closest("[data-cancel]")) done(false);
            else if (e.target.closest("[data-ok]")) done(true);
        });
        document.addEventListener("keydown", onKey);
        document.body.appendChild(bg);
        bg.querySelector("[data-cancel]").focus();
    });
}

function download(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name || "download";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 5000);
}
async function fetchBlob(path) { return (await apiRaw(path)).blob(); }

// Minimal safe markdown (input is escaped first).
function md(src) {
    const stash = [];
    const keep = (html) => { stash.push(html); return `\u0000${stash.length - 1}\u0000`; };
    let t = esc(src);
    t = t.replace(/```[^\n]*\n([\s\S]*?)```/g, (m, c) => keep(`<pre><code>${c}</code></pre>`));
    t = t.replace(/`([^`\n]+)`/g, (m, c) => keep(`<code>${c}</code>`));
    const inline = (s) => s
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>")
        .replace(/\[([^\]\n]+)\]\(((?:https?:\/\/|mailto:)[^)\s]+)\)/g,
            '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const out = [];
    for (const block of t.split(/\n{2,}/)) {
        const lines = block.split("\n");
        if (lines.every((l) => /^\s*[-*] /.test(l))) {
            out.push("<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*] /, ""))}</li>`).join("") + "</ul>");
        } else if (lines.every((l) => /^\s*\d+[.)] /.test(l))) {
            out.push("<ol>" + lines.map((l) => `<li>${inline(l.replace(/^\s*\d+[.)] /, ""))}</li>`).join("") + "</ol>");
        } else if (lines.every((l) => /^&gt; ?/.test(l))) {
            out.push("<blockquote>" + inline(lines.map((l) => l.replace(/^&gt; ?/, "")).join("<br>")) + "</blockquote>");
        } else if (/^(-{3,}|\*{3,})$/.test(block.trim())) {
            out.push("<hr>");
        } else {
            const buf = [];
            const flush = () => {
                if (!buf.length) return;
                const body = buf.join("<br>");
                out.push(/^\u0000\d+\u0000$/.test(body) ? body : "<p>" + inline(body) + "</p>");
                buf.length = 0;
            };
            for (const l of lines) {
                const h = /^(#{1,6}) (.*)$/.exec(l);
                if (h) { flush(); out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); }
                else buf.push(l);
            }
            flush();
        }
    }
    return out.join("").replace(/\u0000(\d+)\u0000/g, (m, i) => stash[+i]);
}

// ─── Theme ───────────────────────────────────────────────────────────────────
function applyTheme(theme) {
    const light = theme === "light";
    document.body.classList.toggle("light", light);
    $("theme-toggle").textContent = light ? "Dark mode" : "Light mode";
}
function preferredTheme() {
    const saved = pref.get("theme");
    if (saved === "light" || saved === "dark") return saved;
    return window.matchMedia && matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function setSidebarCollapsed(collapsed) {
    document.body.classList.toggle("sidebar-collapsed", collapsed);
    $("sidebar-toggle").textContent = collapsed ? ">" : "\u2039";
    $("sidebar-toggle").setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
}

// ─── Tabs ────────────────────────────────────────────────────────────────────
const loaders = {};   // tab name -> function
let activeTab = "chat";
let logTimer = null;

function switchTab(name) {
    const nav = document.querySelector(`.nav-item[data-tab="${name}"]`);
    if (!nav || nav.hidden) name = "chat";
    if (staleUi) { history.replaceState(null, "", "#" + name); location.reload(); return; }
    activeTab = name;
    document.body.classList.remove("sidebar-open", "thread-open");
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.remove("active"));
    document.querySelectorAll(".nav-item").forEach((n) => n.classList.toggle("active", n.dataset.tab === name));
    $("tab-" + name).classList.add("active");
    if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
    clearInterval(logTimer); logTimer = null;
    if (loaders[name]) Promise.resolve(loaders[name]()).catch(fail);
}

// Delegated click actions: any element with data-action="name".
const actions = {};
document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-action]");
    if (!t) return;
    const fn = actions[t.dataset.action];
    if (fn) { e.preventDefault(); Promise.resolve(fn(t, e)).catch(fail); }
});

// ─── Chat ────────────────────────────────────────────────────────────────────
const chat = {
    threads: [], key: null, stream: null, pending: [], sig: "", thinking: false,
    timers: {}, activity: new Map(), unread: new Set(), names: new Map(), initialized: false, request: 0,
};
const ownPrefix = () => `chat:${me.user_id}:`;
const isOwnChat = (key) => !!(me && me.user_id && key && key.startsWith(ownPrefix()));

function threadTitle(t) {
    if (t.title) return t.title.slice(0, 120);
    if (t.summary) return t.summary.slice(0, 60);
    if (chat.names.has(t.key)) return chat.names.get(t.key);
    if (t.type === "chat") {
        const parts = t.key.split(":");
        return "Chat " + parts.slice(2).join(":").slice(0, 8);
    }
    return t.key;
}

async function loadThreads() {
    const list = await api(API.threads);
    list.sort((a, b) => String(chat.activity.get(b.key) || b.pending_since || b.created_at)
        .localeCompare(String(chat.activity.get(a.key) || a.pending_since || a.created_at)));
    renderThreadList(list);
}

function renderThreadList(list = chat.threads) {
    setBadge("chat", chat.unread.size);
    chat.threads = list;
    const q = ($("thread-search").value || "").trim().toLowerCase();
    const shown = q ? list.filter((t) => `${threadTitle(t)} ${t.key}`.toLowerCase().includes(q)) : list;
    $("thread-list").innerHTML = shown.length ? shown.map((t) => {
        const own = isOwnChat(t.key);
        const kind = t.type === "chat" ? (own ? "" : badge("other chat", "gray")) : badge(t.type, t.type === "automation" ? "blue" : "green");
        const state = t.dead ? badge("dead", "red") : t.lease_owner ? badge("running", "blue") : t.pending_since ? badge("queued", "yellow") : "";
        const unread = chat.unread.has(t.key) ? badge("new activity", "blue") : "";
        return `<div class="file-item thread-item ${t.key === chat.key ? "active" : ""}" role="button" tabindex="0" data-action="select-thread" data-key="${esc(t.key)}">
            <div class="file-name"><span class="t">${esc(threadTitle(t))}</span>${kind}${state}${unread}</div>
            <div class="file-summary">${esc(fmtTime(chat.activity.get(t.key) || t.pending_since || t.created_at))}${t.last_error ? " · " + esc(t.last_error) : ""}</div></div>`;
    }).join("") : empty(q ? "No matching conversations" : "No conversations yet");
}

function renderChatEmpty() {
    const name = me && me.display_name ? `, ${esc(me.display_name.split(/\s+/)[0])}` : "";
    $("messages").innerHTML = `<div class="chat-empty">
        <div class="empty-mark" aria-hidden="true">&#9671;</div>
        <h1>How can I help${name}?</h1>
        <p>Ask Handler to work across your files, connected services, and recurring tasks.</p>
        <div class="starter-grid">
          <button class="starter" type="button" data-prompt="Summarize my recent activity and flag anything that needs attention."><span>&#10022;</span>Catch me up</button>
          <button class="starter" type="button" data-prompt="Review the files in my workspace and tell me what is most important."><span>&#9633;</span>Review my files</button>
          <button class="starter" type="button" data-prompt="Help me create an automation for a recurring task."><span>&#8635;</span>Create an automation</button>
        </div>
      </div>`;
}

function blockHtml(b, role) {
    if (b.type === "text") return role === "assistant" ? `<div>${md(b.text || "")}</div>` : `<span>${esc(b.text || "")}</span>`;
    if (b.type === "file") {
        const isImg = (b.content_type || "").startsWith("image/");
        const img = isImg ? `<img class="chat-image" alt="${esc(b.filename)}" data-blob="${esc(API.upload(b.file_id))}">` : "";
        return `${img}<button type="button" class="file-chip" data-action="download-upload" data-id="${esc(b.file_id)}" data-name="${esc(b.filename)}">&#128206; ${esc(b.filename || b.file_id)}</button>`;
    }
    if (b.type === "tool_call") return `<div><b>${esc(b.name || "tool")}</b></div><pre>${esc(typeof b.input === "string" ? b.input : json(b.input))}</pre>`;
    if (b.type === "tool_result") return `<div class="${b.is_error ? "status-error" : "muted"}">${b.is_error ? "error" : "result"}</div><pre>${esc(typeof b.output === "string" ? b.output : json(b.output))}</pre>`;
    if (b.type === "event") {
        const p = b.payload || {};
        return `<span>${esc(p.name || b.event_type || "event")}</span>` +
            (p.prompt ? `<div>${esc(p.prompt)}</div>` : "");
    }
    return `<pre>${esc(json(b))}</pre>`;
}

function notificationHtml(name, input, failed = false) {
    if (failed || !/(^|[_.])notify$/.test(name || "")) return "";
    if (typeof input === "string") { try { input = JSON.parse(input); } catch { return ""; } }
    return input && input.message ? `<div class="msg assistant"><b>Notification</b><div>${md(input.message)}</div></div>` : "";
}

function messageHtml(m) {
    const blocks = m.blocks || [];
    if (m.role === "tool") {
        const call = blocks.find((b) => b.type === "tool_call");
        const name = call ? call.name : "tool";
        const err = blocks.some((b) => b.type === "tool_result" && b.is_error);
        return notificationHtml(name, call && call.input, err) + `<details class="msg tool"><summary>${esc(name)}${err ? " " + badge("error", "red") : ""}</summary>${blocks.map((b) => blockHtml(b, "tool")).join("")}</details>`;
    }
    if (m.role === "event") return `<div class="msg event">${blocks.map((b) => blockHtml(b, "event")).join(" ")}</div>`;
    const cls = m.role === "assistant" ? "assistant" : "user";
    return `<div class="msg ${cls}">${blocks.map((b) => blockHtml(b, cls)).join("")}</div>`;
}

async function hydrateBlobs(root) {
    for (const img of root.querySelectorAll("img[data-blob]")) {
        const path = img.dataset.blob;
        img.removeAttribute("data-blob");
        fetchBlob(path).then((b) => { img.src = URL.createObjectURL(b); }).catch(() => { img.alt += " (unavailable)"; });
    }
}

function renderThinking() {
    const box = $("messages");
    let el = $("thinking");
    if (chat.thinking && !el) {
        el = document.createElement("div");
        el.id = "thinking"; el.className = "msg assistant thinking-msg";
        el.innerHTML = '<div class="thinking-dots"><span></span><span></span><span></span></div>';
        box.appendChild(el); box.scrollTop = box.scrollHeight;
    } else if (!chat.thinking && el) el.remove();
}

// Durable run steps expose work before the final transcript is committed and
// recover progress missed while the browser or streaming connection was away.
function stepHtml(step) {
    const d = step.data || {};
    if (step.kind === "assistant_text") return messageHtml({role: "assistant", blocks: [{type: "text", text: d.text}]});
    if (step.kind === "tool_call") return notificationHtml(step.name, d.input, step.status !== "ok") + `<details class="msg tool"><summary>${esc(step.name)} ${badge(step.status, step.status === "error" ? "red" : "blue")}</summary>` +
        blockHtml({type: "tool_call", name: step.name, input: d.input}, "tool") +
        (d.output != null ? blockHtml({type: "tool_result", output: d.output, is_error: step.status === "error"}, "tool") : "") + "</details>";
    if (step.kind === "error") return `<div class="msg event run-error">${esc(d.message || d.text || step.name)}</div>`;
    return "";
}

async function loadMessages(force) {
    if (!chat.key) { renderChatEmpty(); chat.sig = ""; return; }
    const key = chat.key;
    const request = ++chat.request;
    const [msgs, detail, runs] = await Promise.all([
        api(API.threadMessages(key)), api(API.thread(key)), api(API.threadRuns(key)),
    ]);
    const latest = runs.slice().sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0];
    // Completed transcript messages are canonical; uncommitted work uses steps.
    const live = latest && latest.status !== "completed" && !msgs.some((m) => m.run_id === latest.id)
        ? await api(API.run(latest.id)) : null;
    if (key !== chat.key || request !== chat.request) return;
    const pending = (detail.pending_events || []).filter((e) => e.type === "chat.message" || e.type === "automation.fired");
    const t = detail.thread || {};
    chat.thinking = !!(t.pending_since && !t.dead);
    const sig = JSON.stringify([msgs, pending, t.last_error, t.dead, live]);
    if (!force && sig === chat.sig) { renderThinking(); return; }
    chat.sig = sig;
    const box = $("messages");
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
    let html = msgs.map(messageHtml).join("");
    html += pending.map((e) => e.type === "chat.message"
        ? `<div class="msg user pending" title="Not yet answered">${blockHtml({type: "text", text: (e.payload || {}).text || ""}, "user")}</div>`
        : messageHtml({role: "event", blocks: [{type: "event", event_type: e.type, payload: e.payload}]})).join("");
    if (live) {
        html += `<div class="msg event" role="status">Agent run: ${esc(live.run.status)}</div>`;
        html += (live.steps || []).map(stepHtml).join("");
    }
    if (t.last_error || (live && live.run.error)) {
        const state = t.dead ? "Run failed and will not be retried" : t.pending_since ? "Run failed; retrying" : "Run failed";
        html += `<div class="msg event run-error" role="status">${esc(state)}: ${esc(t.last_error || live.run.error)}</div>`;
    }
    box.innerHTML = html || empty("No messages yet. Agent activity will appear here.");
    hydrateBlobs(box);
    renderThinking();
    if (atBottom || force) box.scrollTop = box.scrollHeight;
}

function setChatMode() {
    const key = chat.key;
    const canSend = !key || isOwnChat(key);
    $("input-area").hidden = !canSend;
    $("chat-readonly").hidden = canSend;
    $("chat-title").textContent = key ? (threadTitle(chat.threads.find((t) => t.key === key) || {key, type: "chat"})) : "New conversation";
    const tl = $("chat-timeline");
    tl.hidden = !key;
    if (key) tl.href = handlerPath("/timeline");
}

function debounce(name, fn, wait = 200) {
    clearTimeout(chat.timers[name]);
    chat.timers[name] = setTimeout(() => fn().catch(() => {}), wait);
}

function connectStream() {
    if (chat.stream) return;
    const q = "?access_token=" + enc(store.get("handler.token") || "") + "&act_as=" + enc(store.get("handler.actAs") || "");
    const es = new EventSource(handlerPath(API.stream + q));
    chat.stream = es;
    es.onerror = () => { api(API.me).catch(() => {}); };
    es.onopen = refreshChatActivity;
    es.onmessage = (ev) => {
        let m;
        try { m = JSON.parse(ev.data); } catch { return; }
        if (m.type === "task_updated") { onTaskUpdate(m); return; }
        if (m.type === "notification") {
            if (!me || String((m.data || {}).user_id) !== String(me.user_id)) return;
            toast((m.data || {}).message || "New notification", false, 8000);
            debounce("badges", refreshBadges, 150);
            if (activeTab === "notifications") debounce("notifications", loadNotifications, 150);
        }
        const mine = m.thread_key === chat.key;
        if (m.thread_key) {
            chat.activity.set(m.thread_key, m.at || new Date().toISOString());
            if (!mine || activeTab !== "chat") chat.unread.add(m.thread_key);
            if (m.type === "run_started" && (!mine || activeTab !== "chat")) {
                const thread = chat.threads.find((t) => t.key === m.thread_key);
                toast("Agent working in " + threadTitle(thread || {key: m.thread_key}));
            }
            renderThreadList();
        }
        if (mine && (m.type === "event_queued" || m.type === "run_started")) { chat.thinking = true; renderThinking(); }
        if (mine && m.type === "run_finished") { chat.thinking = false; renderThinking(); }
        if (mine) debounce("msgs", () => loadMessages(), 150);
        if (m.type !== "run_event") debounce("threads", loadThreads, 500);
        if (m.type === "run_finished" || m.type === "event_queued") debounce("badges", refreshBadges, 800);
    };
}

async function selectThread(key) {
    chat.key = key; chat.sig = ""; chat.thinking = false;
    chat.unread.delete(key);
    document.body.classList.remove("thread-open");
    const t = chat.threads.find((x) => x.key === key);
    if (t && t.pending_since && !t.dead) chat.thinking = true;
    setChatMode();
    renderChatOpts();
    refreshChatOpts().catch(() => {});
    await Promise.all([loadMessages(true), loadThreads()]);
    setChatMode();
    connectStream();
}
actions["select-thread"] = (el) => selectThread(el.dataset.key);
$("thread-list").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.dataset.action === "select-thread") {
        e.preventDefault(); selectThread(e.target.dataset.key);
    }
});
actions["download-upload"] = async (el) => download(await fetchBlob(API.upload(el.dataset.id)), el.dataset.name);

function newConversation() {
    chat.key = null; chat.sig = ""; chat.thinking = false;
    renderChatEmpty(); setChatMode(); loadThreads().catch(() => {});
    renderChatOpts(); refreshChatOpts().catch(() => {});
    connectStream();
    $("input").focus();
}
$("new-chat").addEventListener("click", newConversation);
$("thread-search").addEventListener("input", () => renderThreadList());
$("messages").addEventListener("click", (e) => {
    const starter = e.target.closest("[data-prompt]");
    if (!starter) return;
    $("input").value = starter.dataset.prompt;
    $("input").dispatchEvent(new Event("input"));
    $("input").focus();
});

function renderPending() {
    const box = $("pending-files");
    box.innerHTML = "";
    chat.pending.forEach((p, i) => {
        const tag = document.createElement("span");
        tag.className = "pending-file";
        if (p.preview) { const img = document.createElement("img"); img.src = p.preview; img.alt = p.file.name; tag.appendChild(img); }
        else tag.appendChild(document.createTextNode(p.file.name));
        const rm = document.createElement("button");
        rm.type = "button"; rm.className = "remove"; rm.textContent = "✕";
        rm.setAttribute("aria-label", "Remove " + p.file.name);
        rm.addEventListener("click", () => { chat.pending.splice(i, 1); renderPending(); });
        tag.appendChild(rm);
        box.appendChild(tag);
    });
}
function addFiles(files) {
    for (const f of files) {
        chat.pending.push({file: f, preview: f.type.startsWith("image/") ? URL.createObjectURL(f) : null});
    }
    renderPending();
}
$("upload-btn").addEventListener("click", () => $("file-input").click());
$("file-input").addEventListener("change", (e) => { addFiles(e.target.files); e.target.value = ""; });
$("input").addEventListener("paste", (e) => {
    const items = (e.clipboardData && e.clipboardData.items) || [];
    for (const it of items) {
        if (it.kind === "file" && it.type.startsWith("image/")) { e.preventDefault(); const f = it.getAsFile(); if (f) addFiles([f]); }
    }
});
$("input").addEventListener("input", function () {
    this.style.height = "auto"; this.style.height = Math.min(this.scrollHeight, 120) + "px";
});
$("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage().catch(fail); }
});
$("send-btn").addEventListener("click", () => sendMessage().catch(fail));

async function uploadFiles(files) {
    const fd = new FormData();
    for (const f of files) fd.append("files", f);
    return api(API.uploads, {method: "POST", body: fd});
}

// Per-message model / effort selection, remembered per thread in localStorage.
const CHAT_OPTS_KEY = "handler.chatOpts";
const chatOpts = {data: null, store: {}};
function loadOptStore() {
    try { return JSON.parse(localStorage.getItem(handlerKey(CHAT_OPTS_KEY)) || "{}") || {}; } catch { return {}; }
}
function saveOptStore() {
    try { localStorage.setItem(handlerKey(CHAT_OPTS_KEY), JSON.stringify(chatOpts.store)); } catch { /* storage unavailable */ }
}
const optSlot = (key) => key || "__new__";
// Label of a select's empty option: the value the server says it resolves to.
const defaultLabel = (v) => `Default (${v != null && v !== "" ? v : "harness default"})`;
function fillSelect(sel, values, dflt, current) {
    sel.innerHTML = "";
    sel.add(new Option(defaultLabel(dflt), ""));
    for (const v of values) sel.add(new Option(v, v));
    sel.value = values.includes(current) ? current : "";
}
function renderChatOpts() {
    const o = chatOpts.data;
    if (!o) return;
    const cur = chatOpts.store[optSlot(chat.key)] || {};
    fillSelect($("opt-model"), o.models, o.default_model, cur.model);
    fillSelect($("opt-effort"), o.reasoning_efforts, o.default_reasoning_effort, cur.reasoning_effort);
}
function rememberOpts() {
    chatOpts.store[optSlot(chat.key)] = {model: $("opt-model").value || null, reasoning_effort: $("opt-effort").value || null};
    saveOptStore();
}
// Defaults depend on the thread (an existing one keeps its harness and model).
async function refreshChatOpts() {
    const key = chat.key;
    const data = await apiOpt(API.chatOptions(key), null);
    if (key !== chat.key) return; // switched threads meanwhile
    chatOpts.data = data;
    $("opt-model").hidden = $("opt-effort").hidden = !data;
    renderChatOpts();
}
async function initChatOpts() {
    chatOpts.store = loadOptStore();
    await refreshChatOpts();
}
$("opt-model").addEventListener("change", rememberOpts);
$("opt-effort").addEventListener("change", rememberOpts);

let sending = false;
async function sendMessage() {
    if (sending) return;
    const input = $("input");
    const text = input.value.trim();
    if (!text && !chat.pending.length) return;
    sending = true; $("send-btn").disabled = true;
    try {
        let ids = [];
        if (chat.pending.length) {
            const up = await uploadFiles(chat.pending.map((p) => p.file));
            ids = (up || []).map((u) => u.id);
        }
        const body = {text: text || "(see attached files)", client_message_id: uuid()};
        if (ids.length) body.attachments = ids;
        if ($("opt-model").value) body.model = $("opt-model").value;
        if ($("opt-effort").value) body.reasoning_effort = $("opt-effort").value;
        if (chat.key && isOwnChat(chat.key)) body.conversation_id = chat.key.slice(ownPrefix().length);
        const r = await sendJson("POST", API.chat, body);
        if (r.thread_key !== chat.key && !chat.key) {
            chatOpts.store[optSlot(r.thread_key)] = chatOpts.store[optSlot(null)] || {};
            saveOptStore();
        }
        input.value = ""; input.style.height = "auto";
        chat.pending.forEach((p) => p.preview && URL.revokeObjectURL(p.preview));
        chat.pending = []; renderPending();
        chat.thinking = true;
        if (r.thread_key !== chat.key) await selectThread(r.thread_key);
        else { renderThinking(); await loadMessages(true); loadThreads().catch(() => {}); }
    } finally { sending = false; $("send-btn").disabled = false; }
}

function refreshChatActivity() {
    loadThreads().catch(() => {});
    if (chat.key) loadMessages().catch(() => {});
    refreshBadges().catch(() => {});
    if (activeTab === "notifications") loadNotifications().catch(() => {});
}

loaders.chat = async () => {
    if (!chatOpts.data) await initChatOpts().catch(() => {});
    const schedules = await apiOpt(API.automations, []).catch(() => []);
    chat.names = new Map(schedules.filter((a) => a.thread_key).map((a) => [a.thread_key, a.name]));
    await loadThreads();
    if (!chat.initialized) {
        chat.initialized = true;
        if (!chat.key && chat.threads.length) await selectThread(chat.threads[0].key);
    }
    if (chat.key) chat.unread.delete(chat.key);
    renderThreadList();
    setChatMode();
    if (!chat.stream) connectStream();
    if (chat.key) loadMessages().catch(() => {});
};

// ─── Memory ──────────────────────────────────────────────────────────────────
const mem = {items: [], current: null, version: null};
function memMsg(t) { $("mem-msg").textContent = t || ""; }

async function loadMemory() {
    const items = await apiOpt(API.memory, null);
    mem.items = items || [];
    $("memory-file-list").innerHTML = items === null ? empty("Memory API not available")
        : items.length ? items.map((m) => `<div class="file-item ${m.name === mem.current ? "active" : ""}" role="button" tabindex="0" data-action="mem-open" data-name="${esc(m.name)}">
            <div class="file-name">${esc(m.name)}</div>
            <div class="file-summary">${esc(m.description || "")}</div>
            <div class="file-size">v${esc(m.version)} · ${fmtSize(m.size)}${m.updated_by ? " · " + esc(m.updated_by) : ""}</div></div>`).join("")
            : empty("No memory topics yet");
}
async function openMemory(name) {
    const t = await api(API.memoryItem(name));
    mem.current = t.name || name; mem.version = t.version;
    $("mem-name").value = mem.current; $("mem-name").readOnly = true;
    $("mem-desc").value = t.description || ""; $("mem-content").value = t.content || "";
    memMsg("");
    loadMemory().catch(() => {});
}
actions["mem-open"] = (el) => openMemory(el.dataset.name);
$("memory-file-list").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.dataset.action === "mem-open") { e.preventDefault(); openMemory(e.target.dataset.name).catch(fail); }
});
$("mem-new").addEventListener("click", () => {
    mem.current = null; mem.version = null;
    $("mem-name").value = ""; $("mem-name").readOnly = false;
    $("mem-desc").value = ""; $("mem-content").value = ""; memMsg("");
    $("mem-name").focus();
});
$("mem-save").addEventListener("click", async () => {
    const name = $("mem-name").value.trim().replace(/\.md$/, "");
    if (!name) { memMsg("Enter a topic name."); return; }
    const headers = mem.current && mem.version != null ? {"If-Match": String(mem.version)} : {};
    try {
        await sendJson("PUT", API.memoryItem(name), {content: $("mem-content").value, description: $("mem-desc").value}, headers);
        toast("Saved");
        await openMemory(name);
    } catch (e) {
        if (e.status === 409) memMsg("Conflict: this topic was changed by someone else. Click it in the list to reload the latest version (your edits stay in the editor until you do).");
        else memMsg(e.message);
    }
});
$("mem-delete").addEventListener("click", async () => {
    if (!mem.current) return;
    if (!await confirmDialog(`Delete memory topic "${mem.current}"?`)) return;
    try {
        await api(API.memoryItem(mem.current), {method: "DELETE"});
        mem.current = null; mem.version = null;
        $("mem-name").value = ""; $("mem-desc").value = ""; $("mem-content").value = "";
        toast("Deleted"); await loadMemory();
    } catch (e) { memMsg(e.message); }
});
loaders.memory = loadMemory;

// ─── Automations ─────────────────────────────────────────────────────────────
const auto = {items: [], editing: null};
async function loadAutomations() {
    const items = await apiOpt(API.automations, null);
    auto.items = items || [];
    const box = $("automations-content");
    if (items === null) { box.innerHTML = empty("Automations API not available"); return; }
    if (!items.length) { box.innerHTML = empty("No automations yet"); return; }
    box.innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>Name</th><th>Schedule</th><th>Next run</th><th>Last result</th><th>Enabled</th><th></th></tr></thead><tbody>` +
        items.map((a) => {
            const lr = a.last_run;
            const last = lr ? `<span class="${statusClass(lr.status)}">${esc(lr.status)}</span> <span class="muted">${esc(fmtTime(lr.finished_at))}</span>${lr.error ? `<div class="err-text">${esc(lr.error)}</div>` : ""}` : `<span class="muted">${a.last_run_at ? esc(fmtTime(a.last_run_at)) : "never"}</span>`;
            return `<tr><td><b>${esc(a.name)}</b>${a.one_shot ? " " + badge("one-shot", "blue") : ""}<div class="muted" title="${esc(a.prompt)}">${esc((a.prompt || "").slice(0, 80))}</div></td>
                <td class="mono">${esc(a.spec)}<div class="muted">${esc(a.timezone || "")}</div></td>
                <td>${esc(fmtTime(a.next_run_at))}</td><td>${last}</td>
                <td><input type="checkbox" data-action="auto-toggle" data-id="${esc(a.id)}" ${a.enabled ? "checked" : ""} aria-label="Enabled ${esc(a.name)}"></td>
                <td><div class="inline-actions">
                  <button class="btn-small" data-action="auto-run" data-id="${esc(a.id)}">Run now</button>
                  <button class="btn-small" data-action="auto-edit" data-id="${esc(a.id)}">Edit</button>
                  ${a.thread_key ? `<button class="btn-small" data-action="goto-thread" data-key="${esc(a.thread_key)}">Thread</button>` : ""}
                  <button class="btn-small btn-danger" data-action="auto-del" data-id="${esc(a.id)}">Delete</button></div></td></tr>`;
        }).join("") + "</tbody></table></div>";
}
function showAutoForm(a) {
    auto.editing = a ? a.id : null;
    $("auto-form").hidden = false; $("auto-err").textContent = "";
    $("auto-name").value = a ? a.name : ""; $("auto-prompt").value = a ? a.prompt : "";
    $("auto-schedule").value = a ? a.spec : ""; $("auto-tz").value = a ? (a.timezone || "") : "";
    $("auto-notify").value = a ? (a.notify || []).join(", ") : "";
    $("auto-name").focus();
}
$("auto-new").addEventListener("click", () => showAutoForm(null));
$("auto-cancel").addEventListener("click", () => { $("auto-form").hidden = true; });
$("auto-refresh").addEventListener("click", () => loadAutomations().catch(fail));
$("auto-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const notify = $("auto-notify").value.split(",").map((s) => s.trim()).filter(Boolean);
    const body = {name: $("auto-name").value.trim(), prompt: $("auto-prompt").value, schedule: $("auto-schedule").value.trim(), notify};
    const tz = $("auto-tz").value.trim();
    if (tz) body.timezone = tz;
    try {
        if (auto.editing) await sendJson("PATCH", API.automation(auto.editing), body);
        else await sendJson("POST", API.automations, body);
        $("auto-form").hidden = true; toast("Saved"); await loadAutomations();
    } catch (err) { $("auto-err").textContent = err.message; }
});
actions["auto-edit"] = (el) => showAutoForm(auto.items.find((a) => a.id === el.dataset.id));
actions["auto-run"] = async (el) => { await api(API.automationRun(el.dataset.id), {method: "POST"}); toast("Triggered"); setTimeout(() => loadAutomations().catch(() => {}), 800); };
actions["auto-del"] = async (el) => {
    if (!await confirmDialog("Delete this automation?")) return;
    await api(API.automation(el.dataset.id), {method: "DELETE"}); toast("Deleted"); await loadAutomations();
};
document.addEventListener("change", (e) => {
    const t = e.target.closest('[data-action="auto-toggle"]');
    if (!t) return;
    sendJson("PATCH", API.automation(t.dataset.id), {enabled: t.checked}).then(loadAutomations).catch((err) => { fail(err); loadAutomations().catch(() => {}); });
});
actions["goto-thread"] = async (el) => { switchTab("chat"); await loadThreads(); await selectThread(el.dataset.key); };
loaders.automations = loadAutomations;

// ─── Tasks (background runs) ─────────────────────────────────────────────────
// Every run of a non-chat thread, plus queued work, from /api/tasks. Admins also
// get each task's step trace (model calls, tool calls, errors, approvals).
const act = {run: null, items: [], cursor: null};
const TASK_BADGE = {queued: "yellow", running: "blue", waiting_approval: "yellow",
    succeeded: "green", failed: "red", dead: "red"};
const taskBadge = (s) => badge(String(s || "").replace("_", " "), TASK_BADGE[s] || "gray");
async function loadIgnoredEmail() {
    const rows = await apiOpt("/api/events?source=email&status=ignored&limit=30", []);
    const box = $("activity-ignored");
    if (!rows || !rows.length) { box.innerHTML = ""; return; }
    box.innerHTML = `<div class="section-title" style="margin-top:20px">Ignored email</div><div class="table-wrap"><table class="data-table"><thead><tr><th>Received</th><th>From</th><th>Subject</th><th>Reason</th></tr></thead><tbody>` +
        rows.map((e) => { const p = e.payload || {}, f = p.forwarder || {};
            const detail = p.auth && p.auth.detail ? `<div class="muted">${esc(String(p.auth.detail).slice(0, 160))}</div>` : "";
            return `<tr><td>${esc(fmtTime(e.received_at))}</td><td class="mono">${esc(f.address || "")}</td><td>${esc(p.subject || "")}</td><td>${esc(p.ignored_reason || "")}${detail}</td></tr>`; }).join("") + "</tbody></table></div>";
}
function taskQuery(cursor) {
    const p = new URLSearchParams({limit: "50"});
    if ($("task-type").value) p.set("type", $("task-type").value);
    if ($("task-status").value) p.set("status", $("task-status").value);
    if (cursor) p.set("cursor", cursor);
    return p;
}
function taskRow(t) {
    const tokens = t.usage ? `${fmtNum(t.usage.input_tokens)} / ${fmtNum(t.usage.output_tokens)}` : "-";
    const when = t.started_at || t.queued_at;
    const dur = t.duration_ms != null ? fmtDur(t.duration_ms) : t.retry_at ? "retry " + fmtTime(t.retry_at) : "-";
    return `<tr class="step-row ${t.id === act.run ? "sel" : ""}" tabindex="0" data-action="act-run" data-id="${esc(t.id)}">
        <td>${taskBadge(t.status)}</td><td>${badge(t.thread_type, "gray")}</td>
        <td><b>${esc(t.trigger_summary)}</b><div class="muted">${esc(t.thread_title)}</div>
            ${t.summary ? `<div class="muted">${esc(t.summary)}</div>` : ""}
            ${t.error ? `<div class="err-text">${esc(String(t.error).slice(0, 200))}</div>` : ""}</td>
        <td title="${esc(when || "")}">${esc(fmtTime(when))}<div class="muted">${esc(fmtAge(when))} ago</div></td>
        <td>${esc(dur)}</td><td>${tokens}</td><td>${fmtCost(t.cost_usd)}</td></tr>`;
}
function renderTasks() {
    const box = $("activity-content");
    box.innerHTML = act.items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Status</th><th>Type</th><th>Task</th><th>Started</th><th>Duration</th><th>Tokens in/out</th><th>Cost</th></tr></thead><tbody>` +
        act.items.map(taskRow).join("") + "</tbody></table></div>" : empty("No tasks match these filters");
    $("activity-more").innerHTML = act.cursor ? `<button class="btn-small" type="button" data-action="act-more">Load more</button>` : "";
}
async function loadActivity() {
    loadIgnoredEmail().catch(() => {});
    if (!chat.stream) connectStream();  // live task_updated messages
    const page = await api("/api/tasks?" + taskQuery());
    act.items = page.items || []; act.cursor = page.next_cursor;
    renderTasks();
    if (act.run) loadSteps().catch(() => {});
}
actions["act-more"] = async () => {
    const page = await api("/api/tasks?" + taskQuery(act.cursor));
    act.items = act.items.concat(page.items || []); act.cursor = page.next_cursor;
    renderTasks();
};
function stepsTable(steps, t0ms, endms) {
    const span = Math.max(endms - t0ms, 1);
    return `<div class="table-wrap"><table class="data-table"><thead><tr><th>#</th><th>Kind</th><th>Name</th><th>Status</th><th style="width:30%">Timeline</th><th>Model</th><th>Tokens</th><th>Cost</th></tr></thead><tbody>` +
        steps.map((s, i) => {
            const st = new Date(s.started_at).getTime(), en = new Date(s.ended_at || s.started_at).getTime();
            const left = 100 * (st - t0ms) / span, w = 100 * (en - st) / span;
            const tok = s.usage ? `${fmtNum(s.usage.input_tokens)}/${fmtNum(s.usage.output_tokens)}` : "";
            const lat = s.latency_ms != null ? ` · ~${fmtDur(s.latency_ms)}` : "";
            return `<tr class="step-row" tabindex="0" data-action="act-step" data-seq="${i}"><td>${s.seq ?? ""}</td><td>${esc(s.kind)}</td><td>${esc(s.name)}</td>
                <td class="${statusClass(s.status)}">${esc(s.status)}</td>
                <td><div class="track"><i style="left:${left.toFixed(2)}%;width:${w.toFixed(2)}%"></i></div><span class="muted">${fmtDur(s.duration_ms)}${lat}</span></td>
                <td class="mono">${esc(s.model || "")}</td><td>${tok}</td><td>${fmtCost(s.cost_usd)}</td></tr>
                <tr hidden class="step-data" id="step-data-${i}"><td colspan="8"><pre>${esc(json({data: s.data, raw_ref: s.raw_ref}))}</pre></td></tr>`;
        }).join("") + "</tbody></table></div>";
}
async function loadSteps() {
    const box = $("activity-steps");
    const item = act.items.find((t) => t.id === act.run);
    if (!act.run || !item) { box.innerHTML = ""; return; }
    const open = `<button class="btn-small" type="button" data-action="goto-thread" data-key="${esc(item.thread_key)}">Open transcript</button>`;
    const approvals = item.status === "waiting_approval" ? ` <button class="btn-small" type="button" data-action="goto-approvals">Review approvals</button>` : "";
    if (!item.run_id) {
        box.innerHTML = `<div class="section-title">${taskBadge("queued")} ${esc(item.trigger_summary)}</div><div class="inline-actions">${open}</div>` +
            `<p class="muted">Waiting for a worker${item.retry_at ? "; retry after " + esc(fmtTime(item.retry_at)) : ""}.</p>`;
        return;
    }
    const t = await api("/api/tasks/" + enc(item.run_id));
    if (act.run !== item.id) return;
    const head = `<div class="section-title">${taskBadge(t.status)} ${esc(t.trigger_summary)} <span class="muted mono">${esc(t.run_id)}</span></div>
        <div class="inline-actions">${open}${approvals}</div>
        <p class="muted">${esc(t.thread_title)} · ${esc(t.harness || "")}${t.model ? " · " + esc(t.model) : ""} · ${esc(fmtDur(t.duration_ms))} · ${fmtCost(t.cost_usd)}</p>
        ${t.error ? `<div class="err-text">${esc(t.error)}</div>` : ""}`;
    if (!t.steps) { box.innerHTML = head + `<p class="muted">The step trace is visible to admins only.</p>`; return; }
    const t0 = new Date(t.started_at).getTime();
    const end = Math.max(...t.steps.map((s) => new Date(s.ended_at || s.started_at).getTime()),
        t.finished_at ? new Date(t.finished_at).getTime() : 0, t0 + 1);
    box.innerHTML = head + (t.steps.length ? stepsTable(t.steps, t0, end) : empty("No steps recorded"));
}
actions["act-run"] = (el) => { act.run = el.dataset.id; document.querySelectorAll("#activity-content tr.sel").forEach((r) => r.classList.remove("sel")); el.classList.add("sel"); return loadSteps(); };
actions["act-step"] = (el) => { const d = $("step-data-" + el.dataset.seq); if (d) d.hidden = !d.hidden; };
actions["goto-approvals"] = () => switchTab("approvals");
document.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.matches("tr.step-row[data-action]")) { e.preventDefault(); e.target.click(); }
});
$("act-refresh").addEventListener("click", () => loadActivity().catch(fail));
for (const id of ["task-type", "task-status"]) $(id).addEventListener("change", () => loadActivity().catch(fail));
// Called for task_updated stream messages: refresh the list (and the open trace).
function onTaskUpdate(m) {
    if (activeTab !== "activity") return;
    $("task-live").textContent = "Updated " + fmtTime(m.at || new Date().toISOString());
    debounce("tasks", loadActivity, 400);
}
loaders.activity = loadActivity;

// ─── Usage ───────────────────────────────────────────────────────────────────
const asRows = (x) => Array.isArray(x) ? x : (x && typeof x === "object") ? Object.entries(x).map(([k, v]) => ({key: k, ...(typeof v === "object" ? v : {value: v})})) : [];
const rowLabel = (r) => r.day ?? r.date ?? r.model ?? r.thread_key ?? r.thread ?? r.key ?? r.name ?? "-";
const rowTokens = (r) => r.total_tokens ?? r.tokens ?? (((r.input_tokens || 0) + (r.output_tokens || 0)) || (r.value ?? 0));

function usageTable(title, rows, labelHead) {
    if (!rows.length) return "";
    return `<div class="section-title">${esc(title)}</div><div class="table-wrap"><table class="data-table"><thead><tr><th>${esc(labelHead)}</th><th>Runs</th><th>Input</th><th>Output</th><th>Cost</th></tr></thead><tbody>` +
        rows.map((r) => `<tr><td class="mono">${esc(rowLabel(r))}</td><td>${fmtNum(r.runs)}</td><td>${fmtNum(r.input_tokens)}</td><td>${fmtNum(r.output_tokens)}</td><td>${fmtCost(r.cost_usd)}</td></tr>`).join("") + "</tbody></table></div>";
}
async function loadUsage() {
    const days = $("usage-days").value;
    const u = await apiOpt(API.usage(days), null);
    const box = $("usage-content");
    if (u === null) { box.innerHTML = empty("Usage API not available"); return; }
    const t = u.totals || {};
    const cards = [["Input tokens", fmtNum(t.input_tokens), `cached ${fmtNum(t.cached_input_tokens)}`], ["Output tokens", fmtNum(t.output_tokens), `reasoning ${fmtNum(t.reasoning_tokens)}`],
        ["Cost", t.cost_usd == null ? "unpriced" : "$" + Number(t.cost_usd).toFixed(2), `last ${days} day(s)`], ["Runs", fmtNum(t.runs), ""]];
    let html = `<div class="stat-cards">` + cards.map(([l, v, s]) => `<div class="stat-card"><div class="stat-label">${l}</div><div class="stat-value">${v}</div><div class="stat-sub">${esc(s)}</div></div>`).join("") + "</div>";
    const byDay = asRows(u.by_day).sort((a, b) => String(rowLabel(a)).localeCompare(String(rowLabel(b))));
    if (byDay.length) {
        const max = Math.max(...byDay.map(rowTokens), 1);
        html += `<div class="section-title">Tokens by day</div><div style="margin-bottom:28px">` + byDay.map((r) => {
            const tok = rowTokens(r);
            return `<div class="bar-row"><span class="mono">${esc(rowLabel(r))}</span><div class="bar-track"><div class="bar-fill" style="width:${(100 * tok / max).toFixed(1)}%"></div></div><span class="bar-val">${fmtNum(tok)} · ${fmtCost(r.cost_usd)}</span></div>`;
        }).join("") + "</div>";
    }
    html += usageTable("By model", asRows(u.by_model), "Model") + usageTable("Top threads", asRows(u.by_thread), "Thread");
    box.innerHTML = html;
}
$("usage-days").addEventListener("change", () => loadUsage().catch(fail));
$("usage-refresh").addEventListener("click", () => loadUsage().catch(fail));
loaders.usage = loadUsage;

// ─── Approvals ───────────────────────────────────────────────────────────────
function argsPreview(a) {
    const s = typeof a === "string" ? a : JSON.stringify(a);
    return s && s.length > 300 ? s.slice(0, 300) + "…" : s || "";
}
async function loadApprovals() {
    const history = $("appr-history").checked;
    const [pending, all] = await Promise.all([api(API.approvals("pending")), history ? api(API.approvals()) : Promise.resolve([])]);
    setBadge("approvals", pending.length);
    const box = $("approvals-content");
    let html = `<div class="section-title">Pending (${pending.length})</div>`;
    html += pending.length ? pending.map((a) => `<div class="card"><div><b>${esc(a.tool)}</b> <span class="muted mono">${esc(a.action)}</span> ${a.requested_by ? "by " + esc(a.requested_by) : ""} <span class="muted">· ${fmtAge(a.created_at)} ago · ${esc(a.thread_key)}</span></div>
        ${a.reason ? `<pre style="white-space:pre-wrap;word-break:break-word;margin:8px 0">${esc(a.reason)}</pre>` : ""}
        <pre class="mono" style="white-space:pre-wrap;word-break:break-all;margin:8px 0">${esc(argsPreview(a.args))}</pre>
        <div class="inline-actions"><input type="text" id="reason-${esc(a.id)}" placeholder="Reason (optional)" aria-label="Reason">
        <button class="btn-small btn-primary" data-action="appr-approve" data-id="${esc(a.id)}">Approve</button>
        <button class="btn-small btn-danger" data-action="appr-deny" data-id="${esc(a.id)}">Deny</button></div></div>`).join("") : empty("Nothing waiting for approval");
    if (history) {
        const done = all.filter((a) => a.status !== "pending");
        html += `<div class="section-title">History</div>` + (done.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Tool</th><th>Status</th><th>Requested by</th><th>Decided by</th><th>When</th><th>Reason</th></tr></thead><tbody>` +
            done.map((a) => `<tr><td>${esc(a.tool)}<div class="muted mono">${esc(argsPreview(a.args).slice(0, 80))}</div></td><td class="${statusClass(a.status)}">${esc(a.status)}</td><td>${esc(a.requested_by || "agent")}</td><td>${esc(a.decided_by || "-")}</td><td>${esc(fmtTime(a.decided_at || a.created_at))}</td><td>${esc(a.reason || "")}</td></tr>`).join("") + "</tbody></table></div>" : empty("No decided approvals"));
    }
    box.innerHTML = html;
}
actions["appr-approve"] = async (el) => { await sendJson("POST", API.approve(el.dataset.id), {}); toast("Approved"); await loadApprovals(); };
actions["appr-deny"] = async (el) => {
    const r = $("reason-" + el.dataset.id);
    await sendJson("POST", API.deny(el.dataset.id), {reason: r ? r.value : ""}); toast("Denied"); await loadApprovals();
};
$("appr-history").addEventListener("change", () => loadApprovals().catch(fail));
$("appr-refresh").addEventListener("click", () => loadApprovals().catch(fail));
loaders.approvals = loadApprovals;

// ─── Notifications ───────────────────────────────────────────────────────────
const isRead = (n) => !!(n.read_at || n.read === true);
async function loadNotifications() {
    const unreadOnly = $("notif-unread").checked;
    let items;
    try { items = await api(API.notifications(unreadOnly)); }
    catch (e) {
        if (e.status === 400) { $("notifications-content").innerHTML = empty("Notifications need a user. Sign in as a user or set 'Act as'."); return; }
        throw e;
    }
    items = unreadOnly ? items.filter((n) => !isRead(n)) : items;
    const box = $("notifications-content");
    box.innerHTML = items.length ? (items.some((n) => !isRead(n)) ? `<div class="inline-actions" style="margin-bottom:12px"><button class="btn-small" data-action="notif-all">Mark all read</button></div>` : "") +
        items.map((n) => `<div class="card" ${isRead(n) ? 'style="opacity:.6"' : ""}><div>${esc(n.message)}</div>
            <div class="muted">${esc(fmtTime(n.created_at))} · <a class="link" role="button" tabindex="0" data-action="goto-thread" data-key="${esc(n.thread_key)}">${esc(n.thread_key)}</a></div>
            ${isRead(n) ? "" : `<div class="inline-actions" style="margin-top:8px"><button class="btn-small" data-action="notif-read" data-id="${esc(n.id)}">Mark read</button></div>`}</div>`).join("")
        : empty(unreadOnly ? "No unread notifications" : "No notifications");
    refreshBadges().catch(() => {});
}
actions["notif-read"] = async (el) => { await api(API.notificationRead(el.dataset.id), {method: "POST"}); await loadNotifications(); };
actions["notif-all"] = async () => {
    const items = (await api(API.notifications(true))).filter((n) => !isRead(n));
    await Promise.all(items.map((n) => api(API.notificationRead(n.id), {method: "POST"})));
    await loadNotifications();
};
$("notif-unread").addEventListener("change", () => loadNotifications().catch(fail));
$("notif-refresh").addEventListener("click", () => loadNotifications().catch(fail));
loaders.notifications = loadNotifications;

function setBadge(name, n) {
    const b = $("badge-" + name);
    b.textContent = n > 99 ? "99+" : n; b.hidden = !n;
}
async function refreshBadges() {
    const [appr, notes] = await Promise.all([
        api(API.approvals("pending")).catch(() => null),
        me && me.user_id ? api(API.notifications(true)).catch(() => null) : Promise.resolve(null),
    ]);
    if (appr) setBadge("approvals", appr.length);
    if (notes) setBadge("notifications", notes.filter((n) => !isRead(n)).length);
}

// ─── Settings (forms rendered from JSON Schema) ──────────────────────────────
const sett = {all: null, section: null, build: null};
const LONG_TEXT = new Set(["system", "identity", "persona", "prompt", "instructions"]);

function resolveSchema(schema, root) {
    let s = schema || {}, nullable = false, extra = {};
    for (let i = 0; i < 8; i++) {
        extra = {...s, ...extra};
        if (s.$ref) { s = {...root.$defs[s.$ref.split("/").pop()]}; continue; }
        if (s.allOf && s.allOf.length === 1) { s = s.allOf[0]; continue; }
        const alt = s.anyOf || s.oneOf;
        if (alt) {
            const nonNull = alt.filter((a) => a.type !== "null");
            if (nonNull.length < alt.length) nullable = true;
            if (nonNull.length === 1) { s = nonNull[0]; continue; }
        }
        break;
    }
    return {schema: s, nullable, title: extra.title, description: extra.description};
}

function field(label, node, description) {
    const wrap = document.createElement("div");
    wrap.className = "sf-field";
    const l = document.createElement("label");
    l.className = "sf-label"; l.textContent = label;
    wrap.appendChild(l);
    if (node.id === undefined || node.id === "") { node.id = "sf-" + Math.random().toString(36).slice(2); }
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(node.tagName)) l.htmlFor = node.id;
    wrap.appendChild(node);
    if (description) { const h = document.createElement("span"); h.className = "help"; h.textContent = description; wrap.appendChild(h); }
    return wrap;
}

// Returns {el, get}; get() throws Error on invalid input.
function buildNode(schemaIn, value, root, name, dflt) {
    const {schema, nullable, description} = resolveSchema(schemaIn, root);
    if (schema.enum) {
        const sel = document.createElement("select");
        if (nullable) sel.add(new Option(defaultLabel(dflt), ""));
        for (const v of schema.enum) sel.add(new Option(String(v), String(v)));
        sel.value = value == null ? "" : String(value);
        return {el: sel, description, get: () => sel.value === "" ? null : sel.value};
    }
    if (schema.type === "boolean") {
        const c = document.createElement("input");
        c.type = "checkbox"; c.checked = !!value;
        return {el: c, description, get: () => c.checked, inline: true};
    }
    if (schema.type === "integer" || schema.type === "number") {
        const i = document.createElement("input");
        i.type = "number"; i.step = schema.type === "integer" ? "1" : "any";
        if (schema.minimum !== undefined) i.min = schema.minimum;
        if (schema.exclusiveMinimum !== undefined) i.min = schema.exclusiveMinimum;
        i.value = value == null ? "" : value;
        return {el: i, description, get: () => {
            if (i.value === "") { if (nullable) return null; throw new Error(`${name}: a number is required`); }
            return Number(i.value);
        }};
    }
    if (schema.type === "string") {
        const long = LONG_TEXT.has(name) || (typeof value === "string" && (value.includes("\n") || value.length > 80)) || (schema.maxLength || 0) > 200;
        const i = document.createElement(long ? "textarea" : "input");
        if (long) { i.rows = 8; i.className = "input"; } else i.type = "text";
        i.value = value == null ? "" : value;
        return {el: i, description, get: () => (i.value === "" && nullable) ? null : i.value};
    }
    if (schema.type === "object" && schema.properties) {
        const fs = document.createElement("fieldset");
        fs.className = "sf-group";
        const lg = document.createElement("legend"); lg.textContent = name; fs.appendChild(lg);
        const getters = {}, children = {};
        for (const [k, sub] of Object.entries(schema.properties)) {
            const child = buildNode(sub, value ? value[k] : undefined, root, k, dflt ? dflt[k] : undefined);
            getters[k] = child.get; children[k] = child;
            fs.appendChild(child.fieldset ? child.el : wrapField(k, child));
        }
        followHarness(children);
        return {el: fs, fieldset: true, get: () => Object.fromEntries(Object.entries(getters).map(([k, g]) => [k, g()]))};
    }
    if (schema.type === "object" && schema.additionalProperties) {
        const keySchema = schema.propertyNames ? resolveSchema(schema.propertyNames, root).schema : null;
        const fs = document.createElement("fieldset");
        fs.className = "sf-group";
        const lg = document.createElement("legend"); lg.textContent = name; fs.appendChild(lg);
        const rows = document.createElement("div"); fs.appendChild(rows);
        const entries = [];
        const addRow = (k, v) => {
            const row = document.createElement("div"); row.className = "sf-row";
            let keyEl;
            if (keySchema && keySchema.enum) {
                keyEl = document.createElement("select");
                for (const o of keySchema.enum) keyEl.add(new Option(o, o));
                keyEl.value = k || keySchema.enum[0];
            } else { keyEl = document.createElement("input"); keyEl.type = "text"; keyEl.value = k || ""; keyEl.placeholder = "key"; }
            keyEl.setAttribute("aria-label", name + " key");
            const child = buildNode(schema.additionalProperties, v, root, name, dflt ? dflt[k] : undefined);
            child.el.setAttribute("aria-label", name + " value");
            const rm = document.createElement("button");
            rm.type = "button"; rm.className = "btn-small"; rm.textContent = "Remove";
            const rec = {keyEl, child};
            rm.addEventListener("click", () => { row.remove(); entries.splice(entries.indexOf(rec), 1); });
            row.append(keyEl, child.el, rm);
            rows.appendChild(row); entries.push(rec);
        };
        for (const [k, v] of Object.entries(value || {})) addRow(k, v);
        const add = document.createElement("button");
        add.type = "button"; add.className = "btn-small"; add.textContent = "+ Add";
        add.addEventListener("click", () => addRow("", undefined));
        fs.appendChild(add);
        const spacer = document.createElement("div"); spacer.style.height = "10px"; fs.appendChild(spacer);
        return {el: fs, fieldset: true, get: () => {
            const out = {};
            for (const {keyEl, child} of entries) {
                if (!keyEl.value.trim()) throw new Error(`${name}: empty key`);
                out[keyEl.value.trim()] = child.get();
            }
            return out;
        }};
    }
    if (schema.type === "array" && schema.items && ["string", "integer", "number"].includes(resolveSchema(schema.items, root).schema.type)) {
        const fs = document.createElement("fieldset");
        fs.className = "sf-group";
        const lg = document.createElement("legend"); lg.textContent = name; fs.appendChild(lg);
        const rows = document.createElement("div"); fs.appendChild(rows);
        const entries = [];
        const addRow = (v) => {
            const row = document.createElement("div"); row.className = "sf-row";
            const child = buildNode(schema.items, v, root, name);
            const rm = document.createElement("button");
            rm.type = "button"; rm.className = "btn-small"; rm.textContent = "Remove";
            rm.addEventListener("click", () => { row.remove(); entries.splice(entries.indexOf(child), 1); });
            row.append(child.el, rm); rows.appendChild(row); entries.push(child);
        };
        (value || []).forEach(addRow);
        const add = document.createElement("button");
        add.type = "button"; add.className = "btn-small"; add.textContent = "+ Add";
        add.addEventListener("click", () => addRow(undefined));
        fs.appendChild(add);
        const spacer = document.createElement("div"); spacer.style.height = "10px"; fs.appendChild(spacer);
        return {el: fs, fieldset: true, get: () => entries.map((c) => c.get())};
    }
    // Fallback: raw JSON.
    const ta = document.createElement("textarea");
    ta.className = "input"; ta.rows = 5; ta.value = json(value ?? null);
    return {el: ta, description, get: () => { try { return JSON.parse(ta.value); } catch { throw new Error(`${name}: invalid JSON`); } }};
}
// A thread type's model/effort defaults depend on its harness: relabel their
// "Default (...)" options from the server's per-harness table when it changes.
function followHarness(children) {
    const table = sett.harnessDefaults, h = children.harness;
    if (!table || !h || h.el.tagName !== "SELECT") return;
    h.el.addEventListener("change", () => {
        const d = table[h.el.value] || {};
        for (const k of ["model", "reasoning_effort"]) {
            const opt = children[k] && children[k].el.tagName === "SELECT" && children[k].el.options[0];
            if (opt && opt.value === "") opt.text = defaultLabel(d[k]);
        }
    });
}
function wrapField(label, child) {
    if (child.inline) {
        const w = field(label, child.el, child.description);
        w.style.flexDirection = "row"; w.style.alignItems = "center"; w.style.gap = "10px";
        w.insertBefore(child.el, w.firstChild); // checkbox before its label
        return w;
    }
    return field(label, child.el, child.description);
}

async function loadSettings() {
    const all = await apiOpt(API.settings, null);
    sett.all = all;
    if (!all) { $("settings-form").innerHTML = empty("Settings API not available"); return; }
    const names = Object.keys(all);
    $("settings-sections").innerHTML = names.map((n) => `<div class="file-item ${n === sett.section ? "active" : ""}" role="button" tabindex="0" data-action="sett-open" data-name="${esc(n)}"><div class="file-name">${esc(n)}</div></div>`).join("");
    if (!sett.section || !all[sett.section]) sett.section = names[0];
    openSection(sett.section);
}
function openSection(name) {
    sett.section = name;
    const s = sett.all[name];
    document.querySelectorAll("#settings-sections .file-item").forEach((e) => e.classList.toggle("active", e.dataset.name === name));
    $("settings-title").textContent = name;
    $("settings-version").textContent = s.version == null ? "defaults (never saved)" : "version " + s.version;
    $("settings-err").textContent = ""; $("settings-history").innerHTML = "";
    const form = $("settings-form");
    form.innerHTML = "";
    const root = s.schema;
    sett.harnessDefaults = s.harness_defaults || null;
    const top = buildNode(root, s.value, root, name, s.defaults);
    sett.build = top;
    if (top.fieldset) {
        // Flatten the outermost group: its legend is the section title already.
        top.el.querySelector("legend").remove();
        top.el.style.border = "none"; top.el.style.padding = "0";
    }
    form.appendChild(top.fieldset ? top.el : field(name, top.el, top.description));
    const admin = isAdmin();
    $("settings-save").hidden = !admin; $("settings-reset").hidden = !admin;
    $("settings-actions").hidden = false;
    if (!admin) form.querySelectorAll("input,select,textarea,button").forEach((e) => { e.disabled = true; });
    renderPromptPreview(name);
}
const PROMPT_TYPES = ["chat", "gmail", "automation", "crontal", "email", "mail"];
let promptType = "chat";
function renderPromptPreview(section) {
    const box = $("settings-prompt");
    box.innerHTML = "";
    if (section !== "persona") return;
    const title = document.createElement("div");
    title.className = "section-title"; title.style.marginTop = "20px"; title.textContent = "Rendered prompt";
    const sel = document.createElement("select");
    sel.setAttribute("aria-label", "Thread type");
    for (const t of PROMPT_TYPES) sel.add(new Option(t, t));
    sel.value = promptType;
    const pre = document.createElement("pre");
    pre.className = "prompt-preview"; pre.textContent = "Loading...";
    const load = async () => {
        promptType = sel.value;
        try { const r = await api(API.prompt(sel.value)); pre.textContent = r.prompt; }
        catch (e) { pre.textContent = "Could not load prompt: " + e.message; }
    };
    sel.addEventListener("change", load);
    const note = document.createElement("span");
    note.className = "help"; note.textContent = "Read-only: saved persona plus memory index, as the agent sees it (unsaved edits not included).";
    box.append(title, sel, note, pre);
    load();
}
actions["sett-open"] = (el) => openSection(el.dataset.name);
$("settings-sections").addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && e.target.dataset.action === "sett-open") { e.preventDefault(); openSection(e.target.dataset.name); }
});
$("settings-form").addEventListener("submit", (e) => e.preventDefault());
$("settings-reset").addEventListener("click", () => openSection(sett.section));
$("settings-save").addEventListener("click", async () => {
    const err = $("settings-err"); err.textContent = "";
    let value;
    try { value = sett.build.get(); } catch (e) { err.textContent = e.message; return; }
    const s = sett.all[sett.section];
    const headers = s.version != null ? {"If-Match": String(s.version)} : {};
    try {
        await sendJson("PUT", API.setting(sett.section), value, headers);
        toast("Saved");
        sett.all[sett.section] = await api(API.setting(sett.section));
        openSection(sett.section);
    } catch (e) {
        err.textContent = e.status === 409 ? "Conflict: these settings changed elsewhere (" + e.message + "). Click Reset to load the latest version." : e.message;
    }
});
$("settings-hist-btn").addEventListener("click", async () => {
    const hist = await apiOpt(API.settingHistory(sett.section), []);
    $("settings-history").innerHTML = `<div class="section-title" style="margin-top:20px">History</div>` + (hist.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Version</th><th>Updated by</th><th>When</th><th></th></tr></thead><tbody>` +
        [...hist].sort((a, b) => b.version - a.version).map((h) => `<tr><td>${esc(h.version)}</td><td>${esc(h.updated_by || "-")}</td><td>${esc(fmtTime(h.updated_at))}</td><td>${isAdmin() ? `<button class="btn-small" data-action="sett-rollback" data-version="${esc(h.version)}">Roll back</button>` : ""}</td></tr>`).join("") + "</tbody></table></div>" : empty("No history"));
});
actions["sett-rollback"] = async (el) => {
    if (!await confirmDialog(`Roll back "${sett.section}" to version ${el.dataset.version}?`, "Roll back")) return;
    try {
        await sendJson("POST", API.settingRollback(sett.section), {version: Number(el.dataset.version)});
        toast("Rolled back");
        sett.all[sett.section] = await api(API.setting(sett.section));
        openSection(sett.section);
    } catch (e) { $("settings-err").textContent = e.message; }
};
loaders.settings = loadSettings;

// ─── Tools ───────────────────────────────────────────────────────────────────
const riskKind = (r) => ({read: "green", write_internal: "blue", write_external: "yellow", exec: "red"}[r] || "gray");
const decisionKind = (d) => ({allow: "green", ask: "yellow", deny: "red"}[d] || "gray");
async function loadTools() {
    const tools = await apiOpt(API.tools, null);
    const box = $("tools-content");
    if (tools === null) { box.innerHTML = empty("Tools API not available"); return; }
    box.innerHTML = tools.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Tool</th><th>Action</th><th>Risk</th><th>Decision</th><th>Description</th></tr></thead><tbody>` +
        tools.map((t) => `<tr><td class="mono">${esc(t.name)}</td><td class="mono">${esc(t.action)}</td><td>${badge(t.risk, riskKind(t.risk))}</td><td>${badge(t.decision, decisionKind(t.decision))}</td><td>${esc(t.description)}</td></tr>`).join("") + "</tbody></table></div>" : empty("No tools");
}
$("tools-refresh").addEventListener("click", () => loadTools().catch(fail));
loaders.tools = loadTools;

// ─── Files ───────────────────────────────────────────────────────────────────
async function loadFiles() {
    const files = await apiOpt(API.uploads, null);
    const box = $("files-content");
    if (files === null) { box.innerHTML = empty("Uploads API not available"); return; }
    box.innerHTML = files.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Name</th><th>Type</th><th>Size</th>${files.some((f) => f.owner || f.user_id) ? "<th>Owner</th>" : ""}<th></th></tr></thead><tbody>` +
        files.map((f) => `<tr><td>${esc(f.filename)}</td><td class="muted">${esc(f.content_type || "")}</td><td>${fmtSize(f.size)}</td>${files.some((x) => x.owner || x.user_id) ? `<td>${esc(f.owner || f.user_id || "")}</td>` : ""}
            <td><div class="inline-actions"><button class="btn-small" data-action="file-dl" data-id="${esc(f.id)}" data-name="${esc(f.filename)}">Download</button>
            <button class="btn-small btn-danger" data-action="file-del" data-id="${esc(f.id)}" data-name="${esc(f.filename)}">Delete</button></div></td></tr>`).join("") + "</tbody></table></div>" : empty("No uploaded files");
}
actions["file-dl"] = async (el) => download(await fetchBlob(API.upload(el.dataset.id)), el.dataset.name);
actions["file-del"] = async (el) => {
    if (!await confirmDialog(`Delete ${el.dataset.name}?`)) return;
    await api(API.upload(el.dataset.id), {method: "DELETE"}); toast("Deleted"); await loadFiles();
};
$("files-upload").addEventListener("click", () => $("files-input").click());
$("files-input").addEventListener("change", async (e) => {
    const files = [...e.target.files]; e.target.value = "";
    if (!files.length) return;
    $("files-err").textContent = "";
    try { await uploadFiles(files); toast("Uploaded"); await loadFiles(); }
    catch (err) { $("files-err").textContent = err.message; }
});
$("files-refresh").addEventListener("click", () => loadFiles().catch(fail));
loaders.files = loadFiles;

// ─── Logs (admin) ────────────────────────────────────────────────────────────
async function loadLogs() {
    const params = {lines: $("log-lines").value};
    if ($("log-level").value) params.level = $("log-level").value;
    if ($("log-q").value.trim()) params.q = $("log-q").value.trim();
    const logs = await apiOpt(API.logs(params), null);
    const box = $("logs-content");
    if (logs === null) { box.textContent = "Logs API not available"; return; }
    const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    const skip = new Set(["ts", "time", "timestamp", "asctime", "level", "levelname", "logger", "name", "message", "msg", "event"]);
    box.innerHTML = logs.length ? logs.map((l) => {
        if (typeof l === "string") return `<div class="log-line">${esc(l)}</div>`;
        const lvl = String(l.level || l.levelname || "").toLowerCase();
        const rest = Object.fromEntries(Object.entries(l).filter(([k]) => !skip.has(k)));
        return `<div class="log-line lvl-${esc(lvl)}">${esc(l.ts || l.time || l.timestamp || l.asctime || "")} ${esc(lvl.toUpperCase())} ${esc(l.logger || l.name || "")} ${esc(l.message ?? l.msg ?? l.event ?? "")}${Object.keys(rest).length ? " " + esc(JSON.stringify(rest)) : ""}</div>`;
    }).join("") : "No log lines";
    if (atBottom) box.scrollTop = box.scrollHeight;
}
function setLogAuto() {
    clearInterval(logTimer); logTimer = null;
    if ($("log-auto").checked && activeTab === "logs") logTimer = setInterval(() => loadLogs().catch(() => {}), 5000);
}
$("log-auto").addEventListener("change", setLogAuto);
for (const id of ["log-level", "log-lines"]) $(id).addEventListener("change", () => loadLogs().catch(fail));
$("log-q").addEventListener("keydown", (e) => { if (e.key === "Enter") loadLogs().catch(fail); });
$("log-refresh").addEventListener("click", () => loadLogs().catch(fail));
loaders.logs = async () => { await loadLogs(); setLogAuto(); $("logs-content").scrollTop = $("logs-content").scrollHeight; };

// ─── Connections ─────────────────────────────────────────────────────────────
async function loadConnections() {
    const items = await apiOpt(API.connections, null);
    const box = $("connections-content");
    if (items === null) { box.innerHTML = empty("Connections API not available"); return; }
    box.innerHTML = items.length ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Connector</th><th>Owner</th><th>Provider</th><th>Status</th><th>Scopes</th><th>Last used</th></tr></thead><tbody>` +
        items.map((c) => `<tr><td><b>${esc(c.connector)}</b></td><td>${esc(c.owner)}</td><td>${esc(c.provider)}</td><td>${badge(c.status, c.status === "active" ? "green" : c.status === "needs_reauth" ? "yellow" : "red")}</td><td class="mono">${esc((c.scopes || []).join(", "))}</td><td>${esc(fmtTime(c.last_used_at))}</td></tr>`).join("") + "</tbody></table></div>" : empty("No connections yet");
}
// Google (Gmail and Workspace): one connection for the signed-in Handler user.
async function loadGoogleConnection() {
    const box = $("google-connection");
    const params = new URLSearchParams(location.search);
    if (params.has("google") || params.has("google_error")) {
        if (params.get("google_error")) toast(params.get("google_error"), true, 8000);
        else toast("Google connected" + (params.get("account") ? ": " + params.get("account") : ""));
        history.replaceState(null, "", location.pathname + location.hash);
    }
    const g = await apiOpt("/api/connections/google", null);
    if (g === null) { box.innerHTML = ""; return; }
    const c = g.connection;
    let body;
    if (!g.configured) {
        body = `<span class="muted">Not configured: set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET.</span>`;
    } else if (!c) {
        body = `<span class="muted">Not connected for your Handler user.</span> <button class="btn-small btn-primary" data-action="google-connect">Connect Google</button>`;
    } else {
        const ok = c.status === "active";
        body = `<b>${esc(c.account || "(unknown account)")}</b> ${badge(ok ? "connected" : "needs reconnect", ok ? "green" : "yellow")}` +
            ` <button class="btn-small${ok ? "" : " btn-primary"}" data-action="google-connect">${ok ? "Reconnect" : "Reconnect Google"}</button> <button class="btn-small btn-danger" data-action="google-disconnect" data-id="${esc(c.id)}">Disconnect</button>` +
            `<div class="muted">Granted scopes: <span class="mono">${esc((c.scopes || []).join(" "))}</span></div>` +
            `<div class="muted">Connected by ${esc(c.created_by || "-")} on ${esc(fmtTime(c.created_at))}; last used ${esc(fmtTime(c.last_used_at))}</div>`;
    }
    const redirect = g.redirect_uri ? `<div class="muted">Redirect URI: <span class="mono">${esc(g.redirect_uri)}</span></div>` : "";
    box.innerHTML = `<div class="card"><div><b>Google (Gmail and Workspace)</b></div><div>${body}</div>${redirect}</div>`;
}
$("google-connection").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    try {
        if (btn.dataset.action === "google-connect") {
            const r = await sendJson("POST", "/api/connections/google/start", {});
            location.assign(r.url);
        } else if (btn.dataset.action === "google-disconnect") {
            if (!confirm("Disconnect Google? This revokes handler's access to your Google account.")) return;
            const r = await api(`/api/connections/google/${enc(btn.dataset.id)}`, {method: "DELETE"});
            toast(r && r.revoked ? "Disconnected and revoked" : "Disconnected (Google did not confirm the revoke)");
            await loaders.connections();
        }
    } catch (err) { fail(err); }
});
// IMAP/SMTP mailboxes (the `mailbox` integration); absent when it is off.
async function loadMailboxes() {
    const box = $("mailbox-connections");
    const data = await apiOpt("/api/mailboxes", null);
    if (data === null) { box.innerHTML = ""; return; }
    const rows = (data.mailboxes || []).map((m) => {
        const tone = !m.enabled ? "gray" : m.status === "active" ? "green" : m.status === "auth_failed" ? "red" : "yellow";
        const state = m.enabled ? m.status : "disabled";
        return `<tr><td><b>${esc(m.label || m.email)}</b><div class="muted">${esc(m.email)}</div></td>` +
            `<td class="mono">${esc(m.imap_host)}:${esc(m.imap_port)} ${esc(m.imap_security)}<br>${esc(m.smtp_host)}:${esc(m.smtp_port)} ${esc(m.smtp_security)}</td>` +
            `<td>${badge(state, tone)}${m.last_error ? `<div class="muted">${esc(m.last_error)}</div>` : ""}</td>` +
            `<td>${esc(m.wake_mode)}</td><td>${esc(fmtTime(m.last_sync_at))}</td>` +
            `<td><button class="btn-small" data-action="mbx-test" data-id="${esc(m.id)}">Test</button> ` +
            `<button class="btn-small" data-action="mbx-sync" data-id="${esc(m.id)}"${m.enabled ? "" : " disabled"}>Sync now</button> ` +
            `<button class="btn-small" data-action="mbx-toggle" data-id="${esc(m.id)}" data-enabled="${m.enabled ? 1 : 0}">${m.enabled ? "Disable" : "Enable"}</button></td></tr>`;
    }).join("");
    const table = rows ? `<div class="table-wrap"><table class="data-table"><thead><tr><th>Mailbox</th><th>IMAP / SMTP</th><th>Status</th><th>Wake</th><th>Last sync</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>` : `<span class="muted">No mailboxes connected.</span>`;
    box.innerHTML = `<div class="card"><div><b>Mailboxes (IMAP/SMTP)</b></div>${table}<div class="muted">Add mailboxes and change passwords in Crontal → Handler settings → Mailboxes (or POST /api/mailboxes).</div></div>`;
}
$("mailbox-connections").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-action]");
    if (!btn) return;
    const id = enc(btn.dataset.id);
    try {
        if (btn.dataset.action === "mbx-test") {
            const r = await sendJson("POST", `/api/mailboxes/${id}/test`, {});
            toast(r.ok ? "IMAP and SMTP login OK" : `Test failed: ${(r.imap && r.imap.error) || (r.smtp && r.smtp.error) || "unknown error"}`, !r.ok, 8000);
        } else if (btn.dataset.action === "mbx-sync") {
            await sendJson("POST", `/api/mailboxes/${id}/sync`, {});
            toast("Sync scheduled");
        } else if (btn.dataset.action === "mbx-toggle") {
            await sendJson("PATCH", `/api/mailboxes/${id}`, {enabled: btn.dataset.enabled !== "1"});
            await loadMailboxes();
        }
    } catch (err) { fail(err); }
});
$("conn-refresh").addEventListener("click", () => loaders.connections().catch(fail));
loaders.connections = () => Promise.all([loadGoogleConnection(), loadMailboxes(), loadConnections()]);

// ─── Users (admin) ───────────────────────────────────────────────────────────
const usr = {tokens: null};
async function loadUsers() {
    const users = await api(API.users);
    $("users-content").innerHTML = `<div class="table-wrap"><table class="data-table"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>` +
        users.map((u) => `<tr><td><b>${esc(u.id)}</b><div class="muted">${esc(u.display_name)}</div></td>
            <td><select data-action="user-role" data-id="${esc(u.id)}" aria-label="Role of ${esc(u.id)}"><option ${u.role === "member" ? "selected" : ""}>member</option><option ${u.role === "admin" ? "selected" : ""}>admin</option></select></td>
            <td>${u.disabled ? badge("disabled", "red") : badge("active", "green")}</td><td>${esc(fmtTime(u.created_at))}</td>
            <td><div class="inline-actions"><button class="btn-small" data-action="user-toggle" data-id="${esc(u.id)}" data-disabled="${u.disabled ? "1" : ""}">${u.disabled ? "Enable" : "Disable"}</button>
            <button class="btn-small" data-action="user-issue" data-id="${esc(u.id)}">Issue token</button>
            <button class="btn-small" data-action="user-tokens" data-id="${esc(u.id)}">Tokens</button></div></td></tr>
            <tr hidden id="tokens-${esc(u.id)}"><td colspan="5"></td></tr>`).join("") + "</tbody></table></div>";
}
actions["user-toggle"] = async (el) => { await sendJson("PATCH", API.user(el.dataset.id), {disabled: !el.dataset.disabled}); await loadUsers(); };
document.addEventListener("change", (e) => {
    const t = e.target.closest('[data-action="user-role"]');
    if (t) sendJson("PATCH", API.user(t.dataset.id), {role: t.value}).then(() => toast("Role updated")).catch((err) => { fail(err); loadUsers().catch(() => {}); });
});
actions["user-issue"] = async (el) => {
    const r = await sendJson("POST", API.userTokens(el.dataset.id), {label: "web ui"});
    const box = $("token-reveal");
    box.hidden = false;
    box.innerHTML = `<b>Token for ${esc(el.dataset.id)}</b> (shown once, copy it now)<div class="mono" style="margin:6px 0">${esc(r.token)}</div><button class="btn-small" data-action="token-hide">Dismiss</button>`;
    box.scrollIntoView({block: "nearest"});
};
actions["token-hide"] = () => { $("token-reveal").hidden = true; $("token-reveal").textContent = ""; };
actions["user-tokens"] = async (el) => {
    const row = $("tokens-" + el.dataset.id);
    if (!row.hidden) { row.hidden = true; return; }
    const toks = await api(API.userTokens(el.dataset.id));
    row.hidden = false;
    row.firstElementChild.innerHTML = toks.length ? `<table class="data-table"><thead><tr><th>Id</th><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>` +
        toks.map((t) => `<tr><td class="mono">${esc(t.id)}</td><td>${esc(t.label)}</td><td>${esc(fmtTime(t.created_at))}</td><td>${esc(fmtTime(t.last_used_at))}</td>
        <td>${t.revoked_at ? badge("revoked", "red") : `<button class="btn-small btn-danger" data-action="token-revoke" data-id="${esc(t.id)}" data-user="${esc(el.dataset.id)}">Revoke</button>`}</td></tr>`).join("") + "</tbody></table>" : '<span class="muted">No tokens</span>';
};
actions["token-revoke"] = async (el) => {
    if (!await confirmDialog("Revoke this token? Anyone using it will be signed out.", "Revoke")) return;
    await api(API.token(el.dataset.id), {method: "DELETE"}); toast("Revoked");
    const row = $("tokens-" + el.dataset.user); row.hidden = true;
    document.querySelector(`[data-action="user-tokens"][data-id="${CSS.escape(el.dataset.user)}"]`).click();
};
$("user-form").addEventListener("submit", async (e) => {
    e.preventDefault(); $("user-err").textContent = "";
    try {
        await sendJson("POST", API.users, {id: $("user-id").value.trim(), display_name: $("user-name").value.trim(), role: $("user-role").value});
        $("user-form").reset(); toast("User created"); await loadUsers();
    } catch (err) { $("user-err").textContent = err.message; }
});
$("users-refresh").addEventListener("click", () => loadUsers().catch(fail));
loaders.users = loadUsers;

// ─── Startup ─────────────────────────────────────────────────────────────────
const toastEl = document.createElement("div");
toastEl.id = "toast"; toastEl.setAttribute("role", "status");
document.body.appendChild(toastEl);

document.querySelector("#nav-list").addEventListener("click", (e) => {
    const b = e.target.closest(".nav-item");
    if (b) switchTab(b.dataset.tab);
});
$("theme-toggle").addEventListener("click", () => {
    const next = document.body.classList.contains("light") ? "dark" : "light";
    pref.set("theme", next); applyTheme(next);
});
$("sidebar-toggle").addEventListener("click", () => {
    if (window.matchMedia && matchMedia("(max-width: 900px)").matches) {
        document.body.classList.remove("sidebar-open");
        return;
    }
    const collapsed = !document.body.classList.contains("sidebar-collapsed");
    pref.set("sidebarCollapsed", collapsed ? "1" : "0");
    setSidebarCollapsed(collapsed);
});
$("mobile-nav-toggle").addEventListener("click", () => document.body.classList.toggle("sidebar-open"));
$("sidebar-scrim").addEventListener("click", () => document.body.classList.remove("sidebar-open", "thread-open"));
$("thread-toggle").addEventListener("click", () => document.body.classList.toggle("thread-open"));
$("logout-btn").addEventListener("click", logout);
applyTheme(preferredTheme());
setSidebarCollapsed(pref.get("sidebarCollapsed") === "1");

async function start() {
    try { me = await api(API.me); } catch { return; }
    $("sidebar-user").textContent = (me.display_name || me.user_id || "admin key") + " (" + me.role + ")";
    $("user-avatar").textContent = String(me.display_name || me.user_id || "H").trim().charAt(0).toUpperCase() || "H";
    if (me.via === "trusted_network") $("logout-btn").hidden = true;
    if (isAdmin()) document.querySelectorAll("[data-admin]").forEach((n) => { n.hidden = false; });
    refreshBadges().catch(() => {});
    setInterval(() => refreshBadges().catch(() => {}), 30000);
    connectStream();
    setInterval(() => {
        if (document.visibilityState !== "hidden") refreshChatActivity();
    }, 5000);
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") refreshChatActivity();
    });
    switchTab((location.hash || "#chat").slice(1));
}
window.addEventListener("hashchange", () => { const n = location.hash.slice(1); if (n && n !== activeTab) switchTab(n); });
start();
