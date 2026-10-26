import pytest

from handler.runtime.gateway.secret_box import EncryptedSecrets, SecretBox
from handler.runtime.integrations.mailbox.config import Mailbox
from handler.runtime.integrations.mailbox.service import MailboxService
from handler.runtime.integrations.mailbox.store import MailStore
from handler.runtime.store.sqlite import SQLiteStore
from handler.runtime.workspaces import WorkspaceSync

from .fake_mail import FakeMailServer, FakeTransport


class Env:
    def __init__(self, tmp_path, store):
        self.tmp_path = tmp_path
        self.store = store
        self.mail_store = MailStore(store)
        self.server = FakeMailServer()
        self.transport = FakeTransport(self.server)
        self.secrets = EncryptedSecrets(store, SecretBox("test-secret-key"))
        self.workspaces = tmp_path / "workspaces"
        self.service = MailboxService(
            store=store, mail_store=self.mail_store, secrets=self.secrets,
            transport=self.transport, workspace_sync=WorkspaceSync(None, tmp_path),
            workspaces_dir=self.workspaces)

    async def add_mailbox(self, **overrides) -> Mailbox:
        values = dict(label="Sales", email="sales@example.cn", username="sales@example.cn",
                      imap_host="imap.example.cn", imap_port=993, imap_security="tls",
                      smtp_host="smtp.example.cn", smtp_port=465, smtp_security="tls",
                      owner_user_id="7")
        values.update(overrides)
        mailbox = Mailbox(**values)
        await self.service.set_password(mailbox, self.server.password)
        await self.mail_store.put_mailbox(mailbox)
        return mailbox

    async def events(self):
        rows = await self.store._call(lambda db: [dict(r) for r in db.execute(
            "SELECT * FROM events ORDER BY received_at, id")])
        return rows


@pytest.fixture
async def env(tmp_path):
    store = SQLiteStore(tmp_path / "handler.db")
    try:
        yield Env(tmp_path, store)
    finally:
        await store.close()
