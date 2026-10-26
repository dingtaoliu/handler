"""Parsing of real-world (Chinese) mail: charsets, encoded words, filenames."""
import base64

from handler.runtime.integrations.mailbox.message import (
    decode_bytes,
    decode_header_value,
    normalize_subject,
    parse_message,
)


def b64(text: str, charset: str) -> str:
    return base64.b64encode(text.encode(charset)).decode()


def test_gb2312_label_with_gbk_only_characters_decodes():
    # 镕 is in GBK but not in GB2312: strict gb2312 would fail on it.
    text = "不锈钢管报价，镕铭公司"
    assert decode_bytes(text.encode("gbk"), "gb2312") == text
    assert decode_bytes(text.encode("gb18030"), "GB2312") == text
    assert decode_bytes(text.encode("utf-8"), "gb2312") == text  # mislabelled UTF-8 bytes


def test_rfc2047_headers_in_several_charsets():
    subject = f"=?GB2312?B?{b64('回复: 316L 圆钢询价', 'gbk')}?= =?utf-8?B?{b64('（加急）', 'utf-8')}?="
    assert decode_header_value(subject) == "回复: 316L 圆钢询价（加急）"
    assert decode_header_value("=?gbk?Q?=B1=A8=BC=DB?=") == "报价"
    assert decode_header_value("Plain ASCII") == "Plain ASCII"


def test_gbk_message_with_raw_8bit_headers_and_encoded_attachment_name():
    body = "您好，\r\n请报价 304 不锈钢板 2mm，数量 5 吨。\r\n".encode("gbk")
    name = f"=?GB2312?B?{b64('询价单-2026年10月.xlsx', 'gbk')}?="
    raw = (b"From: =?GBK?B?" + b64("张经理", "gbk").encode() + b"?= <zhang@buyer.cn>\r\n"
           b"To: sales@yeup-tube.com\r\n"
           b"Subject: " + "询价 304 板".encode("gbk") + b"\r\n"  # raw 8-bit, no charset
           b"Message-ID: <abc@buyer.cn>\r\n"
           b"In-Reply-To: <root@yeup-tube.com>\r\n"
           b"References: <root@yeup-tube.com> <mid@buyer.cn>\r\n"
           b"Date: Sun, 25 Oct 2026 10:00:00 +0800\r\n"
           b"MIME-Version: 1.0\r\n"
           b'Content-Type: multipart/mixed; boundary="b1"\r\n\r\n'
           b"--b1\r\nContent-Type: text/plain; charset=gb2312\r\n"
           b"Content-Transfer-Encoding: base64\r\n\r\n"
           + base64.encodebytes(body) +
           b"--b1\r\nContent-Type: application/vnd.ms-excel; name=\"" + name.encode() + b"\"\r\n"
           b"Content-Disposition: attachment; filename=\"" + name.encode() + b"\"\r\n"
           b"Content-Transfer-Encoding: base64\r\n\r\n"
           + base64.encodebytes(b"PK\x03\x04data") + b"--b1--\r\n")
    parsed = parse_message(raw)
    assert parsed.sender.name == "张经理" and parsed.sender.address == "zhang@buyer.cn"
    assert parsed.subject == "询价 304 板"
    assert "请报价 304 不锈钢板 2mm" in parsed.body_text
    assert parsed.message_id == "abc@buyer.cn"
    assert parsed.parent_ids == ["root@yeup-tube.com", "mid@buyer.cn"]
    assert parsed.date_iso == "2026-10-25T02:00:00+00:00"
    (attachment,) = parsed.attachments
    assert attachment.filename == "询价单-2026年10月.xlsx"
    assert attachment.data == b"PK\x03\x04data"


def test_rfc2231_filename_and_html_only_body():
    raw = (b"From: a@b.cn\r\nTo: c@d.cn\r\nSubject: hi\r\nMIME-Version: 1.0\r\n"
           b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n'
           b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
           b"<p>Hello <b>there</b></p>\r\n"
           b"--x\r\nContent-Type: application/pdf\r\n"
           b"Content-Disposition: attachment; filename*=UTF-8''%E5%90%88%E5%90%8C.pdf\r\n\r\n"
           b"%PDF\r\n--x--\r\n")
    parsed = parse_message(raw)
    assert "Hello" in parsed.body_text and "<p>" not in parsed.body_text
    assert parsed.attachments[0].filename == "合同.pdf"
    assert parsed.message_id.startswith("sha256-")


def test_normalize_subject():
    assert normalize_subject("Re: RE:  Fwd: 报价单") == "报价单"
    assert normalize_subject("回复：转发: Quote") == "quote"
    assert normalize_subject("[EXT] Re[2]: Order 55") == "order 55"
