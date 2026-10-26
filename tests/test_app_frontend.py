"""Web front of the app window: setup, settings, app-mode upload page, strings, CSP.

Templates are rendered with a Jinja environment built like the one in `web/app.py`, so
these tests don't depend on the backend routes. All data is synthetic.
"""

from __future__ import annotations

import html as html_lib
import re
import string
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from transcribator import i18n
from transcribator.web import display, strings

WEB = Path(strings.__file__).parent
TEMPLATES = WEB / "templates"
CSRF = "test-csrf-token"
GB = 1_000_000_000
MB = 1_000_000


@pytest.fixture(params=["en", "ru"])
def lang(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Interface language for one test, independent of the environment and settings."""
    value: str = request.param
    monkeypatch.setattr(i18n, "ui_lang", lambda: value)
    monkeypatch.setattr(strings, "ui_lang", lambda: value)
    yield value


def make_env(*, app_mode: bool) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["human_date"] = display.human_date
    env.filters["short_date"] = display.short_date
    env.filters["when"] = display.when
    env.filters["elapsed"] = display.elapsed
    env.filters["human_duration"] = display.human_duration
    env.filters["clock"] = display.clock
    env.filters["local_input"] = display.local_input_value
    env.globals["status_label"] = display.status_label
    env.globals["error_text"] = display.error_text
    env.globals["plural"] = strings.plural
    env.globals["t"] = strings.t
    env.globals["language_name"] = strings.language_name
    env.globals["recording_langs"] = i18n.RECORDING_LANGS
    env.globals["csrf"] = CSRF
    env.globals["app_mode"] = app_mode
    return env


def render(name: str, *, app_mode: bool = True, **ctx: Any) -> str:
    ctx.setdefault("ui_lang", i18n.ui_lang())
    return make_env(app_mode=app_mode).get_template(name).render(**ctx)


def setup_state(phase: str, done: int = 0, total: int = 625 * MB, error: str | None = None) -> Any:
    return {"phase": phase, "done_bytes": done, "total_bytes": total, "error": error}


def setup_page(state: dict[str, Any], free: int = 50 * GB) -> str:
    return render("setup.html", setup=state, total_bytes=state["total_bytes"], free_bytes=free)


def status_fragment(state: dict[str, Any], free: int = 50 * GB) -> str:
    return render(
        "_setup_status.html", setup=state, total_bytes=state["total_bytes"], free_bytes=free
    )


def visible_text(html: str) -> str:
    return html_lib.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)))


# --- strings ----------------------------------------------------------------------------


def _fields(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def test_every_string_has_en_and_ru_with_same_placeholders() -> None:
    for key, variants in strings.STRINGS.items():
        assert set(variants) == {"en", "ru"}, key
        assert all(text.strip() for text in variants.values()), key
        assert _fields(variants["en"]) == _fields(variants["ru"]), key


def test_russian_strings_are_translated() -> None:
    # Keys that are the same in both languages on purpose.
    same = {"lang_native_en", "lang_native_ru", "decimal_sep", "error_code"}
    new_keys = [
        k for k in strings.STRINGS if k.startswith(("setup_", "settings_", "js_", "tour_", "rec_"))
    ]
    assert new_keys
    for key in new_keys:
        if key in same:
            continue
        assert re.search(r"[А-Яа-яЁё]", strings.STRINGS[key]["ru"]), key


def test_strings_follow_the_writing_rules() -> None:
    # Plain copy: no semicolons in Russian sentences, no exclamations, no stray double
    # spaces, no "we", no marketing words, and only the current product name and paths.
    for key, variants in strings.STRINGS.items():
        for code, text in variants.items():
            assert "!" not in text and "  " not in text, (key, code)
            assert text == text.strip(), (key, code)
            assert text.count("—") <= 1, (key, code)
            for old in ("Transcribator", "Транскрибатор", "Transcribator App"):
                assert old not in text, (key, code)
            if code == "ru":
                assert ";" not in text, key
                assert not re.search(r"\b(мы|нас|нам|наш\w*|упс)\b", text, re.I), key
            else:
                assert not re.search(r"\b(we|us|our|oops)\b", text, re.I), key
                assert not re.search(r"seamless|powerful|robust|effortless", text, re.I), key
    assert strings.STRINGS["app_name"] == {"en": "LocTranscriptor", "ru": "LocTranscriptor"}


# --- sizes ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "en", "ru"),
    [
        (0, "0 MB", "0 МБ"),
        (5_300_000, "5.3 MB", "5,3 МБ"),
        (625 * MB, "625 MB", "625 МБ"),
        (1_234_000_000, "1.2 GB", "1,2 ГБ"),
        (107_040_000_000, "107 GB", "107 ГБ"),
    ],
)
def test_size_macro(lang: str, n: int, en: str, ru: str) -> None:
    template = make_env(app_mode=True).from_string(
        '{% import "_macros.html" as m %}{{ m.size(n) }}'
    )
    assert template.render(n=n) == (en if lang == "en" else ru)


# --- setup ------------------------------------------------------------------------------


def test_setup_idle_explains_network_and_privacy(lang: str) -> None:
    html = setup_page(setup_state("idle"), free=50 * GB)
    text = visible_text(html)
    assert "huggingface.co" in text
    assert ("625 MB" if lang == "en" else "625 МБ") in text
    assert ("50 GB" if lang == "en" else "50 ГБ") in text
    assert strings.t("privacy_local") in text
    assert strings.t("setup_point_network") in text
    assert 'hx-post="/setup/start"' in html
    assert strings.t("setup_download") in text
    # Idle doesn't poll: nothing happens until the button is pressed.
    assert "hx-get" not in html
    assert "http://" not in html and "https://" not in html


def test_setup_idle_after_partial_download_offers_resume(lang: str) -> None:
    html = status_fragment(setup_state("idle", done=125 * MB))
    assert strings.t("setup_resume") in html
    assert 'value="20"' in html


def test_setup_running_polls_and_shows_progress(lang: str) -> None:
    html = status_fragment(setup_state("running", done=313_000_000))
    assert 'hx-get="/setup/status"' in html and 'hx-trigger="every 1s"' in html
    assert 'id="setup-status"' in html and 'hx-swap="outerHTML"' in html
    assert "data-poll" in html
    assert 'value="50"' in html and "50%" in html
    expected = "313 MB of 625 MB" if lang == "en" else "313 МБ из 625 МБ"
    assert expected in visible_text(html)
    assert "hx-post" not in html


def test_setup_verifying_polls_full_bar(lang: str) -> None:
    html = status_fragment(setup_state("verifying", done=625 * MB))
    assert 'hx-get="/setup/status"' in html
    assert strings.t("setup_verifying") in html
    assert 'value="100"' in html


def test_setup_done_stops_polling(lang: str) -> None:
    html = status_fragment(setup_state("done", done=625 * MB))
    assert "hx-get" not in html
    assert strings.t("setup_done") in html
    assert 'href="/"' in html


@pytest.mark.parametrize(
    "code", ["no_space", "offline", "server_error", "checksum", "write_failed", None, "odd"]
)
def test_setup_errors_have_text_and_continue(lang: str, code: str | None) -> None:
    html = status_fragment(setup_state("error", done=100 * MB, error=code), free=200 * MB)
    text = visible_text(html)
    assert 'hx-post="/setup/start"' in html
    assert strings.t("setup_continue") in text
    assert "hx-get" not in html
    assert "{" not in text and "}" not in text
    if code in ("offline", "server_error", "checksum", "write_failed"):
        assert strings.t(f"setup_err_{code}") in text
    elif code != "no_space":
        assert strings.t("setup_err_unknown") in text
    assert strings.t("setup_error_code", code=code or "unknown") in text


def test_setup_no_space_names_needed_and_free(lang: str) -> None:
    html = status_fragment(setup_state("error", done=125 * MB, error="no_space"), free=200 * MB)
    text = visible_text(html)
    if lang == "en":
        assert "need 500 MB more, and 200 MB is free" in text
    else:
        assert "Нужно ещё 500 МБ, свободно 200 МБ" in text


def test_setup_status_fragment_is_self_contained() -> None:
    # The fragment is swapped in alone: it must not extend the page frame.
    html = status_fragment(setup_state("running", done=1))
    assert "<html" not in html and "<nav" not in html
    assert html.strip().startswith('<div id="setup-status"')


# --- settings ---------------------------------------------------------------------------


def settings_page(app_mode: bool = True, ui: str = "ru", rec: str = "en") -> str:
    return render(
        "settings.html",
        app_mode=app_mode,
        ui_lang=ui,
        recording_lang=rec,
        ui_langs=i18n.UI_LANGS,
        recording_langs=i18n.RECORDING_LANGS,
        data_dir="/Users/test/Library/Application Support/LocTranscriptor",
        log_dir="~/Library/Logs/LocTranscriptor",
        nav="settings",
    )


def test_settings_form_and_selection(lang: str) -> None:
    html = settings_page(ui="ru", rec="en")
    assert 'hx-post="/settings"' in html and 'hx-trigger="change"' in html
    assert f'<input type="hidden" name="csrf" value="{CSRF}">' in html
    assert re.search(r'<option value="ru" lang="ru" selected>Русский</option>', html)
    assert re.search(r'<option value="en" lang="en">English</option>', html)
    assert re.search(r'name="recording_lang".*?<option value="en" selected>', html, re.S)
    assert 'name="ui_lang"' in html and 'name="recording_lang"' in html
    assert "Application Support/LocTranscriptor" in html
    assert "~/Library/Logs/LocTranscriptor" in html
    assert strings.t("privacy_local") in visible_text(html)
    assert 'data-bridge="revealData"' in html
    # Uninstalling is described in the README: Settings has no delete button or section.
    assert "/delete" not in html
    assert strings.t("settings_data_sources") in visible_text(html)
    assert 'aria-current="page">' + strings.t("nav_settings") in html


def test_settings_without_app_has_no_finder_button() -> None:
    html = settings_page(app_mode=False)
    assert 'data-bridge="revealData"' not in html


def test_data_dir_is_escaped() -> None:
    html = render(
        "settings.html",
        ui_lang="en",
        recording_lang="en",
        ui_langs=i18n.UI_LANGS,
        recording_langs=i18n.RECORDING_LANGS,
        data_dir='/Users/test/<script>"x"',
    )
    assert "<script>" not in html and "&lt;script&gt;" in html


# --- upload page and frame --------------------------------------------------------------


def index_page(app_mode: bool) -> str:
    return render(
        "index.html",
        app_mode=app_mode,
        nav="add",
        default_lang="en",
        items=[],
        started={},
        active=False,
        languages={},
    )


def test_index_in_app_uses_native_picker(lang: str) -> None:
    html = index_page(app_mode=True)
    assert "data-native-pick" in html
    assert 'hx-post="/pick"' not in html
    assert strings.t("drop_note_app") in html
    assert strings.t("drop_note") not in html
    assert "data-msg-add-failed=" in html and "data-msg-no-bridge=" in html
    assert 'id="drop-adding"' in html
    assert 'href="/settings"' in html
    # The note links to the Settings section that explains which files get copied.
    assert 'href="/settings#data-title">' + strings.t("drop_note_more") + "</a>" in html


def test_paths_are_shown_relative_to_home() -> None:
    from transcribator.web.app import home_relative

    home = Path.home()
    assert home_relative(home / "Library/Application Support/LocTranscriptor") == (
        "~/Library/Application Support/LocTranscriptor"
    )
    assert home_relative(Path("/Volumes/Data/x")) == "/Volumes/Data/x"
    assert home_relative(home) == "~"


def test_index_in_browser_is_unchanged(lang: str) -> None:
    html = index_page(app_mode=False)
    assert 'hx-post="/pick"' in html
    assert "data-native-pick" not in html
    assert strings.t("drop_note") in html
    assert "data-msg-add-failed" not in html and "drop-adding" not in html
    assert 'href="/settings"' not in html
    assert 'data-msg-uploading-n="' + strings.t("uploading_n") + '">' in html


def test_settings_link_only_in_app_mode() -> None:
    for name in ("meetings.html",):
        ctx: dict[str, Any] = {"items": [], "q": "", "groups": [], "nav": "list"}
        assert 'href="/settings"' in render(name, app_mode=True, **ctx)
        assert 'href="/settings"' not in render(name, app_mode=False, **ctx)


# --- CSP: no inline code anywhere ------------------------------------------------------


def test_templates_have_no_inline_scripts_styles_or_handlers() -> None:
    for template in TEMPLATES.glob("*.html"):
        text = template.read_text()
        assert "<style" not in text and "style=" not in text, template.name
        assert "hx-on" not in text, template.name
        assert re.search(r"\son[a-z]+\s*=", text) is None, template.name
        assert re.search(r"<script(?![^>]*\ssrc=\"/static/)", text) is None, template.name
        assert "javascript:" not in text, template.name


def test_new_pages_render_without_inline_code(lang: str) -> None:
    pages = [
        setup_page(setup_state("running", done=1)),
        settings_page(),
        index_page(app_mode=True),
    ]
    for html in pages:
        assert html.count("<script") == html.count('<script src="/static/') == 2
        assert "style=" not in html
        assert "http://" not in html and "https://" not in html


def test_app_js_bridge_is_csp_safe() -> None:
    script = (WEB / "static" / "app.js").read_text()
    assert "window.transcribatorDrop = function" in script
    assert "messageHandlers?.transcribator" in script
    assert 'fetch("/enqueue"' in script
    assert re.search(r"[А-Яа-яЁё]", script) is None
    for forbidden in ("eval(", "new Function", "innerHTML", "insertAdjacentHTML", 'setTimeout("'):
        assert forbidden not in script, forbidden


# --- frame: header, titles, tour hooks --------------------------------------------------


def _header(html: str) -> str:
    match = re.search(r'<header class="nav">(.*?)</header>', html, re.S)
    assert match is not None
    return match.group(1)


def _title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, re.S)
    assert match is not None
    return html_lib.unescape(match.group(1))


def meetings_page(app_mode: bool) -> str:
    return render("meetings.html", app_mode=app_mode, items=[], q="", groups=[], nav="list")


@pytest.mark.parametrize("app_mode", [True, False])
def test_header_has_no_brand(lang: str, app_mode: bool) -> None:
    # The window title (app) or the tab title (browser) names the product.
    for html in (index_page(app_mode), meetings_page(app_mode)):
        header = _header(html)
        assert strings.t("app_name") not in header
        assert 'class="brand"' not in header
        assert strings.t("nav_upload") in header and strings.t("nav_archive") in header


def test_title_is_page_name_in_app_and_named_in_browser(lang: str) -> None:
    page = strings.t("title_archive")
    assert _title(meetings_page(app_mode=True)) == page
    assert _title(meetings_page(app_mode=False)) == f"{page} · {strings.t('app_name')}"
    assert _title(settings_page()) == strings.t("title_settings")
    assert _title(setup_page(setup_state("idle"))) == strings.t("title_setup")


def test_current_page_is_marked_once(lang: str) -> None:
    pages = {
        "/": index_page(app_mode=True),
        "/meetings": meetings_page(app_mode=True),
        "/settings": settings_page(),
    }
    for href, html in pages.items():
        header = _header(html)
        assert header.count('aria-current="page"') == 1
        assert re.search(rf'<a href="{re.escape(href)}"[^>]*aria-current="page"', header), href


def test_tour_hooks(lang: str) -> None:
    app = index_page(app_mode=True)
    for hook in ("drop", "record", "language", "queue", "archive", "settings"):
        assert app.count(f'data-tour="{hook}"') == 1, hook
    assert re.search(r'<section id="drop"[^>]*data-tour="drop"', app)
    assert re.search(r'data-tour="language">.*?<select id="language"', app, re.S)
    assert re.search(r'<a href="/meetings" data-tour="archive"', app)
    assert re.search(r'<a href="/settings" data-tour="settings"', app)
    browser = index_page(app_mode=False)
    assert 'data-tour="settings"' not in browser and 'data-tour="record"' not in browser
    assert 'data-tour="drop"' in browser and 'data-tour="language"' in browser


# --- select enhancement ----------------------------------------------------------------


def test_selects_stay_native_in_markup(lang: str) -> None:
    # Forms, hx-include and no-script use keep working on the real <select>.
    assert '<select id="language" class="select" name="language">' in index_page(app_mode=True)
    html = settings_page()
    assert '<select id="ui_lang" class="select" name="ui_lang">' in html
    assert 'role="combobox"' not in html and 'role="listbox"' not in html


def test_app_js_select_enhancement_contract() -> None:
    script = (WEB / "static" / "app.js").read_text()
    for marker in (
        'setAttribute("role", "combobox")',
        'setAttribute("role", "listbox")',
        'setAttribute("role", "option")',
        'setAttribute("aria-haspopup", "listbox")',
        '"aria-activedescendant"',
        '"aria-expanded"',
        '"aria-selected"',
        'select.dispatchEvent(new Event("change", { bubbles: true }))',
        "select.hidden = true",
        "showPopover",
        "typeahead",
        '"Escape"',
        '"Tab"',
        '"Home"',
        '"End"',
        '("select.select")',
    ):
        assert marker in script, marker


# --- vendored palette and icons ---------------------------------------------------------


VENDOR = WEB / "static" / "vendor" / "radix-colors"


def test_radix_colors_are_vendored_with_license(lang: str) -> None:
    license_text = (VENDOR / "LICENSE").read_text()
    assert "MIT License" in license_text and "Radix" in license_text
    readme = (VENDOR / "README.md").read_text()
    assert "@radix-ui/colors` 3.0.0" in readme
    assert re.search(r"sha256: `[0-9a-f]{64}`", readme)
    html = index_page(app_mode=True)
    pattern = r'<link rel="stylesheet" href="/static/vendor/radix-colors/([\w-]+\.css)"'
    linked = re.findall(pattern, html)
    assert linked and len(linked) == len(set(linked))
    for name in linked:
        assert (VENDOR / name).is_file(), name
    # Light scales always, dark ones only under the dark appearance.
    dark = re.findall(r'radix-colors/([\w-]+\.css)" media="\(prefers-color-scheme: dark\)"', html)
    assert dark and all("-dark" in name for name in dark)
    assert all(name in dark for name in linked if "-dark" in name)
    assert html.index("radix-colors/") < html.index('href="/static/app.css"')


def test_css_uses_only_local_resources() -> None:
    css = (WEB / "static" / "app.css").read_text()
    assert "@import" not in css
    urls = re.findall(r'url\("([^"]+)"\)', css)
    assert urls
    for url in urls:
        assert url.startswith("/static/icons/"), url
        assert (WEB / url.removeprefix("/")).is_file(), url
    for name in VENDOR.glob("*.css"):
        assert "url(" not in name.read_text() and "@import" not in name.read_text(), name


# --- first-run tips ---------------------------------------------------------------------


TOUR_KEYS = [k for k in strings.STRINGS if k.startswith("tour_")]


def tour_index(app_mode: bool, tour_auto: bool = False) -> str:
    return render(
        "index.html",
        app_mode=app_mode,
        nav="add",
        default_lang="en",
        items=[],
        started={},
        active=False,
        languages={},
        tour_auto=tour_auto,
    )


def _steps(html: str) -> list[dict[str, str]]:
    block = re.search(r'<ol class="tour-steps" hidden>(.*?)</ol>', html, re.S)
    assert block is not None
    items = re.findall(r"<li ([^>]*)></li>", block.group(1))
    return [
        {k: html_lib.unescape(v) for k, v in re.findall(r'data-([a-z]+)="([^"]*)"', item)}
        for item in items
    ]


def test_tour_strings_are_plain(lang: str) -> None:
    assert len(TOUR_KEYS) >= 12
    for key in TOUR_KEYS:
        for code, text in strings.STRINGS[key].items():
            assert "!" not in text and "—" not in text, (key, code)
            if code == "ru":
                assert ";" not in text, key
                assert not re.search(r"\b(мы|нас|нам|упс)\b", text, re.I), key
                assert re.search(r"[А-Яа-яЁё]", text), key
            else:
                assert not re.search(r"\b(we|us|our|oops)\b", text, re.I), key


def test_tour_markup_in_app(lang: str) -> None:
    html = tour_index(app_mode=True, tour_auto=True)
    assert 'id="tour" class="tour" hidden' in html
    assert re.search(
        r'role="dialog" aria-modal="true" aria-labelledby="tour-title"'
        r' aria-describedby="tour-body"',
        html,
    )
    assert 'id="tour-live" class="visually-hidden" aria-live="polite"' in html
    # The tips sit outside <main>, which is made inert while they are open.
    assert html.index("</main>") < html.index('id="tour"')
    assert "data-autostart" in html and "data-remember" in html
    assert f'data-count="{strings.t("tour_count")}"' in html
    assert "{n}" in strings.t("tour_count") and "{m}" in strings.t("tour_count")
    assert f'data-next="{strings.t("tour_next")}"' in html
    assert f'data-done="{strings.t("tour_done")}"' in html
    for action, key in (("skip", "tour_skip"), ("back", "tour_back"), ("next", "tour_next")):
        assert re.search(rf'data-tour-action="{action}">{re.escape(strings.t(key))}</button>', html)
    steps = _steps(html)
    assert [s["target"] for s in steps] == ["drop", "record", "language", "queue", "archive"]
    for step in steps:
        assert html.count(f'data-tour="{step["target"]}"') == 1
    assert steps[0]["title"] == strings.t("tour_drop_title")
    # The recording step is optional: app.js drops it while the block is hidden.
    assert re.search(r'<li data-target="record" data-optional ', html)
    assert steps[1]["title"] == strings.t("tour_record_title")
    assert steps[1]["body"] == strings.t("tour_record_body")
    assert steps[3]["body"] == strings.t("tour_queue_body_app")
    assert steps[4]["also"] == "settings"
    assert steps[4]["body"] == strings.t("tour_archive_body") + " " + strings.t(
        "tour_archive_settings"
    )
    assert html.count('<span class="tour-dots"') == 1
    dots = re.search(r'<span class="tour-dots"[^>]*>(.*?)</span></span>', html)
    assert dots is not None and dots.group(1).count("<span>") >= len(steps)


def test_tour_markup_in_browser(lang: str) -> None:
    html = tour_index(app_mode=False, tour_auto=True)
    assert 'id="tour"' in html
    assert "data-remember" not in html
    steps = _steps(html)
    assert [s["target"] for s in steps] == ["drop", "language", "queue", "archive"]
    assert steps[2]["body"] == strings.t("tour_queue_body")
    assert "also" not in steps[3]
    assert steps[3]["body"] == strings.t("tour_archive_body")


def test_tour_autostarts_only_when_asked(lang: str) -> None:
    assert "data-autostart" not in tour_index(app_mode=True, tour_auto=False)
    assert "data-autostart" not in tour_index(app_mode=True)


def test_app_js_tour_contract() -> None:
    script = (WEB / "static" / "app.js").read_text()
    for marker in (
        "window.transcribatorTour = {",
        "start: tour.start",
        'fetch("/tour/seen"',
        '"X-CSRF-Token": csrfToken()',
        "el.inert = true",
        "el.inert = false",
        '"Escape"',
        '"ArrowRight"',
        '"ArrowLeft"',
        '"Tab"',
        "returnTo.focus(",
        'byId("tour-live")',
        'dataset.count || ""',
        'querySelector("dialog[open]")',
        "ResizeObserver",
        "reducedMotion()",
    ):
        assert marker in script, marker
    assert re.search(r"[А-Яа-яЁё]", script) is None
    assert ".style.cssText" not in script and 'setAttribute("style"' not in script


def test_tour_css_respects_reduced_motion() -> None:
    css = (WEB / "static" / "app.css").read_text()
    assert ".tour-spot" in css and ".tour-card" in css and ".tour-arrow" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert "transition-duration: 0.01ms" in reduced and ".tour-card.is-changing" in reduced


# --- record block (app) ------------------------------------------------------------------

REC_ERRORS = [
    "already_recording",
    "mic_denied",
    "no_source",
    "source_gone",
    "start_failed",
    "disk_low",
    "finalize_failed",
    "enqueue_failed",
    "max_duration",
]
REC_EARLY = ["source_gone", "mic_denied", "disk_low", "max_duration", "unknown"]
REC_WARNINGS = ["silent_system", "silent_mic", "device_changed", "disk_low"]


def _rec_block(html: str) -> str:
    match = re.search(r'<section id="rec".*?</section>', html, re.S)
    assert match is not None
    return match.group(0)


def test_record_block_is_in_app_mode_only(lang: str) -> None:
    html = index_page(app_mode=True)
    block = _rec_block(html)
    # Hidden until app.js finds the native bridge.
    assert re.match(r"<section id=\"rec\" class=\"rec\"[^>]*\shidden>", block)
    assert 'data-tour="record"' in block
    text = visible_text(block)
    for key in ("rec_title", "rec_hint", "rec_source_label", "rec_source_all", "rec_mic_label"):
        assert strings.t(key) in text, key
    for key in ("rec_start", "rec_stop", "rec_mic_hint", "rec_level_mic", "rec_level_system"):
        assert strings.t(key) in text, key
    # Source select with its label, microphone switch, device select, language is the shared one.
    assert re.search(r'<label[^>]*for="rec-source"', block)
    assert re.search(r'<select id="rec-source" class="select"', block)
    assert re.search(
        r'<button type="button" class="switch" id="rec-mic" role="switch" aria-checked="false"',
        block,
    )
    assert re.search(r'<select id="rec-device" class="select">', block)
    assert 'id="rec-start"' in block and 'id="rec-stop"' in block
    assert re.search(r'id="rec-stop"[^>]*\shidden>', block)
    assert 'id="rec-time"' in block and 'role="timer"' in block
    assert (
        'id="rec-notice" aria-live="polite"' in block
        and 'id="rec-result" aria-live="polite"' in block
    )
    assert 'id="rec-announce" class="visually-hidden" aria-live="polite"' in block
    assert block.count('id="language"') == 0 and html.count('id="language"') == 1
    # The block sits between the drop zone and the language row.
    assert html.index('id="drop"') < html.index('id="rec"') < html.index('class="drop-lang"')
    # The browser install has none of it.
    browser = index_page(app_mode=False)
    assert 'id="rec"' not in browser and "rec-" not in browser
    assert strings.t("rec_start") not in browser


def test_record_block_has_every_error_and_warning_text(lang: str) -> None:
    block = _rec_block(index_page(app_mode=True))
    items = {
        (kind, code): html_lib.unescape(text)
        for kind, code, text in re.findall(
            r'<li data-kind="(\w+)" data-code="(\w+)"[^>]*>(.*?)</li>', block, re.S
        )
    }
    for code in [*REC_ERRORS, "unknown", "bridge"]:
        assert items[("err", code)] == strings.t(f"rec_err_{code}"), code
    for code in REC_WARNINGS:
        assert items[("warn", code)] == strings.t(f"rec_warn_{code}"), code
    for code in REC_EARLY:
        assert items[("early", code)] == strings.t(f"rec_early_{code}"), code
    assert f'data-ended-early="{html_lib.escape(strings.t("rec_ended_early"))}"' in block
    # The fixes that live in System Settings come with a button.
    assert re.search(r'data-code="mic_denied" data-pane="mic"', block)
    assert re.search(r'data-code="silent_system" data-pane="systemAudio"', block)
    for attr, key in (
        ("open-settings", "rec_open_settings"),
        ("open-meeting", "rec_open_meeting"),
        ("queued", "rec_queued"),
        ("starting", "rec_state_starting"),
        ("recording", "rec_state_recording"),
        ("stopping", "rec_state_stopping"),
        ("with-mic", "rec_with_mic"),
    ):
        assert f'data-{attr}="{strings.t(key)}"' in block, attr


def test_record_strings_exist_in_both_languages() -> None:
    keys = [k for k in strings.STRINGS if k.startswith("rec_")]
    assert len(keys) >= 30
    for code in REC_ERRORS + ["unknown", "bridge"]:
        assert f"rec_err_{code}" in strings.STRINGS, code
    for code in REC_EARLY:
        assert f"rec_early_{code}" in strings.STRINGS, code
    for code in REC_WARNINGS:
        assert f"rec_warn_{code}" in strings.STRINGS, code
    for key in keys:
        assert set(strings.STRINGS[key]) == {"en", "ru"}, key
        assert re.search(r"[А-Яа-яЁё]", strings.STRINGS[key]["ru"]), key
        assert not re.search(r"[А-Яа-яЁё]", strings.STRINGS[key]["en"]), key
        # One sentence per message line plus at most one more, no line breaks, no dashes.
        for text in strings.STRINGS[key].values():
            assert "\n" not in text and "—" not in text and "\u2013" not in text, key


def test_record_early_end_and_cancel_texts(lang: str) -> None:
    assert "{reason}" in strings.STRINGS["rec_ended_early"]["en"]
    assert "{reason}" in strings.STRINGS["rec_ended_early"]["ru"]
    assert "12" in strings.t("rec_err_max_duration") and "12" in strings.t("rec_early_max_duration")
    # A pinned app that never played anything: the text says so, not "the app quit".
    assert "sound" in strings.STRINGS["rec_err_source_gone"]["en"]
    assert "звука" in strings.STRINGS["rec_err_source_gone"]["ru"]
    assert "starts playing sound" in strings.STRINGS["rec_starting_app_hint"]["en"]
    assert strings.STRINGS["rec_cancel"] == {"en": "Cancel", "ru": "Отменить"}
    block = _rec_block(index_page(app_mode=True))
    assert re.search(
        r'id="rec-stop-label" data-stop="[^"]+" data-cancel="' + strings.t("rec_cancel") + '"',
        block,
    )
    assert 'data-any="' in block and 'data-app="' in block


def test_record_buttons_are_verbs() -> None:
    assert strings.STRINGS["rec_start"]["ru"] == "Начать запись"
    assert strings.STRINGS["rec_stop"]["ru"] == "Остановить"


def test_record_block_has_no_inline_code(lang: str) -> None:
    block = _rec_block(index_page(app_mode=True))
    assert "style=" not in block and "<script" not in block
    assert re.search(r"\son[a-z]+\s*=", block) is None
    assert "http://" not in block and "https://" not in block


def test_app_js_recorder_contract() -> None:
    script = (WEB / "static" / "app.js").read_text()
    for marker in (
        "window.transcribatorRecorder = { onStatus: recorder.onStatus }",
        'type: "recording.status"',
        'type: "recording.sources"',
        'type: "recording.start"',
        'type: "recording.stop"',
        'type: "recording.openSettings"',
        'describe("early", early)',
        "texts.endedEarly",
        "ui.stopLabel.dataset.cancel",
        "ui.startingHint.dataset.app",
        "cancelRequested",
        "window.webkit?.messageHandlers?.transcribator",
        'style.setProperty("--level"',
        'new Event("options-changed")',
        'new Event("combo-open")',
        "textContent",
    ):
        assert marker in script, marker
    # The hook is defined before the first status call is made.
    assert script.index("window.transcribatorRecorder =") < script.index("recorder.init();")
    # Warnings and results are built from text nodes, never from markup.
    assert ".style.cssText" not in script and 'setAttribute("style"' not in script


def test_record_css_has_dark_theme_focus_and_reduced_motion() -> None:
    css = (WEB / "static" / "app.css").read_text()
    for selector in (".rec {", ".switch {", ".rec-meter-fill {", ".msg-warn {", ".rec-time {"):
        assert selector in css, selector
    assert "var(--level, 0)" in css
    # Only palette tokens and no gradients in the new block; the switch thumb is plain white.
    block = css[
        css.index("/* --- record block (app)") : css.index("progress {\n  -webkit-appearance")
    ]
    assert "gradient" not in block
    assert re.findall(r"#[0-9a-fA-F]{3,8}\b", block) == ["#fff"]
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert ".rec-meter-fill" in reduced and ".switch" in reduced
    for icon in ("record", "stop", "mic"):
        assert f'.i-{icon} {{ --icon: url("/static/icons/{icon}.svg"); }}' in css
        assert (WEB / "static" / "icons" / f"{icon}.svg").is_file()


# --- microphone speaker ------------------------------------------------------------------


def test_me_speaker_placeholder_is_not_numbered(lang: str) -> None:
    speakers = [
        {"label": "ME", "shown": "Me", "name": "", "index": "", "spk": "spk-1"},
        {"label": "S1", "shown": "Speaker 1", "name": "", "index": "1", "spk": "spk-2"},
        {"label": "S2", "shown": "Speaker 2", "name": "", "index": "2", "spk": "spk-3"},
    ]
    meeting = SimpleNamespace(id=7, status="done", title="T", recorded_at=None, duration_s=60)
    html = render(
        "meeting.html",
        meeting=meeting,
        speakers=speakers,
        segments=[],
        stage=None,
        started=None,
        language=None,
        long=False,
        nav="list",
    )
    placeholders = re.findall(r'class="inline inline-speaker"[^>]*placeholder="([^"]*)"', html)
    assert placeholders == [
        strings.t("speaker_me"),
        strings.t("speaker_n", n=1),
        strings.t("speaker_n", n=2),
    ]
    assert strings.t("speaker_me") == ("Me" if lang == "en" else "Я")
    assert len(set(re.findall(r'id="(spk-form-[^"]*)"', html))) == 3


# --- keep the audio (app) ----------------------------------------------------------------


def _meeting_page(**extra: Any) -> str:
    meeting = SimpleNamespace(id=7, status="done", title="T", recorded_at=None, duration_s=60)
    return render(
        "meeting.html",
        meeting=meeting,
        speakers=[],
        segments=[],
        stage=None,
        started=None,
        language=None,
        long=False,
        nav="list",
        **extra,
    )


def test_keep_audio_switch_is_in_the_record_block_only(lang: str) -> None:
    block = _rec_block(index_page(app_mode=True))
    assert re.search(
        r'<button type="button" class="switch" id="rec-keep" role="switch" aria-checked="false"'
        r' aria-describedby="rec-keep-hint">',
        block,
    )
    assert re.search(
        r'<label[^>]*for="rec-keep"[^>]*>' + re.escape(strings.t("rec_keep_label")), block
    )
    assert re.search(r'id="rec-keep-hint">' + re.escape(strings.t("rec_keep_hint")), block)
    match = re.search(r'data-audio-not-saved="([^"]*)"', block)
    assert match and html_lib.unescape(match.group(1)) == strings.t("rec_audio_not_saved")
    # The switch starts off and sits in the setup part, which is hidden while recording.
    assert (
        block.index('id="rec-setup"')
        < block.index('id="rec-keep"')
        < block.index('id="rec-live-view"')
    )
    browser = index_page(app_mode=False)
    assert "rec-keep" not in browser and strings.t("rec_keep_label") not in browser


def test_keep_audio_strings_exist_in_both_languages() -> None:
    for key in ("rec_keep_label", "rec_keep_hint", "rec_audio_not_saved", "audio_download"):
        assert set(strings.STRINGS[key]) == {"en", "ru"}, key
        assert re.search(r"[А-Яа-яЁё]", strings.STRINGS[key]["ru"]), key
        assert not re.search(r"[А-Яа-яЁё]", strings.STRINGS[key]["en"]), key
    assert set(strings.STRINGS["delete_text_audio"]) == {"en", "ru"}


def test_app_js_keep_audio_contract() -> None:
    script = (WEB / "static" / "app.js").read_text()
    for marker in (
        "keepAudio: keep",
        "reply.last.keepAudio",
        "status.audioSaved === false",
        "texts.audioNotSaved",
        'q("rec-keep")',
        "ui.keep.disabled = active",
    ):
        assert marker in script, marker


def test_meeting_page_offers_audio_only_when_it_exists(lang: str) -> None:
    link = '<a class="btn" href="/meetings/7/audio" download>'
    with_audio = _meeting_page(has_audio=True)
    assert link in with_audio
    assert strings.t("audio_download") in visible_text(with_audio)
    assert strings.t("delete_text_audio") in visible_text(with_audio)
    for html in (_meeting_page(has_audio=False), _meeting_page()):
        assert "/audio" not in html
        assert strings.t("audio_download") not in html
        assert strings.t("delete_text") in visible_text(html)
        assert strings.t("delete_text_audio") not in visible_text(html)


def test_meeting_page_with_audio_has_no_inline_code(lang: str) -> None:
    html = _meeting_page(has_audio=True)
    assert "style=" not in html and "<script" in html
    assert re.search(r"\son[a-z]+\s*=", html) is None
