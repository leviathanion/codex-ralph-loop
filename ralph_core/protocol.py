from __future__ import annotations

import hashlib

from ralph_core.model import SUMMARY_LIMIT


def normalize_text(text: str) -> str:
    return ' '.join(text.split())


def truncate_summary(text: str) -> str:
    normalized = normalize_text(text)
    if not normalized:
        return ''
    return normalized[:SUMMARY_LIMIT]


def fingerprint_message(text: str) -> str:
    normalized = normalize_text(text)
    digest = hashlib.sha256(normalized.encode('utf-8')).hexdigest()
    return f'sha256:{digest}'
