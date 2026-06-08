"""
Streaming filter that strips <think>...</think> blocks and leading whitespace.
Compatible with Qwen3, GLM-4, and other reasoning models that emit think tags.
"""


class ThinkFilter:
    """
    Feed tokens in via write(); the emit callback receives filtered tokens.
    Thread-safe for single-thread streaming use.
    """

    def __init__(self, emit):
        self._buf = ""
        self._in_think = False
        self._seen_content = False
        self._emit = emit

    def write(self, token: str) -> None:
        self._buf += token
        self._flush()

    def _flush(self) -> None:
        while True:
            s = self._buf
            if self._in_think:
                end = s.find("</think>")
                if end == -1:
                    return  # hold everything until closing tag
                # drop think block and any trailing whitespace/newlines
                self._buf = s[end + len("</think>"):].lstrip("\n\r ")
                self._in_think = False
                continue

            start = s.find("<think>")
            if start == -1:
                if not s:
                    return
                # suppress leading whitespace before first real content
                if not self._seen_content:
                    s = s.lstrip("\n\r ")
                    if not s:
                        self._buf = ""
                        return
                    self._seen_content = True
                self._emit(s)
                self._buf = ""
                return

            # emit content before <think>
            if start > 0:
                self._seen_content = True
                self._emit(s[:start])
            self._buf = s[start + len("<think>"):]
            self._in_think = True
