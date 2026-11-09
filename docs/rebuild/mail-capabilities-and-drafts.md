# Effective mail capabilities and draft identity

## Tool inventory

`Gateway.specs_for(ctx)` resolves each provider's optional
`available_tool_names(ctx)` once per inventory request. MCP uses its authenticated
run context; the admin tools API uses its effective principal. No user-specific
inventory is cached globally. Invocation still independently validates credentials,
grants and approval snapshots. Registration is not authorization or availability.

Gmail checks stored OAuth scopes for the effective owner without refreshing a
token or calling Gmail. Disconnected users do not see Gmail tools. Read-only
grants do not advertise compose/send capabilities. IMAP tools distinguish actual
connected mailboxes from the read-only intake archive: intake-only instances
retain reads and remember, not server-draft or SMTP operations.

## Draft tools

* `mail_list_drafts`: paged summaries of Handler-managed drafts, including
  interrupted/reconciliation states, without their bodies.
* `mail_read_draft`: paged stored body, hash, recipients, attachment paths and
  explicit server-copy state. This is a local snapshot, not proof that the server
  copy was not edited elsewhere.
* `mail_list_server_drafts`: header-only, newest-first paged listing of current
  IMAP Drafts, including untracked/historical copies.
* `mail_read_server_draft`: current body (plain or HTML-to-text) and attachment
  metadata, using a `UIDVALIDITY:UID` reference and mailbox ID. Reads use PEEK and
  read-only selection. Refuse stale references after UID renumbering.
* `mail_update_draft`: update a tracked draft by ID and the body hash returned by
  `mail_read_draft`. Keep sender/mailbox, threading, omitted fields and draft ID.
  Verify server identity and content before replacement. No SMTP send occurs.

New `mail_draft_reply` calls use an atomic database reservation. Its identity is
a hash of an explicit `inquiry_key` (buyer domain + reference) or the replied-to
RFC Message-ID. The key is instance-wide, so copies across mailboxes cannot race
to append two drafts. New standalone messages without an inquiry key remain
independent. Identical retries reuse the draft; changed body, sender or attachment
bytes produce an explicit conflict directing the agent to read/update the existing
draft. Do not bypass that conflict by inventing another key.

## Server replacement and interruption

The draft stores its server Message-ID, semantic content signature and attachment
hashes. Before an edit, atomically transition it from `draft` to `updating`, with
the planned replacement Message-ID persisted for recovery. Find exactly one old
copy by Message-ID, check its current content signature, append the replacement,
and remove only the verified old UID with UIDPLUS `UID EXPUNGE`. Never issue global
EXPUNGE or delete other drafts. This internal replacement edits the same logical
draft; there is no new tool for deleting independent drafts.

Refuse legacy/untracked server copies without a trusted content snapshot, external
edits, duplicate Message-IDs, or servers without UIDPLUS. Historical duplicates
can now be inspected, but are not automatically edited or deleted.

An APPEND timeout may happen after server acceptance. From the append attempt
onward, any failure puts the draft in `reconcile`, retaining old and potential new
Message-IDs. No automatic retry or send is allowed from that state. A crash can
leave `creating` or `updating`; these states also block duplicate creation and
sending. Inspect server copies and reconcile before manually returning the record
to `draft`. Do not clear states blindly. Successful update changes the stored
version, invalidating a previously approved send snapshot.

## Validation and boundaries

Tests cover effective owners/scopes, authenticated MCP listing, intake-only
capabilities, concurrent reservation, reused drafts, attachment changes,
signature/body inspection, precise server replacement, stale reads, user-side
server edits, reconciliation after cleanup failure, blocked sends, current server
draft listing and stale UIDVALIDITY. The transport test asserts targeted UID STORE
and UID EXPUNGE and forbids global EXPUNGE.

Tests use fake mail servers. No production drafts were edited or deleted and no
production SMTP message was sent for validation. Real-server replacement behavior
should be exercised with a dedicated test mailbox before claiming end-to-end
provider validation. Auto-signature policy is not invented by these tools: the
agent must use the saved signature and inspect the actual body before requesting
a send. Deletion approvals and existing send approvals remain in force.
