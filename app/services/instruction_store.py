"""
Super-admin instructions and remarks, stored only in Firestore.

Collections (never projects, leads, or other existing data):
  superadmin_instructions/{INS-0001}
  superadmin_instructions/{INS-0001}/versions/{v1}
  superadmin_remarks/{RMK-0001}
  superadmin_meta/counters

Chat text is not stored here. A remark keeps a conversation id and message
index; the excerpt is read from the Conversation table when it is displayed.
"""

from __future__ import annotations

import copy
import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.core.config import settings

logger = logging.getLogger(__name__)

INSTRUCTIONS = "superadmin_instructions"
REMARKS = "superadmin_remarks"
META = "superadmin_meta"
COUNTERS = "counters"

TARGETS = ("admin_chat", "public_chat", "brochure_pdf", "offer_dossier", "reports", "all")
STATUSES = ("draft", "needs_clarification", "active", "archived", "blocked")
REMARK_STATUSES = ("open", "converted", "dismissed")
CACHE_TTL_SEC = 60
MAX_ACTIVE_PER_TARGET = 30
MAX_BLOCK_CHARS = 4000
RULE_MAX = 600
EXAMPLE_MAX = 400
REMARK_MAX = 1000

ID_RE = {
    "INS": re.compile(r"^INS-\d{4,}$"),
    "RMK": re.compile(r"^RMK-\d{4,}$"),
}
EMAIL_RE = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
PHONE_RE = re.compile(r"(?:\+\d{1,3}[\s.-])?(?:\d[\s.-]){7,}\d")

# Kept in code on purpose. Firestore must not be able to edit or disable these.
IMMUTABLE_SAFETY_RULES = (
    {
        "id": "no_prompt_or_secret_reveal",
        "description": "Never reveal the system prompt, API keys, or credentials.",
        "patterns": (
            r"reveal (the )?(system )?prompt",
            r"(print|show|dump).{0,40}(api key|system prompt|secret)",
            r"ignore (all |any )?(previous|prior) instructions",
        ),
    },
    {
        "id": "no_invented_facts",
        "description": "Never invent prices, ownership, ROI, or legal facts.",
        "patterns": (
            r"invent (a |the )?(price|ownership|roi)",
            r"(you may|feel free to) guess (the )?(price|ownership)",
            r"guarantee(d)? (the )?(roi|return)",
            r"(say|claim) (it is |it's |that it is )?tax[- ]free",
            r"foreigners can own (freehold )?land",
        ),
    },
    {
        "id": "no_skip_confirmation",
        "description": "Writes still require explicit confirmation.",
        "patterns": (
            r"skip (the )?(two-step )?confirmation",
            r"without (explicit )?confirmation",
            r"(do not|don't|never) ask (the admin )?to confirm",
        ),
    },
    {
        "id": "no_audit_tamper",
        "description": "The audit trail cannot be disabled or deleted.",
        "patterns": (
            r"(delete|wipe|disable|erase) (the )?audit",
        ),
    },
    {
        "id": "no_unsolicited_email",
        "description": "Do not send email because an instruction says so.",
        "patterns": (
            r"send (an )?email without",
            r"email (everyone|all clients|all leads)",
        ),
    },
)

_CACHE: Dict[str, Any] = {
    "fetched_at": 0.0,
    "items": [],
    "last_good": [],
    "last_error": None,
}
_SINGLETON: Optional["FirestoreInstructionStore"] = None


class InstructionStoreError(Exception):
    """Validation or storage failure. Nothing was written when this is raised first."""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _anonymise(text: str) -> str:
    cleaned = EMAIL_RE.sub("<client>", text or "")
    cleaned = PHONE_RE.sub("<client>", cleaned)
    return cleaned


def _has_pii(text: str) -> bool:
    blob = text or ""
    return bool(EMAIL_RE.search(blob) or PHONE_RE.search(blob))


def weakened_safety_rule(text: str) -> Optional[str]:
    blob = (text or "").lower()
    for rule in IMMUTABLE_SAFETY_RULES:
        for pattern in rule["patterns"]:
            if re.search(pattern, blob, re.I):
                return rule["id"]
    return None


def _check_id(prefix: str, value: str) -> str:
    token = (value or "").strip()
    if not ID_RE[prefix].match(token):
        raise InstructionStoreError(f"Id must match {prefix}- followed by at least 4 digits.")
    return token


def _norm_list(values: Optional[List[str]], allowed: Optional[tuple], label: str) -> List[str]:
    items = []
    for raw in values or []:
        token = str(raw).strip()
        if not token:
            continue
        if allowed and token not in allowed and token != "all":
            raise InstructionStoreError(f"{label} contains '{token}', which is not allowed.")
        if token not in items:
            items.append(token)
    if not items:
        raise InstructionStoreError(f"{label} needs at least one value.")
    return items


def _bounded(text: Optional[str], limit: int, label: str, anonymise: bool = False) -> str:
    value = str(text or "")
    if anonymise:
        value = _anonymise(value)
    if len(value) > limit:
        raise InstructionStoreError(f"{label} is longer than {limit} characters.")
    if _has_pii(value):
        raise InstructionStoreError(f"{label} still contains an email or phone number.")
    return value


class FirestoreInstructionStore:
    def __init__(self, client: Any = None):
        self._client = client

    def bind_client(self, client: Any) -> None:
        self._client = client
        self.invalidate_cache()

    def _db(self):
        if self._client is not None:
            return self._client
        from app.core.config import is_test_environment

        if is_test_environment():
            raise InstructionStoreError("Instruction Firestore is not configured for this test.")
        target = (getattr(settings, "INSTRUCTIONS_FIRESTORE_TARGET", None) or "user").strip() or "user"
        from app.services.firestore_service import get_firestore_client

        client = get_firestore_client(target=target)
        if client is None:
            raise InstructionStoreError("Firestore client is unavailable.")
        return client

    def invalidate_cache(self) -> None:
        _CACHE["fetched_at"] = 0.0

    def _run_transaction(self, work):
        client = self._db()
        if hasattr(client, "run_transaction"):
            return client.run_transaction(work)
        from google.cloud import firestore as gfs

        transaction = client.transaction()

        @gfs.transactional
        def _wrapped(transaction):
            return work(_GoogleTxn(transaction))

        return _wrapped(transaction)

    def _allocate(self, prefix: str) -> str:
        ref = self._db().collection(META).document(COUNTERS)

        def work(txn):
            snap = txn.get(ref)
            data = snap.to_dict() if getattr(snap, "exists", False) else {}
            data = data or {}
            number = int(data.get(prefix) or 0) + 1
            txn.set(ref, {prefix: number}, merge=True)
            return f"{prefix}-{number:04d}"

        return self._run_transaction(work)

    def _bump_counter_at_least(self, txn, prefix: str, number: int) -> None:
        ref = self._db().collection(META).document(COUNTERS)
        snap = txn.get(ref)
        data = snap.to_dict() if getattr(snap, "exists", False) else {}
        current = int((data or {}).get(prefix) or 0)
        if number > current:
            txn.set(ref, {prefix: number}, merge=True)

    def _write_version(self, batch_or_txn, doc: Dict[str, Any]) -> None:
        version = int(doc.get("version") or 1)
        ref = (
            self._db()
            .collection(INSTRUCTIONS)
            .document(doc["id"])
            .collection("versions")
            .document(f"v{version}")
        )
        batch_or_txn.set(ref, copy.deepcopy(doc))

    def _save_instruction(self, batch_or_txn, doc: Dict[str, Any]) -> None:
        ref = self._db().collection(INSTRUCTIONS).document(doc["id"])
        batch_or_txn.set(ref, copy.deepcopy(doc))

    def _save_remark(self, batch_or_txn, doc: Dict[str, Any]) -> None:
        ref = self._db().collection(REMARKS).document(doc["id"])
        batch_or_txn.set(ref, copy.deepcopy(doc))

    def _commit(self, apply) -> None:
        """apply(txn) queues sets. One transaction, or nothing is written."""
        def work(txn):
            apply(txn)
            return True

        self._run_transaction(work)
        self.invalidate_cache()

    def _instruction_from(self, payload: Dict[str, Any], actor: str, existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        title = _bounded(payload.get("title") or (existing or {}).get("title"), 160, "title")
        if not title.strip():
            raise InstructionStoreError("title is required.")
        rule = _bounded(payload.get("rule") if "rule" in payload else (existing or {}).get("rule"), RULE_MAX, "rule")
        if not rule.strip():
            raise InstructionStoreError("rule is required.")
        good = _bounded(
            payload.get("good_example") if "good_example" in payload else (existing or {}).get("good_example"),
            EXAMPLE_MAX,
            "good_example",
            anonymise=True,
        )
        avoid = _bounded(
            payload.get("avoid_example") if "avoid_example" in payload else (existing or {}).get("avoid_example"),
            EXAMPLE_MAX,
            "avoid_example",
            anonymise=True,
        )
        safety_blob = "\n".join([title, rule, good, avoid])
        weakened = weakened_safety_rule(safety_blob)
        if weakened:
            raise InstructionStoreError(
                f"This instruction would weaken immutable safety rule '{weakened}'. It was not saved."
            )
        targets = _norm_list(
            payload.get("targets") if "targets" in payload else (existing or {}).get("targets"),
            TARGETS,
            "targets",
        )
        websites = [item.lower() for item in _norm_list(
            payload.get("websites") if "websites" in payload else (existing or {}).get("websites") or ["all"],
            None,
            "websites",
        )]
        languages = [item.lower() for item in _norm_list(
            payload.get("languages") if "languages" in payload else (existing or {}).get("languages") or ["all"],
            None,
            "languages",
        )]
        priority = payload.get("priority") if "priority" in payload else (existing or {}).get("priority", 50)
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise InstructionStoreError("priority must be an integer from 1 to 100.")
        if priority < 1 or priority > 100:
            raise InstructionStoreError("priority must be an integer from 1 to 100.")
        status = str(payload.get("status") or (existing or {}).get("status") or "draft")
        if status not in STATUSES:
            raise InstructionStoreError(f"status must be one of {', '.join(STATUSES)}.")
        now = _now()
        base = dict(existing or {})
        base.update({
            "title": title.strip(),
            "rule": rule.strip(),
            "good_example": good.strip(),
            "avoid_example": avoid.strip(),
            "targets": targets,
            "websites": websites,
            "languages": languages,
            "priority": priority,
            "status": status,
            "supersedes": list(payload.get("supersedes") or base.get("supersedes") or []),
            "source_remarks": list(payload.get("source_remarks") or base.get("source_remarks") or []),
            "conflict_report": payload.get("conflict_report") if "conflict_report" in payload else base.get("conflict_report"),
            "updated_by": actor,
            "updated_at": now,
        })
        if not existing:
            base["created_by"] = actor
            base["created_at"] = now
            base["version"] = 1
            base["activated_by"] = None
            base["activated_at"] = None
            base["status"] = status if status in {"draft", "needs_clarification", "blocked"} else "draft"
        return base

    def create(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        doc = self._instruction_from(payload, actor, existing=None)
        doc["id"] = self._allocate("INS")
        doc["version"] = 1

        def apply(batch):
            self._save_instruction(batch, doc)
            self._write_version(batch, doc)

        self._commit(apply)
        return copy.deepcopy(doc)

    def get(self, instruction_id: str) -> Dict[str, Any]:
        token = _check_id("INS", instruction_id)
        snap = self._db().collection(INSTRUCTIONS).document(token).get()
        if not getattr(snap, "exists", False):
            raise InstructionStoreError(f"{token} was not found.")
        data = snap.to_dict() or {}
        data["id"] = token
        return data

    def list(self, status: Optional[str] = None, target: Optional[str] = None, website: Optional[str] = None) -> List[Dict[str, Any]]:
        found = []
        for snap in self._db().collection(INSTRUCTIONS).stream():
            data = snap.to_dict() or {}
            data["id"] = getattr(snap, "id", None) or data.get("id")
            if not data.get("id") or not str(data["id"]).startswith("INS-"):
                continue
            if status and data.get("status") != status:
                continue
            targets = data.get("targets") or []
            if target and target not in targets and "all" not in targets:
                continue
            sites = [str(item).lower() for item in (data.get("websites") or [])]
            if website and "all" not in sites and website.lower() not in sites:
                continue
            found.append(data)
        found.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("updated_at") or "")), reverse=True)
        return found

    def update(self, instruction_id: str, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        current = self.get(instruction_id)
        nxt = self._instruction_from(payload, actor, existing=current)
        nxt["id"] = current["id"]
        nxt["version"] = int(current.get("version") or 1) + 1
        nxt["created_by"] = current.get("created_by")
        nxt["created_at"] = current.get("created_at")
        nxt["status"] = payload.get("status") or current.get("status") or "draft"
        if nxt["status"] not in STATUSES:
            raise InstructionStoreError("status is not valid.")
        if nxt["status"] == "active":
            nxt["status"] = current.get("status") or "draft"

        def apply(batch):
            self._write_version(batch, current)
            self._save_instruction(batch, nxt)

        self._commit(apply)
        return copy.deepcopy(nxt)

    def _overlaps(self, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        def hit(left_values, right_values) -> bool:
            left_set = {str(item).lower() for item in (left_values or [])}
            right_set = {str(item).lower() for item in (right_values or [])}
            if "all" in left_set or "all" in right_set:
                return True
            return not left_set.isdisjoint(right_set)

        return (
            hit(left.get("targets"), right.get("targets"))
            and hit(left.get("websites"), right.get("websites"))
            and hit(left.get("languages"), right.get("languages"))
        )

    def _conflict_with(self, doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        negative = bool(re.search(r"\b(never|do not|don't|must not|avoid|omit)\b", doc.get("rule") or "", re.I))
        positive = bool(re.search(r"\b(always|must|include|require)\b", doc.get("rule") or "", re.I))
        tokens = set(re.findall(r"[a-z]{5,}", (doc.get("rule") or "").lower()))
        against = []
        for other in self.list(status="active"):
            if other.get("id") == doc.get("id"):
                continue
            if not self._overlaps(doc, other):
                continue
            other_negative = bool(re.search(r"\b(never|do not|don't|must not|avoid|omit)\b", other.get("rule") or "", re.I))
            other_positive = bool(re.search(r"\b(always|must|include|require)\b", other.get("rule") or "", re.I))
            shared = tokens & set(re.findall(r"[a-z]{5,}", (other.get("rule") or "").lower()))
            if len(shared) >= 2 and ((negative and other_positive) or (positive and other_negative)):
                against.append(other["id"])
        if not against:
            return None
        return {
            "against": against,
            "reason": "Opposes an active instruction on the same target.",
            "detected_at": _now(),
        }

    def activate(self, instruction_id: str, actor: str) -> Dict[str, Any]:
        current = self.get(instruction_id)
        nxt = copy.deepcopy(current)
        nxt["version"] = int(current.get("version") or 1) + 1
        nxt["updated_by"] = actor
        nxt["updated_at"] = _now()
        conflict = self._conflict_with(current)
        remark_ids = list(current.get("source_remarks") or [])
        if conflict:
            nxt["status"] = "needs_clarification"
            nxt["conflict_report"] = conflict
            remark_ids = []
        else:
            nxt["status"] = "active"
            nxt["conflict_report"] = None
            nxt["activated_by"] = actor
            nxt["activated_at"] = nxt["updated_at"]

        def apply(batch):
            self._write_version(batch, current)
            self._save_instruction(batch, nxt)
            for remark_id in remark_ids:
                remark = self._remark_or_none(remark_id)
                if not remark:
                    continue
                remark["status"] = "converted"
                remark["instruction_id"] = nxt["id"]
                remark["updated_at"] = nxt["updated_at"]
                self._save_remark(batch, remark)

        self._commit(apply)
        return copy.deepcopy(nxt)

    def archive(self, instruction_id: str, actor: str) -> Dict[str, Any]:
        current = self.get(instruction_id)
        nxt = copy.deepcopy(current)
        nxt["version"] = int(current.get("version") or 1) + 1
        nxt["status"] = "archived"
        nxt["updated_by"] = actor
        nxt["updated_at"] = _now()

        def apply(batch):
            self._write_version(batch, current)
            self._save_instruction(batch, nxt)

        self._commit(apply)
        return copy.deepcopy(nxt)

    def rollback(self, instruction_id: str, version: int, actor: str) -> Dict[str, Any]:
        current = self.get(instruction_id)
        version = int(version)
        snap = (
            self._db()
            .collection(INSTRUCTIONS)
            .document(current["id"])
            .collection("versions")
            .document(f"v{version}")
            .get()
        )
        if not getattr(snap, "exists", False):
            raise InstructionStoreError(f"Version v{version} was not found for {current['id']}.")
        restored = snap.to_dict() or {}
        nxt = copy.deepcopy(restored)
        nxt["id"] = current["id"]
        nxt["version"] = int(current.get("version") or 1) + 1
        nxt["updated_by"] = actor
        nxt["updated_at"] = _now()
        nxt["created_by"] = current.get("created_by")
        nxt["created_at"] = current.get("created_at")
        if nxt.get("status") == "active":
            conflict = self._conflict_with(nxt)
            if conflict:
                nxt["status"] = "needs_clarification"
                nxt["conflict_report"] = conflict

        def apply(batch):
            self._write_version(batch, current)
            self._save_instruction(batch, nxt)

        self._commit(apply)
        return copy.deepcopy(nxt)

    def _remark_or_none(self, remark_id: str) -> Optional[Dict[str, Any]]:
        try:
            token = _check_id("RMK", remark_id)
        except InstructionStoreError:
            return None
        snap = self._db().collection(REMARKS).document(token).get()
        if not getattr(snap, "exists", False):
            return None
        data = snap.to_dict() or {}
        data["id"] = token
        return data

    def add_remark(self, payload: Dict[str, Any], actor: str) -> Dict[str, Any]:
        remark_text = _bounded(payload.get("remark_text"), REMARK_MAX, "remark_text")
        expected = _bounded(payload.get("expected_behavior"), REMARK_MAX, "expected_behavior")
        if not remark_text.strip():
            raise InstructionStoreError("remark_text is required.")
        forbidden = ("chat_text", "excerpt", "message", "customer_name", "email", "phone", "contact_email", "contact_phone")
        for key in forbidden:
            if payload.get(key):
                raise InstructionStoreError("A remark cannot store chat text or contact details.")
        severity = str(payload.get("severity") or "normal")
        targets = _norm_list(payload.get("targets") or ["admin_chat"], TARGETS, "targets")
        doc = {
            "conversation_id": str(payload.get("conversation_id") or ""),
            "message_index": int(payload.get("message_index") or 0),
            "reviewer_email": actor,
            "remark_text": remark_text.strip(),
            "expected_behavior": expected.strip(),
            "severity": severity,
            "targets": targets,
            "status": "open",
            "instruction_id": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        doc["id"] = self._allocate("RMK")

        def apply(batch):
            self._save_remark(batch, doc)

        self._commit(apply)
        return copy.deepcopy(doc)

    def list_remarks(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        found = []
        for snap in self._db().collection(REMARKS).stream():
            data = snap.to_dict() or {}
            data["id"] = getattr(snap, "id", None) or data.get("id")
            if not data.get("id") or not str(data["id"]).startswith("RMK-"):
                continue
            if status and data.get("status") != status:
                continue
            found.append(data)
        found.sort(key=lambda item: str(item.get("created_at") or ""))
        return found

    def convert_remark(self, remark_id: str, actor: str, instruction_fields: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        remark = self._remark_or_none(remark_id)
        if not remark:
            raise InstructionStoreError(f"{remark_id} was not found.")
        fields = dict(instruction_fields or {})
        fields.setdefault("title", (remark.get("remark_text") or "Remark")[:160])
        fields.setdefault("rule", remark.get("expected_behavior") or remark.get("remark_text"))
        fields.setdefault("targets", remark.get("targets") or ["admin_chat"])
        fields.setdefault("source_remarks", [remark["id"]])
        instruction = self._instruction_from(fields, actor, existing=None)
        instruction["id"] = self._allocate("INS")
        instruction["version"] = 1
        instruction["source_remarks"] = [remark["id"]]
        remark = copy.deepcopy(remark)
        remark["status"] = "converted"
        remark["instruction_id"] = instruction["id"]
        remark["updated_at"] = instruction["updated_at"]

        def apply(batch):
            self._save_instruction(batch, instruction)
            self._write_version(batch, instruction)
            self._save_remark(batch, remark)

        self._commit(apply)
        return {"instruction": copy.deepcopy(instruction), "remark": copy.deepcopy(remark)}

    def dismiss_remark(self, remark_id: str, actor: str) -> Dict[str, Any]:
        remark = self._remark_or_none(remark_id)
        if not remark:
            raise InstructionStoreError(f"{remark_id} was not found.")
        remark = copy.deepcopy(remark)
        remark["status"] = "dismissed"
        remark["updated_at"] = _now()
        remark["updated_by"] = actor

        def apply(batch):
            self._save_remark(batch, remark)

        self._commit(apply)
        return remark

    def export_bundle(self) -> Dict[str, Any]:
        return {"instructions": self.list()}

    def export_zip(self) -> bytes:
        bundle = self.export_bundle()
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("instructions.json", json.dumps(bundle, ensure_ascii=False, indent=2))
            for doc in bundle["instructions"]:
                archive.writestr(f"{doc['id']}.md", render_instruction_markdown(doc))
        return buffer.getvalue()

    def import_bundle(self, payload: Dict[str, Any], actor: str) -> List[Dict[str, Any]]:
        records = payload.get("instructions") if isinstance(payload, dict) else None
        if not isinstance(records, list) or not records:
            raise InstructionStoreError("import JSON must contain an instructions list.")
        prepared = []
        seen = set()
        for raw in records:
            if not isinstance(raw, dict):
                raise InstructionStoreError("Each instruction must be an object.")
            token = _check_id("INS", str(raw.get("id") or ""))
            if token in seen:
                raise InstructionStoreError(f"Duplicate id {token} in the import.")
            seen.add(token)
            content = "\n".join(str(raw.get(key) or "") for key in ("title", "rule", "good_example", "avoid_example"))
            if _has_pii(content):
                raise InstructionStoreError(f"{token} contains an email or phone number. Nothing was imported.")
            doc = self._instruction_from(raw, actor, existing=None)
            doc["id"] = token
            doc["version"] = int(raw.get("version") or 1)
            doc["status"] = raw.get("status") or "draft"
            if doc["status"] not in STATUSES:
                raise InstructionStoreError(f"{token} has a status that is not allowed.")
            doc["created_by"] = raw.get("created_by") or actor
            doc["created_at"] = raw.get("created_at") or doc["created_at"]
            prepared.append(doc)
        for left in prepared:
            for right in prepared:
                if left["id"] >= right["id"]:
                    continue
                if left.get("status") == "active" and right.get("status") == "active":
                    if self._would_conflict_pair(left, right):
                        raise InstructionStoreError(
                            f"{left['id']} conflicts with {right['id']}. Nothing was imported."
                        )
        highest = {}
        for doc in prepared:
            prefix = doc["id"].split("-", 1)[0]
            highest[prefix] = max(highest.get(prefix, 0), int(doc["id"].split("-", 1)[1]))

        def apply(batch):
            for doc in prepared:
                current = self._existing_instruction(doc["id"])
                if current:
                    self._write_version(batch, current)
                    doc["version"] = int(current.get("version") or 1) + 1
                self._save_instruction(batch, doc)
                self._write_version(batch, doc)
            for prefix, number in highest.items():
                self._bump_counter_at_least(batch, prefix, number)

        self._commit(apply)
        return [copy.deepcopy(doc) for doc in prepared]

    def _existing_instruction(self, instruction_id: str) -> Optional[Dict[str, Any]]:
        snap = self._db().collection(INSTRUCTIONS).document(instruction_id).get()
        if not getattr(snap, "exists", False):
            return None
        data = snap.to_dict() or {}
        data["id"] = instruction_id
        return data

    def _would_conflict_pair(self, left: Dict[str, Any], right: Dict[str, Any]) -> bool:
        if not self._overlaps(left, right):
            return False
        def polarity(text: str):
            negative = bool(re.search(r"\b(never|do not|don't|must not|avoid|omit)\b", text or "", re.I))
            positive = bool(re.search(r"\b(always|must|include|require)\b", text or "", re.I))
            tokens = set(re.findall(r"[a-z]{5,}", (text or "").lower()))
            return negative, positive, tokens
        ln, lp, lt = polarity(left.get("rule") or "")
        rn, rp, rt = polarity(right.get("rule") or "")
        return len(lt & rt) >= 2 and ((ln and rp) or (lp and rn))

    def active_documents(self) -> List[Dict[str, Any]]:
        now = datetime.now(timezone.utc).timestamp()
        if _CACHE["fetched_at"] and (now - float(_CACHE["fetched_at"])) < CACHE_TTL_SEC:
            return list(_CACHE["items"])
        try:
            items = self.list(status="active")
        except InstructionStoreError as exc:
            if "not configured" in str(exc):
                return list(_CACHE["last_good"])
            _CACHE["last_error"] = str(exc)
            logger.warning("Instruction Firestore read failed; using the last good cache. %s", exc)
            return list(_CACHE["last_good"])
        except Exception as exc:
            _CACHE["last_error"] = str(exc)
            logger.warning("Instruction Firestore read failed; using the last good cache. %s", exc)
            return list(_CACHE["last_good"])
        _CACHE["items"] = items
        _CACHE["last_good"] = items
        _CACHE["fetched_at"] = now
        _CACHE["last_error"] = None
        return list(items)


class _GoogleTxn:
    def __init__(self, transaction):
        self.transaction = transaction

    def get(self, ref):
        return ref.get(transaction=self.transaction)

    def set(self, ref, data, merge: bool = False):
        self.transaction.set(ref, data, merge=merge)


def get_store() -> FirestoreInstructionStore:
    global _SINGLETON
    if _SINGLETON is None:
        _SINGLETON = FirestoreInstructionStore()
    return _SINGLETON


def reset_instruction_store_for_tests(client: Any = None) -> FirestoreInstructionStore:
    global _SINGLETON
    _SINGLETON = FirestoreInstructionStore(client=client)
    _CACHE["fetched_at"] = 0.0
    _CACHE["items"] = []
    _CACHE["last_good"] = []
    _CACHE["last_error"] = None
    return _SINGLETON


def get_active_instruction_block(
    target: str,
    website: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """
    Text injected into a prompt. Fail-open: a Firestore error returns the last
    good block, or an empty string, and never raises.
    """
    try:
        docs = get_store().active_documents()
    except Exception as exc:
        _CACHE["last_error"] = str(exc)
        logger.warning("Instruction block unavailable; continuing without it. %s", exc)
        docs = list(_CACHE["last_good"])
    chosen = []
    site = (website or "").lower()
    lang = (language or "").lower()
    for doc in docs:
        targets = doc.get("targets") or []
        if target not in targets and "all" not in targets:
            continue
        sites = [str(item).lower() for item in (doc.get("websites") or [])]
        if site and "all" not in sites and site not in sites:
            continue
        langs = [str(item).lower() for item in (doc.get("languages") or [])]
        if lang and "all" not in langs and lang not in langs:
            continue
        chosen.append(doc)
    chosen.sort(key=lambda item: (int(item.get("priority") or 0), str(item.get("updated_at") or "")), reverse=True)
    pieces = []
    total = 0
    for doc in chosen[:MAX_ACTIVE_PER_TARGET]:
        piece = _render_piece(doc)
        extra = len(piece) + (2 if pieces else 0)
        if pieces and total + extra > MAX_BLOCK_CHARS:
            break
        if not pieces and len(piece) > MAX_BLOCK_CHARS:
            pieces.append(piece[:MAX_BLOCK_CHARS])
            break
        pieces.append(piece)
        total += extra
    return "\n\n".join(pieces)


def _render_piece(doc: Dict[str, Any]) -> str:
    lines = [f"[{doc.get('id')}] {doc.get('title') or ''}".strip(), (doc.get("rule") or "").strip()]
    if doc.get("good_example"):
        lines.append("Good: " + str(doc["good_example"]).strip())
    if doc.get("avoid_example"):
        lines.append("Avoid: " + str(doc["avoid_example"]).strip())
    return "\n".join(line for line in lines if line)


def render_instruction_markdown(doc: Dict[str, Any]) -> str:
    return "\n".join([
        f"# {doc.get('id')} {doc.get('title') or ''}".strip(),
        "",
        f"- status: {doc.get('status')}",
        f"- targets: {', '.join(doc.get('targets') or [])}",
        f"- websites: {', '.join(doc.get('websites') or [])}",
        f"- languages: {', '.join(doc.get('languages') or [])}",
        f"- priority: {doc.get('priority')}",
        f"- version: {doc.get('version')}",
        "",
        "## Rule",
        doc.get("rule") or "",
        "",
        "## Good example",
        doc.get("good_example") or "",
        "",
        "## Avoid",
        doc.get("avoid_example") or "",
        "",
    ])


def instruction_config_status() -> Dict[str, Any]:
    counts = {name: 0 for name in TARGETS}
    for doc in _CACHE.get("last_good") or []:
        if doc.get("status") != "active":
            continue
        for target in doc.get("targets") or []:
            if target in counts:
                counts[target] += 1
    return {
        "instruction_store": "firestore",
        "firestore_target": (getattr(settings, "INSTRUCTIONS_FIRESTORE_TARGET", None) or "user"),
        "cache_ttl_seconds": CACHE_TTL_SEC,
        "active_count": counts,
        "last_error": _CACHE.get("last_error"),
    }


def lookup_chat_excerpt(db: Any, conversation_id: Optional[str], message_index: int) -> Optional[Dict[str, Any]]:
    """Read-only. The excerpt is never written back to Firestore."""
    if db is None or not conversation_id:
        return None
    from app.db.models import Conversation

    conv = None
    try:
        conv = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    except Exception:
        conv = None
    if conv is None:
        try:
            for row in db.query(Conversation).all():
                if str(row.id) == str(conversation_id):
                    conv = row
                    break
        except Exception:
            return None
    if conv is None:
        return None
    history = list(getattr(conv, "message_history", None) or [])
    if message_index < 0 or message_index >= len(history):
        return None
    turn = history[message_index]
    if isinstance(turn, dict):
        return {"role": turn.get("role"), "content": turn.get("content")}
    return {"role": None, "content": str(turn)}


def review_conversation(db: Any, conversation_id: str) -> Dict[str, Any]:
    """Read-only view of Conversation, PendingAction and AuditLog. Writes nothing."""
    from app.db.models import AuditLog, Conversation, PendingAction

    conv = None
    for row in db.query(Conversation).all():
        if str(row.id) == str(conversation_id):
            conv = row
            break
    if conv is None:
        raise InstructionStoreError("Conversation was not found.")
    pending = []
    for row in db.query(PendingAction).all():
        if str(row.conversation_id or "") == str(conversation_id):
            pending.append({
                "id": str(row.id),
                "tool_name": row.tool_name,
                "status": row.status,
            })
    audits = []
    for row in db.query(AuditLog).all():
        if str(row.conversation_id or "") == str(conversation_id):
            audits.append({
                "id": str(row.id),
                "tool_name": row.tool_name,
                "status": row.status,
            })
    return {
        "conversation_id": str(conv.id),
        "messages": list(conv.message_history or []),
        "pending_actions": pending,
        "audit_logs": audits,
    }
