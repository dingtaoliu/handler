// ─── Toast ───────────────────────────────────────────────────────────────────
const toastEl = document.createElement('div');
toastEl.id = 'toast';
document.body.appendChild(toastEl);
let _toastTimer;
function toast(msg, ms = 2000) {
    toastEl.textContent = msg;
    toastEl.classList.add('show');
    clearTimeout(_toastTimer);
    _toastTimer = setTimeout(() => toastEl.classList.remove('show'), ms);
}

// ─── Theme ────────────────────────────────────────────────────────────────────
const themeToggleEl = document.getElementById('theme-toggle');

function applyTheme(theme) {
    const isLight = theme === 'light';
    document.body.classList.toggle('light', isLight);
    themeToggleEl.textContent = isLight ? 'Dark' : 'Light';
}

function getPreferredTheme() {
    const saved = localStorage.getItem('theme');
    if (saved === 'light' || saved === 'dark') return saved;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
}

function toggleTheme() {
    const next = document.body.classList.contains('light') ? 'dark' : 'light';
    localStorage.setItem('theme', next);
    applyTheme(next);
}

applyTheme(getPreferredTheme());

// ─── Tab switching ────────────────────────────────────────────────────────────
const _loaded = new Set();
const _alwaysRefresh = new Set(['cron', 'logs', 'files']);

function switchTab(name) {
    document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    const pane = document.getElementById('tab-' + name);
    const navItem = document.querySelector('.nav-item[data-tab="' + name + '"]');
    if (pane) pane.classList.add('active');
    if (navItem) navItem.classList.add('active');
    if (_alwaysRefresh && _alwaysRefresh.has(name)) {
        onTabLoad(name);
    } else if (!_loaded.has(name)) {
        _loaded.add(name);
        onTabLoad(name);
    }
}

function onTabLoad(name) {
    if (name === 'chat') loadHistory();
    else if (name === 'memory') loadMemory();
    else if (name === 'tokens') loadTokens();
    else if (name === 'cron') loadCron();
    else if (name === 'logs') loadLogs();
    else if (name === 'config') loadConfig();
    else if (name === 'tools') loadTools();
    else if (name === 'files') loadFiles();
}

// ─── Chat ─────────────────────────────────────────────────────────────────────
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const pendingFilesEl = document.getElementById('pending-files');
const convListEl = document.getElementById('conv-list');
let pendingUploads = [];
let _activeCid = null;  // current conversation id

inputEl.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});

function addMsg(role, content) {
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    if (role === 'assistant') {
        div.innerHTML = marked.parse(content);
    } else {
        div.textContent = content;
    }
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

async function loadHistory() {
    await loadConversationList();
    // Load the conversation from cookie (server sets it), or the first one
    try {
        const res = await fetch('/api/history');
        const data = await res.json();
        if (data.conversation_id) {
            _activeCid = data.conversation_id;
            renderConvList();
            for (const msg of data.messages) addMsg(msg.role, msg.content);
        } else if (_activeCid) {
            await selectConversation(_activeCid, false);
        }
    } catch(e) {}
}

async function loadConversationList() {
    try {
        const res = await fetch('/api/conversations');
        const data = await res.json();
        _convs = data.conversations || [];
        renderConvList();
    } catch(e) {}
}

let _convs = [];

function renderConvList() {
    convListEl.innerHTML = '';
    if (_convs.length === 0) {
        convListEl.innerHTML = '<div class="empty-state" style="padding:16px;font-size:12px">No conversations yet</div>';
        return;
    }
    for (const c of _convs) {
        const el = document.createElement('div');
        el.className = 'file-item conv-item' + (c.id === _activeCid ? ' active' : '');
        const ts = c.last_ts ? new Date(c.last_ts.replace(' ', 'T') + 'Z').toLocaleString([], {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
        const preview = c.last_content ? esc(c.last_content.slice(0, 60)) + (c.last_content.length > 60 ? '…' : '') : '<em>empty</em>';
        el.innerHTML =
            '<div class="conv-ts">' + esc(ts) + '</div>' +
            '<div class="conv-preview">' + preview + '</div>';
        el.onclick = () => selectConversation(c.id);
        convListEl.appendChild(el);
    }
}

async function selectConversation(cid, reload = true) {
    _activeCid = cid;
    messagesEl.innerHTML = '';
    renderConvList();
    try {
        const res = await fetch('/api/history?cid=' + encodeURIComponent(cid));
        const data = await res.json();
        for (const msg of data.messages) addMsg(msg.role, msg.content);
    } catch(e) {}
    inputEl.focus();
    if (reload) await loadConversationList();
}

async function newConversation() {
    const res = await fetch('/api/conversations', { method: 'POST' });
    const data = await res.json();
    _activeCid = data.conversation_id;
    messagesEl.innerHTML = '';
    await loadConversationList();
    inputEl.focus();
}

function handleFiles(fileList) {
    for (const f of fileList) {
        pendingUploads.push(f);
        const tag = document.createElement('span');
        tag.className = 'pending-file';
        tag.innerHTML = f.name + ' <span class="remove" onclick="removeFile(this, \'' + f.name + '\')">✕</span>';
        pendingFilesEl.appendChild(tag);
    }
    document.getElementById('file-input').value = '';
}

function removeFile(el, name) {
    pendingUploads = pendingUploads.filter(f => f.name !== name);
    el.parentElement.remove();
}

async function uploadFiles() {
    if (pendingUploads.length === 0) return [];
    const formData = new FormData();
    for (const f of pendingUploads) formData.append('files', f);
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    pendingUploads = [];
    pendingFilesEl.innerHTML = '';
    return data.files;
}

async function send() {
    const text = inputEl.value.trim();
    if (!text && pendingUploads.length === 0) return;
    inputEl.value = '';
    inputEl.style.height = 'auto';

    const uploaded = await uploadFiles();
    const fileNames = uploaded.map(f => f.name);
    let displayText = text;
    if (fileNames.length > 0) displayText = text + '\n' + fileNames.map(n => '[uploaded: ' + n + ']').join(' ');
    if (displayText.trim()) addMsg('user', displayText.trim());

    let message = text;
    if (uploaded.length > 0) {
        const filePaths = uploaded.map(f => f.name + ' (path: ' + f.path + ')').join(', ');
        message = (text ? text + '\n\n' : '') + 'Uploaded files: ' + filePaths + '\nUse the read_file tool to access them.';
    }

    sendBtn.disabled = true;
    const thinking = addMsg('assistant', 'Thinking...');
    thinking.classList.add('thinking');

    try {
        const body = { message };
        if (_activeCid) body.conversation_id = _activeCid;
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        thinking.remove();
        if (data.conversation_id && data.conversation_id !== _activeCid) {
            _activeCid = data.conversation_id;
        }
        addMsg('assistant', data.error ? 'Error: ' + data.error : data.response);
        // Refresh conversation list to update preview
        loadConversationList();
    } catch(e) {
        thinking.remove();
        addMsg('assistant', 'Error: ' + e.message);
    }
    sendBtn.disabled = false;
    inputEl.focus();
}


// ─── Memory ───────────────────────────────────────────────────────────────────
let _memFiles = [];
let _memSelected = null;
const memListEl = document.getElementById('memory-file-list');
const memFilenameEl = document.getElementById('mem-filename');
const memContentEl = document.getElementById('mem-content');

async function loadMemory() {
    const res = await fetch('/api/memory');
    const data = await res.json();
    _memFiles = data.files || [];
    renderMemList();
}

function renderMemList() {
    memListEl.innerHTML = '';
    if (_memFiles.length === 0) {
        memListEl.innerHTML = '<div class="empty-state">No memory files</div>';
        return;
    }
    for (const f of _memFiles) {
        const el = document.createElement('div');
        el.className = 'file-item' + (_memSelected === f.filename ? ' active' : '');
        el.innerHTML = '<div class="file-name">' + esc(f.filename) + '</div>' +
            '<div class="file-summary">' + esc(f.summary || '') + '</div>' +
            '<div class="file-size">' + formatBytes(f.size) + '</div>';
        el.onclick = () => selectMemFile(f.filename);
        memListEl.appendChild(el);
    }
}

async function selectMemFile(name) {
    _memSelected = name;
    renderMemList();
    const res = await fetch('/api/memory/' + encodeURIComponent(name));
    const data = await res.json();
    memFilenameEl.value = data.filename;
    memFilenameEl.readOnly = true;
    memContentEl.value = data.content;
    memContentEl.focus();
}

function newMemFile() {
    _memSelected = null;
    renderMemList();
    memFilenameEl.value = '';
    memFilenameEl.readOnly = false;
    memContentEl.value = '';
    memFilenameEl.focus();
}

async function saveMemFile() {
    const name = memFilenameEl.value.trim();
    const content = memContentEl.value;
    if (!name) { toast('Enter a filename'); return; }
    const fname = name.endsWith('.md') ? name : name + '.md';
    const res = await fetch('/api/memory/' + encodeURIComponent(fname), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (data.error) { toast('Error: ' + data.error); return; }
    toast('Saved ' + data.filename);
    _memSelected = data.filename;
    await loadMemory();
    memFilenameEl.value = data.filename;
    memFilenameEl.readOnly = true;
}

async function deleteMemFile() {
    const name = memFilenameEl.value.trim() || _memSelected;
    if (!name) return;
    if (!confirm('Delete ' + name + '?')) return;
    const res = await fetch('/api/memory/' + encodeURIComponent(name), { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) {
        toast('Deleted ' + name);
        _memSelected = null;
        memFilenameEl.value = '';
        memContentEl.value = '';
        await loadMemory();
    }
}

// ─── Tokens ───────────────────────────────────────────────────────────────────
async function loadTokens() {
    const [today, month] = await Promise.all([
        fetch('/api/tokens?days=1').then(r => r.json()),
        fetch('/api/tokens?days=30').then(r => r.json()),
    ]);
    const el = document.getElementById('tokens-content');
    el.innerHTML = [
        '<div class="stat-cards">',
        statCard('Today Cost', '$' + today.estimated_cost_usd.toFixed(4), today.total_tokens.toLocaleString() + ' tokens'),
        statCard('Today Runs', today.runs, ''),
        statCard('30-Day Cost', '$' + month.estimated_cost_usd.toFixed(4), month.total_tokens.toLocaleString() + ' tokens'),
        statCard('30-Day Runs', month.runs, ''),
        '</div>',
        '<div class="section-title">Daily Breakdown (last 30 days)</div>',
        buildTable(
            ['Day', 'Runs', 'Input', 'Output', 'Total', 'Cost'],
            (month.daily || []).map(d => [
                d.day, d.runs,
                d.input_tokens.toLocaleString(),
                d.output_tokens.toLocaleString(),
                d.total_tokens.toLocaleString(),
                '$' + d.estimated_cost_usd.toFixed(4),
            ])
        ),
    ].join('');
}

function statCard(label, value, sub) {
    return '<div class="stat-card"><div class="stat-label">' + esc(label) + '</div>' +
        '<div class="stat-value">' + esc(String(value)) + '</div>' +
        (sub ? '<div class="stat-sub">' + esc(sub) + '</div>' : '') +
        '</div>';
}

// ─── Cron ─────────────────────────────────────────────────────────────────────
async function loadCron() {
    const res = await fetch('/api/cron');
    const data = await res.json();
    const el = document.getElementById('cron-content');
    const jobs = data.jobs || [];
    if (jobs.length === 0) {
        el.innerHTML = '<div class="empty-state">No scheduled jobs</div>';
        return;
    }
    el.innerHTML = buildTable(
        ['Name', 'Type', 'Schedule', 'Next Run', 'Last Run', 'Status', ''],
        jobs.map(j => [
            esc(j.name),
            '<span class="badge badge-blue">' + esc(j.type) + '</span>',
            esc(j.schedule),
            esc(j.next_run ? j.next_run.slice(0, 16) : '—'),
            esc(j.last_run ? j.last_run.slice(0, 16) : '—'),
            j.enabled
                ? '<span class="badge badge-green">enabled</span>'
                : '<span class="badge badge-gray">disabled</span>',
            '<button class="btn-small btn-danger" onclick="deleteCronJob(' + j.id + ')">Delete</button>',
        ]),
        true /* raw HTML */
    );
}

async function deleteCronJob(id) {
    if (!confirm('Delete this cron job?')) return;
    const res = await fetch('/api/cron/' + id, { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) { toast('Deleted'); loadCron(); }
}

// ─── Logs ─────────────────────────────────────────────────────────────────────
const logDateEl = document.getElementById('log-date');

async function loadLogs() {
    // Populate date picker on first load
    if (logDateEl.options.length === 0) {
        try {
            const dr = await fetch('/api/logs/dates');
            const dd = await dr.json();
            for (const d of (dd.dates || [])) {
                const opt = document.createElement('option');
                opt.value = d;
                opt.textContent = d;
                logDateEl.appendChild(opt);
            }
        } catch(e) {}
    }
    const lines = document.getElementById('log-lines').value;
    const date = logDateEl.value;
    const url = '/api/logs?lines=' + lines + (date ? '&date=' + encodeURIComponent(date) : '');
    const res = await fetch(url);
    const data = await res.json();
    const el = document.getElementById('logs-content');
    el.textContent = (data.lines || []).join('\n') || '(no log entries for this date)';
    el.scrollTop = el.scrollHeight;
}

// ─── Config ───────────────────────────────────────────────────────────────────
let _configSelected = null;
const configListEl = document.getElementById('config-file-list');
const configFilenameLabel = document.getElementById('config-filename-label');
const configContentEl = document.getElementById('config-content');

async function loadConfig() {
    const res = await fetch('/api/config');
    const data = await res.json();
    const files = data.files || [];
    configListEl.innerHTML = '';
    for (const f of files) {
        const el = document.createElement('div');
        el.className = 'file-item' + (_configSelected === f.name ? ' active' : '');
        el.innerHTML = '<div class="file-name">' + esc(f.name) + '</div>' +
            '<div class="file-size">' + (f.exists ? 'exists' : 'not set') + '</div>';
        el.onclick = () => selectConfigFile(f.name);
        configListEl.appendChild(el);
    }
    if (!_configSelected && files.length > 0) selectConfigFile(files[0].name);
}

async function selectConfigFile(name) {
    _configSelected = name;
    // Re-render list to update active highlight
    configListEl.querySelectorAll('.file-item').forEach((el, i) => {
        el.classList.toggle('active', el.querySelector('.file-name').textContent === name);
    });
    const res = await fetch('/api/config/' + encodeURIComponent(name));
    const data = await res.json();
    configFilenameLabel.textContent = data.name;
    configContentEl.value = data.content;
    configContentEl.focus();
}

async function saveConfigFile() {
    if (!_configSelected) { toast('Select a file first'); return; }
    const content = configContentEl.value;
    const res = await fetch('/api/config/' + encodeURIComponent(_configSelected), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content }),
    });
    const data = await res.json();
    if (data.error) { toast('Error: ' + data.error); return; }
    toast('Saved ' + _configSelected);
    loadConfig();
}

// ─── Files ────────────────────────────────────────────────────────────────────
async function loadFiles() {
    const res = await fetch('/api/files');
    const data = await res.json();
    const el = document.getElementById('files-content');
    const files = data.files || [];
    if (files.length === 0) {
        el.innerHTML = '<div class="empty-state">No uploaded files</div>';
        return;
    }
    el.innerHTML = buildTable(
        ['Name', 'Size', 'Modified', ''],
        files.map(f => [
            esc(f.name),
            formatBytes(f.size),
            esc(new Date(f.modified * 1000).toLocaleString()),
            '<button class="btn-small btn-danger" onclick="deleteFile(' + JSON.stringify(f.name) + ')">Delete</button>',
        ]),
        true
    );
}

async function deleteFile(name) {
    if (!confirm('Delete ' + name + '?')) return;
    const res = await fetch('/api/files/' + encodeURIComponent(name), { method: 'DELETE' });
    const data = await res.json();
    if (data.ok) { toast('Deleted ' + name); loadFiles(); }
    else toast('Error: ' + (data.error || 'unknown'));
}

// ─── Tools ────────────────────────────────────────────────────────────────────
async function loadTools() {
    const res = await fetch('/api/tools');
    const data = await res.json();
    const tools = data.tools || [];
    const el = document.getElementById('tools-content');
    if (tools.length === 0) {
        el.innerHTML = '<div class="empty-state">No tools loaded</div>';
        return;
    }
    el.innerHTML = buildTable(
        ['Tool', 'Description'],
        tools.map(t => [esc(t.name), '<span style="color:var(--muted);font-size:12px">' + esc(t.description) + '</span>']),
        true
    );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────
function esc(s) {
    return String(s)
        .replace(/&/g,'&amp;')
        .replace(/</g,'&lt;')
        .replace(/>/g,'&gt;')
        .replace(/"/g,'&quot;');
}

function formatBytes(b) {
    if (b < 1024) return b + ' B';
    return (b / 1024).toFixed(1) + ' KB';
}

function buildTable(headers, rows, rawHtml = false) {
    const ths = headers.map(h => '<th>' + esc(h) + '</th>').join('');
    const trs = rows.map(row =>
        '<tr>' + row.map(cell => '<td>' + (rawHtml ? cell : esc(String(cell))) + '</td>').join('') + '</tr>'
    ).join('');
    return '<table class="data-table"><thead><tr>' + ths + '</tr></thead><tbody>' + trs + '</tbody></table>';
}

// ─── Init ─────────────────────────────────────────────────────────────────────
switchTab('chat');

