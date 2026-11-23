"""Chat log formats: the app's own JSONL recording, plus importers.

Automatic VOD chat replay retrieval is NOT implemented (no documented endpoint).
Users import a chat log they already have instead.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..detection.detector import ChatMessage


@dataclass
class ChatLog:
    messages: list[ChatMessage]
    source_format: str
    meta: dict = field(default_factory=dict)
    skipped: int = 0


def _iso_to_epoch(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


# --- the app's own recording format ---------------------------------------
def jsonl_record(meta: dict, payload: dict, received_at: float) -> dict:
    """Turn an EventSub channel.chat.message notification into one JSONL line."""
    ev = payload["event"]
    return {
        "message_timestamp": meta.get("message_timestamp"),
        "received_at": received_at,
        "message_id": ev.get("message_id"),
        "broadcaster_user_id": ev.get("broadcaster_user_id"),
        "source_broadcaster_user_id": ev.get("source_broadcaster_user_id"),
        "user_id": ev.get("chatter_user_id"),
        "user": ev.get("chatter_user_login"),
        "text": (ev.get("message") or {}).get("text", ""),
    }


def load_jsonl(path: Path, t0: float | None = None, channel_id: str | None = None,
               include_shared_chat: bool = False) -> ChatLog:
    rows = []
    skipped = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                ts = _iso_to_epoch(r["message_timestamp"]) if r.get("message_timestamp") else float(r["received_at"])
            except (ValueError, KeyError, TypeError):
                skipped += 1
                continue
            src = r.get("source_broadcaster_user_id")
            if channel_id and not include_shared_chat and src and src != channel_id:
                skipped += 1
                continue
            rows.append((ts, r.get("user_id") or r.get("user") or "?", r.get("text", "")))
    rows.sort()
    base = t0 if t0 is not None else (rows[0][0] if rows else 0.0)
    return ChatLog([ChatMessage(ts - base, u, t) for ts, u, t in rows], "jsonl", {"t0_epoch": base}, skipped)


# --- importers --------------------------------------------------------------
def load_twitchdownloader_json(path: Path) -> ChatLog:
    """TwitchDownloader chat export (.json). Offsets are relative to the VOD start."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    comments = data.get("comments")
    if not isinstance(comments, list):
        raise ValueError("not a TwitchDownloader chat JSON: missing 'comments' list")
    msgs, skipped = [], 0
    for c in comments:
        try:
            t = float(c["content_offset_seconds"])
            commenter = c.get("commenter") or {}
            user = commenter.get("_id") or commenter.get("name") or commenter.get("display_name") or "?"
            msg = c.get("message") or {}
            text = msg.get("body")
            if text is None:
                text = "".join(fr.get("text", "") for fr in msg.get("fragments") or [])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        msgs.append(ChatMessage(t, str(user), text))
    msgs.sort(key=lambda m: m.t)
    video = data.get("video") or {}
    meta = {k: video.get(k) for k in ("id", "title", "start", "end", "length") if k in video}
    return ChatLog(msgs, "twitchdownloader_json", meta, skipped)


def load_csv(path: Path) -> ChatLog:
    """Simple CSV: offset_seconds,user,message (header required)."""
    msgs, skipped = [], 0
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        need = {"offset_seconds", "user", "message"}
        if not reader.fieldnames or not need <= set(reader.fieldnames):
            raise ValueError(f"CSV needs columns {sorted(need)}, got {reader.fieldnames}")
        for row in reader:
            try:
                msgs.append(ChatMessage(float(row["offset_seconds"]), row["user"], row["message"]))
            except (TypeError, ValueError):
                skipped += 1
    msgs.sort(key=lambda m: m.t)
    return ChatLog(msgs, "csv", {}, skipped)


def load_any(path: Path) -> ChatLog:
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        return load_jsonl(path)
    if suffix == ".csv":
        return load_csv(path)
    if suffix == ".json":
        return load_twitchdownloader_json(path)
    raise ValueError(f"unsupported chat log type: {suffix}")
