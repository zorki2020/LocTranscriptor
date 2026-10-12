"""Model backend errors.

Like `AudioError`, they carry only a code: the output of models and external programs
may contain meeting text or a file path, and meeting content never goes to logs or
errors. The original exception is not chained (`from None`) so it does not end up in the
traceback.
"""

from __future__ import annotations


class BackendError(Exception):
    """A model or external program failure. Code only, no text or paths."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
