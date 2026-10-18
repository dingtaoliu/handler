// Render the Settings > threads form with the shipped app.js against a real
// /api/settings payload and print what the selects show, as JSON:
//   {labels: {<type>: {harness, model, reasoning_effort}}, relabelled: {...},
//    stale: {reloaded, sameBuildReloaded}}
// argv: app.js path, settings JSON path, the build the page booted with.
const fs = require('node:fs');
const vm = require('node:vm');
const [source, settings, build] = [fs.readFileSync(process.argv[2], 'utf8'),
    JSON.parse(fs.readFileSync(process.argv[3], 'utf8')), process.argv[4]];

function page() {
    const elements = new Map();
    function element(tag = 'div') {
        const e = {tagName: tag.toUpperCase(), children: [], options: [], listeners: {},
            innerHTML: '', textContent: '', hidden: false, style: {}, dataset: {},
            classList: {toggle() {}, add() {}, remove() {}, contains() { return false; }},
            addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); },
            dispatch(type) { (this.listeners[type] || []).forEach((fn) => fn()); },
            setAttribute() {}, remove() {}, focus() {},
            insertBefore(child) { this.children.unshift(child); },
            appendChild(child) { this.children.push(child); return child; },
            append(...kids) { this.children.push(...kids); },
            add(option) { this.options.push(option); },
            querySelector() { return element(); }, querySelectorAll() { return []; }};
        if (e.tagName === 'SELECT') {
            Object.defineProperty(e, 'value', {
                get() { return this._value ?? (this.options[0] || {}).value; },
                set(v) { this._value = v; }});
        } else e.value = '';
        return e;
    }
    const document = {body: element(), visibilityState: 'visible', addEventListener() {},
        getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); },
        querySelector() { return element(); }, querySelectorAll() { return []; }, createElement: element};
    const reloads = [];
    const context = vm.createContext({document, Option: class { constructor(text, value) { this.text = text; this.value = value; } },
        window: {addEventListener() {}, HANDLER_CONFIG: {base: '', instance: 'test', build}},
        location: {replace() {}, reload() { reloads.push(1); }, hash: '', search: ''}, history: {replaceState() {}},
        sessionStorage: {getItem() { return null; }}, localStorage: {getItem() { return null; }},
        setTimeout() { return 1; }, clearTimeout() {}, setInterval() {}, clearInterval() {},
        URL, URLSearchParams, console, EventSource: class { close() {} }});
    vm.runInContext(source, context);
    return {run: (code) => vm.runInContext(code, context), elements, reloads};
}

// The form's thread-type fieldsets, in schema order, each {harness, model, reasoning_effort} selects.
function threadSelects(form, types) {
    const selects = [];
    const walk = (n) => { if (n.tagName === 'SELECT') selects.push(n); (n.children || []).forEach(walk); };
    walk(form);
    const out = {};
    types.forEach((type, i) => {
        const [harness, model, reasoning_effort] = selects.slice(i * 3, i * 3 + 3);
        out[type] = {harness, model, reasoning_effort};
    });
    return out;
}
const label = (sel) => ({harness: sel.harness.value, model: sel.model.options[0].text,
    reasoning_effort: sel.reasoning_effort.options[0].text});

const p = page();
p.run('me = {user_id: "1", role: "admin"};');
p.run('sett.all = ' + JSON.stringify(settings) + '; openSection("threads");');
const types = Object.keys(settings.threads.schema.properties);
const selects = threadSelects(p.elements.get('settings-form'), types);
const labels = Object.fromEntries(types.map((t) => [t, label(selects[t])]));
// Flip every thread type to the other harness without saving.
const relabelled = {};
for (const t of types) {
    const s = selects[t];
    s.harness.value = s.harness.value === 'codex' ? 'openai_agents' : 'codex';
    s.harness.dispatch('change');
    relabelled[t] = label(s);
}

// Build mismatch: a response from another build reloads on the next tab switch.
const stale = {};
for (const [key, served] of [['sameBuildReloaded', build], ['reloaded', build + '-new']]) {
    const q = page();
    q.run('me = {user_id: "1", role: "admin"};');
    q.run(`noteBuild({headers: {get: (h) => h === "X-Handler-Build" ? ${JSON.stringify(served)} : null}});`);
    q.run('switchTab("settings")');
    stale[key] = q.reloads.length > 0;
}
process.stdout.write(JSON.stringify({labels, relabelled, stale}));
