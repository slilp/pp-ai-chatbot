"""SSE event formatting helpers."""

import json
from typing import AsyncIterator


def text_event(token: str) -> str:
    """Encode a text token as an SSE data line."""
    return f"data: {json.dumps(token)}\n\n"


def status_event(message: str) -> str:
    """Encode a status update (shown in the UI loading state)."""
    payload = json.dumps({"type": "status", "message": message})
    return f"data: {payload}\n\n"


def error_event(message: str) -> str:
    return f"data: [ERROR] {message}\n\n"


DONE_EVENT = "data: [DONE]\n\n"
