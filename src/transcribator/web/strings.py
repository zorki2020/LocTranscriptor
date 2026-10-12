"""Every user-visible string of the web interface, in English and Russian.

One table `{key: {"en": ..., "ru": ...}}` for templates, `display.py`, `app.py`,
`security.py`, `picker.py` and `app.js` (the script gets its strings through `data-`
attributes, see `index.html`). The language is the interface language
(`TRANSCRIBATOR_UI_LANG`), read on every call.

Counted words ("3 speakers", "3 спикера") live in `PLURALS`: English has two forms,
Russian three.
"""

from __future__ import annotations

from collections.abc import Mapping

from transcribator.i18n import LANGUAGE_NAMES, Table, pick, ui_lang
from transcribator.pipeline.format_md import LABELS as MD_LABELS

STRINGS: Table = {
    # --- frame and navigation
    "app_name": {"en": "Transcribator", "ru": "Транскрибатор"},
    "skip_to_content": {"en": "Skip to content", "ru": "К содержимому"},
    "nav_label": {"en": "Sections", "ru": "Разделы"},
    "nav_upload": {"en": "Upload", "ru": "Загрузка"},
    "nav_archive": {"en": "Archive", "ru": "Архив"},
    "nav_search": {"en": "Search", "ru": "Поиск"},
    # --- page titles
    "title_upload": {"en": "Upload", "ru": "Загрузка"},
    "title_archive": {"en": "Archive", "ru": "Архив"},
    "title_meeting": {"en": "Meeting", "ru": "Встреча"},
    "title_error": {"en": "Error", "ru": "Ошибка"},
    # --- upload page
    "add_recording": {"en": "Add a recording", "ru": "Добавить запись"},
    "drop_label": {"en": "Add a recording", "ru": "Добавление записи"},
    "drop_idle": {"en": "Drop a recording here or", "ru": "Перетащите запись сюда или"},
    "choose_file": {"en": "Choose file", "ru": "Выбрать файл"},
    "drop_release": {"en": "Release to add", "ru": "Отпустите, чтобы добавить"},
    "drop_picking": {
        "en": "The file dialog is open in Finder",
        "ru": "Окно выбора файла открыто в Finder",
    },
    "uploading": {"en": "Uploading", "ru": "Загрузка"},
    "uploading_n": {"en": "Uploading {i} of {n}", "ru": "Загрузка {i} из {n}"},
    "drop_note": {
        "en": "A chosen recording is read where it is and never copied. A dropped one is"
        " copied to the data folder and deleted once it has been processed; if processing"
        " fails, the copy is kept for up to 7 days so you can retry.",
        "ru": "Выбранная запись читается с диска и не копируется. Перетащенная временно"
        " копируется в каталог данных и удаляется после обработки; если обработка не"
        " удалась, копия хранится до 7 дней, чтобы можно было повторить.",
    },
    "recording_language": {"en": "Recording language", "ru": "Язык записи"},
    "queue": {"en": "Queue", "ru": "Очередь"},
    "queue_empty": {
        "en": "Recordings you add and their progress will show up here.",
        "ru": "Здесь появятся добавленные записи и ход их обработки.",
    },
    "retry": {"en": "Retry", "ru": "Повторить"},
    "open": {"en": "Open", "ru": "Открыть"},
    "error_code": {"en": "Code: {code}", "ru": "Код: {code}"},
    "running_for": {"en": "{time} so far", "ru": "уже {time}"},
    # --- app.js messages
    "js_too_large": {
        "en": "The file is larger than 8 GB. Add it with “Choose file” instead:"
        " that way the recording isn't copied.",
        "ru": "Файл больше 8 ГБ. Добавьте его кнопкой «Выбрать файл»: так запись не копируется.",
    },
    "js_queued": {"en": "Added to the queue: {title}", "ru": "Добавлено в очередь: {title}"},
    "js_failed": {
        "en": "Couldn't upload the file. Try again.",
        "ru": "Не удалось загрузить файл. Попробуйте ещё раз.",
    },
    "js_aborted": {
        "en": "The upload stopped. Check that the service is running and add the recording again.",
        "ru": "Загрузка прервалась. Проверьте, что сервис запущен, и добавьте запись снова.",
    },
    # --- processing status
    "status_label": {"en": "Processing status", "ru": "Статус обработки"},
    "retry_processing": {"en": "Retry processing", "ru": "Повторить обработку"},
    "status_wait": {
        "en": "The transcript will appear here once processing is done. Track progress on the",
        "ru": "Транскрипт появится здесь после обработки. Ход обработки виден на странице",
    },
    "status_wait_link": {"en": "Upload", "ru": "Загрузка"},
    "status_wait_end": {"en": " page.", "ru": "."},
    "status_queued": {"en": "Queued", "ru": "В очереди"},
    "status_done": {"en": "Done", "ru": "Готово"},
    "status_error": {"en": "Failed", "ru": "Ошибка"},
    "status_processing": {"en": "Processing", "ru": "Обработка"},
    "stage_start": {"en": "Preparing", "ru": "Подготовка"},
    "stage_audio": {"en": "Preparing audio", "ru": "Подготовка аудио"},
    "stage_diarize": {"en": "Separating speakers", "ru": "Разделение по спикерам"},
    "stage_transcribe": {"en": "Recognizing speech", "ru": "Распознавание речи"},
    "stage_merge": {"en": "Merging", "ru": "Склейка"},
    "stage_save": {"en": "Saving", "ru": "Сохранение"},
    # --- processing and file errors: what happened and what to do
    "err_not_media": {
        "en": "This isn't audio or video, or the file is damaged. Use an mp3, mp4, m4a or wav"
        " recording.",
        "ru": "Это не аудио и не видео либо файл повреждён. Нужна запись mp3, mp4, m4a или wav.",
    },
    "err_source_missing": {
        "en": "The recording is no longer where it was. Put the file back and retry.",
        "ru": "Запись не найдена на прежнем месте. Верните файл и повторите.",
    },
    "err_timeout": {
        "en": "Processing ran out of time. Retry when the Mac isn't busy.",
        "ru": "Обработка не уложилась в отведённое время. Повторите, когда Mac свободен.",
    },
    "err_no_audio_stream": {
        "en": "The file has no audio track.",
        "ru": "В файле нет звуковой дорожки.",
    },
    "err_empty_audio": {"en": "The recording is silent.", "ru": "В записи нет звука."},
    "err_not_a_file": {
        "en": "This isn't a file. Choose a meeting recording.",
        "ru": "Это не файл. Выберите запись встречи.",
    },
    "err_bad_path": {
        "en": "The path to the recording wasn't recognized. Choose the file again.",
        "ru": "Путь к записи не распознан. Выберите файл ещё раз.",
    },
    "err_ffmpeg_missing": {
        "en": "ffmpeg wasn't found. Install it and retry.",
        "ru": "Не найден ffmpeg. Установите его и повторите.",
    },
    "err_prepare_failed": {
        "en": "Couldn't prepare the audio. Check that the file isn't damaged.",
        "ru": "Не удалось подготовить аудио. Проверьте, что файл не повреждён.",
    },
    "err_bad_result": {
        "en": "Recognition returned an incomplete result. Retry processing.",
        "ru": "Распознавание вернуло неполный результат. Повторите обработку.",
    },
    "err_unknown": {
        "en": "Processing stopped with an error. Retry processing.",
        "ru": "Обработка остановилась с ошибкой. Повторите обработку.",
    },
    "file_rejected": {
        "en": "This file won't work. {text} Code: {code}.",
        "ru": "Файл не подходит. {text} Код: {code}.",
    },
    # --- meeting page
    "meeting_title": {"en": "Meeting title", "ru": "Название встречи"},
    "recorded_at": {"en": "Recording date", "ru": "Дата записи"},
    "edit_hint": {"en": "Enter saves, Esc cancels", "ru": "Enter сохраняет, Esc отменяет"},
    "export_md": {"en": "Export .md", "ru": "Экспорт .md"},
    "delete_meeting": {"en": "Delete meeting", "ru": "Удалить встречу"},
    "speakers": {"en": "Speakers", "ru": "Спикеры"},
    "speakers_hint": {
        "en": "click a name to rename",
        "ru": "нажмите на имя, чтобы переименовать",
    },
    "speaker_n": MD_LABELS["speaker_n"],
    "speaker_name_for": {"en": "Name for “{name}”", "ru": "Имя для «{name}»"},
    "transcript": MD_LABELS["transcript"],
    "delete_question": {"en": "Delete this meeting?", "ru": "Удалить встречу?"},
    "delete_text": {
        "en": "The transcript will be removed from the archive. This can't be undone.",
        "ru": "Транскрипт удалится из архива. Это нельзя отменить.",
    },
    "cancel": {"en": "Cancel", "ru": "Отмена"},
    "delete": {"en": "Delete", "ru": "Удалить"},
    # --- archive and search
    "search_label": {"en": "Search all transcripts", "ru": "Поиск по всем транскриптам"},
    "search_summary": {"en": "{hits} in {meetings}", "ru": "{hits} в {meetings}"},
    "search_limited": {
        "en": ". Showing the first {limit}, try a narrower search",
        "ru": ". Показаны первые {limit}, уточните запрос",
    },
    "search_nothing": {"en": "Nothing found for “{q}”", "ru": "Ничего не найдено по «{q}»"},
    "search_nothing_hint": {
        "en": "Try a different form of the word or just its beginning.",
        "ru": "Попробуйте другую форму слова или только его начало.",
    },
    "archive_empty": {"en": "No meetings yet", "ru": "Пока нет встреч"},
    "archive_empty_hint": {
        "en": "Add a recording, and its transcript will show up here after processing.",
        "ru": "Добавьте запись, и после обработки здесь появится транскрипт.",
    },
    "open_archive": {"en": "Open the archive", "ru": "Открыть архив"},
    # --- dates and durations
    "day_month": {"en": "{month} {day}", "ru": "{day} {month}"},
    "date_full": {"en": "{day_month}, {year}, {time}", "ru": "{day_month} {year}, {time}"},
    "date_this_year": {"en": "{day_month}, {time}", "ru": "{day_month}, {time}"},
    "date_other_year": {"en": "{day_month}, {year}", "ru": "{day_month} {year}"},
    "date_unset": {"en": "date not set", "ru": "дата не указана"},
    "no_date": {"en": "no date", "ru": "без даты"},
    "yesterday_at": {"en": "yesterday, {time}", "ru": "вчера, {time}"},
    "less_than_minute": {"en": "less than a minute", "ru": "меньше минуты"},
    "dur_s": {"en": "{s} s", "ru": "{s} с"},
    "dur_min": {"en": "{m} min", "ru": "{m} мин"},
    "dur_h_min": {"en": "{h} h {m:02d} min", "ru": "{h} ч {m:02d} мин"},
    # --- messages from the server
    "untitled": {"en": "Untitled", "ru": "Без названия"},
    "no_file_chosen": {"en": "No file chosen.", "ru": "Файл не выбран."},
    "file_unreadable": {"en": "Can't read the file.", "ru": "Файл не читается."},
    "queued_title": {"en": "Added to the queue: {title}", "ru": "Добавлено в очередь: {title}"},
    "file_too_large": {"en": "The file is larger than 8 GB.", "ru": "Файл больше 8 ГБ."},
    "file_empty": {"en": "The file is empty.", "ru": "Пустой файл."},
    "no_space": {
        "en": "Not enough disk space. Free some up and add the recording again.",
        "ru": "Мало места на диске. Освободите место и добавьте запись снова.",
    },
    "bad_language": {
        "en": "This recording language isn't supported.",
        "ru": "Такой язык записи не поддерживается.",
    },
    "bad_date": {"en": "Couldn't read the date.", "ru": "Дата не распознана."},
    "title_empty": {"en": "The title can't be empty.", "ru": "Название не может быть пустым."},
    "name_taken": {
        "en": "Another speaker in this meeting already has this name.",
        "ru": "Это имя уже у другого спикера этой встречи.",
    },
    "no_such_speaker": {"en": "There's no such speaker.", "ru": "Такого спикера нет."},
    "nothing_to_retry": {"en": "There's nothing to retry.", "ru": "Повторять нечего."},
    "meeting_busy": {
        "en": "The meeting is being processed. You can delete it once that's done.",
        "ru": "Встреча обрабатывается, удалить её можно после.",
    },
    "http_400": {"en": "Bad request.", "ru": "Неверный запрос."},
    "http_403": {"en": "Request refused.", "ru": "Запрос отклонён."},
    "http_404": {"en": "Not found.", "ru": "Не найдено."},
    "http_405": {"en": "Method not supported.", "ru": "Метод не поддерживается."},
    "http_409": {
        "en": "This action isn't available right now.",
        "ru": "Сейчас это действие недоступно.",
    },
    "http_413": {"en": "Request too large.", "ru": "Слишком большой запрос."},
    "http_other": {"en": "The request failed.", "ru": "Ошибка запроса."},
    "http_500": {
        "en": "Internal error. No details are kept.",
        "ru": "Внутренняя ошибка. Подробности не сохраняются.",
    },
    # --- Finder dialog (picker.py); no quotes: it goes into an AppleScript string
    "pick_prompt": {"en": "Choose a meeting recording", "ru": "Выберите запись встречи"},
}

MONTHS: Mapping[str, tuple[str, ...]] = {
    "en": (
        "Jan",
        "Feb",
        "Mar",
        "Apr",
        "May",
        "Jun",
        "Jul",
        "Aug",
        "Sep",
        "Oct",
        "Nov",
        "Dec",
    ),
    "ru": (
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ),
}

# English: (one, many). Russian: (one, few, many).
PLURALS: Mapping[str, Mapping[str, tuple[str, ...]]] = {
    "speakers": {"en": ("speaker", "speakers"), "ru": ("спикер", "спикера", "спикеров")},
    "meetings": {"en": ("meeting", "meetings"), "ru": ("встреча", "встречи", "встреч")},
    "in_meetings": {"en": ("meeting", "meetings"), "ru": ("встрече", "встречах", "встречах")},
    "hits": {"en": ("match", "matches"), "ru": ("фрагмент", "фрагмента", "фрагментов")},
}


def t(key: str, **values: object) -> str:
    """String `key` in the interface language."""
    return pick(STRINGS, key, None, **values)


def month(number: int, language: str | None = None) -> str:
    names = MONTHS.get(language or ui_lang(), MONTHS["en"])
    return names[number - 1]


def _form(count: int, forms: tuple[str, ...]) -> str:
    if len(forms) == 2:
        return forms[0] if count == 1 else forms[1]
    tail = count % 100
    if 11 <= tail <= 14:
        return forms[2]
    if count % 10 == 1:
        return forms[0]
    if 2 <= count % 10 <= 4:
        return forms[1]
    return forms[2]


def plural(key: str, count: int, language: str | None = None) -> str:
    """ "1 speaker", "3 speakers"; "1 спикер", "3 спикера", "5 спикеров"."""
    variants = PLURALS[key]
    forms = variants.get(language or ui_lang()) or variants["en"]
    return f"{count} {_form(count, forms)}"


def language_name(code: str) -> str:
    """Name of a recording language in the interface language; unknown codes as is."""
    return pick(LANGUAGE_NAMES, code) if code in LANGUAGE_NAMES else code
