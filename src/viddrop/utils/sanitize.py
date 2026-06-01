"""Credential-safe error sanitization for Viddrop.

Provides a single ``sanitize_error`` function used by any module that handles
raw error strings from yt-dlp, FFmpeg, or external processes. Keeping the
redaction logic here prevents circular imports between core modules.
"""

from __future__ import annotations

import re

_REDACTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Authorization:[^\r\n]+"),
    re.compile(r"--password\s+\S+"),
    re.compile(r"--username\s+\S+"),
    re.compile(r"(?i)cookies?:\s*\S+"),
    re.compile(r"(?:token|key|secret)=[A-Za-z0-9+/=]{8,}"),
    re.compile(r"password=\S+"),
)


def sanitize_error(raw: str) -> str:
    """Redact credential-like patterns and cap the message at 500 chars."""
    cleaned = raw
    for pattern in _REDACTION_PATTERNS:
        cleaned = pattern.sub("<redacted>", cleaned)
    cleaned = cleaned[:500].strip()
    return cleaned or "Unknown error"
