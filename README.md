# LocTranscriptor

Local transcription for meetings on your Mac.

Add a meeting recording and get a word-for-word transcript split by speaker. Search works across all your meetings. Everything runs on your Mac: no cloud speech service, no account, no telemetry.

![Upload page with the processing queue](assets/screenshots/upload.png)

- **Fast.** Speech recognition and speaker diarization run on the Apple Neural Engine via [FluidAudio](https://github.com/FluidInference/FluidAudio). A two-hour meeting takes about 1.5 minutes on a MacBook Air M3.
- **Verbatim.** The transcript keeps every word, with timestamps and speakers. Click a speaker to give them a real name. No summaries, no rewriting.
- **Private.** Recordings are read where they are and stay on your Mac. Recognition runs in a separate process with no network access. The only time the app goes online is the one-time model download.
- **Searchable.** Every meeting goes into one archive with full-text search. Export any transcript as Markdown.

| | |
|---|---|
| ![Archive with search](assets/screenshots/archive.png) | ![Meeting transcript](assets/screenshots/meeting.png) |

## Download for Mac

**[Download LocTranscriptor.dmg](https://github.com/zorki2020/transcribator/releases/latest/download/LocTranscriptor.dmg)** · [all releases](https://github.com/zorki2020/transcribator/releases)

It is a regular Mac app, no Terminal needed. Русская инструкция [ниже](#установка-на-mac-по-русски).

You need a Mac with Apple Silicon (M1 or newer), macOS 15 Sequoia or newer and about 2 GB of free disk space. On an Intel Mac or an older macOS, macOS itself will not open the app.

1. Open `LocTranscriptor.dmg` and drag **LocTranscriptor** into **Applications**.
   <!-- screenshot: dmg window -->
2. Open LocTranscriptor from Applications. macOS shows **“LocTranscriptor” Not Opened**. Click **Done**. This is expected, the reason is below.
   <!-- screenshot: Not Opened dialog -->
3. Open **System Settings → Privacy & Security** and scroll down to *“LocTranscriptor” was blocked to protect your Mac*. Click **Open Anyway**, confirm with your password or Touch ID, then click **Open**. The button stays there for about an hour after step 2. If it is gone, repeat step 2. You do this only once.
   <!-- screenshot: Open Anyway -->
4. On the first screen click **Download models**. The app downloads the speech models, about 650 MB from huggingface.co. This is the only time it goes online. If the download breaks off, it resumes where it stopped.
5. Add a recording: click **Choose file** or drag the file from Finder into the window. Supported formats: mp3, mp4/mov, m4a, wav, aiff, caf, flac.

On first launch the app shows a few tips. **Help → Show Tips** brings them back.

**Why the warning?** Apple shows it for every app that is not signed with a paid Apple Developer certificate. LocTranscriptor is open source, and `scripts/build_app.sh` builds it from this repository. To check that your download is the published one, compare the output of `shasum -a 256 ~/Downloads/LocTranscriptor.dmg` with `LocTranscriptor.dmg.sha256` on the release page.

**Closing and quitting.** When you close the window, LocTranscriptor stays in the menu bar and keeps processing. **Quit LocTranscriptor** (⌘Q) stops it. A recording that was cut off starts over at the next launch. To have the app ready after a restart, turn on **Open at Login** in the app menu.

**Updating.** Download the new `.dmg` and replace the app in Applications. Your meetings and models stay. macOS will ask for **Open Anyway** again. It may also ask again for access to folders and for the login item.

**Where your data is.** Meetings, models and settings are in `~/Library/Application Support/LocTranscriptor`. The log, with error codes only, is in `~/Library/Logs/LocTranscriptor`. **Show in Finder** in Settings opens the data folder.

**Uninstall.** Turn off **Open at Login** if it is on. Quit LocTranscriptor and drag it from Applications to the Trash. To delete your meetings and the models too, delete both folders above (Finder → Go → Go to Folder…).

### Установка на Mac (по-русски)

**[Скачать LocTranscriptor.dmg](https://github.com/zorki2020/transcribator/releases/latest/download/LocTranscriptor.dmg)** · [все версии](https://github.com/zorki2020/transcribator/releases)

Это обычное приложение для Mac, Терминал не нужен.

Нужен Mac на Apple Silicon (M1 и новее), macOS 15 Sequoia или новее и около 2 ГБ свободного места. На Mac с Intel и на старой macOS система сама не даст открыть приложение.

1. Откройте `LocTranscriptor.dmg` и перетащите **LocTranscriptor** в папку **Программы**.
2. Откройте приложение из папки «Программы». macOS напишет **«LocTranscriptor» не открыто**. Нажмите **Готово**. Так и должно быть, причина ниже.
3. Откройте **Системные настройки → Конфиденциальность и безопасность** и прокрутите вниз до строки *«LocTranscriptor» заблокировано для защиты Mac*. Нажмите **Всё равно открыть**, подтвердите паролем или Touch ID и нажмите **Открыть**. Кнопка видна около часа после шага 2. Если её нет, повторите шаг 2. Это нужно сделать один раз.
4. На первом экране нажмите **Скачать модели**. Приложение загрузит модели распознавания, около 650 МБ с huggingface.co. Только в этот момент оно выходит в интернет. Если загрузка прервётся, она продолжится с того же места.
5. Добавьте запись: нажмите **Выбрать файл** или перетащите файл из Finder в окно. Форматы: mp3, mp4/mov, m4a, wav, aiff, caf, flac.

При первом запуске приложение покажет несколько подсказок. Чтобы увидеть их снова, выберите **Справка → Показать подсказки**.

**Почему предупреждение?** Apple показывает его для любого приложения без платного сертификата разработчика. Код приложения открыт, его собирает из этого репозитория скрипт `scripts/build_app.sh`. Чтобы убедиться, что файл тот самый, сравните вывод `shasum -a 256 ~/Downloads/LocTranscriptor.dmg` с файлом `LocTranscriptor.dmg.sha256` на странице релиза.

**Закрыть и выйти.** Если закрыть окно, приложение останется в строке меню и продолжит обработку. **Завершить LocTranscriptor** (⌘Q) останавливает его. Прерванная запись начнётся заново при следующем запуске. Чтобы приложение само открывалось после перезагрузки, включите **Открывать при входе** в меню приложения.

**Обновление.** Скачайте новый `.dmg` и замените приложение в папке «Программы». Встречи и модели сохранятся. macOS снова попросит нажать **Всё равно открыть**. Возможно, она заново спросит доступ к папкам и разрешение на запуск при входе.

**Где данные.** Встречи, модели и настройки лежат в `~/Library/Application Support/LocTranscriptor`. Журнал только с кодами ошибок лежит в `~/Library/Logs/LocTranscriptor`. Папку данных открывает кнопка **Показать в Finder** в настройках.

**Удаление.** Если включено **Открывать при входе**, выключите его. Завершите приложение и перетащите его из папки «Программы» в Корзину. Чтобы удалить встречи и модели, удалите обе папки выше (Finder → Переход → Переход к папке…).

## Languages

LocTranscriptor was built for and tested mainly on **Russian** meetings. **English** works too. You pick the language for each recording, and the interface is available in English and Russian. The underlying model (Parakeet TDT 0.6B v3) covers 25 European languages, but only Russian and English are tested.

## Speed and accuracy

MacBook Air M3, 16 GB, on synthetic two-speaker meetings made with macOS voices:

| Recording | Time | Speakers found | Word error rate |
|---|---|---|---|
| Russian, 2 h | 1 min 28 s | 2 of 2 | 1.4% |
| English, 2 h | 1 min 40 s | 2 of 2 | 0.5% |

Real calls have crosstalk, accents and noisy microphones, so expect more errors there.

## For developers

Running from source, the command line, building the app and the code layout are in [DEVELOPMENT.md](DEVELOPMENT.md).

## Licenses

LocTranscriptor, including the `transcribator` package, is MIT-licensed (`LICENSE`). The speech models are CC-BY-4.0: Parakeet TDT 0.6B v3 by NVIDIA and the pyannote Community-1 diarization pipeline, both converted to CoreML by FluidInference. FluidAudio is Apache-2.0, htmx is 0BSD. [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) has the full list and what each license requires.
