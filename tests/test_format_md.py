from __future__ import annotations

import pytest

from transcribator.models import Segment, Transcript
from transcribator.pipeline.format_md import (
    format_duration,
    format_timestamp,
    render_markdown,
    speaker_labels,
)


def make_transcript() -> Transcript:
    return Transcript(
        segments=[
            Segment(3.0, 5.0, "SPEAKER_01", "Всем привет."),
            Segment(11.4, 14.0, "SPEAKER_00", "Привет, начнём."),
            Segment(3725.9, 3730.0, "SPEAKER_01", "Это было давно."),
        ],
        duration_s=4360.0,
    )


def test_timestamps() -> None:
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(11.9) == "00:00:11"
    assert format_timestamp(3725.9) == "01:02:05"
    assert format_duration(4360) == "1:12:40"
    assert format_duration(59) == "0:00:59"


def test_speakers_are_numbered_by_first_appearance() -> None:
    assert speaker_labels(make_transcript(), lang="ru") == {
        "SPEAKER_01": "Спикер 1",
        "SPEAKER_00": "Спикер 2",
    }
    assert speaker_labels(make_transcript(), lang="en") == {
        "SPEAKER_01": "Speaker 1",
        "SPEAKER_00": "Speaker 2",
    }


def test_custom_names_override_numbering() -> None:
    labels = speaker_labels(make_transcript(), {"SPEAKER_01": "Иван"}, lang="ru")
    assert labels == {"SPEAKER_01": "Иван", "SPEAKER_00": "Спикер 2"}


def test_render_matches_spec_format() -> None:
    text = render_markdown(
        make_transcript(),
        title="Синк по релизу",
        recorded_at="2026-10-09 15:00",
        summary=["Релиз переносится на среду."],
        names={"SPEAKER_01": "Иван"},
        lang="ru",
    )
    assert text == (
        "# Синк по релизу\n"
        "\n"
        "Дата записи: 2026-10-09 15:00 · Длительность: 1:12:40 · Спикеры: Иван, Спикер 2\n"
        "\n"
        "## Кратко\n"
        "\n"
        "- Релиз переносится на среду.\n"
        "\n"
        "## Транскрипт\n"
        "\n"
        "**[00:00:03] Иван:** Всем привет.\n"
        "\n"
        "**[00:00:11] Спикер 2:** Привет, начнём.\n"
        "\n"
        "**[01:02:05] Иван:** Это было давно.\n"
    )


def test_summary_and_date_sections_are_optional() -> None:
    text = render_markdown(make_transcript(), title="Без выжимки", lang="ru")
    assert "## Кратко" not in text
    assert "Дата записи" not in text
    assert text.splitlines()[2].startswith("Длительность: 1:12:40")


def test_english_labels_follow_interface_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRANSCRIBATOR_UI_LANG", "en")
    text = render_markdown(make_transcript(), title="Release sync", recorded_at="2026-10-09")
    assert text.splitlines()[2] == (
        "Recorded: 2026-10-09 · Duration: 1:12:40 · Speakers: Speaker 1, Speaker 2"
    )
    assert "## Transcript" in text
    monkeypatch.setenv("TRANSCRIBATOR_UI_LANG", "ru")
    assert "## Транскрипт" in render_markdown(make_transcript(), title="x")
