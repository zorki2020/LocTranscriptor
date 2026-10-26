// LocTranscriptor.app: a window with the local service, a menu bar icon and the service
// supervisor. Closing the window keeps the service running; Quit stops it gracefully.
// Privacy: the app talks only to 127.0.0.1 and logs nothing.

import AppCore
import AppKit
import ServiceManagement

let showWindowNotification = Notification.Name("app.loctranscriptor.mac.showWindow")
let gitHubURL = URL(string: "https://github.com/zorki2020/transcribator")!

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private let supervisor = ServiceSupervisor()
    private let poller = StatusPoller()
    private var windowController: MainWindowController!
    private var statusItem: StatusItemController!
    private var recorder: RecorderController!
    private var lang: Lang = .system()
    private var lastStatus: ServiceStatus?
    private var terminating = false
    private var poweringOff = false
    private var stoppingWindow: NSWindow?
    private var moveAlertShown = false

    // MARK: - Launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        let atLogin = LoginItem.launchedAtLogin()
        NSApp.mainMenu = MainMenu.build(lang: lang, target: self)

        windowController = MainWindowController()
        windowController.lang = lang
        windowController.onRetry = { [weak self] in self?.supervisor.retry() }
        windowController.onShowLog = { [weak self] in self?.revealLog() }
        windowController.onRevealData = { [weak self] in self?.revealData() }
        windowController.onPageLoaded = { [weak self] in self?.refreshStatus() }
        windowController.onClose = { NSApp.setActivationPolicy(.accessory) }

        recorder = RecorderController(
            dataDirectory: supervisor.dataDirectory,
            connection: { [weak self] in
                guard let supervisor = self?.supervisor, let origin = supervisor.state.origin,
                      let key = supervisor.state.key else { return nil }
                return (origin, key)
            },
            lang: { [weak self] in self?.lang ?? .system() })
        recorder.onStatus = { [weak self] status in self?.recordingChanged(status) }
        windowController.recorder = recorder

        statusItem = StatusItemController()
        statusItem.onToggleRecording = { [weak self] in self?.toggleRecording() }
        statusItem.onOpen = { [weak self] in self?.showMainWindow() }
        statusItem.onToggleLogin = { [weak self] in self?.toggleLoginItem() }
        statusItem.onQuit = { NSApp.terminate(nil) }
        statusItem.onMenuOpen = { [weak self] in self?.refreshStatus() }

        supervisor.language = { [weak self] in self?.lang ?? .system() }
        supervisor.onChange = { [weak self] state in self?.serviceChanged(state) }

        DistributedNotificationCenter.default().addObserver(
            forName: showWindowNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.showMainWindow() }
        }
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.willPowerOffNotification, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.poweringOff = true }
        }

        updateStatusItem()
        if atLogin {
            // Started at login: only the menu bar icon, no window and no Dock icon.
            NSApp.setActivationPolicy(.accessory)
        } else {
            showMainWindow()
        }
        supervisor.start()
        startPolling()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        showMainWindow()
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    func applicationSupportsSecureRestorableState(_ app: NSApplication) -> Bool { true }

    // MARK: - Window

    @objc func showMainWindow() {
        NSApp.setActivationPolicy(.regular)
        windowController.window.makeKeyAndOrderFront(nil)
        NSApp.activate()
        if !moveAlertShown, AppPaths.needsMoveToApplications(bundlePath: Bundle.main.bundlePath) {
            moveAlertShown = true
            let alert = NSAlert()
            alert.messageText = Text.moveTitle.string(lang)
            alert.informativeText = Text.moveText.string(lang)
            alert.addButton(withTitle: Text.ok.string(lang))
            alert.beginSheetModal(for: windowController.window)
        }
    }

    // MARK: - Service and status

    private func serviceChanged(_ state: ServiceState) {
        windowController.show(state: state)
        if let origin = state.origin, let key = state.key {
            windowController.load(origin: origin, key: key)
            refreshStatus()
            recorder.serviceBecameAvailable()
        } else {
            lastStatus = nil
        }
        updateStatusItem()
    }

    private func startPolling() {
        Task { [weak self] in
            while let self {
                self.refreshStatus()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func refreshStatus() {
        guard let origin = supervisor.state.origin, let key = supervisor.state.key else { return }
        Task {
            let status = await poller.fetch(origin: origin, key: key)
            guard supervisor.state.origin == origin else { return }
            lastStatus = status
            if let status { setLanguage(Lang.from(service: status.uiLang)) }
            updateStatusItem()
        }
    }

    private func setLanguage(_ new: Lang) {
        guard new != lang else { return }
        lang = new
        NSApp.mainMenu = MainMenu.build(lang: lang, target: self)
        windowController.lang = lang
    }

    private func updateStatusItem() {
        statusItem.update(
            state: supervisor.state, status: lastStatus, recording: recorder.status, lang: lang,
            login: LoginItem.menuState)
        if let app = NSApp.mainMenu?.items.first?.submenu {
            app.item(withTag: MenuTag.openAtLogin)?.state = LoginItem.menuState
        }
    }

    // MARK: - Recording

    private func recordingChanged(_ status: RecordingStatus) {
        updateStatusItem()
        windowController.pushRecording(status)
    }

    /// Menu bar: start with the saved settings, or stop.
    private func toggleRecording() {
        switch recorder.status.state {
        case .starting, .recording:
            Task { await recorder.stop() }
        case .stopping:
            break
        case .idle, .queued, .failed:
            Task {
                if let error = await recorder.startFromMenu() { showRecordingError(error) }
            }
        }
    }

    private func showRecordingError(_ error: RecordingError) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = L10n.recordingError(error, lang)
        alert.addButton(withTitle: Text.ok.string(lang))
        let pane: RecordingSettingsPane? = switch error {
        case .micDenied: .mic
        case .startFailed: .systemAudio
        default: nil
        }
        if pane != nil { alert.addButton(withTitle: Text.openSystemSettings.string(lang)) }
        NSApp.activate()
        if alert.runModal() == .alertSecondButtonReturn, let pane { recorder.openSettings(pane) }
    }

    /// Quit while recording: stop, save and queue, but wait at most 10 seconds. What does not
    /// finish stays on disk and is queued at the next launch.
    private func stopRecordingForQuit() async {
        let gate = OneShot()
        Task {
            await recorder.stop()
            gate.fire()
        }
        Task {
            try? await Task.sleep(for: .seconds(10))
            gate.fire()
        }
        await gate.wait()
    }

    // MARK: - Menu actions

    @objc func showAbout() {
        var options: [NSApplication.AboutPanelOptionKey: Any] = [:]
        let credits = Bundle.main.url(
            forResource: "Credits", withExtension: "html", subdirectory: nil, localization: lang.rawValue)
            ?? Bundle.main.url(forResource: "Credits", withExtension: "html")
        if let credits, let data = try? Data(contentsOf: credits),
           let text = NSAttributedString(html: data, documentAttributes: nil)
        {
            options[.credits] = text
        }
        NSApp.activate()
        NSApp.orderFrontStandardAboutPanel(options: options)
    }

    @objc func openSettings() {
        showMainWindow()
        windowController.open(path: "/settings")
    }

    @objc func toggleLoginItem() {
        if AppPaths.needsMoveToApplications(bundlePath: Bundle.main.bundlePath) {
            inform(Text.loginNeedsApplications.string(lang))
            return
        }
        do {
            if try LoginItem.toggle() == .requiresApproval {
                let alert = NSAlert()
                alert.messageText = Text.loginApprovalTitle.string(lang)
                alert.informativeText = Text.loginApprovalText.string(lang)
                alert.addButton(withTitle: Text.openLoginItems.string(lang))
                alert.addButton(withTitle: Text.cancel.string(lang))
                NSApp.activate()
                if alert.runModal() == .alertFirstButtonReturn { SMAppService.openSystemSettingsLoginItems() }
            }
        } catch {
            inform(Text.loginFailed.string(lang))
        }
        updateStatusItem()
    }

    @objc func reloadPage() { windowController.reload() }
    @objc func actualSize() { windowController.zoom(0) }
    @objc func zoomIn() { windowController.zoom(1) }
    @objc func zoomOut() { windowController.zoom(-1) }
    @objc func openGitHub() { NSWorkspace.shared.open(gitHubURL) }

    @objc func showTips() {
        showMainWindow()
        windowController.startTour()
    }

    private func revealLog() {
        let log = supervisor.logFile
        let target = FileManager.default.fileExists(atPath: log.path) ? log : log.deletingLastPathComponent()
        NSWorkspace.shared.activateFileViewerSelecting([target])
    }

    private func revealData() {
        NSWorkspace.shared.activateFileViewerSelecting([supervisor.dataDirectory])
    }

    private func inform(_ text: String) {
        let alert = NSAlert()
        alert.messageText = text
        alert.addButton(withTitle: Text.ok.string(lang))
        NSApp.activate()
        alert.runModal()
    }

    // MARK: - Quit

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !terminating {
            terminating = true
            Task { await quit() }
        }
        return .terminateLater
    }

    private func quit() async {
        if !poweringOff, let origin = supervisor.state.origin, let key = supervisor.state.key {
            let status = await poller.fetch(origin: origin, key: key) ?? lastStatus
            if (status?.processing ?? 0) > 0 {
                let alert = NSAlert()
                alert.alertStyle = .warning
                alert.messageText = Text.quitConfirmTitle.string(lang)
                alert.informativeText = Text.quitConfirmText.string(lang)
                alert.addButton(withTitle: Text.quitButton.string(lang))
                alert.addButton(withTitle: Text.cancel.string(lang))
                NSApp.activate()
                if alert.runModal() != .alertFirstButtonReturn {
                    terminating = false
                    NSApp.reply(toApplicationShouldTerminate: false)
                    return
                }
            }
        }
        // A small "Stopping…" window only if stopping takes more than a second.
        let reminder = Task { [weak self] in
            try? await Task.sleep(for: .seconds(1))
            guard let self, !Task.isCancelled else { return }
            let saving = self.recorder.status.state.isActive
            let window = StatusViews.stoppingWindow(
                text: (saving ? Text.recordingSaving : Text.stoppingWindow).string(self.lang))
            self.stoppingWindow = window
            window.orderFrontRegardless()
        }
        // The service must still be up to take the finished recording.
        if recorder.status.state.isActive { await stopRecordingForQuit() }
        await supervisor.stop()
        reminder.cancel()
        stoppingWindow?.orderOut(nil)
        NSApp.reply(toApplicationShouldTerminate: true)
    }
}
