"""
Admin chat front end for Google Drive folder bulk ingestion.

  "Import all property data from this Google Drive folder: https://drive.google.com/drive/folders/<id>"
      -> validate folder, count files by format, start the job (drafts only)
  "import status"            -> batch/file progress, failed/skipped/unsupported files with reasons
  "show import drafts"       -> review table (action, confidence, source, issues)
  "retry failed import files" / "resume import" / "cancel import"
  "approve import"           -> stages ONE pending action listing what will be saved; the existing confirm step
                                (Confirm button or "yes") performs the write. Nothing is saved before that.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.services import drive_folder_ingest as ingest

PREF_KEY = "drive_ingest_job_id"

_IMPORT_ALT = r"import\w*|ingest\w*"
_IMPORT_WORD = r"(?:" + _IMPORT_ALT + r")"
_STATUS = re.compile(r"\b" + _IMPORT_WORD + r"\b.*\b(status|progress)\b|\b(status|progress)\b.*\b" + _IMPORT_WORD + r"\b|how\s+is\s+the\s+import", re.I)
_DRAFTS = re.compile(r"\b(show|list|review|see)\b.*\b" + _IMPORT_WORD + r"\b.*\b(drafts?|results?|records?|properties)\b|\breview\s+(?:the\s+)?import\b", re.I)
_RETRY = re.compile(r"\bretry\b.*\b(" + _IMPORT_ALT + r"|failed\s+files?)\b", re.I)
_RESUME = re.compile(r"\b(resume|continue)\b.*\b" + _IMPORT_WORD + r"\b", re.I)
_CANCEL = re.compile(r"\b(cancel|stop|abort)\b.*\b" + _IMPORT_WORD + r"\b", re.I)
_APPROVE = re.compile(r"\bapprove\b.*\b(" + _IMPORT_ALT + r"|drafts?)\b", re.I)
_NOT_RECURSIVE = re.compile(r"\b(not\s+recursive(?:ly)?|non[-\s]?recursive|top[-\s]?level\s+only|without\s+subfolders|no\s+subfolders|skip\s+subfolders)\b", re.I)
_BATCH = re.compile(r"\bbatch(?:\s+size)?\s*(?:of|=|:)?\s*(\d{1,3})\b", re.I)


def _response(conv: Any, text: str, language: str, *, pending_id: Optional[str] = None, tool: str = "drive_folder_ingestion",
              extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from app.services.response_refurbisher import refurbish_response
    polished_text = refurbish_response(text, role="admin", language=language, context={"tool": tool})
    out = {
        "type": "action_proposal" if pending_id else "text",
        "text": polished_text,
        "session_id": getattr(conv, "session_id", None),
        "conversation_id": str(getattr(conv, "id", "")),
        "requires_confirmation": bool(pending_id),
        "pending_action_id": pending_id,
        "action_status": "proposed" if pending_id else None,
        "tools_used": [{"name": tool}],
        "download_link": None,
        "language": language,
    }
    if extra:
        out.update(extra)
    return out


def _remember_job(db: Any, conv: Any, job_id: str) -> None:
    prefs = dict(getattr(conv, "preferences", None) or {})
    prefs[PREF_KEY] = job_id
    try:
        conv.preferences = prefs
        if db is not None:
            db.commit()
    except Exception:
        pass


def _active_job_id(conv: Any) -> Optional[str]:
    return (getattr(conv, "preferences", None) or {}).get(PREF_KEY)


def _say(db: Any, conv: Any, message: str, text: str) -> None:
    from app.services.conversation_service import append_conversation_turn
    append_conversation_turn(db, conv, message or "", text)


def try_handle_drive_ingest_turn(db: Any, conv: Any, message: Optional[str], staff: Any, language: str) -> Optional[Dict[str, Any]]:
    """Handle a Drive folder import turn, or return None so the normal admin chat continues."""
    text_in = (message or "").strip()
    if not text_in:
        return None
    urls = ingest.find_folder_urls(text_in)
    job_id = _active_job_id(conv)
    if not urls and not job_id:
        return None
    command = None
    if not urls:
        for name, rx in (("approve", _APPROVE), ("retry", _RETRY), ("resume", _RESUME), ("cancel", _CANCEL),
                         ("drafts", _DRAFTS), ("status", _STATUS)):
            if rx.search(text_in):
                command = name
                break
        if command is None:
            return None

    try:
        ingest.require_ingest_role(staff)
    except ingest.IngestionError as exc:
        _say(db, conv, text_in, str(exc))
        return _response(conv, str(exc), language)

    try:
        if urls:
            m = _BATCH.search(text_in)
            job = ingest.create_job(
                db, staff, urls[0],
                recursive=False if _NOT_RECURSIVE.search(text_in) else None,
                batch_size=int(m.group(1)) if m else None,
                conversation_id=str(getattr(conv, "id", "")),
            )
            _remember_job(db, conv, job["job_id"])
            reply = ingest.format_job_started(job)
            if len(urls) > 1:
                reply += "\n\nOnly the first folder link was imported; send the others one at a time."
            _say(db, conv, text_in, reply)
            return _response(conv, reply, language, extra={"drive_ingestion_job": ingest.job_view(job)})

        job = ingest.load_job(job_id, staff)
        if command == "status":
            reply = ingest.format_progress(job)
        elif command == "drafts":
            reply = ingest.format_drafts(job)
        elif command == "retry":
            res = ingest.retry_job(db, staff, job_id, include_skipped_ocr=bool(re.search(r"\bocr\b", text_in, re.I)))
            reply = (f"Re-queued {len(res['requeued'])} failed file(s); completed files are not reprocessed."
                     if res["requeued"] else "There are no failed files to retry.")
            if res["attempts_exhausted"]:
                reply += f" {len(res['attempts_exhausted'])} file(s) reached the retry limit: " + ", ".join(res["attempts_exhausted"][:10])
        elif command == "resume":
            reply = ingest.resume_job(db, staff, job_id)["message"]
        elif command == "cancel":
            ingest.cancel_job(db, staff, job_id)
            reply = (f"Import job `{job_id}` was cancelled. Drafts already extracted stay available for review; "
                     "nothing was written to the database.")
        else:  # approve
            return _stage_approval(db, conv, text_in, staff, job, language)
        _say(db, conv, text_in, reply)
        return _response(conv, reply, language)
    except ingest.IngestionError as exc:
        _say(db, conv, text_in, str(exc))
        return _response(conv, str(exc), language)


def _stage_approval(db: Any, conv: Any, message: str, staff: Any, job: Dict[str, Any], language: str) -> Dict[str, Any]:
    from fastapi import HTTPException
    from app.services.action_service import create_pending_action

    if job.get("status") in ingest.ACTIVE_JOB_STATUSES:
        reply = "The import is still processing. Approve after it finishes (ask **import status**)."
        _say(db, conv, message, reply)
        return _response(conv, reply, language)
    ingest.consolidate_job(job["job_id"], db)
    ids = ingest.ready_draft_ids(job["job_id"])
    store = ingest.get_store()
    if not ids:
        review = [d for d in store.drafts(job["job_id"]) if (d.get("action_override") or d.get("proposed_action")) == ingest.A_REVIEW
                  and d.get("review_status") == ingest.R_PENDING]
        reply = ("No drafts are ready to save."
                 + (f" {len(review)} need manual review first (open the review screen or "
                    f"`GET /api/v1/admin/drive-ingestion/jobs/{job['job_id']}/drafts`)." if review else ""))
        _say(db, conv, message, reply)
        return _response(conv, reply, language)
    summary: List[Dict[str, Any]] = []
    for draft_id in ids:
        d = store.get_draft(draft_id) or {}
        summary.append({"draft_id": draft_id, "property_id": d.get("property_id"),
                        "name": ((d.get("provenance") or {}).get("property_name") or {}).get("value"),
                        "action": d.get("action_override") or d.get("proposed_action"),
                        "source": d.get("source_label")})
    try:
        action = create_pending_action(
            db=db,
            staff_user_id=getattr(staff, "email", None) or "admin",
            tool_name=ingest.APPROVE_TOOL,
            action_payload={"job_id": job["job_id"], "draft_ids": ids, "count": len(ids), "items": summary[:200],
                            "websites": job.get("websites") or []},
            conversation_id=str(getattr(conv, "id", "")),
        )
    except HTTPException as exc:
        reply = f"Could not stage the approval: {exc.detail}"
        _say(db, conv, message, reply)
        return _response(conv, reply, language)
    creates = sum(1 for s in summary if s["action"] == ingest.A_CREATE)
    updates = len(summary) - creates
    lines = [f"**Ready to save {len(ids)} listing(s)** from `{job.get('folder_name')}` "
             f"({creates} new, {updates} update(s)) to: {', '.join(job.get('websites') or []) or 'your selected websites'}", ""]
    for s in summary[:20]:
        lines.append(f"- {s['action']}: **{s['name'] or s['property_id']}** (`{s['property_id']}`) — {s['source']}")
    if len(summary) > 20:
        lines.append(f"- … and {len(summary) - 20} more")
    lines.append("")
    lines.append("Each listing is written to Firestore with a 6-day undo snapshot and an audit log entry. "
                 f"⚠️ **Awaiting confirmation** (ID: `{action.id}`). Reply **confirm** to save, or **cancel**.")
    reply = "\n".join(lines)
    _say(db, conv, message, reply)
    return _response(conv, reply, language, pending_id=str(action.id), tool=ingest.APPROVE_TOOL)


def format_approval_result(result: Dict[str, Any]) -> str:
    saved = [r for r in result.get("results") or [] if r.get("saved")]
    failed = [r for r in result.get("results") or [] if not r.get("saved")]
    lines = [f"✅ **Import approved: {len(saved)} listing(s) saved**" + (f", {len(failed)} not saved" if failed else "") + "."]
    for r in saved[:20]:
        lines.append(f"- {r.get('action')}: `{r.get('property_id')}` — undo operation `{r.get('operation_id')}`"
                     + (" (already saved earlier)" if r.get("already_approved") else ""))
    for r in failed[:20]:
        lines.append(f"- ⚠️ `{r.get('draft_id')}`: {r.get('message')}")
    if saved:
        lines.append("\nEach save can be undone for 6 days with its operation id, or roll back the whole import with "
                     f"`POST /api/v1/admin/drive-ingestion/jobs/{result.get('job_id')}/rollback`.")
    return "\n".join(lines)
