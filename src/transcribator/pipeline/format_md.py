"""Markdown export of a transcript.

Labels (headings, metadata, default speaker names) follow the interface language
(`transcribator.i18n`), the transcript text itself is never changed.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from transcribator.i18n import Table, pick
from transcribator.models import Transcript
from transcribator.pipeline.merge import MIC_SPEAKER

LABELS: Table = {
    "recorded": {"en": "Recorded", "ru": "Дата записи"},
    "duration": {"en": "Duration", "ru": "Длительность"},
    "speakers": {"en": "Speakers", "ru": "Спикеры"},
    "summary": {"en": "Summary", "ru": "Кратко"},
    "transcript": {"en": "Transcript", "ru": "Транскрипт"},
    "speaker_n": {"en": "Speaker {n}", "ru": "Спикер {n}"},
    "speaker_me": {"en": "Me", "ru": "Я"},
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

    Speakers without a given name get the default names of `default_speaker_names`.
    """
    names = names or {}
    defaults = default_speaker_names(transcript.speakers, lang=lang)
    return {raw: names.get(raw) or defaults[raw] for raw in transcript.speakers}


def default_speaker_names(speakers: Iterable[str], *, lang: str | None = None) -> dict[str, str]:
    """Default display names for labels given in order of first appearance: the
    microphone speaker of a two-channel recording is "Me", the others are numbered
    Speaker 1, 2, ... (the microphone speaker does not take a number)."""
    names: dict[str, str] = {}
    number = 0
    for raw in speakers:
        if raw in names:
            continue
        if raw == MIC_SPEAKER:
            names[raw] = pick(LABELS, "speaker_me", lang)
        else:
            number += 1
            names[raw] = pick(LABELS, "speaker_n", lang, n=number)
    return names


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
