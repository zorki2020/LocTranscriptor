"""Markdown export of a transcript.

Labels (headings, metadata, default speaker names) follow the interface language
(`transcribator.i18n`), the transcript text itself is never changed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from transcribator.i18n import Table, pick
from transcribator.models import Transcript

LABELS: Table = {
    "recorded": {"en": "Recorded", "ru": "Дата записи"},
    "duration": {"en": "Duration", "ru": "Длительность"},
    "speakers": {"en": "Speakers", "ru": "Спикеры"},
    "summary": {"en": "Summary", "ru": "Кратко"},
    "transcript": {"en": "Transcript", "ru": "Транскрипт"},
    "speaker_n": {"en": "Speaker {n}", "ru": "Спикер {n}"},
}
# Transcript headings in every language: the WER tool finds the body by them.
TRANSCRIPT_HEADINGS = tuple(f"## {text}" for text in LABELS["transcript"].values())


def format_timestamp(seconds: float) -> str:
    """HH:MM:SS, seconds are rounded down."""
    total = int(max(seconds, 0.0))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def format_duration(seconds: float) -> str:
    """H:MM:SS."""
    total = int(max(seconds, 0.0))
    return f"{total // 3600}:{total % 3600 // 60:02d}:{total % 60:02d}"


def speaker_labels(
    transcript: Transcript,
    names: Mapping[str, str] | None = None,
    *,
    lang: str | None = None,
) -> dict[str, str]:
    """Model label (SPEAKER_00) -> display name.

    Speakers without a given name are numbered in order of first appearance:
    Speaker 1, 2, ...
    """
    names = names or {}
    return {
        raw: names.get(raw) or pick(LABELS, "speaker_n", lang, n=index)
        for index, raw in enumerate(transcript.speakers, start=1)
    }


def render_markdown(
    transcript: Transcript,
    *,
    title: str,
    recorded_at: str | None = None,
    summary: Sequence[str] = (),
    names: Mapping[str, str] | None = None,
    lang: str | None = None,
) -> str:
    labels = speaker_labels(transcript, names, lang=lang)

    meta = []
    if recorded_at:
        meta.append(f"{pick(LABELS, 'recorded', lang)}: {recorded_at}")
    meta.append(f"{pick(LABELS, 'duration', lang)}: {format_duration(transcript.duration_s)}")
    if labels:
        meta.append(f"{pick(LABELS, 'speakers', lang)}: " + ", ".join(labels.values()))

    # Title on one line: a line break inside a heading breaks the markup.
    lines = [f"# {' '.join(title.split())}", "", " · ".join(meta), ""]
    if summary:
        lines += [f"## {pick(LABELS, 'summary', lang)}", ""]
        lines += [f"- {item}" for item in summary]
        lines.append("")
    lines += [f"## {pick(LABELS, 'transcript', lang)}", ""]
    for segment in transcript.segments:
        stamp = format_timestamp(segment.start)
        lines += [f"**[{stamp}] {labels[segment.speaker]}:** {segment.text}", ""]
    return "\n".join(lines)
