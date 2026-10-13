"""Formatting for pages: dates, durations, statuses, search highlighting.

Everything that comes from the database is untrusted text. The only place where HTML is
built by hand is `highlight`: escape first, then `<mark>`. Words come from `strings.py`
in the interface language.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from markupsafe import Markup, escape

from transcribator.storage.repo import search_form
from transcribator.web.strings import month, t

_WORD = re.compile(r"\w+", re.UNICODE)
# Stage code from the worker -> string key.
_STAGES = {
    "start": "stage_start",
    "audio": "stage_audio",
    "convert": "stage_audio",
    "normalize": "stage_audio",
    "diarize": "stage_diarize",
    "diarization": "stage_diarize",
    "transcribe": "stage_transcribe",
    "asr": "stage_transcribe",
    "merge": "stage_merge",
    "save": "stage_save",
}
_STATUSES = {"queued": "status_queued", "done": "status_done", "error": "status_error"}
# Error code -> string key.
_ERRORS = {
    "source_missing": "err_source_missing",
    "timeout": "err_timeout",
    "probe_failed": "err_not_media",
    "probe_bad_output": "err_not_media",
    "no_audio_stream": "err_no_audio_stream",
    "empty_audio": "err_empty_audio",
    "not_a_file": "err_not_a_file",
    "bad_path": "err_bad_path",
    "ffmpeg_missing": "err_ffmpeg_missing",
    "audio_tool_missing": "err_prepare_failed",
    "normalize_failed": "err_prepare_failed",
    "cut_failed": "err_prepare_failed",
    "bad_result": "err_bad_result",
}
SPEAKER_COLORS = 8


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(UTC)).astimezone()


def _day_month(local: datetime) -> str:
    return t("day_month", day=local.day, month=month(local.month))


def human_date(value: str | None) -> str:
    """Full date in the Mac's local time: "Oct 9, 2026, 15:00", "9 октября 2026, 15:00"."""
    stamp = parse_utc(value)
    if stamp is None:
        return t("date_unset")
    local = stamp.astimezone()
    return t("date_full", day_month=_day_month(local), year=local.year, time=f"{local:%H:%M}")


def short_date(value: str | None, now: datetime | None = None) -> str:
    """Date as "Oct 9, 15:00" this year and "Oct 9, 2025" in other years."""
    stamp = parse_utc(value)
    if stamp is None:
        return t("no_date")
    local = stamp.astimezone()
    day_month = _day_month(local)
    if local.year != _now(now).year:
        return t("date_other_year", day_month=day_month, year=local.year)
    return t("date_this_year", day_month=day_month, time=f"{local:%H:%M}")


def when(value: str | None, now: datetime | None = None) -> str:
    """Short event time: "14:05" today, "yesterday, 09:30", otherwise as `short_date`."""
    stamp = parse_utc(value)
    if stamp is None:
        return ""
    local = stamp.astimezone()
    today = _now(now).date()
    if local.date() == today:
        return f"{local:%H:%M}"
    if local.date() == today - timedelta(days=1):
        return t("yesterday_at", time=f"{local:%H:%M}")
    return short_date(value, now)


def _minutes(total: int) -> str:
    hours, minutes = divmod(total, 60)
    if hours:
        return t("dur_h_min", h=hours, m=minutes)
    return t("dur_min", m=minutes)


def elapsed(since: str | None, now: datetime | None = None) -> str:
    """Time passed: "less than a minute", "4 min", "1 h 05 min"."""
    stamp = parse_utc(since)
    if stamp is None:
        return ""
    minutes = max(int((_now(now) - stamp).total_seconds()) // 60, 0)
    if minutes < 1:
        return t("less_than_minute")
    return _minutes(minutes)


def local_input_value(value: str | None) -> str:
    """Value for `<input type="datetime-local">`."""
    stamp = parse_utc(value)
    return stamp.astimezone().strftime("%Y-%m-%dT%H:%M") if stamp else ""


def from_local_input(value: str) -> str | None:
    """Back from `datetime-local` (local time) to UTC ISO. ValueError on garbage and on
    dates at the edge of the range (year 1 or 9999) that don't convert to UTC or back
    to local time for display."""
    value = value.strip()
    if not value:
        return None
    stamp = datetime.fromisoformat(value)
    try:
        if stamp.tzinfo is None:
            stamp = stamp.astimezone()  # taken as local time
        utc = stamp.astimezone(UTC)
        utc.astimezone()  # the page shows the date in local time
    except (OverflowError, OSError) as exc:
        raise ValueError("date out of range") from exc
    return utc.isoformat(timespec="seconds")


def human_duration(value: float | None) -> str:
    """Duration as "1 h 12 min", "45 min", "6 s". Empty if unknown."""
    if not value:
        return ""
    total = int(value)
    if total < 60:
        return t("dur_s", s=total)
    return _minutes(total // 60)


def clock(seconds: float, hours: bool = False) -> str:
    """Segment timestamp: "12:04", or "1:12:04" when the recording is over an hour."""
    total = int(max(seconds, 0.0))
    h, rest = divmod(total, 3600)
    if hours or h:
        return f"{h}:{rest // 60:02d}:{rest % 60:02d}"
    return f"{rest // 60:02d}:{rest % 60:02d}"


def status_label(status: str, stage: str | None = None) -> str:
    """Status chip text. While processing it names the stage, not a generic "processing"."""
    if status == "processing":
        return t(_STAGES.get(stage or "", "status_processing"))
    key = _STATUSES.get(status)
    return t(key) if key else status


def error_text(code: str | None) -> str:
    """What happened and what to do. The code is shown separately next to it."""
    return t(_ERRORS.get(code or "", "err_unknown"))


def file_error(code: str) -> str:
    """Reply to an unsuitable file when adding it: what happened, what to do, the code."""
    return f"{error_text(code)} {t('error_code', code=code)}"


def speaker_classes(labels: Iterable[str]) -> dict[str, str]:
    """Label -> color class `spk-N` in order of first appearance, cycling through eight."""
    classes: dict[str, str] = {}
    for label in labels:
        if label not in classes:
            classes[label] = f"spk-{len(classes) % SPEAKER_COLORS + 1}"
    return classes


def title_from_filename(name: str) -> str:
    """Default title from a file name: no folders, extension or control characters.
    The name is only ever used as text, never as a path."""
    base = re.split(r"[/\\]", name)[-1]
    stem, dot, _ = base.rpartition(".")
    if dot and stem:
        base = stem
    clean = "".join(ch for ch in base if unicodedata.category(ch)[0] != "C")
    return " ".join(clean.split())[:300] or t("untitled")


def highlight(text: str, query: str) -> Markup:
    """Escaped text with the query words (by prefix, ignoring case and "ё") wrapped in
    `<mark>`."""
    terms: Sequence[str] = _WORD.findall(search_form(query))
    if not terms:
        return escape(text)
    parts: list[Markup] = []
    last = 0
    for match in _WORD.finditer(text):
        word = search_form(match.group())
        if any(word.startswith(term) for term in terms):
            parts.append(escape(text[last : match.start()]))
            parts.append(Markup("<mark>{}</mark>").format(match.group()))
            last = match.end()
    parts.append(escape(text[last:]))
    return Markup("").join(parts)
