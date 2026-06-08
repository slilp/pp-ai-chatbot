"""
PII masking for log data before sending to LLM.
Add patterns here to redact sensitive fields.
"""

import re
from typing import Any

# Each tuple: (compiled regex, replacement string)
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'"customerName"\s*:\s*"[^"]+"'), '"customerName":"***"'),
    (re.compile(r'"MobileNo"\s*:\s*"[^"]+"'), '"MobileNo":"***"'),
    (re.compile(r'"cid"\s*:\s*"[^"]+"'), '"cid":"***"'),
    (re.compile(r'"sofAccount"\s*:\s*"[^"]+"'), '"sofAccount":"***"'),
    (re.compile(r'"sofAccountName"\s*:\s*"[^"]+"'), '"sofAccountName":"***"'),
    (re.compile(r'"ref1"\s*:\s*"[^"]+"'), '"ref1":"***"'),
    # SOAP MobileNo
    (re.compile(r'<MobileNo>[^<]+</MobileNo>'), '<MobileNo>***</MobileNo>'),
    # SOAP Password
    (re.compile(r'<Password>[^<]+</Password>'), '<Password>***</Password>'),
]


def mask(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def mask_doc(doc: Any) -> Any:
    """Recursively mask PII in a document (dict/list/str)."""
    if isinstance(doc, str):
        return mask(doc)
    if isinstance(doc, dict):
        return {k: mask_doc(v) for k, v in doc.items()}
    if isinstance(doc, list):
        return [mask_doc(item) for item in doc]
    return doc
