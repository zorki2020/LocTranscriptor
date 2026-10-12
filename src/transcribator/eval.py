"""Quality evaluation: WER between a reference and a transcript.

The user prepares the reference by hand for 2-3 minutes of a recording. The computation
is slow (quadratic) for long texts, so it is meant for short references.
"""

from __future__ import annotations

import re

from transcribator.pipeline.format_md import TRANSCRIPT_HEADINGS

_NON_WORD = re.compile(r"[^\w\s]", re.UNICODE)
_SPEAKER_PREFIX = re.compile(r"^\*\*\[\d{2}:\d{2}:\d{2}\][^:]*:\*\*\s*")
_TIMESTAMP = re.compile(r"^\*\*\[(\d{2}):(\d{2}):(\d{2})\]")


def normalize_words(text: str) -> list[str]:
    """Lowercase, Cyrillic "ё" as "е", no punctuation."""
    cleaned = _NON_WORD.sub(" ", text.lower().replace("ё", "е"))
    return cleaned.split()


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref = normalize_words(reference)
    hyp = normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    previous = list(range(len(hyp) + 1))
    for i, ref_word in enumerate(ref, start=1):
        current = [i] + [0] * len(hyp)
        for j, hyp_word in enumerate(hyp, start=1):
            cost = 0 if ref_word == hyp_word else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
        previous = current
    return previous[-1] / len(ref)


def transcript_text(markdown: str, start_s: float | None = None, end_s: float | None = None) -> str:
    """Extract only the speech from a Markdown export: no headings, timestamps or names.

    With `start_s` and `end_s` only utterances that start in this window are taken. This is
    needed when the reference covers 2-3 minutes and the transcript is longer: otherwise
    insertions would inflate the WER.
    """
    body = markdown
    for marker in TRANSCRIPT_HEADINGS:
        if marker in markdown:
            body = markdown.split(marker, 1)[1]
            break
    lines: list[str] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        stamp = _TIMESTAMP.match(line)
        if stamp and (start_s is not None or end_s is not None):
            moment = int(stamp[1]) * 3600 + int(stamp[2]) * 60 + int(stamp[3])
            if (start_s is not None and moment < start_s) or (
                end_s is not None and moment >= end_s
            ):
                continue
        lines.append(_SPEAKER_PREFIX.sub("", line))
    return " ".join(lines)
