// Open at Login through SMAppService.mainApp, and detection of a launch at login.

import AppKit
import ServiceManagement

@MainActor
enum LoginItem {
    static var status: SMAppService.Status { SMAppService.mainApp.status }

    static var menuState: NSControl.StateValue {
        switch status {
        case .enabled: .on
        case .requiresApproval: .mixed
        default: .off
        }
    }

    /// Turns it on or off. Returns the new status.
    static func toggle() throws -> SMAppService.Status {
        let service = SMAppService.mainApp
        if service.status == .enabled || service.status == .requiresApproval {
            try service.unregister()
        } else {
            try service.register()
        }
        return service.status
    }

    /// True when macOS started the app as a login item (the "open application" Apple
    /// event carries keyAELaunchedAsLogInItem). Call from applicationDidFinishLaunching.
    static func launchedAtLogin() -> Bool {
        guard let event = NSAppleEventManager.shared().currentAppleEvent,
              event.eventID == AEEventID(kAEOpenApplication),
              let data = event.paramDescriptor(forKeyword: AEKeyword(keyAEPropData))
        else { return false }
        return data.enumCodeValue == OSType(keyAELaunchedAsLogInItem)
    }
}
