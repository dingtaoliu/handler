# Private user spaces

A single Handler instance can serve several people without making one person's
agent history visible to another. The durable `owner_user_id` on a thread is
the authorization boundary; thread-key prefixes are not trusted for access
control.

## Rules

- A signed-in user sees only threads they own, including non-chat threads such
  as email, Gmail, Crontal, and automations. Messages, runs, events, tasks,
  approvals, audit entries, usage, and live SSE updates follow that ownership.
- Admin is an operational role, not permission to read personal content. An
  admin user's token sees that admin's own content. The instance admin key must
  explicitly select a user with `X-Handler-User` (or `act_as` for SSE) and then
  receives exactly that user's visibility and role.
- Memory is private by default. Logical topic `preferences` is stored in a
  non-reversible per-user namespace. `shared/preferences` is visible to every
  authenticated user and must be chosen explicitly. Pre-existing unnamespaced
  memory is exposed as `shared/...` so upgrades do not silently reassign it.
- Automations, uploads, notifications, Google connections, generic
  connections, and usage are scoped to their creating user. Cross-user misses
  normally return 404 so identifiers cannot be probed.
- Settings, the agent persona, the users directory, and operational logs remain
  instance-level administration. Logs must not contain conversation bodies or
  credentials.

Chat conversation titles use the first user message as their fallback title.
A user can replace that title manually, and the manual title is preserved.

## Identity and deployment

Daily browser access should use a user token or a trusted-device mapping. Every
person needs a distinct Handler user and device mapping. Do not configure a
catch-all trusted user on a shared instance, because requests without a token
would then all resolve to the same identity.

The raw admin key is for provisioning and instance settings. To troubleshoot a
specific user's private resources, deliberately add `X-Handler-User: <id>`;
there is no implicit admin-wide content view.

## Compatibility

This change needs no database migration. Existing threads with a populated
`owner_user_id` become private to that owner. Unowned threads become invisible
through user-facing content APIs until an owner is assigned. Existing memory
remains available under the explicit `shared/` prefix.
