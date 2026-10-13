// The contract with the service process: ready line, status JSON, exit codes, restart
// policy. Nothing here touches meeting content.

import Foundation

/// The first stdout line of `transcribator serve --app`: `{"ready": true, "port": N}`.
public enum ReadyLine {
    private struct Line: Decodable {
        let ready: Bool
        let port: Int
    }

    /// The port, or nil when the line is anything else.
    public static func parse(_ line: String) -> Int? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.utf8.count <= 200, let data = trimmed.data(using: .utf8),
              let decoded = try? JSONDecoder().decode(Line.self, from: data),
              decoded.ready, (1...65535).contains(decoded.port) else { return nil }
        return decoded.port
    }
}

/// `GET /api/status`: counters only.
public struct ServiceStatus: Decodable, Sendable, Equatable {
    public let queued: Int
    public let processing: Int
    public let done: Int
    public let error: Int
    public let modelsReady: Bool?
    public let uiLang: String?

    enum CodingKeys: String, CodingKey {
        case queued, processing, done, error
        case modelsReady = "models_ready"
        case uiLang = "ui_lang"
    }

    public init(queued: Int, processing: Int, done: Int, error: Int, modelsReady: Bool?, uiLang: String?) {
        self.queued = queued
        self.processing = processing
        self.done = done
        self.error = error
        self.modelsReady = modelsReady
        self.uiLang = uiLang
    }

    public static func decode(_ data: Data) -> ServiceStatus? {
        try? JSONDecoder().decode(ServiceStatus.self, from: data)
    }
}

/// How the service process ended.
public enum ServiceExit: Equatable, Sendable {
    case normal
    /// Another service already uses this data folder (exit code 75).
    case alreadyRunning
    /// The data folder or the configuration can't be used (exit code 2).
    case configError
    case crash(Int32)

    public init(code: Int32) {
        switch code {
        case 0: self = .normal
        case 75: self = .alreadyRunning
        case 2: self = .configError
        default: self = .crash(code)
        }
    }
}

/// Restarts after a crash: pauses of 1, 2, 4, 8, 16 seconds, at most 5 restarts within
/// 2 minutes. After that the window shows an error with "Try Again".
public struct RestartPolicy: Sendable {
    public let maxRestarts: Int
    public let window: TimeInterval
    public let baseDelay: TimeInterval
    private var restarts: [Date] = []

    public init(maxRestarts: Int = 5, window: TimeInterval = 120, baseDelay: TimeInterval = 1) {
        self.maxRestarts = maxRestarts
        self.window = window
        self.baseDelay = baseDelay
    }

    /// The pause before the next restart, or nil when the limit is reached.
    public mutating func nextDelay(now: Date = Date()) -> TimeInterval? {
        restarts.removeAll { now.timeIntervalSince($0) >= window }
        guard restarts.count < maxRestarts else { return nil }
        let delay = baseDelay * Double(1 << restarts.count)
        restarts.append(now)
        return delay
    }

    /// "Try Again" starts from scratch.
    public mutating func reset() { restarts.removeAll() }
}
