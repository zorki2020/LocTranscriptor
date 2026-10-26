// The seam between the recording session (what to record, where it goes) and the capture
// engine (Core Audio taps and the microphone). The session talks only to these types, so
// it can be tested with a fake engine and the engine can change without touching it.
//
// Fixed interface: do not change signatures without agreeing it with both sides.

import Foundation

/// What to capture from the system output.
public enum CaptureSource: Sendable, Equatable {
    /// Every app's output except LocTranscriptor itself.
    case allExceptSelf
    /// The output of one app: all bundle ids of its process group (helpers included).
    case apps(bundleIds: [String])
}

public struct CaptureConfig: Sendable, Equatable {
    public var source: CaptureSource
    public var micEnabled: Bool
    /// Core Audio device UID of the microphone, nil for the system default input.
    public var micDeviceId: String?

    public init(source: CaptureSource, micEnabled: Bool, micDeviceId: String?) {
        self.source = source
        self.micEnabled = micEnabled
        self.micDeviceId = micDeviceId
    }
}

public struct CaptureLevels: Sendable, Equatable {
    /// Peak of the last interval, 0...1.
    public var mic: Float
    public var system: Float

    public init(mic: Float, system: Float) {
        self.mic = mic
        self.system = system
    }
}

public enum CaptureWarning: String, Sendable, Equatable {
    case silentSystem = "silent_system"
    case silentMic = "silent_mic"
    case deviceChanged = "device_changed"
}

public enum CaptureFailure: String, Sendable, Equatable, Error {
    case micDenied = "mic_denied"
    case sourceGone = "source_gone"
    case startFailed = "start_failed"
}

public enum CaptureEvent: Sendable, Equatable {
    /// About twice a second while capturing.
    case levels(CaptureLevels)
    /// Capture goes on, the user should know.
    case warning(CaptureWarning)
    /// Capture is over and cannot recover by itself. The session finalizes what it has.
    case failed(CaptureFailure)
}

public protocol CaptureEngine: AnyObject, Sendable {
    /// Install the handlers before `start`. `samples` receives interleaved Int16 frames at
    /// 16 kHz: one channel (system audio) when the microphone is off, two channels
    /// (0 = microphone, 1 = system audio) when it is on. Both handlers are called from
    /// background queues and the engine does not touch the array after the call returns.
    func setHandlers(
        samples: @escaping @Sendable (_ frames: [Int16], _ channels: Int) -> Void,
        events: @escaping @Sendable (CaptureEvent) -> Void
    )

    /// Starts capturing. It can block for a long time on the first run, while a system
    /// permission dialog is open, so never call it on the main thread.
    func start(_ config: CaptureConfig) throws

    /// Stops capturing. Safe to call more than once and when not started.
    func stop()
}

public struct AudioAppInfo: Sendable, Equatable {
    /// Bundle ids of the app and its helper processes that output audio, grouped.
    public var bundleIds: [String]
    public var name: String
    /// True while one of the group's processes is playing audio right now.
    public var outputting: Bool

    public init(bundleIds: [String], name: String, outputting: Bool) {
        self.bundleIds = bundleIds
        self.name = name
        self.outputting = outputting
    }
}

public struct MicrophoneInfo: Sendable, Equatable {
    public var id: String
    public var name: String
    public var isDefault: Bool

    public init(id: String, name: String, isDefault: Bool) {
        self.id = id
        self.name = name
        self.isDefault = isDefault
    }
}

public enum MicPermission: String, Sendable, Equatable {
    case granted
    case denied
    case undetermined
}

public protocol SourceCatalog: Sendable {
    /// Apps that can be chosen as a source: those outputting audio now plus well-known
    /// conferencing apps and browsers that are running. Names are for the interface only,
    /// never for logs.
    func apps() -> [AudioAppInfo]
    func microphones() -> [MicrophoneInfo]
    func micPermission() -> MicPermission
    /// Asks the system for microphone access if it is undetermined. Returns the final answer.
    func requestMicAccess() async -> Bool
}
