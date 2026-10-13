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
    # The product name is Latin in both languages and never declined: Russian copy is
    # phrased so that it needs no case ending or gender agreement.
    "app_name": {"en": "LocTranscriptor", "ru": "LocTranscriptor"},
    "skip_to_content": {"en": "Skip to content", "ru": "Перейти к содержимому"},
    "nav_label": {"en": "Sections", "ru": "Разделы"},
    "nav_upload": {"en": "Add recording", "ru": "Добавить запись"},
    "nav_archive": {"en": "Archive", "ru": "Архив"},
    "nav_search": {"en": "Search", "ru": "Поиск"},
    "nav_settings": {"en": "Settings", "ru": "Настройки"},
    # --- page titles
    "title_upload": {"en": "Add recording", "ru": "Добавить запись"},
    "title_archive": {"en": "Archive", "ru": "Архив"},
    "title_meeting": {"en": "Meeting", "ru": "Встреча"},
    "title_error": {"en": "Error", "ru": "Ошибка"},
    "title_setup": {"en": "Speech models", "ru": "Модели распознавания"},
    "title_settings": {"en": "Settings", "ru": "Настройки"},
    # --- upload page
    "add_recording": {"en": "Add recording", "ru": "Добавить запись"},
    "drop_label": {"en": "Drop area", "ru": "Область для перетаскивания"},
    "drop_idle": {"en": "Drag a recording here", "ru": "Перетащите запись сюда"},
    "choose_file": {"en": "Choose file", "ru": "Выбрать файл"},
    "drop_release": {"en": "Release to add", "ru": "Отпустите, чтобы добавить"},
    "drop_picking": {
        "en": "Choose a file in the Finder window",
        "ru": "Выберите файл в окне Finder",
    },
    "uploading": {"en": "Uploading", "ru": "Файл загружается"},
    "uploading_n": {"en": "Uploading {i} of {n}", "ru": "Загружается {i} из {n}"},
    "drop_note": {
        "en": "A chosen file is read straight from disk. A dragged file is copied to the data"
        " folder and deleted after processing, or after 7 days if processing fails.",
        "ru": "Выбранный файл читается прямо с диска. Перетащенный файл копируется в папку данных"
        " и удаляется после обработки, а если она не удалась, через 7 дней.",
    },
    # App window: Finder gives the path, so a recording dragged from Finder isn't copied.
    "drop_note_more": {"en": "How copies work", "ru": "Как хранятся копии"},
    "drop_note_app": {
        "en": "Recordings are read straight from disk and never copied, except mail attachments"
        " and files from other apps.",
        "ru": "Записи читаются прямо с диска и не копируются, кроме вложений из почты и файлов"
        " из других программ.",
    },
    "adding": {"en": "Adding…", "ru": "Запись добавляется…"},
    "recording_language": {"en": "Recording language", "ru": "Язык записи"},
    "queue": {"en": "Queue", "ru": "Очередь"},
    "queue_empty": {
        "en": "Added recordings and their progress will appear here",
        "ru": "Здесь появятся добавленные записи и ход их обработки",
    },
    "retry": {"en": "Retry", "ru": "Повторить"},
    "open": {"en": "Open", "ru": "Открыть"},
    "error_code": {"en": "Code: {code}", "ru": "Код: {code}"},
    "running_for": {"en": "{time} so far", "ru": "уже {time}"},
    # --- app.js messages
    "js_too_large": {
        "en": "This file is over 8 GB. Add it with the Choose file button instead.",
        "ru": "Этот файл больше 8 ГБ. Добавьте его кнопкой «Выбрать файл».",
    },
    "js_queued": {"en": "Added to the queue: {title}", "ru": "Добавлено в очередь: {title}"},
    "js_failed": {
        "en": "Couldn't upload the file. Try again.",
        "ru": "Не удалось загрузить файл. Попробуйте ещё раз.",
    },
    "js_aborted": {
        "en": "The upload stopped. Check that LocTranscriptor is running and add the recording"
        " again.",
        "ru": "Загрузка прервалась. Проверьте, что LocTranscriptor работает, и добавьте запись"
        " снова.",
    },
    "js_add_failed": {
        "en": "Couldn't add the recording. Try again.",
        "ru": "Не удалось добавить запись. Попробуйте ещё раз.",
    },
    "js_no_bridge": {
        "en": "The file dialog isn't available. Drag the recording into this window instead.",
        "ru": "Окно выбора файла недоступно. Перетащите запись в это окно.",
    },
    # --- onboarding: first-run tips on the upload page (app.js gets them via data-*)
    "tour_count": {"en": "Step {n} of {m}", "ru": "Шаг {n} из {m}"},
    "tour_skip": {"en": "Skip", "ru": "Пропустить"},
    "tour_back": {"en": "Back", "ru": "Назад"},
    "tour_next": {"en": "Next", "ru": "Далее"},
    "tour_done": {"en": "Done", "ru": "Готово"},
    "tour_drop_title": {"en": "Add a recording", "ru": "Добавьте запись"},
    "tour_drop_body": {
        "en": "Drag a meeting recording here or press Choose file. Audio and video both work.",
        "ru": "Перетащите сюда запись встречи или нажмите «Выбрать файл». Подойдёт аудио и видео.",
    },
    "tour_language_title": {"en": "Recording language", "ru": "Язык записи"},
    "tour_language_body": {
        "en": "Pick the language spoken in the meeting. You can change it for each recording.",
        "ru": "Выберите язык, на котором говорят на встрече. Его можно менять для каждой записи.",
    },
    "tour_queue_title": {"en": "Queue", "ru": "Очередь"},
    "tour_queue_body_app": {
        "en": "Added recordings and their progress show up here. Processing continues after"
        " you close the window.",
        "ru": "Здесь видны добавленные записи и ход обработки. Если закрыть окно, обработка"
        " продолжится.",
    },
    "tour_queue_body": {
        "en": "Added recordings and their progress show up here. Processing continues after"
        " you close the page.",
        "ru": "Здесь видны добавленные записи и ход обработки. Если закрыть страницу,"
        " обработка продолжится.",
    },
    "tour_archive_title": {"en": "Archive and search", "ru": "Архив и поиск"},
    "tour_archive_body": {
        "en": "Every finished transcript is kept in the Archive. Search finds a word across"
        " all meetings.",
        "ru": "В архиве хранятся все готовые транскрипты. Поиск находит слово во всех встречах.",
    },
    "tour_archive_settings": {
        "en": "Languages and the data folder are in Settings.",
        "ru": "Языки и папку данных вы найдёте в настройках.",
    },
    # --- privacy: one wording for the setup page and settings
    "privacy_local": {
        "en": "Recordings and transcripts never leave this Mac.",
        "ru": "Записи и транскрипты не покидают этот Mac.",
    },
    # --- first run: speech models (app only)
    "setup_title": {"en": "Download speech models", "ru": "Скачайте модели распознавания"},
    "setup_lead": {
        "en": "LocTranscriptor recognizes speech on this Mac. First it needs to download speech"
        " models from huggingface.co.",
        "ru": "LocTranscriptor распознаёт речь прямо на этом Mac. Сначала нужно скачать модели"
        " распознавания с huggingface.co.",
    },
    "setup_point_network": {
        "en": "The network is used once, for this download, and only after you press the button.",
        "ru": "Сеть нужна один раз, для этой загрузки, и только после нажатия кнопки.",
    },
    "setup_point_checked": {
        "en": "Each file is verified by its checksum. A stopped download resumes where it left"
        " off.",
        "ru": "Каждый файл сверяется с контрольной суммой. Прерванная загрузка продолжится"
        " с места обрыва.",
    },
    "setup_space": {
        "en": "The models take {size}, and {free} is free on disk now.",
        "ru": "Модели занимают {size}, на диске сейчас свободно {free}.",
    },
    "setup_background": {
        "en": "You can close the window. The download continues in the background while the"
        " app is running.",
        "ru": "Окно можно закрыть. Загрузка продолжится в фоне, пока программа работает.",
    },
    "setup_download": {"en": "Download models", "ru": "Скачать модели"},
    "setup_resume": {"en": "Continue download", "ru": "Продолжить загрузку"},
    "setup_continue": {"en": "Continue", "ru": "Продолжить"},
    "setup_downloading": {"en": "Downloading models", "ru": "Модели скачиваются"},
    "setup_verifying": {"en": "Checking files…", "ru": "Файлы проверяются…"},
    "setup_progress": {"en": "{done} of {total}", "ru": "{done} из {total}"},
    "setup_progress_label": {"en": "Downloaded", "ru": "Скачано"},
    "setup_done": {"en": "Models are ready", "ru": "Модели готовы"},
    "setup_start_using": {"en": "Add recording", "ru": "Добавить запись"},
    "setup_paused": {
        "en": "Downloaded {done} of {total}",
        "ru": "Скачано {done} из {total}",
    },
    "setup_err_no_space": {
        "en": "Not enough disk space. The models need {need} more, and {free} is free. Free up"
        " space and press Continue.",
        "ru": "Не хватает места на диске. Нужно ещё {need}, свободно {free}. Освободите место"
        " и нажмите «Продолжить».",
    },
    "setup_err_offline": {
        "en": "No internet connection. Connect and press Continue. The download resumes where"
        " it stopped.",
        "ru": "Нет подключения к интернету. Подключитесь и нажмите «Продолжить». Загрузка"
        " продолжится с места обрыва.",
    },
    "setup_err_server_error": {
        "en": "huggingface.co returned an error. Wait a minute and press Continue.",
        "ru": "huggingface.co ответил ошибкой. Подождите минуту и нажмите «Продолжить».",
    },
    "setup_err_checksum": {
        "en": "A downloaded file failed the check and will be downloaded again. Press Continue.",
        "ru": "Скачанный файл не прошёл проверку и скачается заново. Нажмите «Продолжить».",
    },
    "setup_err_write_failed": {
        "en": "Couldn't save the models. Check that the disk isn't full or read-only, then"
        " press Continue.",
        "ru": "Не удалось сохранить модели. Проверьте, что диск не заполнен и доступен для"
        " записи, и нажмите «Продолжить».",
    },
    "setup_err_unknown": {
        "en": "The download stopped. Press Continue to resume it.",
        "ru": "Загрузка остановилась. Нажмите «Продолжить», чтобы возобновить её.",
    },
    "setup_error_code": {"en": "Code: {code}", "ru": "Код: {code}"},
    # Sizes: decimal units, as Finder shows them.
    "size_mb": {"en": "{n} MB", "ru": "{n} МБ"},
    "size_gb": {"en": "{n} GB", "ru": "{n} ГБ"},
    "decimal_sep": {"en": ".", "ru": ","},
    # --- settings (app only)
    "settings_language": {"en": "Language", "ru": "Язык"},
    "settings_ui_lang": {"en": "Interface language", "ru": "Язык интерфейса"},
    "settings_recording_lang": {
        "en": "Default recording language",
        "ru": "Язык записей по умолчанию",
    },
    "settings_recording_hint": {
        "en": "You can pick another language for a recording when you add it",
        "ru": "Для отдельной записи язык можно сменить при добавлении",
    },
    "settings_saving": {"en": "Saving…", "ru": "Сохраняется…"},
    "settings_data": {"en": "Data", "ru": "Данные"},
    "settings_data_text": {
        "en": "Transcripts, speech models and settings are kept in this folder.",
        "ru": "В этой папке хранятся транскрипты, модели распознавания и настройки.",
    },
    "settings_data_sources": {
        "en": "Recordings stay where they are, and LocTranscriptor doesn't copy them. Mail"
        " attachments and files from other apps are the exception: they may have no lasting place"
        " on disk, so a copy stays here until the recording is processed. If processing fails,"
        " the copy is deleted after 7 days, and until then you can retry.",
        "ru": "Записи остаются на своих местах, LocTranscriptor их не копирует. Исключение —"
        " вложения из почты и файлы из других программ. У них может не быть постоянного"
        " места на диске, поэтому копия лежит здесь, пока запись не обработается. Если"
        " обработка не удалась, копия удаляется через 7 дней, а до этого запись можно"
        " повторить.",
    },
    "settings_log_text": {
        "en": "The log is kept here. It holds only codes, with no file names or text.",
        "ru": "Здесь лежит журнал. В нём только коды, без имён файлов и текста.",
    },
    "settings_reveal": {"en": "Show in Finder", "ru": "Показать в Finder"},
    "settings_privacy": {"en": "Privacy", "ru": "Приватность"},
    "settings_privacy_text": {
        "en": "The network is used only to download the speech models from Hugging Face."
        " There is no telemetry and no update check.",
        "ru": "Сеть нужна только для скачивания моделей распознавания с Hugging Face."
        " Телеметрии и проверки обновлений нет.",
    },
    # Interface languages are named in their own language, so anyone can find theirs.
    "lang_native_en": {"en": "English", "ru": "English"},
    "lang_native_ru": {"en": "Русский", "ru": "Русский"},
    # --- processing status
    "status_label": {"en": "Processing", "ru": "Обработка"},
    "status_wait": {
        "en": "The transcript will appear here when processing ends",
        "ru": "Транскрипт появится здесь, когда запись обработается",
    },
    "status_queued": {"en": "Queued", "ru": "В очереди"},
    "status_done": {"en": "Done", "ru": "Готово"},
    "status_error": {"en": "Failed", "ru": "Ошибка"},
    "status_processing": {"en": "Processing", "ru": "Обработка"},
    "stage_start": {"en": "Preparing", "ru": "Подготовка"},
    "stage_audio": {"en": "Preparing audio", "ru": "Подготовка аудио"},
    "stage_diarize": {"en": "Finding speakers", "ru": "Поиск спикеров"},
    "stage_transcribe": {"en": "Recognizing speech", "ru": "Распознавание речи"},
    "stage_merge": {"en": "Building transcript", "ru": "Сборка транскрипта"},
    "stage_save": {"en": "Saving", "ru": "Сохранение"},
    # --- processing and file errors: what happened and what to do
    "err_not_media": {
        "en": "This isn't an audio or video file, or the file is damaged. Choose another"
        " recording.",
        "ru": "Это не аудио и не видео, или файл повреждён. Выберите другую запись.",
    },
    "err_source_missing": {
        "en": "The recording file was moved or deleted. Put it back and retry.",
        "ru": "Файл записи перемещён или удалён. Верните его на место и повторите.",
    },
    "err_timeout": {
        "en": "Processing took too long and was stopped. Retry when the Mac is less busy.",
        "ru": "Обработка шла слишком долго и остановилась. Повторите, когда Mac будет меньше"
        " занят.",
    },
    "err_no_audio_stream": {
        "en": "The file has no audio track. Choose another recording.",
        "ru": "В файле нет звуковой дорожки. Выберите другую запись.",
    },
    "err_empty_audio": {
        "en": "The recording is silent. Choose another recording.",
        "ru": "В записи нет звука. Выберите другую запись.",
    },
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
        "en": "Couldn't read the audio. Check that the file isn't damaged.",
        "ru": "Не удалось прочитать звук. Проверьте, что файл не повреждён.",
    },
    "err_bad_result": {
        "en": "Speech recognition returned an incomplete result. Retry processing.",
        "ru": "Распознавание вернуло неполный результат. Повторите обработку.",
    },
    "err_unknown": {
        "en": "Processing stopped with an error. Retry processing.",
        "ru": "Обработка остановилась с ошибкой. Повторите обработку.",
    },
    # --- meeting page
    "meeting_title": {"en": "Meeting title", "ru": "Название встречи"},
    "recorded_at": {"en": "Recording date", "ru": "Дата записи"},
    "edit_hint": {"en": "Enter saves, Esc cancels", "ru": "Enter сохраняет, Esc отменяет"},
    "export_md": {"en": "Download .md", "ru": "Скачать .md"},
    "delete_meeting": {"en": "Delete meeting", "ru": "Удалить встречу"},
    "speakers": {"en": "Speakers", "ru": "Спикеры"},
    "speakers_hint": {
        "en": "Click a name to rename",
        "ru": "Нажмите на имя, чтобы переименовать",
    },
    "speaker_n": MD_LABELS["speaker_n"],
    "speaker_name_for": {"en": "Name for “{name}”", "ru": "Имя для «{name}»"},
    "transcript": MD_LABELS["transcript"],
    "delete_question": {"en": "Delete this meeting?", "ru": "Удалить встречу?"},
    "delete_text": {
        "en": "The transcript will be removed from the archive. This can't be undone.",
        "ru": "Транскрипт удалится из архива. Это нельзя отменить.",
    },
    "cancel": {"en": "Cancel", "ru": "Отменить"},
    "delete": {"en": "Delete", "ru": "Удалить"},
    # --- archive and search
    "search_label": {"en": "Search all transcripts", "ru": "Поиск по всем транскриптам"},
    "search_summary": {"en": "{hits} in {meetings}", "ru": "{hits} в {meetings}"},
    # When the result list is cut at the limit, {hits} is the limit, not the full count.
    "search_summary_limited": {
        "en": "First {hits} in {meetings}. Narrow the search to see the rest",
        "ru": "Первые {hits} в {meetings}. Уточните запрос, чтобы увидеть остальные",
    },
    "search_nothing": {"en": "Nothing found for “{q}”", "ru": "Ничего не найдено по «{q}»"},
    "search_nothing_hint": {
        "en": "Try another form of the word or just its beginning",
        "ru": "Попробуйте другую форму слова или только его начало",
    },
    "archive_empty": {"en": "No meetings yet", "ru": "Пока нет встреч"},
    "archive_empty_hint": {
        "en": "Add a recording, and its transcript will appear here",
        "ru": "Добавьте запись, и здесь появится её транскрипт",
    },
    "open_archive": {"en": "Open archive", "ru": "Открыть архив"},
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
    "file_unreadable": {"en": "Couldn't read the file.", "ru": "Не удалось прочитать файл."},
    "queued_title": {"en": "Added to the queue: {title}", "ru": "Добавлено в очередь: {title}"},
    "file_too_large": {"en": "The file is larger than 8 GB.", "ru": "Файл больше 8 ГБ."},
    "file_empty": {"en": "The file is empty.", "ru": "Файл пустой."},
    "no_space": {
        "en": "Not enough disk space. Free up space and add the recording again.",
        "ru": "Не хватает места на диске. Освободите место и добавьте запись снова.",
    },
    "bad_language": {
        "en": "This recording language isn't supported.",
        "ru": "Такой язык записи не поддерживается.",
    },
    "bad_date": {"en": "Couldn't read the date.", "ru": "Не удалось распознать дату."},
    "title_empty": {"en": "The title can't be empty.", "ru": "Название не может быть пустым."},
    "name_taken": {
        "en": "Another speaker in this meeting already has this name.",
        "ru": "Это имя уже у другого спикера этой встречи.",
    },
    "no_such_speaker": {"en": "There's no such speaker.", "ru": "Такого спикера нет."},
    "nothing_to_retry": {
        "en": "Only a failed recording can be retried.",
        "ru": "Повторить можно только запись с ошибкой.",
    },
    "meeting_busy": {
        "en": "The recording is still being processed. You can delete it when it's done.",
        "ru": "Запись ещё обрабатывается. Удалить её можно, когда обработка закончится.",
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
    "http_other": {"en": "The request failed.", "ru": "Запрос не выполнен."},
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
