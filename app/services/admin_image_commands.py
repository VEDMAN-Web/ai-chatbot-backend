"""
Admin chat commands that add, replace, cover, or remove property images.

They only propose a pending action. Confirm (button or "yes confirm") writes
PostgreSQL and Firestore, and undo restores the previous image list.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_IMAGE_EXT = re.compile(r"\.(?:jpg|jpeg|png|webp)(?:[?#].*)?$", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s,\"'<>]+", re.IGNORECASE)
_IMAGE_WORD = re.compile(r"\b(images?|photos?|pictures?|cover)\b", re.IGNORECASE)
_IMAGE_VERB = re.compile(r"\b(add|change|replace|remove|set|make)\b", re.IGNORECASE)

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
    bare = _URL_RE.sub(" ", message or "")
    return bool(_IMAGE_WORD.search(bare) and _IMAGE_VERB.search(bare))


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
) -> Dict[str, Any]:
    return {
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


def _checked_urls(urls: List[str]) -> Tuple[List[str], Optional[str]]:
    if not urls:
        return [], "I need an http or https image link. Nothing was changed."
    checked: List[str] = []
    for url in urls:
        try:
            checked.append(validate_image_url(url))
        except ImageUrlError as exc:
            return [], f"{exc} Nothing was changed."
    return checked, None


def load_property_images(db: Any, property_id: str) -> List[Dict[str, Any]]:
    """Current images from PostgreSQL, or from Firestore when SQL has none."""
    rows = _sql_image_rows(db, property_id)
    if rows:
        return rows
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


def _sql_image_rows(db: Any, property_id: str) -> List[Dict[str, Any]]:
    if db is None:
        return []
    try:
        from app.db.models import PropertyImage

        rows = (
            db.query(PropertyImage)
            .filter(PropertyImage.property_id == property_id)
            .order_by(PropertyImage.sort_order, PropertyImage.image_url)
            .all()
        )
    except Exception:
        return []
    if not isinstance(rows, list):
        return []
    return [
        {
            "image_url": row.image_url,
            "image_type": row.image_type or "exterior",
            "sort_order": int(row.sort_order or 0),
        }
        for row in rows
        if getattr(row, "image_url", None)
    ]


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


def try_image_command(db: Any, conv: Any, message: str, staff: Any) -> Optional[Dict[str, Any]]:
    command = parse_image_command(message)
    if command is None:
        if looks_like_image_command(message):
            return _reply(
                conv,
                "I could not read that image request. Name the property and the image link, for example: "
                "add image https://example.com/pool.jpg to Probe Villa. Nothing was changed.",
            )
        return None
    from app.services.admin_turn_rules import resolve_write_target

    record, problem = resolve_write_target(db, command["name"])
    if problem:
        return _reply(conv, problem)
    from app.services.admin_turn_rules import _property_id, _property_name

    property_id = _property_id(record)
    property_name = _property_name(record)
    if not property_id:
        return _reply(conv, f"I could not find a property named '{command['name']}'. Nothing was changed.")
    urls = list(command.get("urls") or [])
    if command["op"] in {"add", "replace", "cover", "remove_url"}:
        urls, error = _checked_urls(urls)
        if error:
            return _reply(conv, error)
        if command["op"] == "cover" and len(urls) != 1:
            return _reply(conv, "Set the cover with one image link. Nothing was changed.")
        if command["op"] == "remove_url" and len(urls) != 1:
            return _reply(conv, "Tell me which image URL or number to remove. Nothing was changed.")
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

    action = create_pending_action(
        db=db,
        staff_user_id=getattr(staff, "email", None) or "admin",
        tool_name="manage_property_images",
        action_payload={
            "property_id": property_id,
            "property_name": property_name,
            "op": command["op"],
            "urls": urls,
            "index": command.get("index"),
            "previous_images": [
                {"image_url": row["image_url"], "image_type": row.get("image_type") or "exterior"}
                for row in current
            ],
        },
        conversation_id=str(getattr(conv, "id", "") or ""),
    )
    return _reply(
        conv,
        "\n".join(lines),
        requires=True,
        pending_id=str(action.id),
        token=action.confirmation_token,
    )


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

    _write_sql_images(db, property_id, planned)
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


def _write_sql_images(db: Any, property_id: str, items: List[Dict[str, Any]]) -> None:
    if db is None:
        return
    from urllib.parse import urlparse as _parse

    from app.db.models import PropertyImage

    db.query(PropertyImage).filter(PropertyImage.property_id == property_id).delete(synchronize_session=False)
    for index, item in enumerate(items):
        host = _parse(item["image_url"]).hostname or ""
        db.add(PropertyImage(
            property_id=property_id,
            image_url=item["image_url"],
            image_type=item.get("image_type") or "exterior",
            image_host=host[:100],
            sort_order=index,
        ))
    db.commit()
