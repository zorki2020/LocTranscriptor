// Entry point. One instance per user: a second launch brings the first one forward
// and exits.

import AppKit

@MainActor
private func anotherInstanceRunning() -> Bool {
    // A bare binary (development) has no bundle id: no check.
    guard let id = Bundle.main.bundleIdentifier else { return false }
    let others = NSRunningApplication.runningApplications(withBundleIdentifier: id)
        .filter { $0.processIdentifier != getpid() }
    guard let other = others.first else { return false }
    DistributedNotificationCenter.default().postNotificationName(
        showWindowNotification, object: nil, userInfo: nil, deliverImmediately: true)
    other.activate()
    return true
}

// Writing the access key to a service that has just died must not kill the app.
signal(SIGPIPE, SIG_IGN)

MainActor.assumeIsolated {
    if anotherInstanceRunning() { exit(0) }
    let app = NSApplication.shared
    let delegate = AppDelegate()
    app.delegate = delegate
    app.run()
}
