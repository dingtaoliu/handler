// Exercise the actual no-build browser script, with isolated browser/API mocks.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[2], 'utf8');
function browser() {
    const elements = new Map();
    const element = () => ({innerHTML: '', textContent: '', hidden: false, style: {}, dataset: {},
        scrollHeight: 100, scrollTop: 0, clientHeight: 100,
        classList: {toggle() {}, add() {}, remove() {}, contains() {return false;}},
        remove() {}, addEventListener() {}, setAttribute() {}, appendChild() {}, querySelectorAll() {return [];}, focus() {}});
    const document = {body: element(), visibilityState: 'visible', addEventListener() {},
        getElementById(id) {if (!elements.has(id)) elements.set(id, element()); return elements.get(id);},
        querySelector() {return element();}, querySelectorAll() {return [];}, createElement: element};
    const streams = [];
    const context = vm.createContext({document, window: {addEventListener() {}},
        location: {replace() {}, hash: '', search: ''}, history: {replaceState() {}},
        sessionStorage: {getItem() {return null;}}, localStorage: {getItem() {return null;}},
        setTimeout() {return 1;}, clearTimeout() {}, setInterval() {}, clearInterval() {},
        URL, URLSearchParams, console, EventSource: class {constructor(url) {this.url = url; streams.push(this);} close() {this.closed = true;}}});
    vm.runInContext(source, context);
    const run = (code) => vm.runInContext(code, context);
    run('me = {user_id: "1", role: "admin"};');
    return {run, elements, streams};
}
(async () => {
    const {run, elements, streams} = browser();
    assert.equal(run('threadTitle({key:"chat:1:abc", type:"chat", title:"Supplier quote review", summary:"old summary"})'),
        'Supplier quote review', 'API conversation titles must win over legacy summaries and id fallbacks');
    run('chat.key = "chat:1:selected"; chat.threads = [{key: chat.key, type:"chat"}, {key:"automation:test", type:"automation"}]; connectStream();');
    assert.match(streams[0].url, /^\/api\/stream\?/);
    run('connectStream();');
    assert.equal(streams.length, 1, 'thread navigation must keep global subscription');
    streams[0].onmessage({data: JSON.stringify({thread_key:'automation:test', type:'run_started', at:'2026-10-18T22:00:00Z'})});
    assert.equal(run('chat.key'), 'chat:1:selected', 'background runs must not replace the selected thread');
    assert.equal(run('chat.unread.has("automation:test")'), true);
    assert.match(elements.get('thread-list').innerHTML, /new activity/);
    assert.equal(elements.get('badge-chat').textContent, 1);
    assert.match(elements.get('toast').textContent, /Agent working/);
    run(`let recoveryCalls = [];
        api = async (path) => {
            recoveryCalls.push(path);
            if (path === API.threads) return chat.threads;
            if (path === API.threadMessages(chat.key) || path === API.threadRuns(chat.key)) return [];
            if (path === API.thread(chat.key)) return {thread:{},pending_events:[]};
            return [];
        };`);
    streams[0].onopen();
    await new Promise(setImmediate);
    assert.equal(run('recoveryCalls.includes(API.threadMessages(chat.key))'), true, 'reconnect must reload durable transcript');
    assert.equal(run('recoveryCalls.includes(API.threadRuns(chat.key))'), true, 'reconnect must recover missed run progress');
    streams[0].onmessage({data:JSON.stringify({type:'notification',thread_key:'automation:test',data:{user_id:'2',message:'Private'}})});
    assert.doesNotMatch(elements.get('toast').textContent, /Private/);
    streams[0].onmessage({data:JSON.stringify({type:'notification',thread_key:'automation:test',data:{user_id:'1',message:'Your reminder'}})});
    assert.equal(elements.get('toast').textContent, 'Your reminder');

    const b = browser();
    b.run(`chat.key = "automation:test";
        let finished = false;
        api = async (path) => {
            if (path === API.threadMessages(chat.key)) return finished ? [{id:"m1",run_id:"r1",role:"assistant",blocks:[{type:"text",text:"Work complete"}]}] : [];
            if (path === API.thread(chat.key)) return {thread:{pending_since: finished ? null : "2026-10-18"}, pending_events:[]};
            if (path === API.threadRuns(chat.key)) return [{id:"r1",status:finished ? "completed" : "running",started_at:"2026-10-18"}];
            if (path === API.run("r1")) return {run:{status:"running"},steps:[
                {kind:"assistant_text",data:{text:"Checking inbox"}},
                {kind:"tool_call",name:"gmail.search",status:"running",data:{input:"unread"}},
                {kind:"tool_call",name:"notify",status:"ok",data:{output:"Delivered"}}]};
            throw new Error(path);
        };`);
    await b.run('loadMessages(true)');
    let html = b.elements.get('messages').innerHTML;
    assert.match(html, /Checking inbox/); assert.match(html, /gmail.search/); assert.match(html, /Delivered/);
    b.run('finished = true');
    await b.run('loadMessages()');
    html = b.elements.get('messages').innerHTML;
    assert.equal((html.match(/Work complete/g) || []).length, 1);
    assert.doesNotMatch(html, /Checking inbox|Agent run:|gmail.search/);
    assert.equal(b.run('chat.thinking'), false);
    assert.match(b.run('blockHtml({type:"event",event_type:"automation.fired",payload:{name:"Daily email",prompt:"Find unread mail"}}, "event")'), /Daily email.*Find unread mail/);
    // Data remains escaped in run/event rendering.
    assert.doesNotMatch(b.run('stepHtml({kind:"assistant_text",data:{text:"<script>bad()</script>"}})'), /<script>/);
    assert.match(b.run('messageHtml({role:"tool",blocks:[{type:"tool_call",name:"notify",input:{message:"Visible reminder",to:["1"]}},{type:"tool_result",output:{}}]})'), /Notification<\/b><div><p>Visible reminder/);
    // ask_user: a question with option buttons (and a text box when allowed) that post answers.
    const q = b.run('blockHtml({type:"question_request",question_id:"q_1",question:"Sea or <b>air</b>?",options:[{id:"o1",label:"Sea"},{id:"o2",label:"Air"}],allow_text:true,untrusted:true}, "event")');
    assert.match(q, /Question:<\/b> Sea or &lt;b&gt;air/);
    assert.match(q, /data-action="q-answer" data-id="q_1" data-option="o2">Air</);
    assert.match(q, /id="qtext-q_1"/); assert.match(q, /outside input/);
    assert.doesNotMatch(b.run('blockHtml({type:"question_request",question_id:"q_2",question:"x",options:[{id:"o1",label:"A"}]}, "event")'), /qtext|outside input/);
    assert.match(b.run('blockHtml({type:"event",event_type:"question.answered",payload:{question:"Sea or air?",status:"answered",answered_by:"u1",answer:{label:"Air"}}}, "event")'), /u1 answered "Sea or air\?": <b>Air/);
    assert.match(b.run('blockHtml({type:"event",event_type:"question.answered",payload:{question:"Sea?",status:"expired"}}, "event")'), /expired unanswered/);
    b.run('var answerCalls = []; api = async (path, opts) => { answerCalls.push([path, opts && opts.body]); return {}; }');
    await b.run('actions["q-answer"]({dataset:{id:"q_1",option:"o2"}})');
    assert.deepEqual(JSON.parse(JSON.stringify(b.run('answerCalls'))), [["/api/questions/q_1/answer", JSON.stringify({option_id:"o2"})]]);

    // Tasks tab: list from /api/tasks, admin trace, member note, live refresh.
    const tk = browser();
    tk.run(`activeTab = "activity"; let taskCalls = [];
        api = async (path) => {
            taskCalls.push(path);
            if (path.startsWith("/api/events")) return [];
            if (path.startsWith("/api/tasks?")) return {items: [
                {id:"queued:email:new", run_id:null, thread_key:"email:new", thread_type:"email", thread_title:"Hello",
                 trigger_summary:"Email from bob@example.com: Hello", status:"queued", queued_at:"2026-10-18T10:00:00Z"},
                {id:"run_1", run_id:"run_1", thread_key:"crontal:rfq:42", thread_type:"crontal", thread_title:"RFQ 42",
                 trigger_summary:"rfq 42 · <b>bid</b>", status:"waiting_approval", started_at:"2026-10-18T09:00:00Z",
                 duration_ms:1500, cost_usd:0.01, usage:{input_tokens:10,output_tokens:5}, summary:"Need approval"}],
                next_cursor: "abc"};
            if (path === "/api/tasks/run_1") return {id:"run_1", run_id:"run_1", thread_key:"crontal:rfq:42", status:"waiting_approval",
                trigger_summary:"rfq 42", thread_title:"RFQ 42", started_at:"2026-10-18T09:00:00Z", duration_ms:1500,
                steps: me.role === "admin" ? [{seq:1, kind:"llm_call", name:"gpt", status:"ok", model:"gpt-test",
                    started_at:"2026-10-18T09:00:01Z", usage:{input_tokens:10,output_tokens:5}, latency_ms:900, data:{}},
                    {seq:null, kind:"approval", name:"gmail_send", status:"pending", started_at:"2026-10-18T09:00:02Z", data:{args:{to:"a"}}}] : undefined};
            throw new Error(path);
        };`);
    await tk.run('loadActivity()');
    let tasks = tk.elements.get('activity-content').innerHTML;
    assert.match(tasks, /Email from bob@example.com: Hello/);
    assert.match(tasks, /waiting approval/);
    assert.doesNotMatch(tasks, /<b>bid<\/b>/, 'task text is escaped');
    assert.match(tk.elements.get('activity-more').innerHTML, /Load more/);
    assert.equal(tk.streams.length, 1, 'Tasks subscribe to the live stream');
    await tk.run('actions["act-run"]({dataset:{id:"run_1"}, classList:{add() {}}})');
    let trace = tk.elements.get('activity-steps').innerHTML;
    assert.match(trace, /llm_call/); assert.match(trace, /gmail_send/); assert.match(trace, /Review approvals/);
    assert.match(trace, /Open transcript/);
    tk.run('me = {user_id: "2", role: "member"}');
    await tk.run('loadSteps()');
    assert.match(tk.elements.get('activity-steps').innerHTML, /visible to admins only/);
    tk.run('taskCalls = []');
    tk.streams[0].onmessage({data: JSON.stringify({type:'task_updated', thread_key:'crontal:rfq:42', data:{status:'succeeded'}})});
    assert.match(tk.elements.get('task-live').textContent, /Updated/);
    assert.equal(tk.run('chat.unread.has("crontal:rfq:42")'), false, 'task updates do not mark chats unread');

    console.log('Chat activity behavior checks passed');
})().catch((error) => {console.error(error); process.exitCode = 1;});
