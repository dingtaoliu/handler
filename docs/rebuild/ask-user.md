# ask_user: the agent asks people a question

Branch `feat/ask-user` (from `rebuild` @ 84a906a). Not merged or deployed.

The agent can ask a multiple-choice question, end its run, and continue in a new
run when someone answers (or the question expires). Questions mirror approvals:
a parked record, a compare-and-set transition, an event into the thread that
wakes the agent, and a transcript block the UIs render with buttons.

## Owner's decisions

1. Anyone who can see the thread can answer (`can_see_thread`: chat threads are
   visible to their owner and admins, every other thread to every user). The
   first answer wins; later answers get 409.
2. Questions expire after 24 hours, like approvals. Expiry is recorded
   (`status: expired`) and wakes the agent.
3. A typed chat message does not answer a question. It reaches the agent as a
   normal turn; the question stays open until someone picks an answer or it
   expires.

## Flow

```
run: agent calls ask_user ──► Question(pending) stored, people notified
                              tool returns {"status": "waiting_for_answer", ...}
     transcript: tool message, then a question_request event message
     agent ends its turn; the run finishes (nothing is held open)

person: POST /api/questions/{id}/answer ──► CAS pending → answered
        └─► Event question.answered into the thread ──► new run
housekeeping (every 15 s): pending and past expires_at ──► CAS → expired ──► same event
admin:  POST /api/questions/{id}/cancel ──► CAS → canceled ──► same event
```

## Storage

Table `questions` (SQLite `store/schema_questions.sql`, Postgres
`store/schema_pg_questions.sql`). Both are applied the same way as every other
table: `CREATE TABLE IF NOT EXISTS` at first use, per instance schema on
Postgres (`search_path`), so existing instances get the table on their next
start with no migration. The approvals table is untouched.

`Question` (`types.py`): `id` (`q_…`), `run_id`, `thread_key`, `asked_by` (the
run's acting_as/user, None for the agent), `question`, `options`
(`[{id, label}]`, 2–5, ids `o1…o5`), `allow_text`, `status`
(`pending|answered|expired|canceled`), `answer` (`{option_id, label, text}`),
`answered_by`, `answered_at`, `expires_at`, `untrusted`, `created_at`.

Store methods (`store/base.py` `QuestionStore`; `sqlite_gateway.py`,
`postgres_domains.py`): `create_question`, `get_question`,
`list_questions(status, limit, thread_key, run_ids)` (newest first) and
`transition_question(id, expected, new, *, answer=None, answered_by=None)`, a
compare-and-set on `status` that also writes `answer`/`answered_by`/`answered_at`
when given an answer.

## The tool

`ask_user` (action `question.ask`, risk `write_internal`, so allowed by
default). Registered on the gateway in `server.py` next to `notify`, so both
harnesses get it over MCP. Code: `gateway/questions.py` (`QuestionService`,
`QuestionTools`).

Arguments:

| Argument | |
|---|---|
| `question` | required, ≤ 2000 chars |
| `options` | 2–5 labels, each non-empty, ≤ 80 chars, unique (case-insensitive) |
| `allow_text` | default false: also accept a typed answer |
| `recipients` | non-chat threads only: user ids to notify (default `"all"`) |

Result: `{"status": "waiting_for_answer", "question_id": "q_…", "message":
"Asked. End your turn; the answer will arrive in this thread as a new message."}`.
Bad input returns an error result and stores nothing.

**Notifications** go through the existing `Notifier` (`notifications.py`), so
they are stored per user, published live, and pushed to Crontal's bell through
the `deliveries` outbox and the Crontal sink, with `thread_key` for the link.
Message: `Question: <question, clipped to 200 chars>`. Recipients: on a chat
thread, the thread's owner only (no one else can see it; `recipients` is
ignored there); otherwise `recipients`, or `"all"`. A notification failure is
logged and does not fail the question.

### Lockdown floor

On untrusted threads (`email`, `crontal`, `gmail`, `mail`) the floor in
`gateway/policy.py` turns every `write_internal` action outside
`INSIDE_ACTIONS` into "needs approval". **Decision:** `question.ask` is added
to `INSIDE_ACTIONS`, next to `notify.send`. Asking only notifies the merchant's
own users and waits for one of them to choose; it sends nothing outside the
merchant, runs no code, and persists nothing other threads read. Gating it
behind an approval would mean approving a question before it can be asked,
which is the same human step twice. The risk that outside input steers the
question is handled by the `untrusted` flag instead: the record and the
transcript block carry `untrusted: true` on those threads, and the UIs warn the
person before they answer. An admin can still require approval with a grant
rule (`question.ask: ask`) or turn the tool off (`question.ask: deny`).

## API

`questions_router` in `gateway/api.py`, mounted next to `approvals_router`.
Any signed-in principal; visibility is `can_see_thread`.

| Route | |
|---|---|
| `GET /api/questions?status=&thread_key=&limit=` | Newest first, only threads the caller can see. Unknown status: 422. `limit` 1–500 (default 100) |
| `GET /api/questions/{id}` | 404 when missing or not visible |
| `POST /api/questions/{id}/answer` | Body `{option_id?, text?}`, see below |
| `POST /api/questions/{id}/cancel` | Admins only (403 otherwise); 409 when not pending |

Answer rules:
- 404 when the question doesn't exist or the caller can't see its thread.
- 409 when it isn't pending (answered, expired, canceled). A pending question
  past `expires_at` (the sweep hasn't run yet) is expired on the spot and also
  answers 409.
- 422 when: no `option_id` and no `text`; an unknown `option_id`; `text` on a
  question without `allow_text`; `text` over 4000 chars.
- With `allow_text`, `option_id` and `text` may both be given (a choice plus a
  note), or either alone. Blank text counts as absent.
- `answered_by` is the principal's identity (`user_id`, or `"admin"` for the
  admin key). The response is the updated question.

Responses are the `Question` as JSON (times ISO 8601, enums as strings), the
same shape as the approvals API.

## Event

Answering, expiring or canceling emits one event into the question's thread,
which wakes the agent with a new run:

```json
{"type": "question.answered", "source": "gateway",
 "idempotency_key": "question:<id>:answered",
 "thread_key": "<thread>",
 "payload": {"question_id": "q_…", "question": "…",
             "status": "answered" | "expired" | "canceled",
             "answer": {"option_id": "o2", "label": "Air", "text": null} | null,
             "answered_by": "<user id>" | null,
             "answered_by_name": "<display name>" | null}}
```

`render_turn` (`run.py`) gives the model one line:
- `Ann answered your question "Ship by sea or air?" (q_…): Air`
- with text: `…: Air (and wrote: "only the first lot")`, or `…: "Rail"` for text alone
- `Your question "…" (q_…) expired unanswered.`
- `Your question "…" (q_…) was canceled by admin; don't wait for an answer.`

The event is logged in the transcript by `event_messages` on that run, as an
`event` block (`event_type: "question.answered"`, the payload above).

## Transcript block

When a tool result says `waiting_for_answer`, `RunExecutor._question_requests`
logs an event message right after the tool message (mirroring
`_approval_requests`; the transcript is written at run end under the lease):

```json
{"type": "question_request", "question_id": "q_…", "question": "…",
 "options": [{"id": "o1", "label": "Sea"}, {"id": "o2", "label": "Air"}],
 "allow_text": false, "expires_at": "2026-10-28T12:00:00+00:00",
 "untrusted": false}
```

The message's `raw` is `{"question_id": "q_…"}`. Only questions of the same
thread are logged.

## Tasks

New status `waiting_answer` and a `pending_question_ids` field on every task
item (`store/tasks.py`, `tasks.py`); the admin step trace gets a `question`
step. **Decision:** a separate status rather than folding into
`waiting_approval`: the two need different actions from a person (approve or
deny a call vs. pick an answer), and Crontal's and Handler's task lists can
label and link them differently. If a run waits on both, `waiting_approval`
wins (it is checked first). `docs/rebuild/tasks.md` lists the new status.

## Prompt

`instructions.py` adds `ASK_USER_GUIDANCE` to every prompt: ask only when
genuinely blocked by a person's decision, give a few concrete options, don't
ask when a reasonable default exists, end the turn after asking (no waiting or
polling), and a typed message in the meantime is not the answer.

## Handler web UI

`web/static/app/app.js`: `question_request` renders the question with one
button per option (and a text box plus "Send answer" when `allow_text`), and a
warning when `untrusted`. Buttons post to the answer API (`q-answer`,
`q-answer-text` actions). `question.answered` events render as a one-line
note. The Tasks tab knows `waiting_answer` (badge and filter).

## What Crontal needs (hephaestus, not changed here)

1. **Proxy routes** in `backend/src/api_server/controllers/handler_controller.py`,
   mirroring the approvals proxy (`/approvals`, `/approvals/<id>/<action>`):
   - `GET /api/v1/handler/questions` → `GET /api/questions` (pass query args);
   - `GET /api/v1/handler/questions/<id>` → `GET /api/questions/<id>`;
   - `POST /api/v1/handler/questions/<id>/answer` → `POST /api/questions/<id>/answer`
     with the JSON body `{option_id?, text?}`;
   - optionally `POST /api/v1/handler/questions/<id>/cancel` (site admins only).

   The proxy must act as the signed-in Crontal user (as it does for approvals)
   so `can_see_thread` and `answered_by` apply to the right person. Pass
   404/409/422 through unchanged so the UI can show "already answered".
2. **Chat panel**: render a `question_request` block with the generic choice
   card being built there (question, option buttons, optional text box,
   disabled once answered), posting to the answer proxy. Show the `untrusted`
   warning. Render `question.answered` event blocks as a one-line note.
3. **Bell**: nothing new. Questions arrive as ordinary notifications
   (`notification.created`, message `Question: …`, with `thread_key`).
4. **Tasks tab**: show `waiting_answer` like `waiting_approval`, linking to the
   thread instead of the approvals list.

## Rollout

- Deploy Handler; the `questions` table is created on start in each instance
  schema. No settings change: the tool is allowed by default.
- Until Crontal ships the proxy and the choice card, Crontal users get the bell
  notification but must answer in Handler's own UI (`/app`), or the question
  expires after 24 h and the agent is told so.
- Rollback: the previous release ignores the `questions` table; pending
  questions simply never resolve. Drop the table only if desired.

## Not covered / risks

- Notifying `"all"` on a non-chat thread with no `recipients` relies on the
  instance's users list (local) and Crontal resolving `"all"` (sink), as for
  `notify`.
- Expiry runs in the existing housekeeping loop (every 15 s), so expiry can lag
  by that much; answering past the deadline is still refused.
- Untrusted threads can ask without approval (see the lockdown floor above);
  the `untrusted` warning depends on each UI rendering it.
