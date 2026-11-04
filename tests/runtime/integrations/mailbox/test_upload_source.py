"""Provenance of email attachments the agent uploads to Crontal Files."""

from handler.runtime.integrations.mailbox.tools import MailTools

from .test_tools import call, gw  # noqa: F401  (fixture)


async def test_upload_source_names_the_email_of_a_saved_attachment(gw):  # noqa: F811
    tools = MailTools(gw.service, gw.mail_store, workspace_for=lambda key: gw.workspace)
    found = await call(gw, "mail_search", {"text": "圆钢"})
    (hit,) = found.content["messages"]
    attachment = await call(gw, "mail_get_attachment", {"message_id": hit["id"],
                                                        "attachment": 0})
    detail = await tools.upload_source(gw.ctx, attachment.content["saved_to"])
    assert detail == {
        "mailbox": "sales@example.cn", "mailbox_id": gw.mailbox.id,
        "sender": "li@client.cn", "sender_name": "Li", "subject": "RFQ 316L 圆钢",
        "message_id": "rfq1@client.cn", "date": detail["date"]}

    # Saved at ingest: mail/<mailbox>/<date>-<hash>/attachments/<name>
    folder = f"mail/{gw.mailbox.id}/2026-11-04-abc"
    await gw.mail_store.set_saved_dir(hit["id"], folder)
    ingest = await tools.upload_source(gw.ctx, f"{folder}/attachments/清单.csv")
    assert ingest["message_id"] == "rfq1@client.cn"

    # Anything else is not an email attachment.
    for path in ("quote.pdf", f"{folder}/body.txt", f"mail/other/{hit['id']}/x.csv",
                 "downloads/po.csv"):
        assert await tools.upload_source(gw.ctx, path) is None, path
