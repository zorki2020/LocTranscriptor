import Foundation
import Testing

@testable import AppCore

final class FakeTransport: HTTPTransport, @unchecked Sendable {
    private let lock = NSLock()
    private var script: [Result<(Data, Int), Error>]
    private(set) var requests: [URLRequest] = []

    init(_ script: [Result<(Data, Int), Error>]) { self.script = script }

    func send(_ request: URLRequest) async throws -> (Data, Int) {
        let next: Result<(Data, Int), Error> = lock.withLock {
            requests.append(request)
            return script.isEmpty ? .failure(URLError(.cannotConnectToHost)) : script.removeFirst()
        }
        return try next.get()
    }
}

final class PauseLog: @unchecked Sendable {
    private let lock = NSLock()
    private(set) var delays: [TimeInterval] = []
    func add(_ value: TimeInterval) {
        lock.lock()
        delays.append(value)
        lock.unlock()
    }
}

let enqueueKey = String(repeating: "ab", count: 32)
let csrfOK: Result<(Data, Int), Error> = .success((Data(#"{"csrf": "tok123"}"#.utf8), 200))
let queuedOK: Result<(Data, Int), Error> = .success((Data(#"{"meeting_id": 42}"#.utf8), 200))

func samplePayload() -> RecordingEnqueuePayload {
    RecordingEnqueuePayload(
        ref: "rec" + String(repeating: "0", count: 24) + ".wav", title: nil,
        startedAt: Date(timeIntervalSince1970: 1_792_749_600), language: "ru", layout: "mic_system", durationS: 12.3456)
}

@Suite struct RecordingRequestTests {
    let origin = LocalOrigin(port: 51234)

    @Test func csrfRequest() {
        let request = RecordingRequests.csrf(origin: origin, key: enqueueKey)
        #expect(request.url?.absoluteString == "http://127.0.0.1:51234/api/csrf")
        #expect(request.httpMethod == "GET")
        #expect(request.value(forHTTPHeaderField: "Cookie") == "tb_key=\(enqueueKey)")
        #expect(request.httpShouldHandleCookies == false)
    }

    @Test func enqueueRequest() throws {
        let request = try #require(
            RecordingRequests.enqueue(origin: origin, key: enqueueKey, csrf: "tok123", payload: samplePayload()))
        #expect(request.url?.absoluteString == "http://127.0.0.1:51234/api/recordings")
        #expect(request.httpMethod == "POST")
        #expect(request.value(forHTTPHeaderField: "Cookie") == "tb_key=\(enqueueKey)")
        #expect(request.value(forHTTPHeaderField: "Origin") == "http://127.0.0.1:51234")
        #expect(request.value(forHTTPHeaderField: "X-CSRF-Token") == "tok123")
        #expect(request.value(forHTTPHeaderField: "Content-Type") == "application/json")
        let body = try #require(request.httpBody)
        #expect(body.count < 4096)
        let json = try #require(try JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(Set(json.keys) == ["ref", "title", "started_at", "language", "layout", "duration_s"])
        #expect(json["ref"] as? String == "rec" + String(repeating: "0", count: 24) + ".wav")
        #expect(json["title"] is NSNull)
        #expect(json["started_at"] as? String == "2026-10-23T10:00:00Z")
        #expect(json["language"] as? String == "ru")
        #expect(json["layout"] as? String == "mic_system")
        #expect(json["duration_s"] as? Double == 12.346)
    }

    @Test func titleGoesAsData() throws {
        var payload = samplePayload()
        payload.title = "Weekly \"sync\"\n\u{1F600}"
        let body = try #require(RecordingRequests.body(payload))
        let json = try #require(try JSONSerialization.jsonObject(with: body) as? [String: Any])
        #expect(json["title"] as? String == payload.title)
    }

    @Test func parsers() {
        #expect(RecordingRequests.parseCSRF(Data(#"{"csrf":"abc"}"#.utf8)) == "abc")
        #expect(RecordingRequests.parseCSRF(Data(#"{"csrf":""}"#.utf8)) == nil)
        #expect(RecordingRequests.parseCSRF(Data("nope".utf8)) == nil)
        #expect(RecordingRequests.parseMeetingId(Data(#"{"meeting_id": 7}"#.utf8)) == 7)
        #expect(RecordingRequests.parseMeetingId(Data(#"{"meeting_id": "7"}"#.utf8)) == nil)
        #expect(RecordingRequests.parseErrorCode(Data(#"{"error":"file_missing"}"#.utf8)) == "file_missing")
        #expect(RecordingRequests.parseErrorCode(Data(#"{"error":"Some free text!"}"#.utf8)) == nil)
    }

    @Test func retryScheduleAndRule() {
        #expect(RecordingRequests.retryDelays == [1, 3, 10])
        #expect(RecordingRequests.shouldRetry(status: 500))
        #expect(RecordingRequests.shouldRetry(status: 503))
        #expect(!RecordingRequests.shouldRetry(status: 400))
        #expect(!RecordingRequests.shouldRetry(status: 404))
    }
}

@Suite struct RecordingEnqueueClientTests {
    let link: RecordingEnqueueClient.Connection = { (LocalOrigin(port: 51234), enqueueKey) }

    func client(_ transport: FakeTransport, _ log: PauseLog) -> RecordingEnqueueClient {
        RecordingEnqueueClient(connection: link, transport: transport, pause: { log.add($0) })
    }

    @Test func succeedsAtOnce() async {
        let transport = FakeTransport([csrfOK, queuedOK])
        let log = PauseLog()
        let outcome = await client(transport, log).enqueue(samplePayload())
        #expect(outcome == .queued(meetingId: 42))
        #expect(transport.requests.count == 2)
        #expect(log.delays.isEmpty)
    }

    @Test func retriesThreeTimesWithTheScheduleThenGivesUp() async {
        let transport = FakeTransport([])  // nothing answers
        let log = PauseLog()
        let outcome = await client(transport, log).enqueue(samplePayload())
        #expect(outcome == .unavailable)
        #expect(log.delays == [1, 3, 10])
        #expect(transport.requests.count == 4)
    }

    @Test func recoversAfterServerErrors() async {
        let fail: Result<(Data, Int), Error> = .success((Data(), 503))
        let transport = FakeTransport([csrfOK, fail, csrfOK, fail, csrfOK, queuedOK])
        let log = PauseLog()
        let outcome = await client(transport, log).enqueue(samplePayload())
        #expect(outcome == .queued(meetingId: 42))
        #expect(log.delays == [1, 3])
    }

    @Test func aClientErrorIsFinal() async {
        let rejected: Result<(Data, Int), Error> = .success((Data(#"{"error":"bad_layout"}"#.utf8), 400))
        let transport = FakeTransport([csrfOK, rejected])
        let log = PauseLog()
        let outcome = await client(transport, log).enqueue(samplePayload())
        #expect(outcome == .rejected("bad_layout"))
        #expect(log.delays.isEmpty)
    }

    @Test func noConnectionMeansRetryThenUnavailable() async {
        let log = PauseLog()
        let none = RecordingEnqueueClient(
            connection: { nil }, transport: FakeTransport([]), pause: { log.add($0) })
        #expect(await none.enqueue(samplePayload()) == .unavailable)
        #expect(log.delays == [1, 3, 10])
    }

    @Test func csrfNotFoundIsFinal() async {
        let transport = FakeTransport([.success((Data(#"{"error":"not_app_mode"}"#.utf8), 404))])
        let outcome = await client(transport, PauseLog()).enqueue(samplePayload())
        #expect(outcome == .rejected("not_app_mode"))
        #expect(transport.requests.count == 1)
    }
}
