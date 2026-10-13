// Native strings of the app in English and Russian. The language comes from the service
// (`/api/status` -> `ui_lang`, the user's choice in Settings); until the service answers,
// from the first preferred system language.

import Foundation

public enum Lang: String, Sendable, CaseIterable {
    case en, ru

    /// Russian if the first preferred language is Russian, English otherwise.
    public static func system(_ preferredLanguages: [String] = Locale.preferredLanguages) -> Lang {
        preferredLanguages.first?.lowercased().hasPrefix("ru") == true ? .ru : .en
    }

    /// The service's `ui_lang` when it is known, the system language otherwise.
    public static func from(service value: String?, preferredLanguages: [String] = Locale.preferredLanguages) -> Lang {
        if let value, let lang = Lang(rawValue: value.lowercased()) { return lang }
        return system(preferredLanguages)
    }
}

/// Fixed strings. Formatted ones are functions on `L10n`.
public enum Text: CaseIterable, Sendable {
    case appName
    // app menu
    case about, settings, openAtLogin, hide, hideOthers, showAll, quit
    // edit menu
    case edit, undo, redo, cut, copy, paste, delete, selectAll
    // view menu
    case view, reload, actualSize, zoomIn, zoomOut, enterFullScreen
    // window menu
    case window, minimize, zoom, close, bringAllToFront
    // help menu
    case help, showTips, github
    // status item
    case openWindow, quitShort, starting, stopping, running, notRunning, setupNeeded
    // native views and alerts
    case serviceFailedText, tryAgain, showLog, alreadyRunningTitle, alreadyRunningText
    case configErrorText, quitConfirmTitle, quitConfirmText, quitButton, cancel
    case moveTitle, moveText, ok, loginApprovalTitle, loginApprovalText, openLoginItems
    case loginFailed, loginNeedsApplications, saveFailed, stoppingWindow
    case pickMessage, pickButton

    public func string(_ lang: Lang) -> String {
        let (en, ru) = pair
        return lang == .ru ? ru : en
    }

    private var pair: (String, String) {
        switch self {
        case .appName: ("LocTranscriptor", "LocTranscriptor")
        case .about: ("About LocTranscriptor", "О программе LocTranscriptor")
        case .settings: ("Settings…", "Настройки…")
        case .openAtLogin: ("Open at Login", "Открывать при входе")
        case .hide: ("Hide LocTranscriptor", "Скрыть LocTranscriptor")
        case .hideOthers: ("Hide Others", "Скрыть остальные")
        case .showAll: ("Show All", "Показать все")
        case .quit: ("Quit LocTranscriptor", "Завершить LocTranscriptor")
        case .edit: ("Edit", "Правка")
        case .undo: ("Undo", "Отменить")
        case .redo: ("Redo", "Повторить")
        case .cut: ("Cut", "Вырезать")
        case .copy: ("Copy", "Скопировать")
        case .paste: ("Paste", "Вставить")
        case .delete: ("Delete", "Удалить")
        case .selectAll: ("Select All", "Выбрать все")
        case .view: ("View", "Вид")
        case .reload: ("Reload Page", "Обновить страницу")
        case .actualSize: ("Actual Size", "Фактический размер")
        case .zoomIn: ("Zoom In", "Увеличить")
        case .zoomOut: ("Zoom Out", "Уменьшить")
        case .enterFullScreen: ("Enter Full Screen", "Войти в полноэкранный режим")
        case .window: ("Window", "Окно")
        case .minimize: ("Minimize", "Свернуть")
        case .zoom: ("Zoom", "Изменить масштаб")
        case .close: ("Close", "Закрыть")
        case .bringAllToFront: ("Bring All to Front", "Все окна — на передний план")
        case .help: ("Help", "Справка")
        case .showTips: ("Show Tips", "Показать подсказки")
        case .github: ("LocTranscriptor on GitHub", "LocTranscriptor на GitHub")
        case .openWindow: ("Open Window", "Открыть окно")
        case .quitShort: ("Quit", "Выйти")
        case .starting: ("Starting…", "Запускается…")
        case .stopping: ("Stopping…", "Останавливается…")
        case .running: ("Running", "Работает")
        case .notRunning: ("Not running", "Не работает")
        case .setupNeeded: ("Speech models not downloaded", "Модели не скачаны")
        case .serviceFailedText:
            ("Five restarts in a row didn't help. The log holds only codes, with no file names or text.",
             "Пять перезапусков подряд не помогли. В журнале только коды, без имён файлов и текста.")
        case .tryAgain: ("Try Again", "Попробовать снова")
        case .showLog: ("Show Log in Finder", "Показать журнал в Finder")
        case .alreadyRunningTitle: ("LocTranscriptor is already running", "LocTranscriptor уже работает")
        case .alreadyRunningText:
            ("Another copy of LocTranscriptor is using the same data folder. Quit it and press Try Again.",
             "Другая копия LocTranscriptor работает с той же папкой данных. Завершите её и нажмите «Попробовать снова».")
        case .configErrorText:
            ("Check that the disk with your home folder has free space, then press Try Again.",
             "Проверьте, что на диске с домашней папкой есть свободное место, и нажмите «Попробовать снова».")
        case .quitConfirmTitle: ("Processing is in progress. Quit?", "Идёт обработка. Выйти?")
        case .quitConfirmText:
            ("Processing will start over when you open LocTranscriptor again.",
             "Обработка начнётся заново, когда вы снова откроете LocTranscriptor.")
        case .quitButton: ("Quit", "Выйти")
        case .cancel: ("Cancel", "Отменить")
        case .moveTitle: ("Move LocTranscriptor to Applications", "Перенесите LocTranscriptor в «Программы»")
        case .moveText:
            ("The app is running from a disk image or a temporary folder. Quit it, drag LocTranscriptor into the Applications folder and open it from there. It works as it is, but Open at Login needs the copy in Applications.",
             "Программа открыта с образа диска или из временной папки. Завершите её, перетащите LocTranscriptor в папку «Программы» и откройте оттуда. Работать можно и так, но «Открывать при входе» включается только из папки «Программы».")
        case .ok: ("OK", "ОК")
        case .loginApprovalTitle: ("Allow LocTranscriptor to open at login", "Разрешите LocTranscriptor открываться при входе")
        case .loginApprovalText:
            ("Turn on LocTranscriptor in System Settings → General → Login Items.",
             "Включите LocTranscriptor в Системных настройках → Основные → Объекты входа.")
        case .openLoginItems: ("Open Login Items Settings", "Открыть настройки входа")
        case .loginFailed: ("Couldn't change Open at Login.", "Не удалось изменить «Открывать при входе».")
        case .loginNeedsApplications:
            ("Open at Login works only when LocTranscriptor is in the Applications folder.",
             "«Открывать при входе» работает, только когда LocTranscriptor лежит в папке «Программы».")
        case .saveFailed: ("Couldn't save the file.", "Не удалось сохранить файл.")
        case .stoppingWindow: ("Stopping LocTranscriptor…", "LocTranscriptor завершает работу…")
        case .pickMessage: ("Choose meeting recordings", "Выберите записи встреч")
        case .pickButton: ("Add", "Добавить")
        }
    }
}

/// Strings with values.
public enum L10n {
    public static func queued(_ n: Int, _ lang: Lang) -> String { lang == .ru ? "в очереди \(n)" : "\(n) queued" }
    public static func processing(_ n: Int, _ lang: Lang) -> String {
        lang == .ru ? "обрабатывается \(n)" : "\(n) processing"
    }
    public static func failed(_ n: Int, _ lang: Lang) -> String { lang == .ru ? "ошибок \(n)" : "\(n) failed" }

    public static func serviceFailedTitle(_ code: Int32, _ lang: Lang) -> String {
        lang == .ru ? "Сервис не запускается (код \(code))" : "The service doesn't start (code \(code))"
    }

    /// Code 3: the window's block list could not be prepared (the app's own check).
    public static let webSetupErrorCode: Int32 = 3

    public static func configErrorTitle(_ code: Int32, _ lang: Lang) -> String {
        if code == webSetupErrorCode {
            return lang == .ru
                ? "Не удаётся безопасно подготовить окно (код \(code))"
                : "The window can't be prepared safely (code \(code))"
        }
        return lang == .ru ? "Не удаётся открыть папку данных (код \(code))" : "The data folder can't be used (code \(code))"
    }

    /// Status line of the menu bar icon: counters only, never titles or text.
    public static func statusLine(_ status: ServiceStatus, _ lang: Lang) -> String {
        if status.modelsReady == false { return Text.setupNeeded.string(lang) }
        var parts = [Text.running.string(lang)]
        if status.queued > 0 { parts.append(queued(status.queued, lang)) }
        if status.processing > 0 { parts.append(processing(status.processing, lang)) }
        if status.error > 0 { parts.append(failed(status.error, lang)) }
        return parts.joined(separator: " · ")
    }
}
