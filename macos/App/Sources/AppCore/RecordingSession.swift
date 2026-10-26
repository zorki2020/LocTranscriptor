// The recording session: one state machine around the capture engine (what to record), the
// chunk writer (where it goes while recording), the finalizer (one WAV in uploads/) and the
// enqueue client (tell the service). It talks to the engine only through `CaptureEngine`.
// Privacy: no titles, names or paths are logged or put into errors, codes only.

import Foundation

/// Keeps the Mac awake while a recording runs.
public protocol SleepBlocker: Sendable {
    func begin()
    func end()
}

public final class ProcessSleepBlocker: SleepBlocker, @unchecked Sendable {
    private let lock = NSLock()
    private var token: (any NSObjectProtocol)?

    public init() {}

    public func begin() {
        lock.lock()
        defer { lock.unlock() }
        guard token == nil else { return }
        // Idle system sleep only: the display may still sleep.
        token = ProcessInfo.processInfo.beginActivity(
            options: .idleSystemSleepDisabled, reason: "Recording a meeting")
    }

    public func end() {
        lock.lock()
        let current = token
        token = nil
        lock.unlock()
        if let current { ProcessInfo.processInfo.endActivity(current) }
    }
}

@MainActor
public final class RecordingSession {
    public private(set) var status = RecordingStatus.idle
    /// Called on every state change and once a second while recording.
    public var onStatus: (@MainActor (RecordingStatus) -> Void)?

    private let engine: any CaptureEngine
    private let catalog: any SourceCatalog
    private let enqueuer: any RecordingEnqueuing
    private let dataDirectory: URL
    private let sleepBlocker: any SleepBlocker
    private let lang: @MainActor () -> Lang
    private let freeSpace: @Sendable (URL) -> Int64?
    private let clock: @MainActor () -> Double
    private let wallClock: @MainActor () -> Date
    private let chunkSeconds: Int
    private let tickInterval: Duration
    private let queuedLinger: Duration
    private let failedLinger: Duration
    private let maxDurationS: Double

    private var warnings: Set<RecordingWarning> = []
    private var deviceChangedAt: Double = 0
    private var folder: URL?
    private var meta: RecordingMeta?
    private var writer: RecordingChunkWriter?
    private var startedMono: Double = 0
    private var tickTask: Task<Void, Never>?
    private var lingerTask: Task<Void, Never>?
    private var tickCount = 0
    private var stopRequested = false
    private var recovering = false

    public init(
        engine: any CaptureEngine,
        catalog: any SourceCatalog,
        enqueuer: any RecordingEnqueuing,
        dataDirectory: URL,
        sleepBlocker: any SleepBlocker = ProcessSleepBlocker(),
        lang: @escaping @MainActor () -> Lang = { Lang.system() },
        freeSpace: @escaping @Sendable (URL) -> Int64? = { RecordingDisk.availableBytes(at: $0) },
        clock: @escaping @MainActor () -> Double = { ProcessInfo.processInfo.systemUptime },
        wallClock: @escaping @MainActor () -> Date = { Date() },
        chunkSeconds: Int = RecordingFormat.chunkSeconds,
        tickInterval: Duration = .seconds(1),
        queuedLinger: Duration = .seconds(5),
        failedLinger: Duration = .seconds(60),
        maxDurationS: Double = RecordingFormat.maxDurationS
    ) {
        self.engine = engine
        self.catalog = catalog
        self.enqueuer = enqueuer
        self.dataDirectory = dataDirectory
        self.sleepBlocker = sleepBlocker
        self.lang = lang
        self.freeSpace = freeSpace
        self.clock = clock
        self.wallClock = wallClock
        self.chunkSeconds = chunkSeconds
        self.tickInterval = tickInterval
        self.queuedLinger = queuedLinger
        self.failedLinger = failedLinger
        self.maxDurationS = maxDurationS
    }

    // MARK: - Start

    /// Starts a recording. Returns nil on success, an error code otherwise (the status then
    /// is `failed` with the same code). The engine's start can wait for a system dialog, so
    /// this call can take long. It never blocks the main thread.
    public func start(_ request: RecordingStartRequest) async -> RecordingError? {
        guard !status.state.isActive else { return .alreadyRecording }
        lingerTask?.cancel()
        stopRequested = false
        warnings = []
        var fresh = RecordingStatus()
        fresh.state = .starting
        fresh.mic = request.micEnabled
        fresh.source = request.source
        fresh.keepAudio = request.keepAudio
        status = fresh
        push()

        guard request.source.kind == .all || !request.source.bundleIds.isEmpty else { return failStart(.noSource) }
        if request.micEnabled {
            var permission = catalog.micPermission()
            if permission == .undetermined {
                _ = await catalog.requestMicAccess()
                permission = catalog.micPermission()
            }
            guard permission == .granted else { return failStart(.micDenied) }
        }
        if stopRequested { return abortStart() }

        sleepBlocker.begin()
        let sessionFolder: URL
        let newMeta: RecordingMeta
        do {
            let root = RecordingStore.recordingsDirectory(dataDirectory)
            try PrivateFiles.ensureDirectory(root)
            sessionFolder = root.appendingPathComponent(try RecordingStore.newSessionId(), isDirectory: true)
            try PrivateFiles.ensureDirectory(sessionFolder)
            newMeta = RecordingMeta(
                ref: try RecordingStore.newRef(), startedAt: RecordingStore.isoString(wallClock()),
                language: request.language, channels: request.channels, title: request.title)
            folder = sessionFolder
            meta = newMeta
            try RecordingStore.writeMeta(newMeta, in: sessionFolder)
        } catch {
            return failStart(.startFailed)
        }

        let chunkWriter = RecordingChunkWriter(
            folder: sessionFolder, channels: request.channels, chunkSeconds: chunkSeconds,
            onFailure: { [weak self] in
                Task { @MainActor in await self?.finish(error: nil, warning: .diskLow) }
            })
        writer = chunkWriter
        engine.setHandlers(
            samples: { frames, channels in chunkWriter.append(frames, channels: channels) },
            events: { [weak self] event in
                Task { @MainActor in self?.handle(event) }
            })
        let capture = self.engine
        let config = request.config
        do {
            try await Task.detached { try capture.start(config) }.value
        } catch {
            capture.stop()
            _ = await chunkWriter.close()
            let code = (error as? CaptureFailure).map(RecordingError.init) ?? .startFailed
            return failStart(code)
        }

        status.state = .recording
        startedMono = clock()
        tickCount = 0
        startTicker()
        push()
        if stopRequested { await stop() }
        return nil
    }

    private func failStart(_ error: RecordingError) -> RecordingError {
        discardSession()
        sleepBlocker.end()
        status.state = .failed
        status.error = error
        push()
        scheduleLinger(failedLinger)
        return error
    }

    private func abortStart() -> RecordingError? {
        discardSession()
        status = .idle
        push()
        return nil
    }

    private func discardSession() {
        if let folder { RecordingStore.removeSession(folder) }
        folder = nil
        meta = nil
        writer = nil
    }

    // MARK: - During the recording

    private func handle(_ event: CaptureEvent) {
        guard status.state == .recording else { return }
        switch event {
        case .levels(let levels):
            status.levelMic = Double(min(max(levels.mic, 0), 1))
            status.levelSystem = Double(min(max(levels.system, 0), 1))
            if levels.system > 0 { warnings.remove(.silentSystem) }
            if levels.mic > 0 { warnings.remove(.silentMic) }
            refreshWarning()
        case .warning(let warning):
            let mapped = RecordingWarning(warning)
            warnings.insert(mapped)
            if mapped == .deviceChanged { deviceChangedAt = clock() }
            refreshWarning()
            push()
        case .failed(let failure):
            Task { await finish(error: RecordingError(failure), warning: nil) }
        }
    }

    /// The status shows the most important of the active warnings, so a short notice such
    /// as a device change never hides a silent channel.
    private func refreshWarning() {
        status.warning = RecordingWarning.priority.first { warnings.contains($0) }
    }

    private func startTicker() {
        tickTask?.cancel()
        let interval = tickInterval
        tickTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(for: interval)
                guard !Task.isCancelled, let self else { return }
                await self.tick()
            }
        }
    }

    /// Once a second: the timer, and every fifth second the free-space check.
    func tick() async {
        guard status.state == .recording else { return }
        status.elapsedS = max(0, clock() - startedMono)
        tickCount += 1
        if warnings.contains(.deviceChanged), clock() - deviceChangedAt >= RecordingFormat.deviceChangedNoticeS {
            warnings.remove(.deviceChanged)
            refreshWarning()
        }
        push()
        // A hard limit: the final file would not fit a WAV header after about 18 hours.
        if status.elapsedS >= maxDurationS {
            await finish(error: .maxDuration, warning: nil)
            return
        }
        if tickCount % 5 == 0 { await checkDisk() }
    }

    func checkDisk() async {
        guard status.state == .recording, let free = freeSpace(dataDirectory),
              free < RecordingDisk.minimumFreeBytes else { return }
        await finish(error: nil, warning: .diskLow)
    }

    // MARK: - Stop

    /// Stops, writes the final file and tells the service. Returns when everything is done.
    public func stop() async {
        switch status.state {
        case .starting:
            stopRequested = true
            await waitWhile { $0 == .starting || $0 == .stopping }
        case .recording:
            await finish(error: nil, warning: nil)
        case .stopping:
            await waitWhile { $0 == .stopping }
        case .idle, .queued, .failed:
            break
        }
    }

    private func waitWhile(_ condition: (RecordingState) -> Bool) async {
        while condition(status.state) { try? await Task.sleep(for: .milliseconds(50)) }
    }

    private func finish(error: RecordingError?, warning: RecordingWarning?) async {
        guard status.state == .recording, let folder, let meta, let writer else { return }
        tickTask?.cancel()
        status.elapsedS = max(0, clock() - startedMono)
        status.state = .stopping
        status.levelMic = 0
        status.levelSystem = 0
        if let warning {
            warnings.insert(warning)
            refreshWarning()
        }
        status.error = error
        push()

        let capture = self.engine
        await Task.detached { capture.stop() }.value
        let summary = await writer.close()
        self.writer = nil

        if summary.frames == 0 {
            RecordingStore.removeSession(folder)
            // An immediate Stop saves nothing and is not an error. An engine that gave no
            // audio at all over a longer time is.
            if error != nil || status.elapsedS >= 3 {
                settle(.failed, error: error ?? .finalizeFailed)
            } else {
                self.folder = nil
                self.meta = nil
                sleepBlocker.end()
                status = .idle
                push()
            }
            return
        }

        let data = dataDirectory
        let duration: Double
        do {
            duration = try await Task.detached { try RecordingFinalizer.finalize(folder: folder, meta: meta, data: data) }.value
        } catch {
            settle(.failed, error: .finalizeFailed)
            return
        }
        // The audio copy is made after the WAV is final and before the service hears about
        // the recording. When it fails, the transcript is made all the same.
        var audioRef: String?
        if status.keepAudio {
            let wavRef = meta.ref
            audioRef = try? await Task.detached { try RecordingAudio.encode(wavRef: wavRef, data: data) }.value
            status.audioSaved = audioRef != nil
        }
        // `folder` stays set until the enqueue is over: a recovery pass that starts while the
        // service restarts must not pick up the session that is being queued right now.
        let payload = RecordingEnqueuePayload(
            ref: meta.ref, title: meta.title, startedAt: RecordingStore.date(fromISO: meta.startedAt) ?? wallClock(),
            language: meta.language, layout: meta.layout, durationS: duration, audioRef: audioRef)
        let outcome = await enqueueDroppingBadAudio(payload)
        if outcome.dropped { status.audioSaved = false }
        switch outcome.result {
        case .queued(let id):
            var done = meta
            done.meetingId = id
            try? RecordingStore.writeMeta(done, in: folder)
            RecordingStore.removeSession(folder)
            status.meetingId = id
            settle(.queued, error: error)
        case .rejected, .unavailable:
            // The final file and the session record stay: the next launch tries again.
            settle(.failed, error: .enqueueFailed)
        }
    }

    /// Enqueues with the audio copy. When the service refuses only the copy, the recording
    /// goes again without it, so the transcript is never lost for the sake of the audio.
    private func enqueueDroppingBadAudio(
        _ payload: RecordingEnqueuePayload
    ) async -> (result: RecordingEnqueueOutcome, dropped: Bool) {
        let first = await enqueuer.enqueue(payload)
        guard let audioRef = payload.audioRef, case .rejected(let code) = first,
              code == "audio_missing" || code == "bad_audio_ref" else { return (first, false) }
        RecordingAudio.removeAudio(ref: audioRef, data: dataDirectory)
        var plain = payload
        plain.audioRef = nil
        return (await enqueuer.enqueue(plain), true)
    }

    private func settle(_ state: RecordingState, error: RecordingError?) {
        folder = nil
        meta = nil
        sleepBlocker.end()
        status.state = state
        status.error = error
        push()
        scheduleLinger(state == .queued ? queuedLinger : failedLinger)
    }

    private func scheduleLinger(_ delay: Duration) {
        lingerTask?.cancel()
        lingerTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self else { return }
            if self.status.state == .queued || self.status.state == .failed {
                self.status = .idle
                self.push()
            }
        }
    }

    private func push() { onStatus?(status) }

    // MARK: - Recovery

    /// Finishes what an earlier run left behind: session folders without a delivered final
    /// file are finalized and queued, final files that were never queued are queued again.
    /// Other names in `recordings/` and `uploads/` are not touched. Returns how many
    /// recordings were queued.
    @discardableResult
    public func recoverLeftovers() async -> Int {
        guard !recovering else { return 0 }
        recovering = true
        defer { recovering = false }
        var queued = 0
        RecordingAudio.removeStaleTemps(data: dataDirectory, now: wallClock())
        for sessionFolder in RecordingStore.leftoverSessions(data: dataDirectory, excluding: folder?.lastPathComponent) {
            if await recover(sessionFolder) { queued += 1 }
        }
        return queued
    }

    private func recover(_ sessionFolder: URL) async -> Bool {
        let parts = RecordingStore.parts(in: sessionFolder)
        var record = RecordingStore.readMeta(in: sessionFolder)
        if let record, record.meetingId != nil {
            // Queued before the crash, only the clean-up was missing.
            RecordingStore.removeSession(sessionFolder)
            return false
        }
        if record == nil {
            // The session record is gone or unreadable: rebuild it from the first chunk.
            // The first chunk that has a whole header tells the channel count.
            guard let channels = parts.lazy.compactMap({ Self.channels(ofChunk: $0) }).first,
                  let ref = try? RecordingStore.newRef() else {
                // Nothing readable at all: nothing was recorded.
                RecordingStore.removeSession(sessionFolder)
                return false
            }
            let rebuilt = RecordingMeta(
                ref: ref, startedAt: RecordingStore.isoString(wallClock()), language: lang().rawValue,
                channels: channels, title: nil)
            guard (try? RecordingStore.writeMeta(rebuilt, in: sessionFolder)) != nil else { return false }
            record = rebuilt
        }
        guard var current = record else { return false }

        let uploads = RecordingStore.uploadsDirectory(dataDirectory)
        let alreadyFinal = WAVFile.summary(of: uploads.appendingPathComponent(current.ref)) != nil
        let data = dataDirectory
        let snapshot = current
        let duration: Double
        do {
            duration = try await Task.detached {
                try RecordingFinalizer.finalize(folder: sessionFolder, meta: snapshot, data: data)
            }.value
        } catch RecordingFileError.badChunk {
            // Not one chunk is readable, so there is no audio to save.
            RecordingStore.removeSession(sessionFolder)
            return false
        } catch RecordingFileError.tooLong {
            // Can never be finalized. Kept for a week in case a newer version can, then removed.
            if Self.isStale(sessionFolder, now: wallClock()) { RecordingStore.removeSession(sessionFolder) }
            return false
        } catch {
            return false
        }
        if !alreadyFinal, current.title == nil { current.title = Text.recordingRecovered.string(lang()) }
        // A recording whose audio copy was made before the interruption keeps it. One that was
        // interrupted earlier knows nothing of the switch and keeps no audio.
        let payload = RecordingEnqueuePayload(
            ref: current.ref, title: current.title,
            startedAt: RecordingStore.date(fromISO: current.startedAt) ?? wallClock(),
            language: current.language, layout: current.layout, durationS: duration,
            audioRef: RecordingAudio.existingAudioRef(forWavRef: current.ref, data: dataDirectory))
        switch await enqueueDroppingBadAudio(payload).result {
        case .queued(let id):
            current.meetingId = id
            try? RecordingStore.writeMeta(current, in: sessionFolder)
            RecordingStore.removeSession(sessionFolder)
            return true
        case .rejected(let code):
            // The service no longer has the file (already transcribed): nothing left to do.
            if code == "file_missing" {
                // No meeting references the audio copy either, so it would never be deleted.
                if let audioRef = payload.audioRef { RecordingAudio.removeAudio(ref: audioRef, data: dataDirectory) }
                RecordingStore.removeSession(sessionFolder)
            }
            return false
        case .unavailable:
            return false
        }
    }

    private nonisolated static func isStale(_ folder: URL, now: Date) -> Bool {
        guard let modified = (try? folder.resourceValues(forKeys: [.contentModificationDateKey]))?.contentModificationDate
        else { return false }
        return now.timeIntervalSince(modified) > Double(RecordingFormat.staleSessionDays) * 86400
    }

    private nonisolated static func channels(ofChunk url: URL) -> Int? {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        return try? CAFChunk.info(fd: fd).channels
    }
}
