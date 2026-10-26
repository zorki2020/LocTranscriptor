import Foundation
import Testing

@testable import AppCore

final class FakeEngine: CaptureEngine, @unchecked Sendable {
    private let lock = NSLock()
    private var samples: (@Sendable ([Int16], Int) -> Void)?
    private var events: (@Sendable (CaptureEvent) -> Void)?
    var startError: (any Error)?
    private(set) var startedConfig: CaptureConfig?
    private(set) var stopCount = 0

    func setHandlers(
        samples: @escaping @Sendable ([Int16], Int) -> Void, events: @escaping @Sendable (CaptureEvent) -> Void
    ) {
        lock.lock()
        self.samples = samples
        self.events = events
        lock.unlock()
    }

    func start(_ config: CaptureConfig) throws {
        if let startError { throw startError }
        lock.lock()
        startedConfig = config
        lock.unlock()
    }

    func stop() {
        lock.lock()
        stopCount += 1
        lock.unlock()
    }

    func emit(_ frames: [Int16], channels: Int) { samples?(frames, channels) }
    func send(_ event: CaptureEvent) { events?(event) }
}

final class FakeCatalog: SourceCatalog, @unchecked Sendable {
    var permission: MicPermission
    var grantOnRequest: Bool
    private(set) var asked = 0

    init(permission: MicPermission = .granted, grantOnRequest: Bool = true) {
        self.permission = permission
        self.grantOnRequest = grantOnRequest
    }

    func apps() -> [AudioAppInfo] { [] }
    func microphones() -> [MicrophoneInfo] { [] }
    func micPermission() -> MicPermission { permission }
    func requestMicAccess() async -> Bool {
        asked += 1
        permission = grantOnRequest ? .granted : .denied
        return grantOnRequest
    }
}

final class FakeEnqueuer: RecordingEnqueuing, @unchecked Sendable {
    private let lock = NSLock()
    private var outcomes: [RecordingEnqueueOutcome]
    private(set) var payloads: [RecordingEnqueuePayload] = []
    /// Runs while the enqueue is "in flight", before it answers.
    var hook: (@Sendable () async -> Void)?

    init(_ outcomes: [RecordingEnqueueOutcome] = [.queued(meetingId: 7)]) { self.outcomes = outcomes }

    func enqueue(_ payload: RecordingEnqueuePayload) async -> RecordingEnqueueOutcome {
        await hook?()
        return lock.withLock {
            payloads.append(payload)
            return outcomes.count > 1 ? outcomes.removeFirst() : (outcomes.first ?? .unavailable)
        }
    }
}

final class FakeSleep: SleepBlocker, @unchecked Sendable {
    private let lock = NSLock()
    private var begun = 0
    private var ended = 0
    var counts: (begin: Int, end: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (begun, ended)
    }
    func begin() { lock.lock(); begun += 1; lock.unlock() }
    func end() { lock.lock(); ended += 1; lock.unlock() }
}

@MainActor final class SessionTestClock {
    var now: Double = 100
}

@MainActor
final class Harness {
    let dir: URL
    let engine = FakeEngine()
    let catalog: FakeCatalog
    let enqueuer: FakeEnqueuer
    let sleep = FakeSleep()
    let clock = SessionTestClock()
    var states: [RecordingState] = []
    let session: RecordingSession

    init(
        permission: MicPermission = .granted, outcomes: [RecordingEnqueueOutcome] = [.queued(meetingId: 7)],
        chunkSeconds: Int = 300, free: @escaping @Sendable (URL) -> Int64? = { _ in 100_000_000_000 },
        linger: Duration = .seconds(3600), lang: Lang = .en, maxDurationS: Double = 43200
    ) throws {
        dir = try makeTempDir()
        catalog = FakeCatalog(permission: permission)
        enqueuer = FakeEnqueuer(outcomes)
        let clock = self.clock
        session = RecordingSession(
            engine: engine, catalog: catalog, enqueuer: enqueuer, dataDirectory: dir, sleepBlocker: sleep,
            lang: { lang }, freeSpace: free, clock: { clock.now },
            wallClock: { Date(timeIntervalSince1970: 1_792_749_600) }, chunkSeconds: chunkSeconds,
            tickInterval: .seconds(3600), queuedLinger: linger, failedLinger: linger, maxDurationS: maxDurationS)
        session.onStatus = { [unowned self] in
            if self.states.last != $0.state { self.states.append($0.state) }
        }
    }

    deinit { try? FileManager.default.removeItem(at: dir) }

    func request(mic: Bool = false, title: String? = nil, source: RecordingSourceInfo = .all) -> RecordingStartRequest {
        RecordingStartRequest(source: source, micEnabled: mic, micId: nil, language: "ru", title: title)
    }

    var uploads: [URL] {
        let folder = RecordingStore.uploadsDirectory(dir)
        return ((try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []).sorted()
            .map { folder.appendingPathComponent($0) }
    }

    var sessionFolders: [URL] { RecordingStore.leftoverSessions(data: dir, excluding: nil) }
}

func eventually(_ condition: @MainActor () -> Bool) async -> Bool {
    for _ in 0..<200 {
        if await condition() { return true }
        try? await Task.sleep(for: .milliseconds(10))
    }
    return false
}

@MainActor
@Suite struct RecordingSessionTests {
    @Test func monoRecordingEndsAsOneWavInUploadsAndIsQueued() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request(title: "Weekly")) == nil)
        #expect(h.session.status.state == .recording)
        #expect(h.engine.startedConfig == CaptureConfig(source: .allExceptSelf, micEnabled: false, micDeviceId: nil))
        let samples = pattern(frames: 24000, channels: 1)
        h.engine.emit(Array(samples[0..<10000]), channels: 1)
        h.engine.emit(Array(samples[10000...]), channels: 1)
        await h.session.stop()

        #expect(h.session.status.state == .queued)
        #expect(h.session.status.meetingId == 7)
        #expect(h.states == [.starting, .recording, .stopping, .queued])
        #expect(h.engine.stopCount == 1)
        let payload = try #require(h.enqueuer.payloads.first)
        #expect(payload.layout == "mono")
        #expect(payload.language == "ru")
        #expect(payload.title == "Weekly")
        #expect(abs(payload.durationS - 1.5) < 1e-9)
        #expect(RecordingStore.isRef(payload.ref))
        let file = try #require(h.uploads.first)
        #expect(h.uploads.count == 1)
        #expect(file.lastPathComponent == payload.ref)
        #expect(mode(of: file) == 0o600)
        #expect(try Data(contentsOf: file) == WAVFile.header(dataBytes: 48000, channels: 1) + bytes(of: samples))
        // The working folder is gone after a successful enqueue.
        #expect(h.sessionFolders.isEmpty)
        #expect(h.sleep.counts == (1, 1))
    }

    @Test func micAndSystemMakeTwoChannels() async throws {
        let h = try Harness(chunkSeconds: 1)
        #expect(await h.session.start(h.request(mic: true)) == nil)
        #expect(h.engine.startedConfig?.micEnabled == true)
        let samples = pattern(frames: 40000, channels: 2)
        h.engine.emit(samples, channels: 2)
        await h.session.stop()
        #expect(h.session.status.state == .queued)
        #expect(h.enqueuer.payloads.first?.layout == "mic_system")
        let file = try #require(h.uploads.first)
        #expect(try Data(contentsOf: file) == WAVFile.header(dataBytes: 160_000, channels: 2) + bytes(of: samples))
    }

    @Test func aSecondStartIsRefused() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        #expect(await h.session.start(h.request()) == .alreadyRecording)
        #expect(h.session.status.state == .recording)
        h.engine.emit([1, 2, 3], channels: 1)
        await h.session.stop()
    }

    @Test func micDeniedFailsWithoutLeavingFiles() async throws {
        let h = try Harness(permission: .denied)
        #expect(await h.session.start(h.request(mic: true)) == .micDenied)
        #expect(h.session.status.state == .failed)
        #expect(h.session.status.error == .micDenied)
        #expect(h.sessionFolders.isEmpty)
        #expect(h.sleep.counts.begin == 0)
        // Without the microphone the same Mac records fine.
        #expect(await h.session.start(h.request(mic: false)) == nil)
        h.engine.emit([1, 2], channels: 1)
        await h.session.stop()
    }

    @Test func undeterminedMicAsksFirst() async throws {
        let h = try Harness(permission: .undetermined)
        #expect(await h.session.start(h.request(mic: true)) == nil)
        #expect(h.catalog.asked == 1)
        await h.session.stop()

        let denied = try Harness(permission: .undetermined)
        denied.catalog.grantOnRequest = false
        #expect(await denied.session.start(denied.request(mic: true)) == .micDenied)
    }

    @Test func anAppSourceNeedsBundleIds() async throws {
        let h = try Harness()
        let empty = RecordingSourceInfo(kind: .app, bundleIds: [], name: "X")
        #expect(await h.session.start(h.request(source: empty)) == .noSource)
        let some = RecordingSourceInfo(kind: .app, bundleIds: ["com.example.a", "com.example.b"], name: "X")
        #expect(await h.session.start(h.request(source: some)) == nil)
        #expect(h.engine.startedConfig?.source == .apps(bundleIds: ["com.example.a", "com.example.b"]))
        h.engine.emit([1, 2], channels: 1)
        await h.session.stop()
    }

    @Test func engineFailureOnStartCleansUp() async throws {
        let h = try Harness()
        h.engine.startError = CaptureFailure.micDenied
        #expect(await h.session.start(h.request()) == .micDenied)
        #expect(h.session.status.state == .failed)
        #expect(h.sessionFolders.isEmpty)
        #expect(h.sleep.counts == (1, 1))
        h.engine.startError = NSError(domain: "x", code: 1)
        #expect(await h.session.start(h.request()) == .startFailed)
    }

    @Test func aFailureEventSavesWhatExists() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit(pattern(frames: 8000, channels: 1), channels: 1)
        h.engine.send(.failed(.sourceGone))
        #expect(await eventually { h.session.status.state == .queued })
        #expect(h.session.status.error == .sourceGone)
        #expect(h.enqueuer.payloads.count == 1)
        #expect(h.uploads.count == 1)
    }

    @Test func warningsAndLevelsShowInTheStatus() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request(mic: true)) == nil)
        h.engine.send(.warning(.silentSystem))
        #expect(await eventually { h.session.status.warning == .silentSystem })
        h.engine.send(.levels(CaptureLevels(mic: 0.25, system: 0.5)))
        #expect(await eventually { h.session.status.levelMic == 0.25 })
        #expect(h.session.status.levelSystem == 0.5)
        #expect(h.session.status.warning == nil)  // sound arrived
        h.engine.emit([1, 2], channels: 2)
        await h.session.stop()
    }

    @Test func lowDiskStopsAndKeepsTheRecording() async throws {
        let h = try Harness(free: { _ in 1_000_000_000 })
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit(pattern(frames: 16000, channels: 1), channels: 1)
        await h.session.checkDisk()
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.warning == .diskLow)
        #expect(h.uploads.count == 1)
        #expect(h.enqueuer.payloads.count == 1)
    }

    @Test func enoughDiskKeepsRecordingAndTheTickCountsSeconds() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        h.clock.now = 107.5
        await h.session.tick()
        #expect(h.session.status.elapsedS == 7.5)
        for _ in 0..<5 { await h.session.tick() }  // includes one disk check
        #expect(h.session.status.state == .recording)
        h.engine.emit([1, 2], channels: 1)
        await h.session.stop()
    }

    @Test func aFailedEnqueueKeepsFileAndFolderForTheNextLaunch() async throws {
        let h = try Harness(outcomes: [.unavailable])
        #expect(await h.session.start(h.request(title: "Keep me")) == nil)
        h.engine.emit(pattern(frames: 16000, channels: 1), channels: 1)
        await h.session.stop()
        #expect(h.session.status.state == .failed)
        #expect(h.session.status.error == .enqueueFailed)
        #expect(h.uploads.count == 1)
        #expect(h.sessionFolders.count == 1)
        #expect(h.sleep.counts == (1, 1))

        // Next launch: the service is up and takes the recording, without a second file.
        let again = FakeEnqueuer([.queued(meetingId: 9)])
        let clock = h.clock
        let next = RecordingSession(
            engine: FakeEngine(), catalog: FakeCatalog(), enqueuer: again, dataDirectory: h.dir,
            sleepBlocker: FakeSleep(), lang: { .en }, clock: { clock.now })
        #expect(await next.recoverLeftovers() == 1)
        #expect(h.sessionFolders.isEmpty)
        #expect(h.uploads.count == 1)
        let payload = try #require(again.payloads.first)
        #expect(payload.title == "Keep me")
        #expect(payload.ref == h.uploads.first?.lastPathComponent)
    }

    @Test func stopRightAfterStartSavesNothingAndIsNotAnError() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        await h.session.stop()
        #expect(h.session.status.state == .idle)
        #expect(h.uploads.isEmpty)
        #expect(h.sessionFolders.isEmpty)
        #expect(h.enqueuer.payloads.isEmpty)
    }

    @Test func silenceFromTheEngineForALongTimeIsAnError() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        h.clock.now = 200
        await h.session.stop()
        #expect(h.session.status.state == .failed)
        #expect(h.session.status.error == .finalizeFailed)
    }

    @Test func queuedReturnsToIdleAfterAWhile() async throws {
        let h = try Harness(linger: .milliseconds(30))
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit([1, 2], channels: 1)
        await h.session.stop()
        #expect(h.session.status.state == .queued)
        #expect(await eventually { h.session.status.state == .idle })
        #expect(h.session.status.meetingId == nil)
        #expect(h.states.suffix(2) == [.queued, .idle])
    }

    @Test func recoversALeftoverFolderWithTheRecoveredTitle() async throws {
        let h = try Harness(lang: .ru)
        // A crashed run: session record and two chunks, no final file.
        let folder = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("00112233445566ff")
        try PrivateFiles.ensureDirectory(folder)
        let meta = RecordingMeta(
            ref: "rec" + String(repeating: "b", count: 24) + ".wav", startedAt: "2026-10-23T09:00:00Z",
            language: "ru", channels: 2, title: nil)
        try RecordingStore.writeMeta(meta, in: folder)
        let writer = RecordingChunkWriter(folder: folder, channels: 2, chunkSeconds: 1)
        let samples = pattern(frames: 20000, channels: 2)
        writer.append(samples, channels: 2)
        _ = await writer.close()
        // Things that are not ours stay as they are.
        let root = RecordingStore.recordingsDirectory(h.dir)
        try FileManager.default.createDirectory(at: root.appendingPathComponent("notes"), withIntermediateDirectories: true)
        try PrivateFiles.ensureDirectory(RecordingStore.uploadsDirectory(h.dir))
        let foreign = RecordingStore.uploadsDirectory(h.dir).appendingPathComponent("keep.wav")
        try Data([1, 2, 3]).write(to: foreign)

        #expect(await h.session.recoverLeftovers() == 1)
        let payload = try #require(h.enqueuer.payloads.first)
        #expect(payload.title == "Восстановленная запись")
        #expect(payload.layout == "mic_system")
        #expect(payload.ref == meta.ref)
        #expect(RecordingStore.isoString(payload.startedAt) == "2026-10-23T09:00:00Z")
        let final = RecordingStore.uploadsDirectory(h.dir).appendingPathComponent(meta.ref)
        #expect(try Data(contentsOf: final) == WAVFile.header(dataBytes: 80000, channels: 2) + bytes(of: samples))
        #expect(FileManager.default.fileExists(atPath: foreign.path))
        #expect(FileManager.default.fileExists(atPath: root.appendingPathComponent("notes").path))
        #expect(!FileManager.default.fileExists(atPath: folder.path))
    }

    @Test func recoveryRebuildsAMissingSessionRecordFromTheChunks() async throws {
        let h = try Harness()
        let folder = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("aabbccddeeff0011")
        try PrivateFiles.ensureDirectory(folder)
        let writer = RecordingChunkWriter(folder: folder, channels: 1)
        writer.append(pattern(frames: 100, channels: 1), channels: 1)
        _ = await writer.close()
        #expect(await h.session.recoverLeftovers() == 1)
        #expect(h.enqueuer.payloads.first?.title == "Recovered recording")
        #expect(h.enqueuer.payloads.first?.layout == "mono")
    }

    @Test func recoveryDropsEmptyFoldersAndQueuedOnes() async throws {
        let h = try Harness()
        let empty = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("1111111111111111")
        try PrivateFiles.ensureDirectory(empty)
        let done = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("2222222222222222")
        try PrivateFiles.ensureDirectory(done)
        try RecordingStore.writeMeta(
            RecordingMeta(ref: "rec" + String(repeating: "c", count: 24) + ".wav", startedAt: "2026-10-23T09:00:00Z",
                          language: "en", channels: 1, title: nil, meetingId: 3), in: done)
        #expect(await h.session.recoverLeftovers() == 0)
        #expect(h.enqueuer.payloads.isEmpty)
        #expect(h.sessionFolders.isEmpty)
    }

    @Test func recoveryLeavesAFolderWhenTheServiceIsDown() async throws {
        let h = try Harness(outcomes: [.unavailable])
        let folder = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("3333333333333333")
        try PrivateFiles.ensureDirectory(folder)
        let writer = RecordingChunkWriter(folder: folder, channels: 1)
        writer.append(pattern(frames: 100, channels: 1), channels: 1)
        _ = await writer.close()
        #expect(await h.session.recoverLeftovers() == 0)
        #expect(h.sessionFolders.count == 1)
        #expect(await h.session.recoverLeftovers() == 0)
        #expect(h.uploads.count == 1)  // the second try reuses the final file
    }

    @Test func theActiveSessionFolderIsNeverRecovered() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit(pattern(frames: 100, channels: 1), channels: 1)
        #expect(await h.session.recoverLeftovers() == 0)
        #expect(h.sessionFolders.count == 1)
        await h.session.stop()
        #expect(h.enqueuer.payloads.count == 1)
    }

    @Test func folderIsPrivate() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request()) == nil)
        let folder = try #require(h.sessionFolders.first)
        #expect(mode(of: folder) == 0o700)
        #expect(mode(of: RecordingStore.recordingsDirectory(h.dir)) == 0o700)
        h.engine.emit([1, 2, 3, 4], channels: 1)
        #expect(await eventually { !RecordingStore.parts(in: folder).isEmpty })
        let part = try #require(RecordingStore.parts(in: folder).first)
        #expect(mode(of: part) == 0o600)
        await h.session.stop()
    }

    @Test func theTwelveHourLimitStopsAndSavesTheRecording() async throws {
        let h = try Harness(maxDurationS: 60)
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit(pattern(frames: 16000, channels: 1), channels: 1)
        h.clock.now = 100 + 59
        await h.session.tick()
        #expect(h.session.status.state == .recording)
        h.clock.now = 100 + 60
        await h.session.tick()
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.error == .maxDuration)
        #expect(h.enqueuer.payloads.count == 1)
        #expect(h.uploads.count == 1)
        #expect(RecordingFormat.maxDurationS == 43200)
    }

    @Test func aDeviceNoticeDoesNotHideASilentChannelAndFadesAway() async throws {
        let h = try Harness()
        #expect(await h.session.start(h.request(mic: true)) == nil)
        h.engine.send(.warning(.silentSystem))
        #expect(await eventually { h.session.status.warning == .silentSystem })
        h.engine.send(.warning(.deviceChanged))
        // Let the event be handled, then check that the silent warning is still the one shown.
        try await Task.sleep(for: .milliseconds(50))
        #expect(h.session.status.warning == .silentSystem)
        // Sound arrives: the device notice is what is left, until it expires.
        h.engine.send(.levels(CaptureLevels(mic: 0.1, system: 0.1)))
        #expect(await eventually { h.session.status.warning == .deviceChanged })
        h.clock.now += 6
        await h.session.tick()
        #expect(h.session.status.warning == nil)
        h.engine.emit([1, 2], channels: 2)
        await h.session.stop()
    }

    @Test func aRecoveryPassDuringTheEnqueueLeavesTheSessionAlone() async throws {
        let h = try Harness()
        let session = h.session
        let seen = Counter()
        h.enqueuer.hook = {
            let n = await session.recoverLeftovers()
            seen.add(n)
        }
        #expect(await h.session.start(h.request()) == nil)
        h.engine.emit(pattern(frames: 16000, channels: 1), channels: 1)
        await h.session.stop()
        #expect(h.session.status.state == .queued)
        #expect(seen.total == 0)
        #expect(h.enqueuer.payloads.count == 1)
    }

    @Test func recoveryKeepsTheReadableChunksWhenOneIsBroken() async throws {
        let h = try Harness()
        let folder = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("4444444444444444")
        try PrivateFiles.ensureDirectory(folder)
        let first = pattern(frames: 100, channels: 1)
        let last = pattern(frames: 50, channels: 1, from: 100)
        try (CAFChunk.header(channels: 1) + bytes(of: first)).write(to: folder.appendingPathComponent("part-0001.caf"))
        try Data().write(to: folder.appendingPathComponent("part-0002.caf"))
        try (CAFChunk.header(channels: 1) + bytes(of: last)).write(to: folder.appendingPathComponent("part-0003.caf"))
        try CAFChunk.header(channels: 1).prefix(30).write(to: folder.appendingPathComponent("part-0004.caf"))
        #expect(await h.session.recoverLeftovers() == 1)
        let ref = try #require(h.enqueuer.payloads.first?.ref)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(h.dir).appendingPathComponent(ref))
        #expect(file == WAVFile.header(dataBytes: 300, channels: 1) + bytes(of: first + last))
    }

    @Test func recoveryDeletesASessionOnlyWhenNoChunkIsReadable() async throws {
        let h = try Harness()
        let folder = RecordingStore.recordingsDirectory(h.dir).appendingPathComponent("5555555555555555")
        try PrivateFiles.ensureDirectory(folder)
        try Data().write(to: folder.appendingPathComponent("part-0001.caf"))
        try CAFChunk.header(channels: 2).prefix(40).write(to: folder.appendingPathComponent("part-0002.caf"))
        #expect(await h.session.recoverLeftovers() == 0)
        #expect(h.sessionFolders.isEmpty)
        #expect(h.enqueuer.payloads.isEmpty)
    }
}

final class Counter: @unchecked Sendable {
    private let lock = NSLock()
    private var value = 0
    func add(_ n: Int) { lock.withLock { value += n } }
    var total: Int { lock.withLock { value } }
}
