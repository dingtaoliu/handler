# Gmail Channel Design

## Overview

Gmail integration in two phases:
1. **Tools (build first)** — agent queries/reads Gmail on demand when prompted
2. **Channel (build later)** — new emails push into the environment loop automatically

## Phase 1: Gmail Tools

Three tools in `handler/actions.py` that give the agent on-demand access to Gmail:

### `search_gmail(query, max_results=10) -> str`
- Search emails using Gmail query syntax (same as the Gmail search bar)
- Examples: `from:irs.gov`, `subject:tax`, `after:2024/01/01 before:2024/12/31`, `has:attachment`
- Returns: list of emails with id, subject, from, date, snippet
- Uses `messages.list` + `messages.get(format=metadata)` for efficiency

### `read_email(gmail_id) -> str`
- Fetch and read a specific email by Gmail ID (from search results)
- Downloads raw message, parses MIME, extracts body
- Preprocesses body text (reuse `mail/preprocessing.py`: remove sigs, URLs, quoted replies)
- Returns: formatted email with headers + cleaned body, truncated to 10k chars

### `list_gmail_labels() -> str`
- List all Gmail labels (useful for the agent to understand email organization)
- Returns: label names and message counts

### Auth
- Reuse OAuth flow from `mail/indexer.py:_authenticate()`
- Same credentials path (`credentials/desktop.json`) and token path (`token.json`)
- Same scope: `gmail.readonly`
- Auth happens once at tool creation, service object is shared across calls

### Reuse from `mail/`
- `indexer.py`: `_authenticate()`, `_decode_mime_header()`, `_parse_email_address()`, `_extract_body()`
- `preprocessing.py`: `preprocess_email_for_classification()`, `html_to_plain()`

### Not reused
- Database (no local storage needed — agent reads on demand)
- .eml file saving
- Categorizer/rules/ML classifier (the agent itself replaces all of this)
- Retry logic (keep simple, add if needed)

## Phase 2: Gmail Channel (later)

### `GmailChannel` in `handler/channels/gmail.py`

Pull-based channel that polls for new emails on an interval.

### Polling strategy
- Use `users.history.list(startHistoryId=X)` for efficient incremental fetching
- Only returns changes since last check (new messages, label changes)
- Falls back to `users.messages.list(q="is:unread newer_than:1d")` on first run
- Store `lastHistoryId` in Handler's memory table

### Event format
```python
Event(
    type="email_received",
    source="gmail",
    data={
        "gmail_id": "...",
        "thread_id": "...",
        "subject": "...",
        "from_email": "...",
        "from_name": "...",
        "body": "...",  # preprocessed
        "labels": [...],
        "has_attachments": True/False,
    },
    conversation_id="gmail:{thread_id}",
)
```

### Conversation mapping
- `conversation_id = "gmail:{thread_id}"` — emails in the same thread are one conversation
- Agent sees full thread context when replying to follow-up emails

### Response delivery
- Default: no-op (agent observes, categorizes, remembers — doesn't reply)
- Future: add `draft_reply` and `apply_label` tools to let agent take action on emails

### Polling interval
- Configurable, default 60s
- Only runs when environment is active

### What the agent does with emails (decided by system prompt, not hardcoded)
- Categorize and remember important facts
- Flag urgent items
- Summarize daily email activity
- Answer user questions about their email ("what did my accountant say?")
