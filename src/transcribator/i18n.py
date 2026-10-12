"""Interface language and recording language.

Two separate settings:
- interface language (web UI, Markdown export labels): `TRANSCRIBATOR_UI_LANG`, `en` by
  default;
- default language of a recording (passed to the speech recognizer as a hint):
  `TRANSCRIBATOR_LANGUAGE`, `en` by default. It can be changed per meeting on the upload
  page and is stored with the meeting.

Strings live in small per-module tables `{key: {"en": ..., "ru": ...}}` and are looked up
with `pick`. A missing translation falls back to English, a missing key is a bug.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

UI_LANG_ENV = "TRANSCRIBATOR_UI_LANG"
RECORDING_LANG_ENV = "TRANSCRIBATOR_LANGUAGE"
UI_LANGS = ("en", "ru")
RECORDING_LANGS = ("en", "ru")
FALLBACK = "en"

Table = Mapping[str, Mapping[str, str]]

# Names of recording languages in each interface language.
LANGUAGE_NAMES: Table = {
    "en": {"en": "English", "ru": "Английский"},
    "ru": {"en": "Russian", "ru": "Русский"},
}


def _setting(env: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(env, "").strip().lower()
    return value if value in allowed else FALLBACK


def ui_lang() -> str:
    return _setting(UI_LANG_ENV, UI_LANGS)


def default_recording_lang() -> str:
    return _setting(RECORDING_LANG_ENV, RECORDING_LANGS)


def pick(table: Table, key: str, lang: str | None = None, **values: object) -> str:
    """String `key` from `table` in `lang` (interface language by default)."""
    variants = table[key]
    text = variants.get(lang or ui_lang()) or variants[FALLBACK]
    return text.format(**values) if values else text
