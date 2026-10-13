// Transcribator menu bar icon: shows the state of the local service
// and controls its LaunchAgent. AppKit + Foundation only, no third-party dependencies.
// Privacy: talks only to http://127.0.0.1:8765/api/status, logs nothing.

import AppKit
import Foundation

private let serviceURL = URL(string: "http://127.0.0.1:8765")!
private let statusURL = URL(string: "http://127.0.0.1:8765/api/status")!

/// UI strings in English and Russian. Russian if the first preferred language is Russian,
/// English otherwise.
enum L10n {
    static let isRussian = Locale.preferredLanguages.first?.lowercased().hasPrefix("ru") ?? false

    private static func pick(_ en: String, _ ru: String) -> String { isRussian ? ru : en }

    static let appName = "LocTranscriptor"

    static var checking: String { pick("Checking…", "Проверяется…") }
    static var stopped: String { pick("Stopped", "Остановлен") }
    static var starting: String { pick("Starting…", "Запускается…") }
    static var running: String { pick("Running", "Работает") }
    static var runningManual: String { pick("Running (started manually)", "Работает (запущен вручную)") }
    static func queued(_ n: Int) -> String { pick("\(n) queued", "в очереди \(n)") }
    static func processing(_ n: Int) -> String { pick("\(n) processing", "обрабатывается \(n)") }
    static func failed(_ n: Int) -> String { pick("\(n) failed", "ошибок \(n)") }

    static var open: String { pick("Open LocTranscriptor", "Открыть LocTranscriptor") }
    static var start: String { pick("Start", "Запустить") }
    static var stop: String { pick("Stop", "Остановить") }
    static var startedManually: String { pick("Started manually", "Запущен вручную") }
    static var startAtLogin: String { pick("Start at login", "Запускать при входе") }
    static var quit: String { pick("Quit icon", "Выйти из значка") }

    static var autostartMissing: String {
        pick("Autostart is not installed. Run in Terminal: ",
             "Автозапуск не установлен. Выполните в терминале: ")
            + "cd ~/transcribator && uv run transcribator autostart install"
    }
    static func startFailed(_ code: Int32) -> String {
        pick("Could not start the service (launchctl code \(code)).",
             "Не удалось запустить сервис (код launchctl \(code)).")
    }
    static func stopFailed(_ code: Int32) -> String {
        pick("Could not stop the service (launchctl code \(code)).",
             "Не удалось остановить сервис (код launchctl \(code)).")
    }
    static func loginFailed(_ code: Int32) -> String {
        pick("Could not change the start-at-login setting (launchctl code \(code)).",
             "Не удалось изменить автозапуск (код launchctl \(code)).")
    }
}

/// /api/status response (counters only, no meeting content).
struct ServiceStatus: Decodable, Sendable {
    let queued: Int
    let processing: Int
    let done: Int
    let error: Int
}

enum ServiceState: Sendable {
    case stopped                 // agent not loaded, HTTP not responding
    case starting                // agent loaded, HTTP not responding yet
    case running(ServiceStatus, manual: Bool)  // HTTP responds; manual: agent not loaded (started by hand)
}

/// /bin/launchctl calls: absolute path, argument array, no shell.
/// Synchronous, so they are only called from a background task (see `background`).
enum Launchctl {
    static let label = "local.transcribator"
    static var domain: String { "gui/\(getuid())" }
    static var target: String { "\(domain)/\(label)" }
    static var plistPath: String {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(label).plist").path
    }

    /// Runs launchctl, returns the exit code and stdout (stderr is discarded).
    @discardableResult
    static func run(_ args: [String]) -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        process.arguments = args
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { return (-1, "") }
        // Read all output before waiting: otherwise a full pipe can deadlock.
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return (process.terminationStatus, String(decoding: data, as: UTF8.self))
    }

    static func isLoaded() -> Bool { run(["print", target]).status == 0 }

    /// Looks for a line like `"local.transcribator" => disabled` (or `=> true` on older versions).
    static func isDisabled() -> Bool {
        let output = run(["print-disabled", domain]).output
        return output.split(separator: "\n").contains { line in
            let s = line.trimmingCharacters(in: .whitespaces)
            return s.hasPrefix("\"\(label)\"") && (s.hasSuffix("disabled") || s.hasSuffix("true"))
        }
    }

    static func stop() -> Int32 { run(["bootout", target]).status }

    /// launchd won't load a disabled service, so enable it for the start
    /// and then restore the "don't start at login" flag.
    static func start(disabled: Bool) -> Int32 {
        if disabled { run(["enable", target]) }
        let status = run(["bootstrap", domain, plistPath]).status
        if disabled { run(["disable", target]) }
        return status
    }

    static func setEnabled(_ enabled: Bool) -> Int32 {
        run([enabled ? "enable" : "disable", target]).status
    }
}

/// Runs blocking work off the main thread.
func background<T: Sendable>(_ work: @escaping @Sendable () -> T) async -> T {
    await Task.detached(priority: .userInitiated) { work() }.value
}

/// Forbids redirects: the request must never go anywhere but 127.0.0.1.
final class NoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(_ session: URLSession, task: URLSessionTask,
                    willPerformHTTPRedirection response: HTTPURLResponse,
                    newRequest request: URLRequest) async -> URLRequest? { nil }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let stateItem = NSMenuItem(title: L10n.checking, action: nil, keyEquivalent: "")
    private let toggleItem = NSMenuItem(title: L10n.start, action: #selector(toggleService), keyEquivalent: "")
    private let loginItem = NSMenuItem(title: L10n.startAtLogin, action: #selector(toggleLogin), keyEquivalent: "")
    private var state: ServiceState = .stopped
    private var disabled = false
    private var refreshing = false
    private var refreshPending = false  // a refresh was requested while one was in flight

    // Ephemeral session: no cache, cookies or proxy, 2 s timeout.
    private let session: URLSession = {
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 2
        config.timeoutIntervalForResource = 2
        return URLSession(configuration: config)
    }()

    func applicationDidFinishLaunching(_ notification: Notification) {
        stateItem.isEnabled = false
        menu.autoenablesItems = false
        menu.delegate = self
        let openItem = NSMenuItem(title: L10n.open, action: #selector(openService), keyEquivalent: "")
        let quitItem = NSMenuItem(title: L10n.quit, action: #selector(quit), keyEquivalent: "")
        for item in [openItem, toggleItem, loginItem, quitItem] { item.target = self }
        for item in [stateItem, openItem, .separator(), toggleItem, loginItem, .separator(), quitItem] {
            menu.addItem(item)
        }
        statusItem.menu = menu
        updateUI()
        // Poll every 5 seconds while the app is alive.
        Task { [weak self] in
            while let self {
                self.refresh()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    func menuWillOpen(_ menu: NSMenu) { refresh() }

    // MARK: - State

    /// If a refresh is already running, remembers the request and repeats it right after.
    private func refresh() {
        guard !refreshing else {
            refreshPending = true
            return
        }
        refreshing = true
        Task {
            let loaded = await background { Launchctl.isLoaded() }
            let disabled = await background { Launchctl.isDisabled() }
            // Always poll HTTP: the service may have been started by hand, without the LaunchAgent.
            let status = await fetchStatus()
            let newState: ServiceState
            if let status {
                newState = .running(status, manual: !loaded)
            } else {
                newState = loaded ? .starting : .stopped
            }
            self.state = newState
            self.disabled = disabled
            self.refreshing = false
            updateUI()
            if self.refreshPending {
                self.refreshPending = false
                refresh()
            }
        }
    }

    private func fetchStatus() async -> ServiceStatus? {
        var request = URLRequest(url: statusURL)
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 2
        guard let (data, response) = try? await session.data(for: request, delegate: NoRedirects()),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return try? JSONDecoder().decode(ServiceStatus.self, from: data)
    }

    private func updateUI() {
        var symbol = "waveform"
        var stopped = false
        var manual = false
        switch state {
        case .stopped:
            stateItem.title = L10n.stopped
            stopped = true
        case .starting:
            stateItem.title = L10n.starting
        case .running(let s, let isManual):
            manual = isManual
            var parts = [isManual ? L10n.runningManual : L10n.running]
            if s.queued > 0 { parts.append(L10n.queued(s.queued)) }
            if s.processing > 0 { parts.append(L10n.processing(s.processing)) }
            if s.error > 0 { parts.append(L10n.failed(s.error)) }
            stateItem.title = parts.joined(separator: " · ")
            if s.processing > 0 { symbol = "waveform.badge.ellipsis" }
        }
        // A manually started service is not owned by launchd: launchctl can't stop it.
        toggleItem.title = manual ? L10n.startedManually : (stopped ? L10n.start : L10n.stop)
        toggleItem.isEnabled = !manual
        loginItem.state = disabled ? .off : .on
        // waveform.badge.ellipsis is missing in some SF Symbols versions: fall back to waveform.circle.
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: L10n.appName)
            ?? NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: L10n.appName)
        image?.isTemplate = true
        statusItem.button?.image = image
        statusItem.button?.appearsDisabled = stopped
        statusItem.button?.setAccessibilityLabel(L10n.appName)
    }

    // MARK: - Menu actions

    @objc private func openService() { NSWorkspace.shared.open(serviceURL) }

    @objc private func toggleService() {
        if case .running(_, manual: true) = state { return }
        if case .stopped = state {
            guard FileManager.default.fileExists(atPath: Launchctl.plistPath) else {
                showAlert(L10n.autostartMissing)
                return
            }
            let wasDisabled = disabled
            perform(L10n.startFailed) { Launchctl.start(disabled: wasDisabled) }
        } else {
            perform(L10n.stopFailed) { Launchctl.stop() }
        }
    }

    @objc private func toggleLogin() {
        let enable = disabled
        perform(L10n.loginFailed) { Launchctl.setEnabled(enable) }
    }

    @objc private func quit() { NSApp.terminate(nil) }

    /// Runs a launchctl command in the background, shows the exit code on failure, then refreshes the menu.
    private func perform(_ failure: @escaping (Int32) -> String, _ work: @escaping @Sendable () -> Int32) {
        Task {
            let status = await background(work)
            if status != 0 { showAlert(failure(status)) }
            refresh()
        }
    }

    private func showAlert(_ text: String) {
        NSApp.activate()
        let alert = NSAlert()
        alert.messageText = L10n.appName
        alert.informativeText = text
        alert.runModal()
    }
}

// Entry point: top-level code in main.swift runs on the main thread.
MainActor.assumeIsolated {
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
}
