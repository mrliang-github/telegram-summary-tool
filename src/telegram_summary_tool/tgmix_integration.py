from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List, Optional, Union

import yaml

from .telegram_export import TelegramMessage


@dataclass(frozen=True)
class TGMixResult:
    export_dir: Path
    toon_file: Path


def resolve_export_dir(input_path: Union[str, Path]) -> Path:
    path = Path(input_path).expanduser().resolve()
    if path.is_dir():
        if not (path / "result.json").exists():
            raise FileNotFoundError(f"result.json not found under: {path}")
        return path

    if path.is_file():
        if path.name != "result.json":
            raise ValueError("For tgmix mode, input file must be named result.json.")
        return path.parent

    raise FileNotFoundError(f"Input path does not exist: {path}")


def run_tgmix(
    export_dir: Union[str, Path],
    anonymize: bool = False,
    skip_if_exists: bool = True,
) -> TGMixResult:
    export_path = Path(export_dir).expanduser().resolve()
    toon_file = export_path / "tgmix_output.toon.txt"
    media_dir = export_path / "tgmix_media"

    if skip_if_exists and toon_file.exists():
        return TGMixResult(export_dir=export_path, toon_file=toon_file)

    if toon_file.exists():
        toon_file.unlink()
    if media_dir.exists():
        shutil.rmtree(media_dir)

    cmd = ["uvx", "--from", "tgmix", "tgmix", str(export_path)]
    if anonymize:
        cmd.append("--anonymize")

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "tgmix preprocessing failed.\n"
            f"Command: {' '.join(cmd)}\n"
            f"STDOUT:\n{proc.stdout}\n"
            f"STDERR:\n{proc.stderr}"
        )

    if not toon_file.exists():
        raise RuntimeError(
            "tgmix finished but tgmix_output.toon.txt was not found "
            f"in {export_path}"
        )
    return TGMixResult(export_dir=export_path, toon_file=toon_file)


def _parse_time(value: object) -> Optional[datetime]:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(
                tzinfo=None
            )
        except ValueError:
            return None
    return None


def _extract_author_map(raw_map: object) -> Dict[str, str]:
    if not isinstance(raw_map, dict):
        return {}
    out: Dict[str, str] = {}
    for author_id, raw in raw_map.items():
        key = str(author_id)
        if isinstance(raw, str):
            out[key] = raw
        elif isinstance(raw, dict):
            name = raw.get("name")
            user_id = raw.get("id")
            if isinstance(name, str) and name.strip():
                out[key] = name.strip()
            elif isinstance(user_id, str) and user_id.strip():
                out[key] = user_id.strip()
            else:
                out[key] = key
        else:
            out[key] = key
    return out


def _find_messages_key(payload: dict) -> Optional[str]:
    for k in payload.keys():
        if isinstance(k, str) and k.startswith("messages"):
            return k
    return None


def load_messages_from_tgmix_toon(
    toon_file: Union[str, Path]
) -> tuple[str, List[TelegramMessage]]:
    file_path = Path(toon_file).expanduser().resolve()
    raw = file_path.read_text(encoding="utf-8")

    payload = yaml.safe_load(raw)
    if not isinstance(payload, dict):
        raise ValueError("Unexpected tgmix output format: root is not a mapping.")

    chat_name = str(payload.get("chat_name") or "Unknown Chat")
    author_map = _extract_author_map(payload.get("author_map"))

    messages_key = _find_messages_key(payload)
    if messages_key is None:
        raise ValueError("Unexpected tgmix output format: messages key not found.")

    raw_messages = payload.get(messages_key)
    if not isinstance(raw_messages, list):
        raise ValueError("Unexpected tgmix output format: messages should be a list.")

    messages: List[TelegramMessage] = []
    for row in raw_messages:
        if not isinstance(row, dict):
            continue

        raw_text = row.get("text")
        if not isinstance(raw_text, str):
            continue
        text = raw_text.strip()
        if not text:
            continue

        raw_id = row.get("id")
        try:
            message_id = int(raw_id)
        except (TypeError, ValueError):
            continue

        dt = _parse_time(row.get("time"))
        if dt is None:
            continue

        raw_author_id = row.get("author_id")
        author_id = str(raw_author_id) if raw_author_id is not None else ""
        author = author_map.get(author_id) or author_id or "Unknown"

        raw_reply = row.get("reply_to_message_id")
        reply_to_message_id: Optional[int] = None
        if raw_reply is not None:
            try:
                reply_to_message_id = int(raw_reply)
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
    return chat_name, messages
