from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Optional, Union


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    date: datetime
    author: str
    text: str
    reply_to_message_id: Optional[int]
    topic_id: Optional[int] = None  # Forum Topics 的话题 ID（thread_id）


@dataclass(frozen=True)
class TelegramExport:
    chat_name: str
    messages: list[TelegramMessage]


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _parse_datetime(raw: Any, fallback_unix: Any) -> Optional[datetime]:
    if isinstance(raw, str) and raw:
        try:
            return _to_naive_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
        except ValueError:
            pass

    try:
        if fallback_unix is not None:
            return datetime.fromtimestamp(int(fallback_unix), tz=timezone.utc).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None
    return None


def _extract_text(raw_text: Any) -> str:
    if isinstance(raw_text, str):
        return raw_text.strip()

    if isinstance(raw_text, list):
        parts: list[str] = []
        for chunk in raw_text:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict):
                value = chunk.get("text")
                if isinstance(value, str):
                    parts.append(value)
        return "".join(parts).strip()

    return ""


def load_telegram_export(path: Union[str, Path]) -> TelegramExport:
    file_path = Path(path).expanduser().resolve()
    with file_path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    chat_name = str(payload.get("name") or "Unknown Chat")
    rows = payload.get("messages", [])
    if not isinstance(rows, list):
        rows = []

    messages: list[TelegramMessage] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "message":
            continue

        text = _extract_text(row.get("text"))
        if not text:
            continue

        dt = _parse_datetime(row.get("date"), row.get("date_unixtime"))
        if dt is None:
            continue

        try:
            message_id = int(row.get("id"))
        except (TypeError, ValueError):
            continue

        author = str(row.get("from") or row.get("from_id") or "Unknown")
        reply_to = row.get("reply_to_message_id")
        reply_to_message_id: Optional[int] = None
        if reply_to is not None:
            try:
                reply_to_message_id = int(reply_to)
            except (TypeError, ValueError):
                reply_to_message_id = None

        messages.append(
            TelegramMessage(
                message_id=message_id,
                date=dt,
                author=author,
                text=text,
                reply_to_message_id=reply_to_message_id,
            )
        )

    messages.sort(key=lambda m: (m.date, m.message_id))
    return TelegramExport(chat_name=chat_name, messages=messages)
