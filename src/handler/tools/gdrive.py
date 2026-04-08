"""Google Drive tool — single entry point for file management, Docs, and Sheets.

Credentials setup (same OAuth client as Gmail):
1. Go to console.cloud.google.com → APIs & Services → Credentials
2. Use existing OAuth 2.0 Client ID (Desktop app) from Gmail setup
3. Enable Google Drive API, Google Docs API, and Google Sheets API in the console
4. On first run, a browser opens for OAuth consent (one-time)

Token is stored separately from Gmail at data/credentials/drive_token.json.
"""

from __future__ import annotations

import json
import logging
import os

from agents import function_tool

logger = logging.getLogger("handler.tools.gdrive")

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
]
from ..paths import DATA_DIR as _DATA_DIR

_CREDENTIALS_PATH = _DATA_DIR / "credentials" / "desktop.json"
_TOKEN_PATH = _DATA_DIR / "credentials" / "drive_token.json"

_HELP_TEXT = """\
google_drive — manage Google Drive files, Docs, and Sheets.

Actions:
  list           — Search or list files.
                   Params: query (Drive search query, optional), max_results (default 20).
                   Query examples: "name contains 'report'", "mimeType='application/vnd.google-apps.spreadsheet'",
                   "modifiedTime > '2025-01-01'", "'root' in parents", "fullText contains 'budget'".
  read           — Read file content by ID. Google Docs → text, Sheets → tab-delimited rows.
                   Params: file_id.
  create_doc     — Create a new Google Doc.
                   Params: title, content (optional initial text).
  create_sheet   — Create a new Google Sheet.
                   Params: title, data (optional JSON list of lists, e.g. '[["Name","Age"],["Alice",30]]').
  update_doc     — Append or replace entire Google Doc content.
                   Params: file_id, content, mode ('append' or 'replace', default 'append').
  edit_doc       — Find-and-replace text in a Google Doc. Supports multiple replacements in one call.
                   Params: file_id, replacements (JSON list of {"find": "...", "replace": "..."} objects).
  update_sheet   — Write data to a Google Sheet.
                   Params: file_id, data (JSON list of lists), sheet_name (default 'Sheet1'), start_cell (default 'A1').
  add_sheet_tab  — Add a new tab to an existing spreadsheet and optionally write data.
                   Params: file_id, sheet_name (name for new tab), data (optional JSON list of lists).
  add_doc_tab    — Add a new tab to an existing Google Doc and optionally write content.
                   Params: file_id, title (tab name), content (optional text to write to the new tab)."""


def _get_credentials():
    """Authenticate and return OAuth credentials for Drive, Docs, and Sheets."""
    import google.auth.exceptions
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds_path = str(_CREDENTIALS_PATH)
    token_path = str(_TOKEN_PATH)

    if not os.path.exists(creds_path):
        raise FileNotFoundError(
            f"Google credentials not found at {creds_path}. "
            "Download OAuth client JSON from Google Cloud Console → "
            "APIs & Services → Credentials, and save it there."
        )

    _CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except google.auth.exceptions.RefreshError:
                logger.warning("Drive token refresh failed, re-authenticating...")
                if os.path.exists(token_path):
                    os.remove(token_path)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(token_path, "w") as f:
            f.write(creds.to_json())

    return creds


def _build_drive_service(creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
    return build("drive", "v3", http=authorized_http)


def _build_sheets_service(creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
    return build("sheets", "v4", http=authorized_http)


def _build_docs_service(creds):
    import httplib2
    from google_auth_httplib2 import AuthorizedHttp
    from googleapiclient.discovery import build

    authorized_http = AuthorizedHttp(creds, http=httplib2.Http(timeout=120))
    return build("docs", "v1", http=authorized_http)


def gdrive_tool():
    """Create a single google_drive tool. Authenticates on first call.

    Returns a single @function_tool.
    Raises FileNotFoundError if credentials are not set up.
    """
    creds = _get_credentials()

    def _action_list(query: str, max_results: int) -> str:
        svc = _build_drive_service(creds)
        params = {
            "pageSize": min(max_results, 100),
            "fields": "files(id, name, mimeType, modifiedTime, size, webViewLink, owners)",
            "orderBy": "modifiedTime desc",
        }
        if query:
            params["q"] = query

        results = svc.files().list(**params).execute()
        files = results.get("files", [])

        if not files:
            return f"No files found{f' for: {query}' if query else ''}."

        lines = [f"Found {len(files)} file(s){f' for: {query}' if query else ''}:\n"]
        type_map = {
            "application/vnd.google-apps.document": "Google Doc",
            "application/vnd.google-apps.spreadsheet": "Google Sheet",
            "application/vnd.google-apps.presentation": "Google Slides",
            "application/vnd.google-apps.folder": "Folder",
        }
        for f in files:
            mime = f.get("mimeType", "")
            ftype = type_map.get(mime, mime.split("/")[-1] if "/" in mime else mime)
            owner = ""
            if f.get("owners"):
                owner = f["owners"][0].get("displayName", "")

            lines.append(
                f"---\n"
                f"ID: {f['id']}\n"
                f"Name: {f['name']}\n"
                f"Type: {ftype}\n"
                f"Modified: {f.get('modifiedTime', 'unknown')}\n"
                f"Link: {f.get('webViewLink', 'N/A')}"
            )
            if owner:
                lines[-1] += f"\nOwner: {owner}"

        logger.info(f"google_drive list: query={query!r} results={len(files)}")
        return "\n".join(lines)

    def _action_read(file_id: str) -> str:
        drive_svc = _build_drive_service(creds)

        meta = drive_svc.files().get(
            fileId=file_id,
            fields="id, name, mimeType, modifiedTime, webViewLink",
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", "unknown")

        header = (
            f"File: {name}\n"
            f"Type: {mime}\n"
            f"Modified: {meta.get('modifiedTime', 'unknown')}\n"
            f"Link: {meta.get('webViewLink', 'N/A')}\n\n"
        )

        if mime == "application/vnd.google-apps.document":
            docs_svc = _build_docs_service(creds)
            doc = docs_svc.documents().get(
                documentId=file_id, includeTabsContent=True,
            ).execute()
            tabs = doc.get("tabs", [])
            parts = []
            for tab in tabs:
                props = tab.get("tabProperties", {})
                tab_title = props.get("title", "Untitled")
                tab_id = props.get("tabId", "")
                body = tab.get("documentTab", {}).get("body", {})
                # Extract text from structural elements
                text_parts = []
                for elem in body.get("content", []):
                    para = elem.get("paragraph")
                    if para:
                        for pe in para.get("elements", []):
                            tr = pe.get("textRun")
                            if tr:
                                text_parts.append(tr.get("content", ""))
                tab_text = "".join(text_parts)
                parts.append(f"## Tab: {tab_title} (id: {tab_id})\n{tab_text}")
            content = "\n\n".join(parts) if parts else "(empty document)"
            if len(content) > 10000:
                content = content[:7000] + "\n\n[...truncated...]\n\n" + content[-3000:]
            logger.info(f"google_drive read: doc {file_id} ({len(tabs)} tabs, {len(content)} chars)")
            return header + content

        if mime == "application/vnd.google-apps.spreadsheet":
            sheets_svc = _build_sheets_service(creds)
            spreadsheet = sheets_svc.spreadsheets().get(
                spreadsheetId=file_id
            ).execute()
            sheet_names = [
                s["properties"]["title"]
                for s in spreadsheet.get("sheets", [])
            ]
            parts = []
            for sname in sheet_names[:10]:
                result = sheets_svc.spreadsheets().values().get(
                    spreadsheetId=file_id,
                    range=f"'{sname}'",
                ).execute()
                rows = result.get("values", [])
                if rows:
                    parts.append(f"## Sheet: {sname}\n")
                    for row in rows[:500]:
                        parts.append("\t".join(str(c) for c in row))
                    parts.append("")

            content = "\n".join(parts) if parts else "(empty spreadsheet)"
            logger.info(f"google_drive read: sheet {file_id} ({len(content)} chars)")
            return header + content

        if mime.startswith("application/vnd.google-apps."):
            try:
                content = drive_svc.files().export(
                    fileId=file_id, mimeType="text/plain"
                ).execute()
                if isinstance(content, bytes):
                    content = content.decode("utf-8", errors="replace")
                logger.info(f"google_drive read: exported {file_id} ({len(content)} chars)")
                return header + content
            except Exception:
                return header + "(Unsupported Google Workspace file type.)"

        return header + "(Binary or non-text file — cannot display content.)"

    def _action_create_doc(title: str, content: str) -> str:
        docs_svc = _build_docs_service(creds)

        doc = docs_svc.documents().create(body={"title": title}).execute()
        doc_id = doc["documentId"]

        if content:
            docs_svc.documents().batchUpdate(
                documentId=doc_id,
                body={"requests": [
                    {"insertText": {"location": {"index": 1}, "text": content}},
                ]},
            ).execute()

        link = f"https://docs.google.com/document/d/{doc_id}/edit"
        logger.info(f"google_drive create_doc: {doc_id} title={title!r}")
        return f"Document created.\nTitle: {title}\nID: {doc_id}\nLink: {link}"

    def _action_create_sheet(title: str, data: str) -> str:
        sheets_svc = _build_sheets_service(creds)
        spreadsheet = sheets_svc.spreadsheets().create(
            body={"properties": {"title": title}}
        ).execute()
        sheet_id = spreadsheet["spreadsheetId"]

        if data:
            rows = json.loads(data)
            if rows and isinstance(rows, list):
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=sheet_id,
                    range="Sheet1!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": rows},
                ).execute()

        link = f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
        logger.info(f"google_drive create_sheet: {sheet_id} title={title!r}")
        return f"Spreadsheet created.\nTitle: {title}\nID: {sheet_id}\nLink: {link}"

    def _action_update_doc(file_id: str, content: str, mode: str) -> str:
        docs_svc = _build_docs_service(creds)

        if mode == "replace":
            # Get current doc to find end index, then replace all content
            doc = docs_svc.documents().get(documentId=file_id).execute()
            end_index = doc["body"]["content"][-1]["endIndex"]
            requests = []
            if end_index > 2:
                requests.append({
                    "deleteContentRange": {
                        "range": {"startIndex": 1, "endIndex": end_index - 1},
                    }
                })
            requests.append({
                "insertText": {"location": {"index": 1}, "text": content},
            })
            docs_svc.documents().batchUpdate(
                documentId=file_id, body={"requests": requests},
            ).execute()
        else:
            # Append: insert at the end of the document
            doc = docs_svc.documents().get(documentId=file_id).execute()
            end_index = doc["body"]["content"][-1]["endIndex"]
            docs_svc.documents().batchUpdate(
                documentId=file_id,
                body={"requests": [
                    {"insertText": {"location": {"index": end_index - 1}, "text": "\n" + content}},
                ]},
            ).execute()

        link = f"https://docs.google.com/document/d/{file_id}/edit"
        logger.info(f"google_drive update_doc: {file_id} mode={mode}")
        return f"Document updated ({mode}).\nID: {file_id}\nLink: {link}"

    def _action_edit_doc(file_id: str, replacements: str) -> str:
        docs_svc = _build_docs_service(creds)
        items = json.loads(replacements)

        if not isinstance(items, list):
            return "Error: replacements must be a JSON list of {\"find\": \"...\", \"replace\": \"...\"} objects."

        requests = []
        for item in items:
            find = item.get("find", "")
            replace = item.get("replace", "")
            if not find:
                continue
            requests.append({
                "replaceAllText": {
                    "containsText": {"text": find, "matchCase": True},
                    "replaceText": replace,
                }
            })

        if not requests:
            return "No valid replacements provided."

        result = docs_svc.documents().batchUpdate(
            documentId=file_id, body={"requests": requests},
        ).execute()

        total = sum(
            r.get("replaceAllText", {}).get("occurrencesChanged", 0)
            for r in result.get("replies", [])
        )
        link = f"https://docs.google.com/document/d/{file_id}/edit"
        logger.info(f"google_drive edit_doc: {file_id} {total} occurrences changed")
        return f"Document edited: {total} occurrence(s) replaced across {len(requests)} find-and-replace(s).\nID: {file_id}\nLink: {link}"

    def _action_update_sheet(file_id: str, data: str, sheet_name: str, start_cell: str) -> str:
        sheets_svc = _build_sheets_service(creds)
        rows = json.loads(data)

        if not isinstance(rows, list):
            return "Error: data must be a JSON list of lists."

        range_str = f"'{sheet_name}'!{start_cell}"
        result = sheets_svc.spreadsheets().values().update(
            spreadsheetId=file_id,
            range=range_str,
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()

        updated = result.get("updatedCells", 0)
        link = f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        logger.info(f"google_drive update_sheet: {file_id} updated {updated} cells")
        return f"Sheet updated: {updated} cells written.\nRange: {sheet_name}!{start_cell}\nID: {file_id}\nLink: {link}"

    def _action_add_sheet_tab(file_id: str, sheet_name: str, data: str) -> str:
        sheets_svc = _build_sheets_service(creds)

        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=file_id,
            body={"requests": [
                {"addSheet": {"properties": {"title": sheet_name}}},
            ]},
        ).execute()

        if data:
            rows = json.loads(data)
            if rows and isinstance(rows, list):
                sheets_svc.spreadsheets().values().update(
                    spreadsheetId=file_id,
                    range=f"'{sheet_name}'!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": rows},
                ).execute()

        link = f"https://docs.google.com/spreadsheets/d/{file_id}/edit"
        logger.info(f"google_drive add_sheet_tab: {file_id} tab={sheet_name!r}")
        return f"Tab '{sheet_name}' added.\nID: {file_id}\nLink: {link}"

    def _action_add_doc_tab(file_id: str, title: str, content: str) -> str:
        docs_svc = _build_docs_service(creds)

        # Create the new tab
        result = docs_svc.documents().batchUpdate(
            documentId=file_id,
            body={"requests": [
                {"addDocumentTab": {"tabProperties": {"title": title}}},
            ]},
        ).execute()

        # Get the new tab ID from the response
        tab_id = result["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]

        if content:
            docs_svc.documents().batchUpdate(
                documentId=file_id,
                body={"requests": [
                    {"insertText": {
                        "location": {"index": 1, "tabId": tab_id},
                        "text": content,
                    }},
                ]},
            ).execute()

        link = f"https://docs.google.com/document/d/{file_id}/edit?tab={tab_id}"
        logger.info(f"google_drive add_doc_tab: {file_id} tab={title!r} tab_id={tab_id}")
        return f"Tab '{title}' added.\nTab ID: {tab_id}\nID: {file_id}\nLink: {link}"

    @function_tool
    def google_drive(
        action: str,
        query: str = "",
        file_id: str = "",
        title: str = "",
        content: str = "",
        data: str = "",
        replacements: str = "",
        mode: str = "append",
        sheet_name: str = "Sheet1",
        start_cell: str = "A1",
        max_results: int = 20,
    ) -> str:
        """Google Drive: manage files, docs, and sheets. Call with action='help' for detailed usage.

        Args:
            action:       One of: help, list, read, create_doc, create_sheet, update_doc, edit_doc, update_sheet, add_sheet_tab, add_doc_tab.
            query:        (list) Drive search query. Leave empty for recent files.
            file_id:      (read, update_doc, edit_doc, update_sheet, add_sheet_tab) Google Drive file ID.
            title:        (create_doc, create_sheet) Title for new document/spreadsheet.
            content:      (create_doc, update_doc) Text content.
            data:         (create_sheet, update_sheet, add_sheet_tab) JSON list of lists, e.g. '[["Name","Age"],["Alice",30]]'.
            replacements: (edit_doc) JSON list of find-and-replace objects, e.g. '[{"find": "old text", "replace": "new text"}]'.
            mode:         (update_doc) 'append' or 'replace'. Default 'append'.
            sheet_name:   (update_sheet, add_sheet_tab) Sheet tab name. Default 'Sheet1'.
            start_cell:   (update_sheet) Top-left cell. Default 'A1'.
            max_results:  (list) Max results. Default 20.
        """
        try:
            if action == "help":
                return _HELP_TEXT
            if action == "list":
                return _action_list(query, max_results)
            if action == "read":
                if not file_id:
                    return "Missing required field: file_id."
                return _action_read(file_id)
            if action == "create_doc":
                if not title:
                    return "Missing required field: title."
                return _action_create_doc(title, content)
            if action == "create_sheet":
                if not title:
                    return "Missing required field: title."
                return _action_create_sheet(title, data)
            if action == "update_doc":
                if not file_id or not content:
                    return "Missing required fields: file_id, content."
                return _action_update_doc(file_id, content, mode)
            if action == "edit_doc":
                if not file_id or not replacements:
                    return "Missing required fields: file_id, replacements."
                return _action_edit_doc(file_id, replacements)
            if action == "update_sheet":
                if not file_id or not data:
                    return "Missing required fields: file_id, data."
                return _action_update_sheet(file_id, data, sheet_name, start_cell)
            if action == "add_sheet_tab":
                if not file_id or not sheet_name:
                    return "Missing required fields: file_id, sheet_name."
                return _action_add_sheet_tab(file_id, sheet_name, data)
            if action == "add_doc_tab":
                if not file_id or not title:
                    return "Missing required fields: file_id, title."
                return _action_add_doc_tab(file_id, title, content)
            return f"Unknown action '{action}'. Use action='help' for usage."
        except json.JSONDecodeError as e:
            return f"Error: invalid JSON — {e}"
        except Exception as e:
            logger.error(f"google_drive {action} failed: {e}", exc_info=True)
            return f"Error in google_drive {action}: {e}"

    return google_drive
