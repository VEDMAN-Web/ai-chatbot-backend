"""
Finding folders and files in the signed-in staff member's own Google Drive from the admin chat
("find this folder named varsovia", "search my drive for Oferta", "list the files in folder varsovia").

Read-only: the Drive permission the backend holds is drive.readonly. Nothing here reads file contents; reading a file and adding
listings from it stays the separate, confirm-first Drive import.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

FOLDER_MIME = "application/vnd.google-apps.folder"
MAX_MATCHES = 10
MAX_FOLDER_ITEMS = 15

_DRIVE_WORDS = re.compile(r"\b(folder|folders|directory|directories|drive|google\s+drive|gdrive)\b", re.I)
_FILE_NAMED = re.compile(r"\b(?:file|document|pdf|doc|docx|xlsx|spreadsheet)s?\b.*\b(?:named|called)\b", re.I)
_FIND_VERBS = re.compile(r"\b(find|search|look\s+for|looking\s+for|locate|list|show|where\s+is|where's|any|check\s+for)\b", re.I)
_ACTION_VERBS = re.compile(r"\b(read|import|add|update|create|delete|send|email|summari[sz]e|extract|ingest|upload)\b", re.I)
_STATUS_WORDS = re.compile(r"\b(connect(?:ed|ion)?|linked?|status|log(?:ged)?\s*in|authori[sz]ed)\b", re.I)
_QUOTED = re.compile(r"[\"“‘']([^\"”’']{1,120})[\"”’']")
_AFTER_NAME = re.compile(r"\b(?:named|called|titled)\s+(?P<n>.+?)\s*[?.!]*$", re.I)
_AFTER_FOR = re.compile(r"\b(?:search|look(?:ing)?|find|check)\s+(?:my\s+|the\s+|in\s+)*(?:google\s+)?(?:drive\s+)?for\s+(?P<n>.+?)\s*[?.!]*$", re.I)
_AFTER_FOLDER = re.compile(r"\b(?:folder|directory)\s+(?:named\s+|called\s+)?(?P<n>[^\s?.!][^?.!]*?)\s*[?.!]*$", re.I)
_FILE_TOKEN = re.compile(r"\b([\w\-.]{2,80}\.(?:pdf|docx?|xlsx?|csv|txt))\b", re.I)
_NOISE = re.compile(r"\b(?:in|from|on|of|inside|within)\s+(?:my\s+|the\s+)?(?:google\s+)?(?:drive|gdrive)\b|\bmy\s+drive\b|\bgoogle\s+drive\b", re.I)


@dataclass
class DriveSearch:
    query: Optional[str]      # None when the message did not say what to look for
    kind: str                 # "folder", "file" or "any"


def detect_drive_search(message: str) -> Optional[DriveSearch]:
    """A request to find or list folders/files in Drive, or None. Reading a file, importing from it, and 'is my drive connected' are not searches."""
    text = (message or "").strip()
    if not text or _STATUS_WORDS.search(text) and not _FIND_VERBS.search(text):
        return None
    if not (_DRIVE_WORDS.search(text) or _FILE_NAMED.search(text)):
        return None
    if not _FIND_VERBS.search(text) or _ACTION_VERBS.search(text):
        return None
    low = text.lower()
    kind = "folder" if re.search(r"\b(folder|folders|directory|directories)\b", low) else (
        "file" if re.search(r"\b(file|files|document|documents)\b", low) else "any")
    query = None
    quoted = _QUOTED.search(text)
    if quoted:
        query = quoted.group(1)
    if not query:
        token = _FILE_TOKEN.search(text)
        if token:
            query = token.group(1)
    if not query:
        for pattern in (_AFTER_NAME, _AFTER_FOR, _AFTER_FOLDER):
            m = pattern.search(text)
            if m:
                query = m.group("n")
                break
    if query:
        query = _NOISE.sub("", query).strip(" .,;:'\"")
        query = re.sub(r"^(?:the|a|an|this|that|my|folder|file|named|called)\s+", "", query, flags=re.I).strip(" .,;:'\"")
        query = re.sub(r"\s+(?:folder|directory)$", "", query, flags=re.I).strip()
        if not query or query.lower() in {"drive", "google drive", "folder", "folders", "file", "files", "it", "this", "that"}:
            query = None
    return DriveSearch(query=query, kind=kind)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def search_drive(query: str, kind: str, staff_email: str, db: Any = None) -> Dict[str, Any]:
    """Folders/files whose name contains `query` in this staff member's own Drive (never the shared account), plus what is inside
    the folders found. Status: ok, not_connected or error."""
    from app.services.google_services import google_manager

    creds = google_manager.get_credentials_for_user(db, staff_email)
    if not creds:
        return {"status": "not_connected", "matches": [], "message": "Your Google Workspace is not connected."}
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds)
        clauses = [f"name contains '{_escape(query)}'", "trashed = false"]
        if kind == "folder":
            clauses.append(f"mimeType = '{FOLDER_MIME}'")
        elif kind == "file":
            clauses.append(f"mimeType != '{FOLDER_MIME}'")
        found = service.files().list(
            q=" and ".join(clauses), pageSize=MAX_MATCHES, orderBy="modifiedTime desc",
            fields="files(id, name, mimeType, modifiedTime, webViewLink, size)",
        ).execute().get("files", [])
        matches: List[Dict[str, Any]] = []
        for item in found[:MAX_MATCHES]:
            entry = {
                "id": item.get("id"), "name": item.get("name"), "type": "folder" if item.get("mimeType") == FOLDER_MIME else "file",
                "mime": item.get("mimeType"), "modified": (item.get("modifiedTime") or "")[:10], "link": item.get("webViewLink"),
                "size": item.get("size"),
            }
            if entry["type"] == "folder" and len([m for m in matches if m["type"] == "folder"]) < 3:
                inside = service.files().list(
                    q=f"'{_escape(str(item.get('id')))}' in parents and trashed = false", pageSize=MAX_FOLDER_ITEMS,
                    orderBy="name", fields="files(id, name, mimeType, modifiedTime, size)",
                ).execute().get("files", [])
                entry["contents"] = [
                    {"id": f.get("id"), "name": f.get("name"), "type": "folder" if f.get("mimeType") == FOLDER_MIME else "file",
                     "mime": f.get("mimeType"), "size": f.get("size")} for f in inside
                ]
            matches.append(entry)
        return {"status": "ok", "query": query, "kind": kind, "matches": matches}
    except Exception as exc:   # an API failure is reported, never turned into "no results"
        return {"status": "error", "matches": [], "message": f"Google Drive could not be searched ({type(exc).__name__}). Try again, or reconnect Google Workspace."}


def _size(value: Any) -> str:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    return f"{n / 1_048_576:.1f} MB" if n >= 1_048_576 else f"{max(1, n // 1024)} KB"


def format_drive_reply(result: Dict[str, Any], query: Optional[str], kind: str) -> str:
    what = {"folder": "folder", "file": "file", "any": "folder or file"}[kind]
    if result["status"] == "not_connected":
        return ("Your Google Workspace is not connected, so I cannot look in your Drive. Use the **Connect** button (bottom left) "
                "and sign in with the Google account that holds the files.")
    if result["status"] == "error":
        return result["message"]
    matches = result["matches"]
    if not matches:
        return f"I found no {what} with '{query}' in its name in your Google Drive. Nothing was changed."
    lines = [f"Found **{len(matches)}** {what}{'s' if len(matches) != 1 else ''} matching '{query}' in your Google Drive:\n"]
    for m in matches:
        extra = " · ".join(x for x in (m["type"], _size(m.get("size")), f"modified {m['modified']}" if m.get("modified") else "") if x)
        link = f" — [open]({m['link']})" if m.get("link") else ""
        lines.append(f"- **{m['name']}** ({extra}){link}")
        for f in m.get("contents") or []:
            lines.append(f"    - {f['name']}{' (folder)' if f['type'] == 'folder' else ''}{(' · ' + _size(f.get('size'))) if _size(f.get('size')) else ''}")
        if m.get("contents") == [] and m["type"] == "folder":
            lines.append("    - (empty folder)")
    lines.append("\nI only read your Drive; nothing was changed. To use a file, say `read file <name> from Drive`.")
    return "\n".join(lines)
