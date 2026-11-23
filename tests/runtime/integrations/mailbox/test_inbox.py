from handler.runtime.integrations.mailbox.inbox import conversation_groups, split_quoted_text


def test_group_links_are_transitive_and_use_only_supplied_mailboxes():
    links = [
        {"a_account": "a", "a_id": "first", "b_account": "b", "b_id": "second"},
        {"a_account": "b", "a_id": "second", "b_account": "c", "b_id": "third"},
    ]
    grouped = conversation_groups(links)
    assert grouped[("c", "third")] == [("a", "first"), ("b", "second"), ("c", "third")]
    assert grouped[("b", "second")] == grouped[("a", "first")]


def test_reply_headers_and_forward_markers_are_collapsed():
    for history in (
        "From: Buyer\nSent: Monday\nTo: Sales\nSubject: Quote\n\nOld text",
        "发件人：买方\n发送时间：周一\n收件人：销售\n\n原邮件",
        "---------- Forwarded message ---------\nOld text",
        "On Monday Buyer wrote:\n> Old text",
    ):
        assert split_quoted_text("Confirmed.\n\n" + history) == ("Confirmed.", history)
        assert split_quoted_text(history) == ("", history)


def test_inline_answers_and_normal_body_are_preserved():
    for text in ("> What is the price?\nUSD 5", "From: warehouse stock\nTo: customer"):
        assert split_quoted_text(text) == (text, None)
