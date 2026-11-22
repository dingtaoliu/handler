# Importing legacy Crontal AI chats

Crontal is moving every merchant from its built-in agent onto Handler. Each user's old
AI conversations are carried over as Handler chat threads, one call per conversation.
Code: `runtime/web/thread_import_api.py`, `runtime/store/thread_imports.py` (SQLite and
Postgres), table `thread_imports`. Tests: `tests/runtime/store_pg/test_thread_import.py`.

## `POST /api/threads/import`

**Auth:** the tenant's bare instance admin key (`Authorization: Bearer $HANDLER_ADMIN_KEY`,
no `X-Handler-User`), as for sign-in tickets. Any user token, including an admin user's,
and the admin key acting as a user get 403. The route is allowed on free plans (it is
under `/api/threads…`).

**Body** (unknown fields are refused):

```json
{
  "owner_user_id": "331",
  "conversation_id": "crontal-legacy-1234",
  "source": "crontal-legacy",
  "source_id": "1234",
  "title": "Quote for 304 sheet",
  "created_at": "2026-03-01T09:00:00Z",
  "messages": [
    {"role": "user", "text": "Need 304 sheet 2mm, 5 t", "created_at": "2026-03-01T09:00:05Z"},
    {"role": "assistant", "text": "Drafted RFQ #77 ...", "created_at": "2026-03-01T09:00:20Z"}
  ]
}
```

- `owner_user_id`: the Handler user id of the Crontal user, `str(user.id)`.
- `conversation_id`: `^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$`. The thread key is
  `chat:<owner_user_id>:<conversation_id>`, the same as `/api/chat`, so the client
  continues the chat by posting to `/api/chat` with this `conversation_id`. A prefix such
  as `crontal-legacy-<chat id>` keeps it apart from live Handler chats.
- `source`: `^[a-z0-9][a-z0-9._-]{0,63}$`; Crontal sends `crontal-legacy`.
  `source_id`: 1 to 200 characters (the Crontal chat id as a string).
- `title`: optional. Whitespace is collapsed and it is cut to 120 characters. It is
  stored as a manual title, so a generated title never replaces it. Without one, the
  list shows the first user message, as for any chat.
- `created_at` and each message's `created_at`: ISO 8601. A time without an offset is
  taken as UTC. The array order is the order: a message timestamped before the one
  before it is stored at that earlier message's time. The thread's `created_at` is never
  later than its first message.
- `messages`: 1 to 2,000, each `role` `user` or `assistant`, `text` plain text, not
  blank, at most 100,000 UTF-8 bytes. All texts together may be at most 5,000,000 bytes.
  Crontal leaves out tool and system messages and empty assistant turns, and splits or
  trims a chat that is over the caps.

**What it creates:** a `chat` thread owned by that user (interactive lane, the chat
harness, no harness session), with the messages in order and their timestamps, the
title, and the owner's read mark at the last message. No event is stored, so nothing is
routed, no run starts, and nothing counts against the plan's monthly inputs. The thread
is not pending, not unread, and has no runs. `GET /api/threads` lists it by its last
message time (`last_message_at`) among the user's chats, and `GET
/api/threads/<key>/messages` returns the messages. Each message's `raw` is
`{"imported_from": <source>, "source_id": <source_id>}`.

**Responses:**

| Status | When | Body |
|---|---|---|
| 201 | Imported | `{"thread_key", "conversation_id", "messages": n, "created": true}` |
| 200 | This (`source`, `source_id`) was already imported with the same content | same, `"created": false` |
| 409 | The same (`source`, `source_id`) was imported with different content or to another key, or the thread key already exists (a live chat, or another import) | `{"detail": "..."}` |
| 422 | Bad input: unknown role, no messages, over 2,000 messages or a byte cap, blank text, a bad id or time, an unknown field | FastAPI validation detail |
| 404 | `owner_user_id` is not a user of this tenant | `{"detail": "owner user not found"}` |
| 403 / 401 | Not the bare admin key / no credential | |

"Same content" means every field, with times compared as UTC instants. Imports of one
chat that race are safe: one creates it and the others get 200.

## The owner must exist first

The import does not create users; it answers 404 for an unknown owner. Crontal users
become Handler users only by Crontal provisioning them: `POST /api/users`
`{"id": "<str(user.id)>", "display_name": "...", "role": "member" | "admin"}` with the
admin key (201, or 409 when the user already exists, which is fine), or a sign-in ticket
(`POST /api/auth/sign-in-tickets`, which creates or updates the user). The Crontal AI
identity (`gateway/crontal_identity.py`) is the instance's own agent user and has nothing
to do with this. So the migration, per user, is: `POST /api/users` (accepting 201 or
409), then `POST /api/threads/import` per chat. `GET /api/users` lists who exists.

## Continuing an imported chat

When the user writes in the thread (`POST /api/chat` with the same `conversation_id`),
that message is an ordinary input and counts against the plan. The run finds no harness
session and existing messages, so `RunExecutor._stream` puts a recap in front of the new
turn: "Earlier conversation in this thread (from handler's record; you have no session
memory of it):", then the **last 20 messages**, each as `role: <blocks JSON>` cut to
**500 characters**, then "Current turn:" and the new message (about 10 KB at most). The
harness then starts a session, and later turns use it. Older parts of a long imported chat
are not in the recap; the full record stays in the thread.
