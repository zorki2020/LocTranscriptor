// Tells the local service about a finished recording: GET /api/csrf, then POST
// /api/recordings. Only 127.0.0.1, no redirects, no proxy, no cache, no cookie storage.
// Privacy: file names, titles and error texts are never logged, only codes come back.

import Foundation

public struct RecordingEnqueuePayload: Sendable, Equatable {
    public var ref: String
    public var title: String?
    public var startedAt: Date
    public var language: String
    public var layout: String
    public var durationS: Double
    /// `rec<hex>.m4a` in `audio/` when the audio is kept, else nil.
    public var audioRef: String?

    public init(
        ref: String, title: String?, startedAt: Date, language: String, layout: String, durationS: Double,
        audioRef: String? = nil
    ) {
        self.audioRef = audioRef
        self.ref = ref
        self.title = title
        self.startedAt = startedAt
        self.language = language
        self.layout = layout
        self.durationS = durationS
    }
}

public enum RecordingEnqueueOutcome: Sendable, Equatable {
    case queued(meetingId: Int)
    /// The service answered with an error code that retrying will not fix.
    case rejected(String)
    /// No answer (service down, network error, server error) after all retries.
    case unavailable
}

public protocol RecordingEnqueuing: Sendable {
    func enqueue(_ payload: RecordingEnqueuePayload) async -> RecordingEnqueueOutcome
}

public enum RecordingRequests {
    /// 1 s, 3 s and 10 s before the three retries.
    public static let retryDelays: [TimeInterval] = [1, 3, 10]

    private static func base(_ origin: LocalOrigin, _ key: String, path: String, method: String) -> URLRequest {
        var request = URLRequest(url: origin.url(path: path))
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.httpShouldHandleCookies = false
        request.timeoutInterval = 10
        request.setValue("tb_key=\(key)", forHTTPHeaderField: "Cookie")
        return request
    }

    public static func csrf(origin: LocalOrigin, key: String) -> URLRequest {
        base(origin, key, path: "/api/csrf", method: "GET")
    }

    public static func body(_ payload: RecordingEnqueuePayload) -> Data? {
        var object: [String: Any] = [
            "ref": payload.ref,
            "title": payload.title ?? NSNull(),
            "started_at": RecordingStore.isoString(payload.startedAt),
            "language": payload.language,
            "layout": payload.layout,
            "duration_s": (payload.durationS * 1000).rounded() / 1000,
        ]
        if let audioRef = payload.audioRef { object["audio_ref"] = audioRef }
        return try? JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
    }

    public static func enqueue(origin: LocalOrigin, key: String, csrf: String, payload: RecordingEnqueuePayload)
        -> URLRequest?
    {
        guard let body = body(payload) else { return nil }
        var request = base(origin, key, path: "/api/recordings", method: "POST")
        request.setValue("http://\(LocalOrigin.host):\(origin.port)", forHTTPHeaderField: "Origin")
        request.setValue(csrf, forHTTPHeaderField: "X-CSRF-Token")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        return request
    }

    public static func parseCSRF(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let token = object["csrf"] as? String, !token.isEmpty, token.utf8.count <= 512 else { return nil }
        return token
    }

    public static func parseMeetingId(_ data: Data) -> Int? {
        (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["meeting_id"] as? Int
    }

    public static func parseErrorCode(_ data: Data) -> String? {
        guard let code = (try? JSONSerialization.jsonObject(with: data) as? [String: Any])?["error"] as? String,
              code.utf8.count <= 64, code.utf8.allSatisfy({ ($0 >= 97 && $0 <= 122) || $0 == 95 }) else { return nil }
        return code
    }

    /// Only server trouble is worth another try. The service answers a repeated `ref` with
    /// the existing meeting, so retrying never makes duplicates. Any 4xx is final.
    public static func shouldRetry(status: Int) -> Bool {
        status >= 500
    }
}

public protocol HTTPTransport: Sendable {
    /// Body and status code. Throws when there is no answer.
    func send(_ request: URLRequest) async throws -> (Data, Int)
}

/// Forbids redirects: requests must never leave 127.0.0.1.
final class RecordingNoRedirects: NSObject, URLSessionTaskDelegate, Sendable {
    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest
    ) async -> URLRequest? { nil }
}

public final class LocalURLSessionTransport: HTTPTransport, @unchecked Sendable {
    private let session: URLSession

    public init() {
        let config = URLSessionConfiguration.ephemeral
        config.requestCachePolicy = .reloadIgnoringLocalCacheData
        config.urlCache = nil
        config.httpCookieStorage = nil
        config.httpShouldSetCookies = false
        config.connectionProxyDictionary = [:]
        config.timeoutIntervalForRequest = 10
        config.timeoutIntervalForResource = 20
        session = URLSession(configuration: config, delegate: RecordingNoRedirects(), delegateQueue: nil)
    }

    public func send(_ request: URLRequest) async throws -> (Data, Int) {
        // Belt and braces: the transport never talks to anything but the local service.
        guard request.url?.host == LocalOrigin.host else { throw URLError(.unsupportedURL) }
        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw URLError(.badServerResponse) }
        return (data, http.statusCode)
    }
}

public final class RecordingEnqueueClient: RecordingEnqueuing {
    public typealias Connection = @MainActor @Sendable () -> (origin: LocalOrigin, key: String)?
    public typealias Pause = @Sendable (TimeInterval) async -> Void

    private let connection: Connection
    private let transport: any HTTPTransport
    private let pause: Pause
    private let delays: [TimeInterval]

    public init(
        connection: @escaping Connection,
        transport: any HTTPTransport = LocalURLSessionTransport(),
        delays: [TimeInterval] = RecordingRequests.retryDelays,
        pause: @escaping Pause = { seconds in try? await Task.sleep(for: .seconds(seconds)) }
    ) {
        self.connection = connection
        self.transport = transport
        self.delays = delays
        self.pause = pause
    }

    public func enqueue(_ payload: RecordingEnqueuePayload) async -> RecordingEnqueueOutcome {
        var attempt = 0
        while true {
            switch await attemptOnce(payload) {
            case .done(let outcome):
                return outcome
            case .retry:
                guard attempt < delays.count else { return .unavailable }
                await pause(delays[attempt])
                attempt += 1
            }
        }
    }

    private enum Attempt {
        case done(RecordingEnqueueOutcome)
        case retry
    }

    private func attemptOnce(_ payload: RecordingEnqueuePayload) async -> Attempt {
        // The port and the key change when the service restarts: read them for every try.
        guard let link = await connection() else { return .retry }
        do {
            let (tokenData, tokenStatus) = try await transport.send(RecordingRequests.csrf(origin: link.origin, key: link.key))
            if tokenStatus >= 400, !RecordingRequests.shouldRetry(status: tokenStatus) {
                return .done(.rejected(RecordingRequests.parseErrorCode(tokenData) ?? "not_app_mode"))
            }
            guard tokenStatus == 200, let token = RecordingRequests.parseCSRF(tokenData),
                  let request = RecordingRequests.enqueue(
                    origin: link.origin, key: link.key, csrf: token, payload: payload)
            else { return .retry }
            let (data, status) = try await transport.send(request)
            if status == 200, let id = RecordingRequests.parseMeetingId(data) { return .done(.queued(meetingId: id)) }
            let code = RecordingRequests.parseErrorCode(data)
            if RecordingRequests.shouldRetry(status: status) { return .retry }
            return .done(.rejected(code ?? "bad_body"))
        } catch {
            return .retry
        }
    }
}
