"""IMAP/SMTP mailbox integration (`mailbox`).

Connects any number of password-authenticated mailboxes to one instance:
- input: a poller per instance ingests new mail into the instance's store and,
  per the mailbox's wake mode, wakes the agent on a `mail` thread
  (`mail:<mailbox_id>:<conversation_id>`) with a `mail.received` event;
- tools: `mail_list_mailboxes`, `mail_search`, `mail_read`, `mail_read_thread`,
  `mail_get_attachment`, `mail_draft_reply`, `mail_send` (always approved);
- HTTP: `/api/mailboxes` admin API (Crontal proxies it);
- UI: a Connections card.

Config (all optional): `HANDLER_SECRET_KEY` (falls back to `HANDLER_ADMIN_KEY`)
encrypts passwords; `HANDLER_MAIL_CONCURRENCY` (default 2, process-wide IMAP
sessions); `HANDLER_MAIL_MAX_MESSAGE_BYTES` (default 25 MB; larger messages
are stored headers-only); `HANDLER_MAIL_MAX_WAKES_PER_HOUR` (default 30 per
mailbox). See docs/rebuild/mailbox-integration.md.
"""

from __future__ import annotations

from typing import Any

from ..base import (
    BackgroundTask,
    Integration,
    IntegrationConfig,
    IntegrationContext,
    PromptFragment,
    ThreadTypeSpec,
    UIManifestEntry,
)
from .events import EVENT_TYPE, GUIDANCE, THREAD_TYPE, render_mail_event


def _int(env, key: str, default: int, minimum: int = 1) -> int:
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{key} must be an integer") from None
    if value < minimum:
        raise ValueError(f"{key} must be at least {minimum}")
    return value


class MailboxIntegration(Integration):
    name = "mailbox"
    label = "Mailboxes (IMAP/SMTP)"

    def configure(self, config: IntegrationConfig) -> None:
        super().configure(config)
        from ...gateway.secret_box import SecretBox

        env = config.env
        self.box = SecretBox.from_env(env)
        if self.box is None:
            raise ValueError("HANDLER_SECRET_KEY (or HANDLER_ADMIN_KEY) is required to "
                             "encrypt mailbox passwords")
        self.concurrency = _int(env, "HANDLER_MAIL_CONCURRENCY", 2)
        self.max_message_bytes = _int(env, "HANDLER_MAIL_MAX_MESSAGE_BYTES",
                                      25 * 1024 * 1024, 1024)
        self.max_wakes_per_hour = _int(env, "HANDLER_MAIL_MAX_WAKES_PER_HOUR", 30, 0)
        self.transport = None  # tests inject a fake before the runtime hooks run
        self._built: dict[int, Any] = {}
        from ...instructions import register_guidance, register_tool_hint
        from ...run import register_event_renderer

        register_event_renderer(EVENT_TYPE, render_mail_event)
        register_guidance(THREAD_TYPE, GUIDANCE)
        for fragment in self.prompt_fragments():
            register_tool_hint(fragment.key, fragment.text)

    def thread_types(self) -> list[ThreadTypeSpec]:
        return [ThreadTypeSpec(name=THREAD_TYPE, lane="background", visibility="shared",
                               acts_as="owner", label="Mailbox email",
                               default_instructions=GUIDANCE, render_turn=render_mail_event)]

    def prompt_fragments(self) -> list[PromptFragment]:
        return [PromptFragment("mail_tools", (
            "If you have mail_* tools: mail_search and mail_read cover the connected "
            "mailboxes. Prepare replies with mail_draft_reply. Send (mail_send) only when "
            "the user asked for it: every send waits for a human's approval, so never say "
            "an email was sent until a result says status 'sent'."))]

    def ui_entries(self) -> list[UIManifestEntry]:
        return [UIManifestEntry("connection", "mailboxes", "Mailboxes (IMAP/SMTP)",
                                path="/api/mailboxes"),
                UIManifestEntry("thread_type", THREAD_TYPE, "Mailbox email")]

    # ── runtime ────────────────────────────────────────────────────────────

    def _parts(self, ctx: IntegrationContext):
        """Service, store and poller of this instance (built once per context)."""
        key = id(ctx)
        if key in self._built:
            return self._built[key]
        from ...gateway.secret_box import EncryptedSecrets
        from .poller import MailboxPoller
        from .service import MailboxService
        from .store import MailStore
        from .transport import StdlibTransport

        mail_store = MailStore(ctx.store)
        service = MailboxService(
            store=ctx.store, mail_store=mail_store,
            secrets=EncryptedSecrets(ctx.store, self.box),
            transport=self.transport or StdlibTransport(), settings=ctx.settings,
            workspace_sync=ctx.workspace_sync, workspaces_dir=ctx.workspaces_dir,
            notifier=ctx.notifier, instance_name=ctx.instance_name,
            max_message_bytes=self.max_message_bytes,
            max_wakes_per_hour=self.max_wakes_per_hour)
        poller = MailboxPoller(service, mail_store, concurrency=self.concurrency,
                               notifier=ctx.notifier)
        self._built[key] = (service, mail_store, poller)
        self.service, self.mail_store, self.poller = service, mail_store, poller
        return self._built[key]

    def routers(self, ctx: IntegrationContext) -> list[Any]:
        from .api import mailboxes_router

        service, mail_store, poller = self._parts(ctx)
        return [mailboxes_router(service, mail_store, poller, ctx.auth)]

    def tool_sets(self, ctx: IntegrationContext) -> list[Any]:
        from ...run import thread_slug
        from .tools import MailTools

        service, mail_store, _ = self._parts(ctx)
        workspaces_dir = ctx.workspaces_dir
        return [MailTools(service, mail_store,
                          workspace_for=lambda key: workspaces_dir / thread_slug(key))]

    def background_tasks(self, ctx: IntegrationContext) -> list[BackgroundTask]:
        _, _, poller = self._parts(ctx)
        return [BackgroundTask("mailbox-poller", poller.run)]
