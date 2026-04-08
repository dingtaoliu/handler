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
const _alwaysRefresh = new Set(['cron', 'logs', 'files', 'sessions']);

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
    else if (name === 'sessions') loadSessions();
}

// ─── Chat ─────────────────────────────────────────────────────────────────────
const messagesEl = document.getElementById('messages');
const inputEl = document.getElementById('input');
const sendBtn = document.getElementById('send-btn');
const pendingFilesEl = document.getElementById('pending-files');
const convSelectEl = document.getElementById('conv-select');
let pendingUploads = [];
let pendingImages = [];  // {data: base64, media_type: string, preview: objectURL}
let _activeCid = null;  // current conversation id
let _stream = null;
let _pendingThinking = null;

inputEl.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 120) + 'px';
});
inputEl.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); }
});
// Paste images from clipboard
inputEl.addEventListener('paste', e => {
    const items = e.clipboardData && e.clipboardData.items;
    if (!items) return;
    for (const item of items) {
        if (item.type.startsWith('image/')) {
            e.preventDefault();
            const file = item.getAsFile();
            if (file) addImageFile(file);
        }
    }
});

function addImageFile(file) {
    const reader = new FileReader();
    reader.onload = () => {
        const base64 = reader.result.split(',')[1];
        const preview = URL.createObjectURL(file);
        pendingImages.push({ data: base64, media_type: file.type, preview });
        renderPendingImages();
    };
    reader.readAsDataURL(file);
}

function renderPendingImages() {
    // Remove old image previews
    pendingFilesEl.querySelectorAll('.pending-image').forEach(el => el.remove());
    pendingImages.forEach((img, i) => {
        const tag = document.createElement('span');
        tag.className = 'pending-file pending-image';
        tag.innerHTML = '<img src="' + img.preview + '" style="height:32px;vertical-align:middle;border-radius:4px;margin-right:4px">'
            + '<span class="remove" onclick="removePendingImage(' + i + ')">&#10005;</span>';
        pendingFilesEl.appendChild(tag);
    });
}

function removePendingImage(index) {
    URL.revokeObjectURL(pendingImages[index].preview);
    pendingImages.splice(index, 1);
    renderPendingImages();
}

function addMsg(role, content) {
    if (role !== 'user' && role !== 'assistant') return;
    const div = document.createElement('div');
    div.className = 'msg ' + role;
    div.dataset.role = role;
    // Store content for dedup (stringify if array)
    const contentKey = typeof content === 'string' ? content : JSON.stringify(content);
    div.dataset.content = contentKey;

    if (Array.isArray(content)) {
        // Multi-modal content blocks
        for (const block of content) {
            if (block.type === 'text' && block.text) {
                const span = document.createElement('span');
                if (role === 'assistant') {
                    span.innerHTML = marked.parse(block.text);
                } else {
                    span.textContent = block.text;
                }
                div.appendChild(span);
            } else if (block.type === 'image' && block.path) {
                const img = document.createElement('img');
                const fname = block.path.split('/').pop();
                img.src = '/api/uploads/' + encodeURIComponent(fname);
                img.className = 'chat-image';
                img.alt = 'Image';
                div.appendChild(img);
            }
        }
    } else if (role === 'assistant') {
        div.innerHTML = marked.parse(content);
    } else {
        div.textContent = content;
    }
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

function hasAssistantMessage(content) {
    const key = typeof content === 'string' ? content : JSON.stringify(content);
    return Array.from(messagesEl.querySelectorAll('.msg.assistant')).some(el =>
        (el.dataset.content || '') === key
    );
}

function connectStream(cid) {
    if (!cid) return;
    if (_stream) {
        _stream.close();
        _stream = null;
    }
    _stream = new EventSource('/api/stream?cid=' + encodeURIComponent(cid));
    _stream.addEventListener('message', ev => {
        try {
            const payload = JSON.parse(ev.data || '{}');
            if (!payload || payload.role !== 'assistant') return;
            if (hasAssistantMessage(payload.content)) return;
            if (_pendingThinking) {
                _pendingThinking.remove();
                _pendingThinking = null;
            }
            addMsg('assistant', payload.content);
            loadConversationList();
        } catch (e) {
            console.error('stream parse error', e);
        }
    });
}

async function loadHistory() {
    await loadConversationList();
    if (_convs.length > 0) {
        await selectConversation(_convs[0].id);
    }
}

async function loadConversationList() {
    try {
        const res = await fetch('/api/conversations');
        const data = await res.json();
        _convs = data.conversations || [];
        renderConvSelect();
    } catch(e) {}
}

let _convs = [];

function renderConvSelect() {
    convSelectEl.innerHTML = '';
    if (_convs.length === 0) {
        const opt = document.createElement('option');
        opt.textContent = 'No conversations';
        opt.disabled = true;
        convSelectEl.appendChild(opt);
        return;
    }
    for (const c of _convs) {
        const opt = document.createElement('option');
        opt.value = c.id;
        const ts = c.last_ts
            ? new Date(c.last_ts.replace(' ', 'T') + 'Z').toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'})
            : '';
        const preview = c.last_content ? c.last_content.slice(0, 50) : '(empty)';
        opt.textContent = (ts ? ts + ' — ' : '') + preview;
        convSelectEl.appendChild(opt);
    }
    if (_activeCid) convSelectEl.value = _activeCid;
}

function onConvSelect(cid) {
    if (cid && cid !== _activeCid) selectConversation(cid);
}

async function selectConversation(cid) {
    _activeCid = cid;
    messagesEl.innerHTML = '';
    convSelectEl.value = cid;
    connectStream(cid);
    try {
        const res = await fetch('/api/history?cid=' + encodeURIComponent(cid));
        const data = await res.json();
        for (const msg of data.messages) addMsg(msg.role, msg.content);
    } catch(e) {}
    inputEl.focus();
}

async function newConversation() {
    const res = await fetch('/api/conversations', { method: 'POST' });
    const data = await res.json();
    _activeCid = data.conversation_id;
    messagesEl.innerHTML = '';
    await loadConversationList();
    convSelectEl.value = _activeCid;
    connectStream(_activeCid);
    inputEl.focus();
}

function handleFiles(fileList) {
    for (const f of fileList) {
        // Route images to the image pipeline for multi-modal support
        if (f.type.startsWith('image/')) {
            addImageFile(f);
            continue;
        }
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

function handleImageUpload(fileList) {
    for (const f of fileList) {
        if (f.type.startsWith('image/')) addImageFile(f);
    }
    document.getElementById('file-input').value = '';
}

async function send() {
    const text = inputEl.value.trim();
    const hasImages = pendingImages.length > 0;
    if (!text && pendingUploads.length === 0 && !hasImages) return;
    inputEl.value = '';
    inputEl.style.height = 'auto';

    const uploaded = await uploadFiles();
    const fileNames = uploaded.map(f => f.name);

    // Build display content
    let displayText = text;
    if (fileNames.length > 0) displayText = text + '\n' + fileNames.map(n => '[uploaded: ' + n + ']').join(' ');
    if (displayText.trim()) addMsg('user', displayText.trim());
    // Show pending image previews in chat (temporary local display)
    if (hasImages) {
        const imgDiv = document.createElement('div');
        imgDiv.className = 'msg user';
        for (const img of pendingImages) {
            const el = document.createElement('img');
            el.src = img.preview;
            el.className = 'chat-image';
            imgDiv.appendChild(el);
        }
        messagesEl.appendChild(imgDiv);
        messagesEl.scrollTop = messagesEl.scrollHeight;
    }

    // Build message for API
    let message = text;
    if (uploaded.length > 0) {
        const filePaths = uploaded.map(f => f.name + ' (path: ' + f.path + ')').join(', ');
        message = (text ? text + '\n\n' : '') + 'Uploaded files: ' + filePaths + '\nUse the read_file tool to access them.';
    }

    // Collect images for API
    const apiImages = pendingImages.map(img => ({ data: img.data, media_type: img.media_type }));
    pendingImages.forEach(img => URL.revokeObjectURL(img.preview));
    pendingImages = [];
    renderPendingImages();

    sendBtn.disabled = true;
    const thinking = addMsg('assistant', 'Thinking...');
    thinking.classList.add('thinking');

    try {
        const body = { message };
        if (_activeCid) body.conversation_id = _activeCid;
        if (apiImages.length > 0) body.images = apiImages;
        const res = await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        const data = await res.json();
        if (_pendingThinking === thinking) {
            thinking.remove();
            _pendingThinking = null;
        } else if (thinking.parentNode) {
            thinking.remove();
        }
        if (data.conversation_id && data.conversation_id !== _activeCid) {
            _activeCid = data.conversation_id;
            connectStream(_activeCid);
        }
        const responseText = data.error ? 'Error: ' + data.error : data.response;
        if (!hasAssistantMessage(responseText)) addMsg('assistant', responseText);
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
    const parts = [
        '<div class="stat-cards">',
        statCard('Today Cost', '$' + today.estimated_cost_usd.toFixed(4), today.total_tokens.toLocaleString() + ' tokens'),
        statCard('Today Runs', today.runs, ''),
        statCard('30-Day Cost', '$' + month.estimated_cost_usd.toFixed(4), month.total_tokens.toLocaleString() + ' tokens'),
        statCard('30-Day Runs', month.runs, ''),
        '</div>',
    ];

    if (month.by_model && month.by_model.length > 0) {
        parts.push(
            '<div class="section-title">By Model (last 30 days)</div>',
            buildTable(
                ['Model', 'Runs', 'Input', 'Output', 'Total', 'Cost'],
                month.by_model.map(m => [
                    esc(m.model || 'unknown'), m.runs,
                    m.input_tokens.toLocaleString(),
                    m.output_tokens.toLocaleString(),
                    m.total_tokens.toLocaleString(),
                    '$' + m.estimated_cost_usd.toFixed(4),
                ])
            ),
        );
    }

    parts.push(
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
    );

    el.innerHTML = parts.join('');
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

const _defaultModels = {
    'openai': 'gpt-5.4-2026-03-05',
    'openai-manual': 'gpt-5.4-2026-03-05',
    'claude': 'claude-opus-4-6',
};

async function loadConfig() {
    // Load agent config
    try {
        const agentRes = await fetch('/api/agent');
        const agentData = await agentRes.json();
        document.getElementById('agent-backend').value = agentData.backend || 'openai';
        document.getElementById('agent-model').value = agentData.model || '';
    } catch(e) {}

    // Load config files
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

function onAgentBackendChange() {
    const backend = document.getElementById('agent-backend').value;
    document.getElementById('agent-model').value = _defaultModels[backend] || '';
}

async function saveAgentConfig() {
    const backend = document.getElementById('agent-backend').value;
    const model = document.getElementById('agent-model').value.trim();
    if (!model) { toast('Enter a model ID'); return; }
    try {
        const res = await fetch('/api/agent', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ backend, model }),
        });
        const data = await res.json();
        if (data.error) { toast('Error: ' + data.error); return; }
        toast('Agent updated: ' + backend + ' / ' + model);
    } catch(e) {
        toast('Error: ' + e.message);
    }
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

// ─── Sessions ─────────────────────────────────────────────────────────────────
async function loadSessions() {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    const el = document.getElementById('sessions-content');
    const sessions = data.sessions || [];
    if (sessions.length === 0) {
        el.innerHTML = '<div class="empty-state">No sessions found</div>';
        return;
    }
    el.innerHTML = buildTable(
        ['Channel', 'Conversation ID', 'Messages', 'Last Active', 'Last Message'],
        sessions.map(s => {
            const channel = s.channel || _inferChannel(s.id);
            const ts = s.last_ts
                ? new Date(s.last_ts.replace(' ', 'T') + 'Z').toLocaleString([], {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'})
                : '—';
            return [
                _channelBadge(channel),
                '<code style="font-size:11px">' + esc(s.id) + '</code>',
                String(s.message_count),
                esc(ts),
                '<span style="color:var(--muted);font-size:12px">' + esc((s.last_content || '').slice(0, 80) || '(empty)') + '</span>',
            ];
        }),
        true
    );
}

function _inferChannel(cid) {
    if (cid.startsWith('web-') || cid === 'web') return 'web';
    if (cid.startsWith('telegram:')) return 'telegram';
    if (cid.startsWith('scheduler')) return 'scheduler';
    return 'unknown';
}

function _channelBadge(channel) {
    const cls = channel === 'web' ? 'badge-blue' : channel === 'telegram' ? 'badge-green' : 'badge-gray';
    return '<span class="badge ' + cls + '">' + esc(channel) + '</span>';
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

