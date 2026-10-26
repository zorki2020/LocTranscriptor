// What the recording session reports to the page and the menu bar: the state machine's
// states, error and warning codes, the status object and its JSON shape. Codes only,
// never titles or file names.

import Foundation

public enum RecordingState: String, Sendable, Equatable {
    case idle, starting, recording, stopping, queued, failed

    /// A recording is being made or wrapped up: a new one cannot start, Quit must wait.
    public var isActive: Bool { self == .starting || self == .recording || self == .stopping }
}

public enum RecordingError: String, Sendable, Equatable, Error, CaseIterable {
    case alreadyRecording = "already_recording"
    case micDenied = "mic_denied"
    case noSource = "no_source"
    case sourceGone = "source_gone"
    case startFailed = "start_failed"
    case diskLow = "disk_low"
    case finalizeFailed = "finalize_failed"
    case enqueueFailed = "enqueue_failed"
    /// The recording reached the hard limit, stopped by itself and was saved.
    case maxDuration = "max_duration"

    public init(_ failure: CaptureFailure) {
        switch failure {
        case .micDenied: self = .micDenied
        case .sourceGone: self = .sourceGone
        case .startFailed: self = .startFailed
        }
    }
}

public enum RecordingWarning: String, Sendable, Equatable, CaseIterable {
    case silentSystem = "silent_system"
    case silentMic = "silent_mic"
    case deviceChanged = "device_changed"
    case diskLow = "disk_low"

    /// Most important first: what the status shows when several warnings are active.
    public static let priority: [RecordingWarning] = [.diskLow, .silentSystem, .silentMic, .deviceChanged]

    public init(_ warning: CaptureWarning) {
        switch warning {
        case .silentSystem: self = .silentSystem
        case .silentMic: self = .silentMic
        case .deviceChanged: self = .deviceChanged
        }
    }
}

/// What is being recorded, as the page sees it.
public struct RecordingSourceInfo: Sendable, Equatable {
    public enum Kind: String, Sendable { case all, app }
    public var kind: Kind
    public var bundleIds: [String]
    public var name: String?

    public init(kind: Kind, bundleIds: [String] = [], name: String? = nil) {
        self.kind = kind
        self.bundleIds = bundleIds
        self.name = name
    }

    public static let all = RecordingSourceInfo(kind: .all)

    public var captureSource: CaptureSource {
        kind == .all ? .allExceptSelf : .apps(bundleIds: bundleIds)
    }
}

public struct RecordingStatus: Sendable, Equatable {
    public var state: RecordingState = .idle
    public var elapsedS: Double = 0
    public var mic: Bool = false
    public var source: RecordingSourceInfo = .all
    public var levelMic: Double = 0
    public var levelSystem: Double = 0
    public var warning: RecordingWarning?
    public var error: RecordingError?
    public var meetingId: Int?
    /// What the user asked for: keep the audio next to the transcript.
    public var keepAudio: Bool = false
    /// Nil until the recording is finalized. Then true when the m4a exists and was passed on,
    /// false when it was requested but could not be made.
    public var audioSaved: Bool?

    public init() {}

    public static let idle = RecordingStatus()

    /// The contract's status object. Only property-list types (String, NSNumber, NSNull,
    /// Dictionary), so it can go to `callAsyncJavaScript` as an argument or into a reply.
    public var jsonObject: [String: Any] {
        var sourceObject: [String: Any] = ["kind": source.kind.rawValue]
        if let name = source.name { sourceObject["name"] = name }
        return [
            "state": state.rawValue,
            "elapsedS": elapsedS,
            "mic": mic,
            "source": sourceObject,
            "level": ["mic": levelMic, "system": levelSystem],
            "warning": warning?.rawValue ?? NSNull(),
            "error": error?.rawValue ?? NSNull(),
            "meetingId": meetingId ?? NSNull(),
            "keepAudio": keepAudio,
            "audioSaved": audioSaved ?? NSNull(),
        ]
    }
}

/// `recording.start` payload, already validated.
public struct RecordingStartRequest: Sendable, Equatable {
    public var source: RecordingSourceInfo
    public var micEnabled: Bool
    public var micId: String?
    public var language: String
    public var title: String?
    public var keepAudio: Bool

    public init(
        source: RecordingSourceInfo, micEnabled: Bool, micId: String?, language: String, title: String?,
        keepAudio: Bool = false
    ) {
        self.keepAudio = keepAudio
        self.source = source
        self.micEnabled = micEnabled
        self.micId = micId
        self.language = language
        self.title = title
    }

    public var config: CaptureConfig {
        CaptureConfig(source: source.captureSource, micEnabled: micEnabled, micDeviceId: micId)
    }

    public var channels: Int { micEnabled ? 2 : 1 }
}

/// Which System Settings pane `recording.openSettings` opens.
public enum RecordingSettingsPane: String, Sendable, Equatable {
    case mic
    case systemAudio

    public var url: URL {
        switch self {
        case .mic: URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!
        case .systemAudio: URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture")!
        }
    }

    /// When the specific pane does not open.
    public static let fallbackURL = URL(string: "x-apple.systempreferences:com.apple.preference.security")!
}

/// Messages of the page bridge with a `type` key (`recording.*`).
public enum RecordingCommand: Equatable, Sendable {
    case sources
    case start(Result<RecordingStartRequest, RecordingError>)
    case stop
    case status
    case openSettings(RecordingSettingsPane)

    public init?(body: Any) {
        guard let dict = body as? [String: Any], let type = dict["type"] as? String,
              type.hasPrefix("recording.") else { return nil }
        switch type {
        case "recording.sources": self = .sources
        case "recording.stop": self = .stop
        case "recording.status": self = .status
        case "recording.start": self = .start(Self.parseStart(dict))
        case "recording.openSettings":
            guard let raw = dict["pane"] as? String, let pane = RecordingSettingsPane(rawValue: raw) else { return nil }
            self = .openSettings(pane)
        default: return nil
        }
    }

    static func parseStart(_ dict: [String: Any]) -> Result<RecordingStartRequest, RecordingError> {
        guard let source = dict["source"] as? [String: Any], let kind = source["kind"] as? String else {
            return .failure(.noSource)
        }
        let info: RecordingSourceInfo
        switch kind {
        case "all":
            info = .all
        case "app":
            let ids = (source["bundleIds"] as? [Any])?.compactMap { $0 as? String } ?? []
            let valid = ids.filter { !$0.isEmpty && $0.utf8.count <= 256 }
            guard !valid.isEmpty, valid.count == ids.count, valid.count <= 64 else { return .failure(.noSource) }
            let name = (source["name"] as? String).map { String($0.prefix(200)) }
            info = RecordingSourceInfo(kind: .app, bundleIds: valid, name: name)
        default:
            return .failure(.noSource)
        }
        guard let mic = dict["mic"] as? [String: Any], let enabled = mic["enabled"] as? Bool else {
            return .failure(.startFailed)
        }
        let micId = mic["id"] as? String
        guard let language = dict["language"] as? String, language == "ru" || language == "en" else {
            return .failure(.startFailed)
        }
        var title = dict["title"] as? String
        if let value = title {
            let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
            title = trimmed.isEmpty ? nil : String(trimmed.prefix(300))
        }
        return .success(RecordingStartRequest(
            source: info, micEnabled: enabled, micId: micId?.isEmpty == false ? micId : nil,
            language: language, title: title, keepAudio: dict["keepAudio"] as? Bool ?? false))
    }
}

/// Settings of the last recording, kept in UserDefaults and returned as `last`.
public struct RecordingLastSettings: Codable, Sendable, Equatable {
    public var sourceKind: String
    public var bundleIds: [String]
    public var sourceName: String?
    public var micEnabled: Bool
    public var micId: String?
    public var keepAudio: Bool

    public init(
        sourceKind: String, bundleIds: [String], sourceName: String?, micEnabled: Bool, micId: String?,
        keepAudio: Bool = false
    ) {
        self.keepAudio = keepAudio
        self.sourceKind = sourceKind
        self.bundleIds = bundleIds
        self.sourceName = sourceName
        self.micEnabled = micEnabled
        self.micId = micId
    }

    /// Nothing saved yet: all system audio, microphone on.
    public static let standard = RecordingLastSettings(
        sourceKind: "all", bundleIds: [], sourceName: nil, micEnabled: true, micId: nil)

    public init(_ request: RecordingStartRequest) {
        self.init(
            sourceKind: request.source.kind.rawValue, bundleIds: request.source.bundleIds,
            sourceName: request.source.name, micEnabled: request.micEnabled, micId: request.micId,
            keepAudio: request.keepAudio)
    }

    enum CodingKeys: String, CodingKey {
        case sourceKind, bundleIds, sourceName, micEnabled, micId, keepAudio
    }

    /// Settings saved by an older version have no `keepAudio`: it stays off.
    public init(from decoder: any Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        sourceKind = try values.decode(String.self, forKey: .sourceKind)
        bundleIds = try values.decode([String].self, forKey: .bundleIds)
        sourceName = try values.decodeIfPresent(String.self, forKey: .sourceName)
        micEnabled = try values.decode(Bool.self, forKey: .micEnabled)
        micId = try values.decodeIfPresent(String.self, forKey: .micId)
        keepAudio = try values.decodeIfPresent(Bool.self, forKey: .keepAudio) ?? false
    }

    static let defaultsKey = "recording.last"

    public static func load(from defaults: UserDefaults = .standard) -> RecordingLastSettings? {
        guard let data = defaults.data(forKey: defaultsKey),
              let value = try? JSONDecoder().decode(RecordingLastSettings.self, from: data),
              value.sourceKind == "all" || (value.sourceKind == "app" && !value.bundleIds.isEmpty)
        else { return nil }
        return value
    }

    public func save(to defaults: UserDefaults = .standard) {
        if let data = try? JSONEncoder().encode(self) { defaults.set(data, forKey: Self.defaultsKey) }
    }

    public var sourceInfo: RecordingSourceInfo {
        sourceKind == "app"
            ? RecordingSourceInfo(kind: .app, bundleIds: bundleIds, name: sourceName) : .all
    }

    /// A start request from the saved settings (the menu bar item).
    public func request(language: String) -> RecordingStartRequest {
        RecordingStartRequest(
            source: sourceInfo, micEnabled: micEnabled, micId: micId, language: language, title: nil,
            keepAudio: keepAudio)
    }

    public var jsonObject: [String: Any] {
        var source: [String: Any] = ["kind": sourceKind]
        if sourceKind == "app" {
            source["bundleIds"] = bundleIds
            if let sourceName { source["name"] = sourceName }
        }
        return ["source": source, "micEnabled": micEnabled, "micId": micId ?? NSNull(), "keepAudio": keepAudio]
    }
}

public enum RecordingSourcesReply {
    /// The reply of `recording.sources`.
    public static func make(
        apps: [AudioAppInfo], microphones: [MicrophoneInfo], last: RecordingLastSettings, permission: MicPermission
    ) -> [String: Any] {
        [
            "apps": apps.map { ["bundleIds": $0.bundleIds, "name": $0.name, "outputting": $0.outputting] as [String: Any] },
            "microphones": microphones.map { ["id": $0.id, "name": $0.name, "isDefault": $0.isDefault] as [String: Any] },
            "last": last.jsonObject,
            "micPermission": permission.rawValue,
        ]
    }
}

/// Runs a continuation once, whichever of several racing callers comes first.
public final class OneShot: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?
    private var fired = false

    public init() {}

    public func wait() async {
        await withCheckedContinuation { (cont: CheckedContinuation<Void, Never>) in
            lock.lock()
            if fired {
                lock.unlock()
                cont.resume()
            } else {
                continuation = cont
                lock.unlock()
            }
        }
    }

    public func fire() {
        lock.lock()
        fired = true
        let cont = continuation
        continuation = nil
        lock.unlock()
        cont?.resume()
    }
}

extension PageScript {
    /// Pushes the status (argument `status`) to the page's recorder block.
    public static let recordingStatus =
        "return window.transcribatorRecorder && typeof window.transcribatorRecorder.onStatus === 'function' ? (window.transcribatorRecorder.onStatus(status), true) : false"
}
