// The menu bar icon: queue counters, Open Window, Open at Login, Quit. SF Symbols are
// used for the icon only here, inside the app's UI.

import AppCore
import AppKit

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let stateItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let openItem = NSMenuItem(title: "", action: #selector(openWindow), keyEquivalent: "")
    private let loginItem = NSMenuItem(title: "", action: #selector(toggleLogin), keyEquivalent: "")
    private let quitItem = NSMenuItem(title: "", action: #selector(quit), keyEquivalent: "")

    var onOpen: (() -> Void)?
    var onToggleLogin: (() -> Void)?
    var onQuit: (() -> Void)?
    var onMenuOpen: (() -> Void)?

    override init() {
        super.init()
        stateItem.isEnabled = false
        menu.autoenablesItems = false
        menu.delegate = self
        for entry in [openItem, loginItem, quitItem] { entry.target = self }
        for entry in [stateItem, .separator(), openItem, loginItem, .separator(), quitItem] { menu.addItem(entry) }
        item.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) { onMenuOpen?() }

    func update(state: ServiceState, status: ServiceStatus?, lang: Lang, login: NSControl.StateValue) {
        openItem.title = Text.openWindow.string(lang)
        loginItem.title = Text.openAtLogin.string(lang)
        loginItem.state = login
        quitItem.title = Text.quitShort.string(lang)
        var symbol = "waveform"
        var dimmed = false
        switch state {
        case .starting:
            stateItem.title = Text.starting.string(lang)
            dimmed = true
        case .stopping:
            stateItem.title = Text.stopping.string(lang)
            dimmed = true
        case .running:
            if let status {
                stateItem.title = L10n.statusLine(status, lang)
                if status.processing > 0 { symbol = "waveform.badge.ellipsis" }
            } else {
                stateItem.title = Text.running.string(lang)
            }
        case .failed(let code), .configError(let code):
            stateItem.title = L10n.serviceFailedTitle(code, lang)
            dimmed = true
        case .alreadyRunning:
            stateItem.title = Text.alreadyRunningTitle.string(lang)
            dimmed = true
        case .stopped:
            stateItem.title = Text.notRunning.string(lang)
            dimmed = true
        }
        let name = Text.appName.string(lang)
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: name)
            ?? NSImage(systemSymbolName: "waveform", accessibilityDescription: name)
        image?.isTemplate = true
        item.button?.image = image
        item.button?.appearsDisabled = dimmed
        item.button?.setAccessibilityLabel(name)
    }

    @objc private func openWindow() { onOpen?() }
    @objc private func toggleLogin() { onToggleLogin?() }
    @objc private func quit() { onQuit?() }
}
