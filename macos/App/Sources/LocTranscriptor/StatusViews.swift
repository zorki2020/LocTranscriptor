// Small native views: "Starting…", the service error with its buttons, "Stopping…".

import AppKit

/// An NSButton that runs a closure.
final class ActionButton: NSButton {
    private var handler: (() -> Void)?

    convenience init(title: String, handler: @escaping () -> Void) {
        self.init(frame: .zero)
        self.title = title
        self.handler = handler
        bezelStyle = .push
        target = self
        action = #selector(run)
    }

    @objc private func run() { handler?() }
}

@MainActor
enum StatusViews {
    static func button(_ title: String, primary: Bool, handler: @escaping () -> Void) -> NSButton {
        let button = ActionButton(title: title, handler: handler)
        if primary { button.keyEquivalent = "\r" }
        return button
    }

    static func loading(text: String) -> NSView {
        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.isIndeterminate = true
        spinner.startAnimation(nil)
        let label = NSTextField(labelWithString: text)
        label.textColor = .secondaryLabelColor
        label.font = .systemFont(ofSize: NSFont.systemFontSize)
        return centered([spinner, label], spacing: 12)
    }

    static func error(title: String, text: String, buttons: [NSButton]) -> NSView {
        let icon = NSImageView(image: NSImage(named: NSImage.cautionName) ?? NSImage())
        icon.imageScaling = .scaleProportionallyUpOrDown
        icon.widthAnchor.constraint(equalToConstant: 48).isActive = true
        icon.heightAnchor.constraint(equalToConstant: 48).isActive = true
        let heading = NSTextField(labelWithString: title)
        heading.font = .systemFont(ofSize: 17, weight: .semibold)
        heading.alignment = .center
        let body = NSTextField(wrappingLabelWithString: text)
        body.alignment = .center
        body.textColor = .secondaryLabelColor
        body.preferredMaxLayoutWidth = 420
        body.widthAnchor.constraint(lessThanOrEqualToConstant: 420).isActive = true
        let row = NSStackView(views: buttons)
        row.orientation = .horizontal
        row.spacing = 12
        let stack = centered([icon, heading, body, row], spacing: 12)
        return stack
    }

    private static func centered(_ views: [NSView], spacing: CGFloat) -> NSView {
        let stack = NSStackView(views: views)
        stack.orientation = .vertical
        stack.alignment = .centerX
        stack.spacing = spacing
        stack.translatesAutoresizingMaskIntoConstraints = false
        let container = NSView()
        container.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            stack.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            stack.leadingAnchor.constraint(greaterThanOrEqualTo: container.leadingAnchor, constant: 24),
            stack.trailingAnchor.constraint(lessThanOrEqualTo: container.trailingAnchor, constant: -24),
        ])
        return container
    }

    /// A small window shown while the service stops (only if that takes over a second).
    static func stoppingWindow(text: String) -> NSWindow {
        let window = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 300, height: 110), styleMask: [.titled],
            backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.title = ""
        window.contentView = loading(text: text)
        window.center()
        window.level = .floating
        return window
    }
}
