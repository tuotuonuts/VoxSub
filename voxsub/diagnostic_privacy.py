"""Privacy boundary for diagnostic exports; no free-form inference content escapes."""
from __future__ import annotations
import json
import os
import re
from typing import Any


def redact(text: str) -> str:
    text = re.sub(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_-]?key|token|password|secret)\s*['\"]?\s*[:=]\s*['\"]?)[^\s,;'\"]+", r"\1[REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(r"https?://[^\s]+", "[URL]", text)
    text = re.sub(r"[A-Za-z]:[\\/][^\n,;\"]+", "[PATH]", text)
    # Model/report roots may be network shares or portable Unix paths, not only C:\Users.
    text = re.sub(r"\\\\[^\\/\s]+[\\/][^\n,;\"']+", "[PATH]", text)
    text = re.sub(r"(?<!\w)/(?:home|Users|tmp|var|opt|mnt|media|Volumes|private)/[^\n,;\"']+", "[PATH]", text)
    for value in (os.environ.get("USERPROFILE"), os.environ.get("HOME")):
        if value:
            text = text.replace(value, "[HOME]")
    return text


def export_logs(text: str) -> str:
    """Only generated allowlisted MODEL_TRACE JSON; other messages are omitted.

    Regex alone cannot reliably distinguish transcript text from exception bodies.
    Preserve timestamps/levels for other records without exporting their message.
    """
    from voxsub.diagnostic_trace import _ALLOWED
    output = []
    for line in text.splitlines()[-2000:]:
        marker = line.find("MODEL_TRACE ")
        if marker >= 0:
            try:
                event = json.loads(line[marker + len("MODEL_TRACE "):])
                allowed = _ALLOWED | {"at", "run_id"}
                clean = {k: v for k, v in event.items() if k in allowed and isinstance(v, (str, int, float, bool, type(None)))}
                output.append(redact(json.dumps(clean, ensure_ascii=False)))
                continue
            except (ValueError, AttributeError):
                pass
        prefix = re.match(r"^([0-9T:.+\- Z]+)?\s*(DEBUG|INFO|WARNING|ERROR|CRITICAL)?", line)
        output.append((prefix.group(0).strip() if prefix else "") + " [message omitted: privacy boundary]")
    return "\n".join(output)


def _private_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized == "token" or any(token in normalized for token in (
        "secret", "password", "apikey", "serial", "username", "hostname",
        "authorization", "accesstoken", "refreshtoken", "bearertoken"))


def safe_snapshot(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: safe_snapshot(v) for k, v in value.items()
                if not _private_key(k)}
    if isinstance(value, (list, tuple)):
        return [safe_snapshot(v) for v in value]
    return value
