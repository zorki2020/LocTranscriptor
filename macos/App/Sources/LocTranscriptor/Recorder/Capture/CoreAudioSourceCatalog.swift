// What the recording interface can offer: apps that play audio (grouped with their helper
// processes), input devices, and the microphone permission.

import AVFoundation
import AppCore
import AppKit
import CoreAudio
import Foundation

/// Unique id prefix of the engine's own private capture devices, hidden from the device list.
let captureDeviceUIDPrefix = "local.loctranscriptor.capture."

struct CoreAudioSourceCatalog: SourceCatalog {
    init() {}

    func apps() -> [AudioAppInfo] {
        let running = NSWorkspace.shared.runningApplications.compactMap {
            app -> CaptureAppGrouping.RunningApp? in
            guard app.activationPolicy == .regular, let id = app.bundleIdentifier else { return nil }
            return CaptureAppGrouping.RunningApp(bundleId: id, name: app.localizedName ?? id)
        }
        let processes = HAL.processes().compactMap { p -> CaptureAppGrouping.AudioProcess? in
            guard !p.bundleId.isEmpty, p.pid != getpid() else { return nil }
            return CaptureAppGrouping.AudioProcess(bundleId: p.bundleId, outputting: p.outputting)
        }
        return CaptureAppGrouping.catalog(
            apps: running, processes: processes, selfBundleId: Bundle.main.bundleIdentifier)
    }

    func microphones() -> [MicrophoneInfo] {
        var devices: [(uid: String, name: String)] = []
        for device in HAL.devices() {
            guard !HAL.inputBuffers(device).isEmpty, !HAL.isHidden(device),
                let uid = HAL.uid(device), !uid.hasPrefix(captureDeviceUIDPrefix)
            else { continue }
            devices.append((uid, HAL.name(device) ?? uid))
        }
        return CaptureDeviceList.microphones(
            devices, defaultUID: HAL.defaultInputDevice().flatMap(HAL.uid))
    }

    func micPermission() -> MicPermission {
        Self.micPermission()
    }

    static func micPermission() -> MicPermission {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return .granted
        case .notDetermined: return .undetermined
        default: return .denied
        }
    }

    func requestMicAccess() async -> Bool {
        switch Self.micPermission() {
        case .granted: return true
        case .denied: return false
        case .undetermined: return await AVCaptureDevice.requestAccess(for: .audio)
        }
    }
}
