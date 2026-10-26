// The menu bar icon: queue counters, Open Window, Open at Login, Quit. SF Symbols are
// used for the icon only here, inside the app's UI.

import AppCore
import AppKit

@MainActor
final class StatusItemController: NSObject, NSMenuDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let menu = NSMenu()
    private let stateItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let recordingLineItem = NSMenuItem(title: "", action: nil, keyEquivalent: "")
    private let recordItem = NSMenuItem(title: "", action: #selector(toggleRecording), keyEquivalent: "")
    private let openItem = NSMenuItem(title: "", action: #selector(openWindow), keyEquivalent: "")
    private let loginItem = NSMenuItem(title: "", action: #selector(toggleLogin), keyEquivalent: "")
    private let quitItem = NSMenuItem(title: "", action: #selector(quit), keyEquivalent: "")

    var onOpen: (() -> Void)?
    var onToggleLogin: (() -> Void)?
    var onQuit: (() -> Void)?
    var onToggleRecording: (() -> Void)?
    var onMenuOpen: (() -> Void)?

    override init() {
        super.init()
        stateItem.isEnabled = false
        recordingLineItem.isEnabled = false
        recordingLineItem.isHidden = true
        menu.autoenablesItems = false
        menu.delegate = self
        for entry in [recordItem, openItem, loginItem, quitItem] { entry.target = self }
        for entry in [
            stateItem, recordingLineItem, .separator(), recordItem, openItem, loginItem, .separator(), quitItem,
        ] { menu.addItem(entry) }
        item.menu = menu
    }

    func menuWillOpen(_ menu: NSMenu) { onMenuOpen?() }

    func update(
        state: ServiceState, status: ServiceStatus?, recording: RecordingStatus, lang: Lang,
        login: NSControl.StateValue
    ) {
        let recordingNow = recording.state == .recording
        recordItem.title = (recordingNow || recording.state == .starting ? Text.recordingStop : Text.recordingStart)
            .string(lang)
        recordItem.isEnabled = recording.state != .stopping
        if let line = L10n.recordingLine(recording, lang) {
            recordingLineItem.title = line
            recordingLineItem.isHidden = false
        } else {
            recordingLineItem.isHidden = true
        }
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
        // A red dot while a recording runs. A drawn image cannot be a template, so the
        // symbol is tinted with the menu bar's own text colour.
        item.button?.image = recordingNow ? image.map(Self.withRecordingDot) : image
        item.button?.appearsDisabled = dimmed
        item.button?.setAccessibilityLabel(name)
    }

    private static func withRecordingDot(_ symbol: NSImage) -> NSImage {
        let canvas = NSSize(width: 22, height: 18)
        nonisolated(unsafe) let source = symbol
        let image = NSImage(size: canvas, flipped: false) { rect in
            let size = source.size
            let origin = NSPoint(x: (rect.width - size.width) / 2 - 1, y: (rect.height - size.height) / 2)
            let tinted = NSImage(size: size, flipped: false) { inner in
                source.draw(in: inner)
                NSColor.labelColor.set()
                inner.fill(using: .sourceAtop)
                return true
            }
            tinted.draw(at: origin, from: .zero, operation: .sourceOver, fraction: 1)
            NSColor.systemRed.setFill()
            NSBezierPath(ovalIn: NSRect(x: rect.width - 8, y: rect.height - 9, width: 7, height: 7)).fill()
            return true
        }
        image.isTemplate = false
        return image
    }

    @objc private func toggleRecording() { onToggleRecording?() }
    @objc private func openWindow() { onOpen?() }
    @objc private func toggleLogin() { onToggleLogin?() }
    @objc private func quit() { onQuit?() }
}
