from __future__ import annotations

import pytest

from transcribator.eval import normalize_words, transcript_text, word_error_rate


def test_normalization_ignores_case_punctuation_and_yo() -> None:
    assert normalize_words("Ёлка, ЕЛКА!") == ["елка", "елка"]


def test_wer_identical_is_zero() -> None:
    assert word_error_rate("раз два три", "Раз, два, три.") == 0.0


def test_wer_counts_substitution_deletion_insertion() -> None:
    assert word_error_rate("раз два три четыре", "раз два три пять") == pytest.approx(0.25)
    assert word_error_rate("раз два три четыре", "раз два три") == pytest.approx(0.25)
    assert word_error_rate("раз два", "раз два три") == pytest.approx(0.5)


def test_wer_empty_reference() -> None:
    assert word_error_rate("", "") == 0.0
    assert word_error_rate("", "слово") == 1.0


def test_transcript_text_strips_headers_timestamps_and_speakers() -> None:
    markdown = (
        "# Название встречи\n\n"
        "Длительность: 0:00:05 · Спикеры: Спикер 1\n\n"
        "## Кратко\n\n- пункт\n\n"
        "## Транскрипт\n\n"
        "**[00:00:01] Спикер 1:** Привет всем.\n\n"
        "**[00:00:03] Спикер 2:** Да, начнём.\n"
    )
    assert transcript_text(markdown) == "Привет всем. Да, начнём."


def test_transcript_text_window_selects_only_utterances_in_range() -> None:
    markdown = (
        "## Транскрипт\n\n"
        "**[00:00:10] Спикер 1:** раз\n\n"
        "**[00:01:00] Спикер 2:** два\n\n"
        "**[00:03:00] Спикер 1:** три\n"
    )
    assert transcript_text(markdown, start_s=30, end_s=120) == "два"
    assert transcript_text(markdown, end_s=30) == "раз"
    assert transcript_text(markdown, start_s=100) == "три"
    assert transcript_text(markdown) == "раз два три"
