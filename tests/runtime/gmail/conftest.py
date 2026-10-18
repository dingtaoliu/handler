"""A fake Google (OAuth token endpoint + Gmail REST API) on httpx.MockTransport."""

from __future__ import annotations

import base64
import json
from email import policy
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from handler.runtime.context import RunContext
from handler.runtime.gateway.gmail import GmailTools
from handler.runtime.gateway.google_oauth import (
    DEFAULT_SCOPES,
    GoogleOAuth,
    GoogleOAuthConfig,
)
from handler.runtime.gateway.secret_box import SecretBox
from handler.runtime.store.sqlite import SQLiteStore

ACCOUNT = "crontal.contacts@gmail.com"
REDIRECT = "https://handler.test/oauth/google/callback"


def b64(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def id_token(email: str) -> str:
    return ".".join([b64('{"alg":"RS256"}'), b64(json.dumps({"email": email})), "sig"])


def part(mime: str, text: str = "", *, filename: str = "", attachment_id: str = "",
         part_id: str = "0", size: int | None = None, charset: str = "utf-8") -> dict:
    headers = [{"name": "Content-Type", "value": f"{mime}; charset={charset}"}]
    body: dict = {"size": size if size is not None else len(text.encode())}
    if attachment_id:
        body["attachmentId"] = attachment_id
    elif text:
        body["data"] = b64(text.encode(charset))
    return {"partId": part_id, "mimeType": mime, "filename": filename,
            "headers": headers, "body": body}


def message(mid: str, thread: str, *, subject="Quote", sender="buyer@acme.test",
            to=ACCOUNT, parts=None, extra_headers=None, snippet="snip") -> dict:
    headers = [{"name": "From", "value": sender}, {"name": "To", "value": to},
               {"name": "Date", "value": "Mon, 12 Oct 2026 09:00:00 +0000"},
               {"name": "Subject", "value": subject},
               {"name": "Message-ID", "value": f"<{mid}@mail.test>"}]
    headers += [{"name": k, "value": v} for k, v in (extra_headers or {}).items()]
    payload = {"partId": "", "mimeType": "multipart/mixed", "filename": "",
               "headers": headers, "body": {"size": 0},
               "parts": parts or [part("text/plain", f"Body of {mid}")]}
    return {"id": mid, "threadId": thread, "snippet": snippet, "labelIds": ["INBOX"],
            "payload": payload}


def parse_raw(raw: str):
    return BytesParser(policy=policy.default).parsebytes(
        base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))


class FakeGoogle:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.refresh_mode = "ok"  # ok | invalid_grant | invalid_client
        self.issued = 0
        self.valid: set[str] = set()
        self.revoked: list[str] = []
        self.granted_scope = " ".join(DEFAULT_SCOPES)
        self.return_refresh = True
        self.messages: dict[str, dict] = {}
        self.attachments: dict[str, bytes] = {}
        self.drafts: dict[str, dict] = {}
        self.uploads: list[httpx.Request] = []
        self.sent: list[dict] = []

    def _token(self) -> str:
        self.issued += 1
        token = f"at-{self.issued}"
        self.valid.add(token)
        return token

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com" and url.path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            if form["grant_type"] == "authorization_code":
                if form["code"] != "good-code" or form["redirect_uri"] != REDIRECT:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                body = {"access_token": self._token(), "expires_in": 3599,
                        "scope": self.granted_scope, "token_type": "Bearer",
                        "id_token": id_token(ACCOUNT)}
                if self.return_refresh:
                    body["refresh_token"] = "rt-1"
                return httpx.Response(200, json=body)
            assert form["grant_type"] == "refresh_token" and form["refresh_token"] == "rt-1"
            if self.refresh_mode != "ok":
                return httpx.Response(400 if self.refresh_mode == "invalid_grant" else 401,
                                      json={"error": self.refresh_mode})
            return httpx.Response(200, json={"access_token": self._token(),
                                             "expires_in": 3599})
        if url.host == "oauth2.googleapis.com" and url.path == "/revoke":
            self.revoked.append(parse_qs(request.content.decode())["token"][0])
            return httpx.Response(200)
        assert url.host == "gmail.googleapis.com", url
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.valid:
            return httpx.Response(401, json={"error": {"code": 401, "message": "bad token"}})
        return self._gmail(request)

    def _gmail(self, request: httpx.Request) -> httpx.Response:
        path, params = request.url.path, request.url.params
        upload = path.startswith("/upload/")
        path = path.removeprefix("/upload").removeprefix("/gmail/v1/users/me")
        method = request.method
        if path == "/messages/send" and method == "POST":
            if upload:
                meta, raw = self._draft_body(request, upload)
            else:
                meta = json.loads(request.content)
                raw = meta["raw"]
            sent = {"id": f"s{len(self.sent) + 1}",
                    "threadId": meta.get("threadId") or f"ts{len(self.sent) + 1}",
                    "labelIds": ["SENT"], "raw": raw}
            self.sent.append(sent)
            return httpx.Response(200, json={k: sent[k] for k in ("id", "threadId", "labelIds")})
        if path == "/drafts/send" and method == "POST":
            draft = self.drafts.pop(json.loads(request.content)["id"], None)
            if draft is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            sent = {"id": f"s{len(self.sent) + 1}", "threadId": draft["message"]["threadId"],
                    "labelIds": ["SENT"], "raw": draft["message"]["raw"]}
            self.sent.append(sent)
            return httpx.Response(200, json={k: sent[k] for k in ("id", "threadId", "labelIds")})
        if path == "/messages" and method == "GET":
            ids = sorted(self.messages)[: int(params.get("maxResults", 100))]
            body = {"messages": [{"id": i, "threadId": self.messages[i]["threadId"]}
                                 for i in ids], "resultSizeEstimate": len(ids)}
            if params.get("q") == "paged":
                body["nextPageToken"] = "page-2"
            return httpx.Response(200, json=body)
        if path.startswith("/messages/") and "/attachments/" in path:
            att = path.rsplit("/", 1)[-1]
            if att not in self.attachments:
                return httpx.Response(404, json={"error": {"message": "no attachment"}})
            return httpx.Response(200, json={"size": len(self.attachments[att]),
                                             "data": b64(self.attachments[att])})
        if path.startswith("/messages/"):
            found = self.messages.get(path.rsplit("/", 1)[-1])
            if found is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            return httpx.Response(200, json=found)
        if path.startswith("/threads/"):
            tid = path.rsplit("/", 1)[-1]
            msgs = [m for m in self.messages.values() if m["threadId"] == tid]
            return httpx.Response(200, json={"id": tid, "messages": msgs})
        if path == "/drafts" and method == "GET":
            return httpx.Response(200, json={"drafts": [
                {"id": d["id"], "message": {"id": d["message"]["id"]}}
                for d in self.drafts.values()]})
        if path == "/drafts" and method == "POST" or path.startswith("/drafts/") and method == "PUT":
            meta, raw = self._draft_body(request, upload)
            draft_id = meta.get("id") or f"d{len(self.drafts) + 1}"
            thread = meta["message"].get("threadId") or f"t-{draft_id}"
            self.drafts[draft_id] = {"id": draft_id, "message": {
                "id": f"m-{draft_id}-{len(self.requests)}", "threadId": thread, "raw": raw}}
            return httpx.Response(200, json={"id": draft_id, "message": {
                "id": self.drafts[draft_id]["message"]["id"], "threadId": thread,
                "labelIds": ["DRAFT"]}})
        if path.startswith("/drafts/") and method == "GET":
            draft = self.drafts.get(path.rsplit("/", 1)[-1])
            if draft is None:
                return httpx.Response(404, json={"error": {"message": "Not Found"}})
            if params.get("format") == "raw":
                return httpx.Response(200, json=draft)
            parsed = parse_raw(draft["message"]["raw"])
            headers = [{"name": k, "value": str(v)} for k, v in parsed.items()]
            return httpx.Response(200, json={"id": draft["id"], "message": {
                "id": draft["message"]["id"], "threadId": draft["message"]["threadId"],
                "snippet": "draft snippet", "payload": {"headers": headers}}})
        return httpx.Response(404, json={"error": {"message": f"unhandled {method} {path}"}})

    def _draft_body(self, request: httpx.Request, upload: bool) -> tuple[dict, str]:
        if not upload:
            body = json.loads(request.content)
            return body, body["message"]["raw"]
        self.uploads.append(request)
        assert request.url.params["uploadType"] == "multipart"
        boundary = request.headers["content-type"].split("boundary=")[1]
        chunks = request.content.split(f"--{boundary}".encode())
        meta = json.loads(chunks[1].split(b"\r\n\r\n", 1)[1])
        raw = chunks[2].split(b"\r\n\r\n", 1)[1].removesuffix(b"\r\n")
        return meta, b64(raw)


class World:
    def __init__(self, store, google, oauth, tools, workspace, ctx, clock):
        self.store, self.google, self.oauth = store, google, oauth
        self.tools, self.workspace, self.ctx, self.clock = tools, workspace, ctx, clock

    async def connect(self, owner="instance"):
        url = self.oauth.start(started_by="root", redirect_uri=REDIRECT, owner=owner)
        state = parse_qs(httpx.URL(url).query.decode())["state"][0]
        return await self.oauth.complete("good-code", state)

    async def call(self, name: str, **args):
        return await self.tools[name].handler(self.ctx, args)


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
async def world(tmp_path: Path):
    store = SQLiteStore(tmp_path / "store.sqlite")
    google = FakeGoogle()
    client = httpx.AsyncClient(transport=httpx.MockTransport(google))
    clock = Clock()
    oauth = GoogleOAuth(GoogleOAuthConfig("cid", "csecret"), store, SecretBox("k" * 32),
                        client=client, clock=clock)
    workspace = tmp_path / "ws"
    gmail = GmailTools(oauth, workspace_for=lambda key: workspace, client=client)
    ctx = RunContext(run_id="r1", thread_key="chat:u:1", user_id="u", acting_as="u")
    yield World(store, google, oauth, {s.name: s for s in gmail.tools()}, workspace, ctx,
                clock)
    await store.close()
