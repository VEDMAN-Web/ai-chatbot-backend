"""
Admin chat commands that add, replace, cover, or remove property images.

They only propose a pending action. Confirm (button or "yes confirm") writes
Firestore, and undo restores the previous image list.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_IMAGE_EXT = re.compile(r"\.(?:jpg|jpeg|png|webp)(?:[?#].*)?$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s,\"'<>]+", re.IGNORECASE)
_IMAGE_LINK = re.compile(r"https?://\S+\.(?:jpe?g|png|webp)(?:[?#]\S*)?", re.IGNORECASE)
_IMAGE_WORD = re.compile(
    r"\b(images?|photos?|pictures?|cover|zdj[eę]ci[ae]|foto|obraz(?:ek|y)?)\b",
    re.IGNORECASE,
)
_IMAGE_VERB = re.compile(
    r"\b(add|change|replace|remove|set|make|dodaj|zmie[nń]|usu[nń]|ustaw)\b",
    re.IGNORECASE,
)
_PL_ADD = re.compile(
    r"""(?ix)
    ^(?:prosze\s+|proszę\s+)?dodaj\s+(?:zdjecie|zdjęcie|zdjecia|zdjęcia|foto|obraz)
    \s+(?P<urls>.+?)\s+do\s+["']?(?P<name>.+?)["']?\s*$
    """
)
_PL_REPLACE = re.compile(
    r"""(?ix)
    ^(?:prosze\s+|proszę\s+)?(?:zmien|zmień)\s+(?:zdjecie|zdjęcie|zdjecia|zdjęcia)
    \s+["']?(?P<name>.+?)["']?\s+na\s+(?P<urls>.+?)\s*$
    """
)
_PL_REMOVE_ALL = re.compile(
    r"""(?ix)
    ^(?:prosze\s+|proszę\s+)?(?:usun|usuń)\s+wszystkie\s+(?:zdjecia|zdjęcia|zdjecie|zdjęcie)
    \s+(?:z\s+|ze\s+)?["']?(?P<name>.+?)["']?\s*$
    """
)
_PENDING_KEY = "pending_property_images"

_ADD = re.compile(
    r"""(?ix)
    ^(?:please\s+)?add\s+(?:an?\s+)?(?:images?|photos?|pictures?)
    \s+(?P<urls>.+?)
    \s+to\s+(?:the\s+)?(?:property\s+)?["']?(?P<name>.+?)["']?\s*$
    """
)
_REPLACE = re.compile(
    r"""(?ix)
    ^(?:please\s+)?(?:change|replace)\s+(?:the\s+)?(?:images?|photos?|pictures?)
    \s+of\s+["']?(?P<name>.+?)["']?
    \s+to\s+(?P<urls>.+?)\s*$
    """
)
_COVER_URL = re.compile(
    r"""(?ix)
    ^(?:please\s+)?set\s+(?:the\s+)?cover\s+image\s+of\s+
    ["']?(?P<name>.+?)["']?\s+to\s+(?P<url>.+?)\s*$
    """
)
_COVER_INDEX = re.compile(
    r"""(?ix)
    ^(?:please\s+)?make\s+image\s+(?P<index>\d+)\s+the\s+cover\s+of\s+
    ["']?(?P<name>.+?)["']?\s*$
    """
)
_REMOVE_ALL = re.compile(
    r"""(?ix)
    ^(?:please\s+)?remove\s+all\s+(?:images?|photos?|pictures?)\s+
    (?:of|from)\s+["']?(?P<name>.+?)["']?\s*$
    """
)
_REMOVE = re.compile(
    r"""(?ix)
    ^(?:please\s+)?remove\s+(?:the\s+)?(?:image|photo|picture)\s+
    (?P<which>.+?)\s+from\s+(?:the\s+)?(?:property\s+)?
    ["']?(?P<name>.+?)["']?\s*$
    """
)


class ImageUrlError(Exception):
    """The admin should see this. Nothing was written."""


def looks_like_image_command(message: Optional[str]) -> bool:
    """True when the words, not a URL path, ask to change images."""
    text = message or ""
    bare = _URL_RE.sub(" ", text)
    if _IMAGE_WORD.search(bare) and _IMAGE_VERB.search(bare):
        return True
    # An image word plus an image URL is never a listing page to ingest.
    return bool(_IMAGE_WORD.search(bare) and _IMAGE_LINK.search(text))


def _clean_name(value: str) -> str:
    return (value or "").strip().strip("\"'").strip(" ?.!")


def _urls_in(blob: str) -> List[str]:
    found: List[str] = []
    for match in _URL_RE.findall(blob or ""):
        url = match.rstrip(".,);]")
        if url not in found:
            found.append(url)
    return found


def lookup_image_content_type(url: str) -> str:
    """HEAD the URL and return the content type. Redirects are not followed."""
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "TajlandiaAdmin/1.0"},
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=5) as response:
            raw = response.headers.get("Content-Type") or ""
            return str(raw).split(";")[0].strip().lower()
    except urllib.error.HTTPError as exc:
        if exc.code in {301, 302, 303, 307, 308}:
            return ""
        raw = exc.headers.get("Content-Type") if exc.headers else ""
        return str(raw or "").split(";")[0].strip().lower()
    except Exception:
        return ""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_image_url(url: str) -> str:
    """Accept http(s) image links that are not private or internal."""
    cleaned = (url or "").strip().strip("\"'").rstrip(".,);")
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ImageUrlError("Only http and https image links are allowed.")
    from app.services.property_web_ingest import IngestError, _resolve_ips

    try:
        _resolve_ips(parsed.hostname)
    except IngestError as exc:
        raise ImageUrlError(str(exc)) from exc
    if _IMAGE_EXT.search(parsed.path or ""):
        return cleaned
    content_type = lookup_image_content_type(cleaned)
    if content_type.startswith("image/"):
        return cleaned
    raise ImageUrlError(f"{cleaned} is not a jpg, png, or webp image.")


def parse_image_command(message: Optional[str]) -> Optional[Dict[str, Any]]:
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return None
    polish_remove = _PL_REMOVE_ALL.match(text)
    if polish_remove:
        return {"op": "remove_all", "name": _clean_name(polish_remove.group("name")), "urls": []}
    polish_replace = _PL_REPLACE.match(text)
    if polish_replace:
        return {
            "op": "replace",
            "name": _clean_name(polish_replace.group("name")),
            "urls": _urls_in(polish_replace.group("urls")),
        }
    polish_add = _PL_ADD.match(text)
    if polish_add:
        return {"op": "add", "name": _clean_name(polish_add.group("name")), "urls": _urls_in(polish_add.group("urls"))}
    remove_all = _REMOVE_ALL.match(text)
    if remove_all:
        return {"op": "remove_all", "name": _clean_name(remove_all.group("name")), "urls": []}
    cover_index = _COVER_INDEX.match(text)
    if cover_index:
        return {
            "op": "cover_index",
            "name": _clean_name(cover_index.group("name")),
            "index": int(cover_index.group("index")),
            "urls": [],
        }
    cover = _COVER_URL.match(text)
    if cover:
        return {"op": "cover", "name": _clean_name(cover.group("name")), "urls": _urls_in(cover.group("url"))}
    replace = _REPLACE.match(text)
    if replace:
        return {"op": "replace", "name": _clean_name(replace.group("name")), "urls": _urls_in(replace.group("urls"))}
    remove = _REMOVE.match(text)
    if remove:
        which = remove.group("which").strip().strip("\"'")
        if re.fullmatch(r"\d+", which):
            return {
                "op": "remove_index",
                "name": _clean_name(remove.group("name")),
                "index": int(which),
                "urls": [],
            }
        return {"op": "remove_url", "name": _clean_name(remove.group("name")), "urls": _urls_in(which)}
    add = _ADD.match(text)
    if add:
        return {"op": "add", "name": _clean_name(add.group("name")), "urls": _urls_in(add.group("urls"))}
    return None


def _count(number: int) -> str:
    return "1 image" if number == 1 else f"{number} images"


def _reply(
    conv: Any,
    text: str,
    *,
    requires: bool = False,
    pending_id: Optional[str] = None,
    token: Optional[str] = None,
    suggestions: Optional[List[str]] = None,
) -> Dict[str, Any]:
    body = {
        "type": "text",
        "text": text,
        "session_id": getattr(conv, "session_id", None),
        "conversation_id": str(getattr(conv, "id", "") or ""),
        "requires_confirmation": requires,
        "pending_action_id": pending_id,
        "confirmation_token": token,
        "action_status": "awaiting_confirmation" if requires else None,
        "tools_used": [{"name": "manage_property_images"}] if requires else [],
        "download_link": None,
        "language": "en",
    }
    if suggestions:
        body["suggestions"] = suggestions
        body["quick_replies"] = suggestions
    return body


def _checked_urls(urls: List[str]) -> Tuple[List[str], Optional[str]]:
    if not urls:
        return [], "I need an http or https image link. Nothing was changed."
    from app.services.admin_image_storage import is_owned_image_url

    checked: List[str] = []
    for url in urls:
        if is_owned_image_url(url):
            checked.append(url.strip())
            continue
        try:
            checked.append(validate_image_url(url))
        except ImageUrlError as exc:
            return [], f"{exc} Nothing was changed."
    return checked, None


def load_property_images(db: Any, property_id: str) -> List[Dict[str, Any]]:
    """Current images from the Firestore property document."""
    try:
        from app.services.firestore_service import get_property_by_id_from_firestore

        doc = get_property_by_id_from_firestore(property_id, target="admin") or {}
    except Exception:
        doc = {}
    if not isinstance(doc, dict):
        return []
    raw = doc.get("_raw") if isinstance(doc.get("_raw"), dict) else doc
    images = raw.get("images") or doc.get("images") or []
    cover = raw.get("coverImage") or raw.get("cover_image") or doc.get("cover_image")
    loaded = []
    for index, url in enumerate(images):
        if not url:
            continue
        loaded.append({
            "image_url": str(url).strip(),
            "image_type": "cover" if str(url).strip() == str(cover or "").strip() else "exterior",
            "sort_order": index,
        })
    return loaded


def _cover_url(items: List[Dict[str, Any]]) -> Optional[str]:
    for item in items:
        if item.get("image_type") == "cover":
            return item["image_url"]
    return items[0]["image_url"] if items else None


def _demote(items: List[Dict[str, Any]]) -> None:
    for item in items:
        if item.get("image_type") == "cover":
            item["image_type"] = "exterior"


def _one_cover(items: List[Dict[str, Any]]) -> None:
    covers = [item for item in items if item.get("image_type") == "cover"]
    if not items:
        return
    if not covers:
        items[0]["image_type"] = "cover"
        return
    seen = False
    for item in items:
        if item.get("image_type") != "cover":
            continue
        if seen:
            item["image_type"] = "exterior"
        seen = True


def plan_image_change(current: List[Dict[str, Any]], command: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str], str]:
    """Return (new list, error, preview sentence)."""
    op = command["op"]
    urls = list(command.get("urls") or [])
    items = [
        {"image_url": row["image_url"], "image_type": row.get("image_type") or "exterior", "sort_order": index}
        for index, row in enumerate(current)
    ]
    name = command.get("property_name") or command.get("name") or "this property"
    if op == "add":
        existing = {item["image_url"] for item in items}
        added = [url for url in urls if url not in existing]
        if not added:
            return None, f"Those images are already attached to {name}. Nothing was changed.", ""
        for url in added:
            items.append({"image_url": url, "image_type": "exterior", "sort_order": len(items)})
        _one_cover(items)
        return items, None, f"Add {_count(len(added))} to {name}."
    if op == "replace":
        items = [
            {"image_url": url, "image_type": "cover" if index == 0 else "exterior", "sort_order": index}
            for index, url in enumerate(urls)
        ]
        return items, None, f"Replace all {len(current)} images of {name} with {_count(len(urls))}."
    if op == "cover":
        url = urls[0]
        _demote(items)
        found = next((item for item in items if item["image_url"] == url), None)
        if found:
            found["image_type"] = "cover"
            sentence = f"Set the cover image of {name} to the image already attached. The previous cover becomes a normal image."
        else:
            items.insert(0, {"image_url": url, "image_type": "cover", "sort_order": 0})
            sentence = f"Set the cover image of {name} to 1 new image."
        for index, item in enumerate(items):
            item["sort_order"] = index
        return items, None, sentence
    if op == "cover_index":
        index = int(command.get("index") or 0)
        if index < 1 or index > len(items):
            return None, f"{name} has {_count(len(items))}. Image {index} is not in that list. Nothing was changed.", ""
        _demote(items)
        items[index - 1]["image_type"] = "cover"
        return items, None, f"Make image {index} the cover of {name}."
    if op == "remove_url":
        url = urls[0] if urls else ""
        kept = [item for item in items if item["image_url"] != url]
        if len(kept) == len(items):
            return None, f"That image is not attached to {name}. Nothing was changed.", ""
        _one_cover(kept)
        return kept, None, f"This will remove 1 image from {name}."
    if op == "remove_index":
        index = int(command.get("index") or 0)
        if index < 1 or index > len(items):
            return None, f"{name} has {_count(len(items))}. Image {index} is not in that list. Nothing was changed.", ""
        del items[index - 1]
        _one_cover(items)
        return items, None, f"This will remove 1 image from {name}."
    if op == "remove_all":
        if not items:
            return None, f"{name} has no images to remove. Nothing was changed.", ""
        return [], None, f"This will remove all {_count(len(items))} from {name}."
    return None, "I could not read that image request. Nothing was changed.", ""


def _accepted_attachments(urls: Optional[List[str]]) -> Tuple[Optional[List[str]], Optional[str]]:
    """Keep only URLs produced by our upload endpoint. A foreign URL rejects the field."""
    if not urls:
        return None, None
    if len(urls) > 10:
        return None, "Attach between 1 and 10 images. Nothing was changed."
    from app.services.admin_image_storage import is_owned_image_url

    cleaned = []
    for url in urls:
        if not is_owned_image_url(str(url or "")):
            return None, "I can only use images uploaded from this chat. Nothing was changed."
        if url not in cleaned:
            cleaned.append(str(url).strip())
    if not cleaned:
        return None, "I can only use images uploaded from this chat. Nothing was changed."
    return cleaned, None


def _load_pending(conv: Any) -> Optional[Dict[str, Any]]:
    prefs = getattr(conv, "preferences", None) or {}
    pending = prefs.get(_PENDING_KEY) if isinstance(prefs, dict) else None
    return pending if isinstance(pending, dict) and pending.get("urls") else None


def _remember_images(db: Any, conv: Any, payload: Optional[Dict[str, Any]]) -> None:
    prefs = dict(getattr(conv, "preferences", None) or {})
    if payload is None:
        prefs.pop(_PENDING_KEY, None)
    else:
        prefs[_PENDING_KEY] = payload
    conv.preferences = prefs
    try:
        if db is not None:
            db.commit()
    except Exception:
        pass


def _parse_attachment_message(message: str) -> Dict[str, Any]:
    """Flexible wording when the images were uploaded instead of pasted."""
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text:
        return {"op": None, "name": None}
    patterns = (
        ("cover", r"(?i)^(?:please\s+)?make this the cover of\s+(.+)$"),
        ("cover", r"(?i)^(?:please\s+)?set this as (?:the )?cover image (?:for|of)\s+(.+)$"),
        ("cover", r"(?i)^(?:please\s+)?set this as (?:the )?cover (?:for|of)\s+(.+)$"),
        ("cover", r"(?i)^set as the cover$"),
        ("add", r"(?i)^(?:please\s+)?add (?:this|these) (?:images?|photos?|pictures?) to\s+(.+)$"),
        ("add", r"(?i)^(?:please\s+)?add (?:this|these) (?:images?|photos?|pictures?)$"),
        ("add", r"(?i)^(?:please\s+)?use this (?:image|photo|picture) for\s+(.+)$"),
        ("add", r"(?i)^add these to\s+(.+)$"),
        ("replace", r"(?i)^(?:please\s+)?(?:change|replace) (?:the )?(?:images?|photos?|pictures?) of\s+(.+?)(?:\s+to\s+.+)?$"),
        ("replace", r"(?i)^(?:please\s+)?(?:change|replace) (?:the )?(?:images?|photos?|pictures?)$"),
        ("replace", r"(?i)^replace the current images$"),
    )
    for op, pattern in patterns:
        match = re.match(pattern, text)
        if not match:
            continue
        name = _clean_name(match.group(1)) if match.groups() else None
        return {"op": op, "name": name or None}
    name = _bare_property_name(text)
    if name:
        return {"op": None, "name": name}
    return {"op": None, "name": None}


def _action_question(name: str) -> Tuple[str, List[str]]:
    text = (
        f"Do you want to add these to {name}, replace the current images, or set as the cover?"
    )
    return text, [
        f"Add these to {name}",
        "Replace the current images",
        "Set as the cover",
    ]


_OTHER_REQUEST = re.compile(
    r"\b(what|how|where|why|when|price|status|show|search|delete|undo|generate|report|email|calendar|archive|list)\b",
    re.IGNORECASE,
)


def _bare_property_name(message: str) -> Optional[str]:
    text = re.sub(r"\s+", " ", (message or "").strip())
    if not text or "?" in text or len(text) > 80:
        return None
    if _IMAGE_VERB.search(text) or _OTHER_REQUEST.search(text) or _URL_RE.search(text):
        return None
    return _clean_name(text)


def _continuing_upload(message: str, pending: Dict[str, Any]) -> bool:
    if parse_image_command(message):
        return False
    parsed = _parse_attachment_message(message)
    if pending.get("stage") == "action":
        return parsed.get("op") in {"add", "replace", "cover"}
    if pending.get("stage") == "urls":
        return bool(_urls_in(message))
    if pending.get("stage") == "property":
        return bool(parsed.get("name"))
    return False


def _propose_image_change(
    db: Any,
    conv: Any,
    staff: Any,
    command: Dict[str, Any],
    property_id: str,
    property_name: str,
) -> Dict[str, Any]:
    urls = list(command.get("urls") or [])
    if command["op"] == "cover" and len(urls) > 1:
        urls = urls[:1]
    if command["op"] in {"add", "replace", "cover", "remove_url"}:
        urls, error = _checked_urls(urls)
        if error:
            return _reply(conv, error)
        if command["op"] == "cover" and len(urls) != 1:
            return _reply(conv, "Set the cover with one image link. Nothing was changed.")
        if command["op"] == "remove_url" and len(urls) != 1:
            return _reply(conv, "Tell me which image URL or number to remove. Nothing was changed.")
    command = dict(command)
    command["urls"] = urls
    command["property_name"] = property_name
    current = load_property_images(db, property_id)
    planned, error, sentence = plan_image_change(current, command)
    if error:
        return _reply(conv, error)
    lines = [f"{property_name}: {sentence}"]
    for url in urls:
        lines.append(f"- {url}")
    if command["op"] in {"remove_url", "remove_index", "remove_all"}:
        lines.append("Confirm to remove them, or cancel. Nothing has been removed.")
    else:
        lines.append("Confirm to apply, or cancel. Nothing has been changed.")
    from app.services.action_service import create_pending_action

    payload = {
        "property_id": property_id,
        "property_name": property_name,
        "op": command["op"],
        "urls": urls,
        "index": command.get("index"),
        "previous_images": [
            {"image_url": row["image_url"], "image_type": row.get("image_type") or "exterior"}
            for row in current
        ],
    }
    if command.get("smart_intent"):
        payload["smart_intent"] = command["smart_intent"]
    action = create_pending_action(
        db=db,
        staff_user_id=getattr(staff, "email", None) or "admin",
        tool_name="manage_property_images",
        action_payload=payload,
        conversation_id=str(getattr(conv, "id", "") or ""),
    )
    _remember_images(db, conv, None)
    if command.get("smart_intent"):
        _audit_smart_intent(db, staff, conv, command["smart_intent"], str(action.id))
    return _reply(
        conv,
        _confirm_line("\n".join(lines), command.get("language"), removing=command["op"] in {"remove_url", "remove_index", "remove_all"}),
        requires=True,
        pending_id=str(action.id),
        token=action.confirmation_token,
    )


def _resolve_named(db: Any, name: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    from app.services.admin_turn_rules import _property_id, _property_name, resolve_write_target

    record, problem = resolve_write_target(db, name)
    if problem:
        return None, None, problem
    property_id = _property_id(record)
    property_name = _property_name(record)
    if not property_id:
        return None, None, f"I could not find a property named '{name}'. Nothing was changed."
    return property_id, property_name, None


def _ask_for_property(
    db: Any,
    conv: Any,
    urls: List[str],
    op: Optional[str],
    *,
    language: Optional[str] = None,
    smart_intent: Optional[Dict[str, Any]] = None,
    question: Optional[str] = None,
) -> Dict[str, Any]:
    _remember_images(db, conv, {
        "urls": urls,
        "op": op,
        "stage": "property",
        "property_id": None,
        "property_name": None,
        "language": language,
        "smart_intent": smart_intent,
    })
    return _reply(conv, question or _question("property", language))


def _ask_for_action(db: Any, conv: Any, urls: List[str], property_id: str, property_name: str) -> Dict[str, Any]:
    _remember_images(db, conv, {
        "urls": urls,
        "op": None,
        "stage": "action",
        "property_id": property_id,
        "property_name": property_name,
    })
    text, suggestions = _action_question(property_name)
    return _reply(conv, text, suggestions=suggestions)


def _finish_attached(
    db: Any,
    conv: Any,
    staff: Any,
    op: Optional[str],
    name: Optional[str],
    urls: List[str],
    *,
    property_id: Optional[str] = None,
    property_name: Optional[str] = None,
    language: Optional[str] = None,
    smart_intent: Optional[Dict[str, Any]] = None,
    image_index: Optional[int] = None,
) -> Dict[str, Any]:
    if not property_id:
        if not name:
            return _ask_for_property(db, conv, urls, op, language=language, smart_intent=smart_intent)
        property_id, property_name, problem = _resolve_named(db, name)
        if problem:
            _remember_images(db, conv, {
                "urls": urls,
                "op": op,
                "stage": "property",
                "property_id": None,
                "property_name": None,
                "language": language,
                "smart_intent": smart_intent,
                "index": image_index,
            })
            return _reply(conv, problem)
    if not op:
        return _ask_for_action(db, conv, urls, property_id, property_name or name or "this property")
    command = {"op": op, "urls": urls, "name": property_name, "language": language, "index": image_index}
    if smart_intent:
        command["smart_intent"] = smart_intent
    return _propose_image_change(
        db,
        conv,
        staff,
        command,
        property_id,
        property_name or name or "this property",
    )


def _handle_attached(db: Any, conv: Any, message: str, staff: Any, urls: List[str]) -> Dict[str, Any]:
    parsed = _parse_attachment_message(message)
    classic = parse_image_command(message)
    op = parsed.get("op")
    name = parsed.get("name")
    if classic and classic.get("name") and classic.get("op") in {"add", "replace", "cover"}:
        op = op or classic["op"]
        name = name or classic["name"]
    return _finish_attached(db, conv, staff, op, name, urls)


def _continue_upload(db: Any, conv: Any, message: str, staff: Any, pending: Dict[str, Any]) -> Dict[str, Any]:
    parsed = _parse_attachment_message(message)
    op = parsed.get("op") or pending.get("op")
    if pending.get("stage") == "urls":
        urls = _urls_in(message)
        return _finish_attached(
            db,
            conv,
            staff,
            op,
            pending.get("property_name") or pending.get("property_text"),
            urls,
            property_id=pending.get("property_id"),
            property_name=pending.get("property_name"),
            language=pending.get("language"),
            smart_intent=pending.get("smart_intent"),
        )
    name = parsed.get("name") or pending.get("property_name")
    return _finish_attached(
        db,
        conv,
        staff,
        op,
        name,
        list(pending.get("urls") or []),
        property_id=pending.get("property_id") if not parsed.get("name") else None,
        property_name=pending.get("property_name") if not parsed.get("name") else None,
        language=pending.get("language"),
        smart_intent=pending.get("smart_intent"),
        image_index=pending.get("index"),
    )


_CONFIRM = {
    "pl": {
        "apply": "Potwierdź, aby zastosować, albo anuluj. Nic jeszcze nie zostało zmienione.",
        "remove": "Potwierdź, aby usunąć, albo anuluj. Nic jeszcze nie zostało usunięte.",
    },
    "hi": {
        "apply": "लागू करने के लिए पुष्टि करें, या रद्द करें। अभी कुछ नहीं बदला गया।",
        "remove": "हटाने के लिए पुष्टि करें, या रद्द करें। अभी कुछ नहीं हटाया गया।",
    },
    "gu": {
        "apply": "લાગુ કરવા માટે પુષ્ટિ કરો, અથવા રદ કરો. હજુ કંઈ બદલાયું નથી.",
        "remove": "દૂર કરવા માટે પુષ્ટિ કરો, અથવા રદ કરો. હજુ કંઈ દૂર થયું નથી.",
    },
}
_QUESTIONS = {
    "en": {
        "property": "Which property should these images go to?",
        "urls": "Which image link should I use?",
        "howto": "To change images, say 'add image <url> to <property>', 'replace the image of <property>', 'make image 2 the cover of <property>', or 'remove image 1 from <property>'. You can also attach a file. Nothing was changed.",
    },
    "pl": {
        "property": "Do której nieruchomości mają trafić te zdjęcia?",
        "urls": "Którego linku do zdjęcia mam użyć?",
        "howto": "Aby zmienić zdjęcia, napisz 'dodaj zdjecie <url> do <nieruchomość>', 'zmien zdjecie <nieruchomość> na <url>' albo 'usun wszystkie zdjecia <nieruchomość>'. Możesz też załączyć plik. Nic nie zostało zmienione.",
    },
    "hi": {
        "property": "ये तस्वीरें किस प्रॉपर्टी में लगानी हैं?",
        "urls": "कौन सा इमेज लिंक इस्तेमाल करूँ?",
        "howto": "इमेज बदलने के लिए लिखें 'add image <url> to <property>' या फाइल अटैच करें। अभी कुछ नहीं बदला गया।",
    },
    "gu": {
        "property": "આ ફોટા કઈ પ્રોપર્ટીમાં મૂકવા છે?",
        "urls": "કઈ ઇમેજ લિંક વાપરું?",
        "howto": "ફોટો બદલવા માટે 'add image <url> to <property>' લખો અથવા ફાઇલ જોડો. હજુ કંઈ બદલાયું નથી.",
    },
}


def _lang(language: Optional[str]) -> str:
    code = (language or "en").split("-")[0].lower()
    return code if code in _QUESTIONS else "en"


def _question(kind: str, language: Optional[str]) -> str:
    return _QUESTIONS[_lang(language)][kind]


def _confirm_line(text: str, language: Optional[str], *, removing: bool = False) -> str:
    code = _lang(language)
    if code == "en":
        return text
    line = _CONFIRM[code]["remove" if removing else "apply"]
    return text.rstrip() + "\n\n" + line


def _audit_smart_intent(db: Any, staff: Any, conv: Any, intent: Dict[str, Any], pending_id: str) -> None:
    try:
        from app.services.admin_intent_parser import scrub_intent
        from app.services.audit_service import record_audit_log

        safe = scrub_intent(intent)
        record_audit_log(
            db=db,
            tool_name="admin_smart_intent",
            staff_user_id=getattr(staff, "email", None),
            tool_arguments=safe,
            proposed_payload=safe,
            confirmed_by_staff=False,
            status="PROPOSED",
            conversation_id=str(getattr(conv, "id", "") or ""),
            pending_action_id=pending_id,
        )
    except Exception:
        return None


def _recent_turns(conv: Any) -> List[Dict[str, Any]]:
    history = getattr(conv, "message_history", None) or []
    turns = []
    for turn in list(history)[-6:]:
        if isinstance(turn, dict):
            turns.append({"role": turn.get("role"), "content": str(turn.get("content") or "")[:500]})
    return turns


def _is_howto(message: str) -> bool:
    text = re.sub(r"\s+", " ", (message or "").strip())
    if "?" not in text and not re.match(r"^(how|what|why|jak|kaise|kese|shu)\b", text, re.I):
        return False
    return bool(re.match(r"^(how|what|why|jak|kaise|kese|shu)\b", text, re.I))


def _howto_language(message: str) -> str:
    if re.search(r"[઀-૿]", message or ""):
        return "gu"
    if re.search(r"[\u0900-\u097F]", message or ""):
        return "hi"
    if re.search(r"\b(jak|zdj[eę]ci|nieruchomo)", message or "", re.I):
        return "pl"
    return "en"


def _image_op(intent: Dict[str, Any]) -> Optional[str]:
    name = intent.get("intent")
    index = intent.get("image_index")
    urls = intent.get("urls") or []
    if name == "image_add":
        return "add"
    if name == "image_replace":
        return "replace"
    if name == "image_set_cover":
        return "cover_index" if index and not urls else "cover"
    if name == "image_remove":
        return "remove_index" if index and not urls else "remove_url"
    if name == "image_remove_all":
        return "remove_all"
    return None


def _apply_image_intent(db: Any, conv: Any, staff: Any, message: str, intent: Dict[str, Any], owned: Optional[List[str]]) -> Dict[str, Any]:
    language = intent.get("language")
    op = _image_op(intent)
    urls = list(owned or intent.get("urls") or [])
    names = list(intent.get("property_texts") or [])
    if len(names) >= 2:
        _remember_images(db, conv, {
            "urls": urls,
            "op": op,
            "stage": "property",
            "property_id": None,
            "property_name": None,
            "language": language,
            "smart_intent": intent,
            "index": intent.get("image_index"),
        })
        shown = " or ".join(names[:4])
        return _reply(conv, f"Which property: {shown}?")
    if op is None or "action" in (intent.get("missing") or []):
        return _reply(conv, _question("howto", language))
    if not intent.get("property_text"):
        return _ask_for_property(db, conv, urls, op, language=language, smart_intent=intent)
    if op in {"add", "replace", "cover", "remove_url"} and not urls:
        _remember_images(db, conv, {
            "urls": [],
            "op": op,
            "stage": "urls",
            "property_text": intent.get("property_text"),
            "property_name": None,
            "property_id": None,
            "language": language,
            "smart_intent": intent,
            "index": intent.get("image_index"),
        })
        property_id, property_name, problem = _resolve_named(db, intent["property_text"])
        if problem:
            return _reply(conv, problem)
        _remember_images(db, conv, {
            "urls": [],
            "op": op,
            "stage": "urls",
            "property_id": property_id,
            "property_name": property_name,
            "language": language,
            "smart_intent": intent,
            "index": intent.get("image_index"),
        })
        return _reply(conv, _question("urls", language))
    return _finish_attached(
        db,
        conv,
        staff,
        op,
        intent.get("property_text"),
        urls,
        language=language,
        smart_intent=intent,
        image_index=intent.get("image_index"),
    )


def _smart_image_reply(db: Any, conv: Any, message: str, staff: Any, owned: Optional[List[str]]) -> Optional[Dict[str, Any]]:
    from app.services.admin_intent_parser import parse_admin_write_intent, smart_intent_kind

    if smart_intent_kind(message, owned) != "image":
        return None
    if _is_howto(message):
        return _reply(conv, _question("howto", _howto_language(message)))
    intent = parse_admin_write_intent(
        message,
        list(owned or []),
        _recent_turns(conv),
        _load_pending(conv),
    )
    if not intent or not str(intent.get("intent") or "").startswith("image_"):
        return None
    return _apply_image_intent(db, conv, staff, message, intent, owned)


def try_image_command(
    db: Any,
    conv: Any,
    message: str,
    staff: Any,
    attached_images: Optional[List[str]] = None,
) -> Optional[Dict[str, Any]]:
    owned, rejected = _accepted_attachments(attached_images)
    if rejected:
        return _reply(conv, rejected)
    smart = _smart_image_reply(db, conv, message, staff, owned)
    if smart is not None:
        return smart
    if owned:
        return _handle_attached(db, conv, message, staff, owned)
    pending = _load_pending(conv)
    if pending and _continuing_upload(message, pending):
        return _continue_upload(db, conv, message, staff, pending)
    command = parse_image_command(message)
    if command is None:
        if looks_like_image_command(message):
            return _reply(
                conv,
                "I could not read that image request. Name the property and the image link, for example: "
                "add image https://example.com/pool.jpg to Probe Villa. Nothing was changed.",
            )
        return None
    property_id, property_name, problem = _resolve_named(db, command["name"])
    if problem:
        return _reply(conv, problem)
    return _propose_image_change(db, conv, staff, command, property_id, property_name)


def execute_property_image_change(db: Any, payload: Dict[str, Any], staff: Any) -> Dict[str, Any]:
    """Apply a confirmed image change and keep the previous list in the undo snapshot."""
    from app.services.admin_batch_service import record_crud_operation_snapshot
    from app.services.cache_service import search_cache
    from app.services.firestore_service import save_or_update_property_firestore

    property_id = str(payload.get("property_id") or "")
    property_name = str(payload.get("property_name") or property_id)
    command = {
        "op": payload.get("op"),
        "urls": list(payload.get("urls") or []),
        "index": payload.get("index"),
        "property_name": property_name,
    }
    if command["op"] in {"add", "replace", "cover", "remove_url"}:
        checked, error = _checked_urls(command["urls"])
        if error:
            raise ValueError(error)
        command["urls"] = checked
    current = load_property_images(db, property_id)
    planned, error, _sentence = plan_image_change(current, command)
    if error or planned is None:
        raise ValueError(error or "I could not apply that image change.")

    operation_id = None
    if staff is not None:
        try:
            operation_id = record_crud_operation_snapshot(
                action_type="update",
                property_id=property_id,
                previous_state={"property_id": property_id, "property_name": property_name, "images": current},
                staff_user=staff,
                source="chat_image_confirm",
                db_session=db,
                confirmed_by_staff=True,
            )
        except Exception:
            operation_id = None

    cover = _cover_url(planned)
    save_or_update_property_firestore(
        property_id=property_id,
        fields={"property_name": property_name},
        images=[item["image_url"] for item in planned],
        cover_image=cover,
        image_mode="replace",
    )
    try:
        search_cache.invalidate()
    except Exception:
        pass
    try:
        from app.services.firestore_service import bump_properties_version
        bump_properties_version(target="user")
    except Exception:
        pass
    return {
        "success": True,
        "property_id": property_id,
        "title": property_name,
        "image_count": len(planned),
        "images": [item["image_url"] for item in planned],
        "cover_image": cover,
        "operation_id": operation_id,
        "previous_images": current,
    }
