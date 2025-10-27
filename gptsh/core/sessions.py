from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from gptsh.interfaces import LLMClient
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gptsh.core.session import ChatSession

_log = logging.getLogger(__name__)


class SessionSummary(TypedDict, total=False):
    id: str
    filename: str
    created_at: str
    updated_at: str
    title: Optional[str]
    agent: Optional[str]
    model: Optional[str]


BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _fmt_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_sessions_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    d = base / "gptsh" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _gen_id() -> str:
    import random

    # 4-char base36 id
    return "".join(random.choices(BASE36_ALPHABET, k=4))


def _list_json_files() -> List[Path]:
    d = get_sessions_dir()
    files = [p for p in d.glob("*.json") if p.is_file()]
    return files


def _find_file_by_id(session_id: str) -> Optional[Path]:
    suf = f"-{session_id}.json"
    for p in _list_json_files():
        if p.name.endswith(suf):
            return p
    return None


def list_sessions(limit: Optional[int] = None) -> List[SessionSummary]:
    out: List[SessionSummary] = []
    files = _list_json_files()
    # Sort newest-first by mtime to reflect last used order
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[: limit or None]:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            _log.error("Failed to read session file %s: %s", p, e)
            continue

        item: SessionSummary = {
            "id": str(data.get("id") or ""),
            "filename": str(p),
            "created_at": str(data.get("created_at") or ""),
            "updated_at": str(data.get("updated_at") or data.get("created_at") or ""),
            "title": data.get("title"),
        }
        agent = data.get("agent") or {}
        item["agent"] = agent.get("name")
        item["model"] = agent.get("model")
        out.append(item)
    return out


def resolve_session_ref(ref: str) -> str:
    ref = (ref or "").strip()
    if not ref:
        raise ValueError("Empty session reference")
    if ref.isdigit():
        idx = int(ref)
        sessions = list_sessions()
        if idx < 0 or idx >= len(sessions):
            raise ValueError(f"Session index out of range: {idx}")
        sid = sessions[idx].get("id") or ""
        if not sid:
            raise ValueError("Selected session has no id")
        return sid
    # treat as id or unique prefix
    sessions = list_sessions()
    matches = [s for s in sessions if (s.get("id") or "").startswith(ref)]
    if not matches:
        raise ValueError(f"No session matches: {ref}")
    ids = [s.get("id") for s in matches if s.get("id")]
    ids_unique = sorted(set(ids), key=ids.index)
    if len(ids_unique) > 1:
        _log.warning(
            "Ambiguous session id prefix '%s'; matches: %s",
            ref,
            ", ".join([i for i in ids_unique if i]),
        )
        raise ValueError(
            f"Ambiguous session id prefix '{ref}'; matches: {', '.join([i for i in ids_unique if i])}"
        )
    return ids_unique[0] or ""


def load_session(session_id_or_file: str) -> Dict[str, Any]:
    p: Optional[Path] = None
    cand = Path(session_id_or_file)
    if cand.exists() and cand.is_file():
        p = cand
    else:
        p = _find_file_by_id(session_id_or_file)
    if p is None:
        raise FileNotFoundError(f"Session not found: {session_id_or_file}")
    data = json.loads(p.read_text(encoding="utf-8"))
    # ensure minimal keys
    if not data.get("id"):
        # infer id from filename suffix
        stem = p.stem
        if "-" in stem:
            data["id"] = stem.split("-")[-1]
    return data


def _generate_unique_id() -> str:
    # loop until unused id is found
    for _ in range(100):
        sid = _gen_id()
        if _find_file_by_id(sid) is None:
            return sid
    raise RuntimeError("Failed to allocate session id")


def _compose_filename(sid: str, ts: Optional[datetime] = None) -> Path:
    ts = ts or _now_utc()
    prefix = ts.strftime("%Y%m%d-%H%M%S")
    return get_sessions_dir() / f"{prefix}-{sid}.json"


def save_session(doc: Dict[str, Any]) -> str:
    # Assign id and timestamps on first save
    is_new = not bool(doc.get("id"))
    if is_new:
        sid = _generate_unique_id()
        doc["id"] = sid
        now = _fmt_iso(_now_utc())
        doc["created_at"] = now
        doc["updated_at"] = now
        path = _compose_filename(sid)
    else:
        sid = str(doc.get("id"))
        path = _find_file_by_id(sid)
        if path is None:
            # Treat as new if file missing
            path = _compose_filename(sid)
        doc["updated_at"] = _fmt_iso(_now_utc())
    # Write JSON
    content = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    Path(path).write_text(content, encoding="utf-8")
    return str(path)


def new_session_doc(
    *,
    agent_info: Dict[str, Any],
    provider_info: Dict[str, Any],
    output: Optional[str] = None,
    mcp_allowed_servers: Optional[List[str]] = None,
) -> Dict[str, Any]:
    doc: Dict[str, Any] = {
        "id": None,
        "title": None,
        "created_at": None,
        "updated_at": None,
        "agent": agent_info,
        "provider": provider_info,
        "messages": [],
        "usage": {"tokens": {}, "cost": 0},
        "meta": {},
    }
    if output:
        doc["meta"]["output"] = output
    if mcp_allowed_servers is not None:
        doc["meta"]["mcp_allowed_servers"] = list(mcp_allowed_servers)
    return doc


def append_messages(
    doc: Dict[str, Any],
    new_messages: List[Dict[str, Any]],
    usage_delta: Optional[Dict[str, Any]] = None,
) -> None:
    if not isinstance(doc.get("messages"), list):
        doc["messages"] = []
    if new_messages:
        doc["messages"].extend(list(new_messages))
    if usage_delta:
        # Merge tokens and cost conservatively
        tokens = usage_delta.get("tokens") or {}
        if tokens:
            doc.setdefault("usage", {}).setdefault("tokens", {}).update(tokens)
        if isinstance(usage_delta.get("cost"), (int, float)):
            try:
                base = doc.setdefault("usage", {}).get("cost") or 0
                doc["usage"]["cost"] = float(base) + float(usage_delta.get("cost") or 0)
            except Exception:
                doc["usage"]["cost"] = usage_delta.get("cost")


def resolve_small_model(agent_conf: Dict[str, Any], provider_conf: Dict[str, Any]) -> Optional[str]:
    a = agent_conf or {}
    p = provider_conf or {}
    if isinstance(a, dict) and a.get("model_small"):
        return a.get("model_small")
    if isinstance(p, dict) and p.get("model_small"):
        return p.get("model_small")
    return None


async def generate_title(
    first_user: str, *, small_model: Optional[str], llm: LLMClient
) -> Optional[str]:
    if not first_user or not small_model:
        return None
    system = (
        "You generate a short, human-friendly title for a conversation based solely on the first user message. "
        "Return 3–7 plain words. No punctuation, no quotes, no extra text."
    )
    params: Dict[str, Any] = {
        "model": small_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": first_user},
        ],
        "temperature": 0.2,
        "max_tokens": 24,
    }
    try:
        resp = await llm.complete(params)
    except Exception as e:
        _log.error("Title generation call failed: %s", e)
        return None

    # Extract text
    from gptsh.llm.chunk_utils import extract_text as _extract
    text = _extract(resp)
    title = str(text).strip()

    # normalize: remove punctuation and lines, title case, trim length
    #import re

    #title = title.splitlines()[0]
    #title = re.sub(
    #    r"[\"'\-–—:,.!?()\[\]{}]|\s+", lambda m: " " if m.group(0).isspace() else "", title
    #)
    #title = " ".join(title.split())
    #if not title:
    #    return None
    # Ensure 3-7 words constraint softly (truncate if too long)
    #words = title.split()
    #if len(words) > 7:
    #    title = " ".join(words[:7])
    # Title case
    #title = title.title()
    return title


def preload_session_to_chat(doc: Dict[str, Any], chat: "ChatSession") -> None:
    """Restore history, system prompt, usage, and title from a saved session doc into ChatSession."""
    try:
        hist = list(doc.get("messages") or [])
    except Exception:
        hist = []
    try:
        prompt_sys = (doc.get("agent") or {}).get("prompt_system")
    except Exception:
        prompt_sys = None
    if prompt_sys and (not hist or (hist and hist[0].get("role") != "system")):
        hist = [{"role": "system", "content": prompt_sys}] + hist
    chat.history = hist
    try:
        if isinstance(doc.get("usage"), dict):
            chat.usage = dict(doc["usage"])  # type: ignore
    except Exception:
        pass
    try:
        t = doc.get("title")
        if isinstance(t, str) and t.strip():
            chat.title = t
    except Exception:
        pass


def save_after_turn(
    doc: Dict[str, Any],
    chat: "ChatSession",
    new_messages: List[Dict[str, Any]],
) -> str:
    """Append new messages, merge usage, sync title, and save the session doc."""
    try:
        if isinstance(chat.title, str) and chat.title.strip():
            doc["title"] = chat.title.strip()
    except Exception:
        pass
    try:
        append_messages(doc, new_messages, usage_delta=getattr(chat, "usage", {}))
    except Exception:
        append_messages(doc, new_messages, usage_delta=None)
    return save_session(doc)
