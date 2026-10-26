// System audio and microphone capture through Core Audio process taps.
//
// One capture is a "graph": a private process tap (all processes except this app, or the
// processes of one app), a private aggregate device whose main sub-device is the output device
// the user hears, the microphone as a drift-compensated sub-device of the same aggregate, and
// one IOProc that receives microphone and tap in the same callback, already aligned. No screen
// recording permission, no virtual drivers.
//
// Threads: `start` runs on the caller's thread (it can block on a permission dialog). Every
// later change (monitoring, rebuilds, teardown) runs on `control`. The IOProc runs on `io`,
// which also delivers the samples. Events are delivered on `events`, so a handler may call
// `stop` directly.

import AVFoundation
import AppCore
import CoreAudio
import Foundation
import os

private let log = Logger(subsystem: "local.loctranscriptor", category: "capture")

final class CoreAudioCaptureEngine: CaptureEngine, @unchecked Sendable {
    private let control = DispatchQueue(label: "local.loctranscriptor.capture.control")
    private let io = DispatchQueue(
        label: "local.loctranscriptor.capture.io", qos: .userInteractive)
    private let events = DispatchQueue(label: "local.loctranscriptor.capture.events")
    private let ioKey = DispatchSpecificKey<Bool>()
    private let controlKey = DispatchSpecificKey<Bool>()

    private let lock = NSLock()
    // Guarded by `lock`.
    private var samplesHandler: (@Sendable ([Int16], Int) -> Void)?
    private var eventsHandler: (@Sendable (CaptureEvent) -> Void)?
    private var generation = 0
    private var current: CaptureRun?
    private var graphCounter = 0

    init() {
        io.setSpecific(key: ioKey, value: true)
        control.setSpecific(key: controlKey, value: true)
    }

    func setHandlers(
        samples: @escaping @Sendable ([Int16], Int) -> Void,
        events: @escaping @Sendable (CaptureEvent) -> Void
    ) {
        lock.withLock {
            samplesHandler = samples
            eventsHandler = events
        }
    }

    // MARK: Start and stop

    func start(_ config: CaptureConfig) throws {
        if config.micEnabled, CoreAudioSourceCatalog.micPermission() == .denied {
            throw CaptureFailure.micDenied
        }
        stop()
        let (generation, samples) = lock.withLock { () -> (Int, @Sendable ([Int16], Int) -> Void) in
            self.generation += 1
            return (self.generation, samplesHandler ?? { _, _ in })
        }

        var target = Self.resolve(config.source)
        if case .apps = config.source, target.processes.isEmpty {
            // A conferencing app or browser often has no audio process until the call starts.
            // Wait for one (the session shows "starting"); `stop` ends the wait at once.
            let outcome = CaptureWait.poll(
                timeout: 60, now: { ProcessInfo.processInfo.systemUptime },
                sleep: { Thread.sleep(forTimeInterval: $0) },
                cancelled: { !self.isCurrent(generation) },
                probe: { () -> TapTarget? in
                    let found = Self.resolve(config.source)
                    return found.processes.isEmpty ? nil : found
                })
            switch outcome {
            case .found(let found): target = found
            case .cancelled: throw CaptureFailure.startFailed
            case .timedOut: throw CaptureFailure.sourceGone
            }
        }

        // The microphone runs before the aggregate is built: a Bluetooth headset switches to its
        // headset profile (and a new output format) only once its microphone is in use.
        var mic: AudioObjectID?
        var warmup: MicWarmup?
        defer { warmup?.stop() }
        if config.micEnabled {
            guard let device = Self.micDevice(for: config.micDeviceId) else {
                log.error("start: no input device")
                throw CaptureFailure.startFailed
            }
            mic = device
            warmup = MicWarmup(device: device)  // can block on the microphone dialog
            if CoreAudioSourceCatalog.micPermission() == .denied { throw CaptureFailure.micDenied }
            Self.waitForStableOutput()
        }
        guard isCurrent(generation) else { throw CaptureFailure.startFailed }

        let processor = CaptureIOProcessor(micEnabled: config.micEnabled, deliver: samples)
        let graph = try buildGraph(config: config, target: target, mic: mic, processor: processor)
        // The first start of a tap can block while the system asks for permission.
        let status = AudioDeviceStart(graph.aggregate, graph.ioProc)
        guard status == noErr else {
            log.error("start: device start failed \(status, privacy: .public)")
            graph.teardown()
            throw CaptureFailure.startFailed
        }

        let run = CaptureRun(
            generation: generation, config: config, processor: processor, graph: graph,
            target: target, start: ProcessInfo.processInfo.systemUptime)
        let installed = lock.withLock { () -> Bool in
            guard self.generation == generation else { return false }
            current = run
            return true
        }
        guard installed else {
            // `stop` was called while the start was waiting for a dialog.
            graph.teardown()
            throw CaptureFailure.startFailed
        }
        // Samples flow from here on: nothing from a start that is later abandoned.
        processor.activate(graph: graph.id)
        control.async { self.startMonitor(run) }
    }

    func stop() {
        let run = lock.withLock { () -> CaptureRun? in
            generation += 1
            defer { current = nil }
            return current
        }
        guard let run else { return }
        if DispatchQueue.getSpecific(key: ioKey) != nil {
            // Called from the samples handler: the IO queue is busy with this very call.
            control.async { self.finish(run) }
        } else if DispatchQueue.getSpecific(key: controlKey) != nil {
            finish(run)
        } else {
            control.sync { finish(run) }
        }
    }

    private func isCurrent(_ generation: Int) -> Bool {
        lock.withLock { self.generation == generation }
    }

    private func emit(_ event: CaptureEvent) {
        guard let handler = lock.withLock({ eventsHandler }) else { return }
        events.async { handler(event) }
    }

    /// Tears the run down and delivers the last partial block. On `control`.
    private func finish(_ run: CaptureRun) {
        guard !run.over else { return }
        run.over = true
        run.timer?.cancel()
        run.timer = nil
        run.graph.teardown()
        if DispatchQueue.getSpecific(key: ioKey) == nil {
            io.sync { run.processor.finish() }
        } else {
            io.async { run.processor.finish() }
        }
    }

    private func fail(_ run: CaptureRun, _ failure: CaptureFailure) {
        let mine = lock.withLock { () -> Bool in
            guard current === run else { return false }
            current = nil
            generation += 1
            return true
        }
        finish(run)
        if mine {
            log.error("capture failed: \(failure.rawValue, privacy: .public)")
            emit(.failed(failure))
        }
    }

    // MARK: Monitoring

    private func startMonitor(_ run: CaptureRun) {
        guard !run.over else { return }
        let timer = DispatchSource.makeTimerSource(queue: control)
        timer.schedule(deadline: .now() + 0.5, repeating: 0.5, leeway: .milliseconds(50))
        timer.setEventHandler { [weak self] in self?.tick(run) }
        run.timer = timer
        timer.resume()
    }

    private func tick(_ run: CaptureRun) {
        guard !run.over, isCurrent(run.generation) else { return }
        run.ticks += 1
        let now = ProcessInfo.processInfo.systemUptime
        let stats = run.processor.takeStats()
        emit(
            .levels(
                CaptureLevels(
                    mic: run.config.micEnabled ? stats.mic.level : 0, system: stats.system.level)))
        if stats.system.frames > 0, !run.rebuildPending { run.backoff.reset() }

        // Processes come and go (an app restarts, a browser starts its audio helper).
        if run.ticks % 4 == 0 {
            let target = Self.resolve(run.config.source)
            run.target = target
            if CaptureRetap.needsRebuild(
                excludesOwnProcess: run.config.source == .allExceptSelf,
                tapped: run.graph.processes, current: target.current)
            {
                requestRebuild(run, .processes)
            }
        }
        if Self.devicesChanged(run) { requestRebuild(run, .device) }

        let observed = CaptureHealthMonitor.Tick(
            now: now, system: stats.system, mic: stats.mic,
            sourceOutputting: run.target.outputting)
        for action in run.monitor.observe(observed) {
            switch action {
            case .warn(let warning): emit(.warning(warning))
            case .restartSilentTap:
                log.info("watchdog: tap silent while source plays")
                requestRebuild(run, .watchdog)
            case .restartStalled:
                log.info("watchdog: no callbacks")
                requestRebuild(run, .device)
            }
        }
    }

    private static func devicesChanged(_ run: CaptureRun) -> Bool {
        let graph = run.graph
        if HAL.defaultOutputDevice() != graph.outputDevice { return true }
        if graph.outputIncluded, let output = graph.outputDevice {
            if !HAL.isAlive(output) { return true }
            if let rate = HAL.nominalRate(output), rate != graph.sampleRate { return true }
        }
        if let rate = HAL.nominalRate(graph.aggregate), rate != graph.sampleRate { return true }
        guard run.config.micEnabled else { return false }
        guard let mic = graph.micDevice, HAL.isAlive(mic) else { return true }
        if let wanted = run.config.micDeviceId {
            // Running on the default input because the chosen one was missing: switch back
            // when it returns.
            if HAL.uid(mic) != wanted, HAL.aliveDevice(uid: wanted) != nil { return true }
        } else if HAL.defaultInputDevice() != mic {
            return true
        }
        return false
    }

    // MARK: Rebuild

    private func requestRebuild(_ run: CaptureRun, _ reason: RebuildReason) {
        if run.rebuildPending {
            if reason == .device { run.pendingReason = .device }
            return
        }
        var delay = 0.0
        if reason == .device {
            guard let next = run.backoff.nextDelay() else {
                fail(run, .startFailed)
                return
            }
            delay = next
        }
        run.rebuildPending = true
        run.pendingReason = reason
        control.asyncAfter(deadline: .now() + delay) { [weak self] in self?.rebuild(run) }
    }

    /// Builds and starts a new graph, then switches the samples over and tears the old one
    /// down. The old graph keeps running until the new one works.
    private func rebuild(_ run: CaptureRun) {
        run.rebuildPending = false
        guard !run.over, isCurrent(run.generation) else { return }
        let reason = run.pendingReason
        var target = run.target
        if case .apps = run.config.source, target.processes.isEmpty {
            target.processes = run.graph.processes
        }
        let mic = run.config.micEnabled ? Self.micDevice(for: run.config.micDeviceId) : nil
        do {
            if run.config.micEnabled, mic == nil { throw CaptureFailure.startFailed }
            let graph = try buildGraph(
                config: run.config, target: target, mic: mic, processor: run.processor)
            let status = AudioDeviceStart(graph.aggregate, graph.ioProc)
            guard status == noErr else {
                log.error("rebuild: device start failed \(status, privacy: .public)")
                graph.teardown()
                throw CaptureFailure.startFailed
            }
            run.processor.activate(graph: graph.id)
            let old = run.graph
            // A rebuild caused only by a sample rate settling or a stalled callback is not a
            // device change the user needs to hear about.
            let identityChanged = old.outputDevice != graph.outputDevice || old.micDevice != graph.micDevice
            run.graph = graph
            old.teardown()
            run.monitor.didRestart(at: ProcessInfo.processInfo.systemUptime)
            log.notice("rebuild done: device reason, identity changed \(identityChanged, privacy: .public)")
            if reason == .device, identityChanged { emit(.warning(.deviceChanged)) }
        } catch {
            if reason == .device {
                run.backoff.failed()
                requestRebuild(run, .device)
            }
            // Process and watchdog rebuilds keep the old graph and try again on a later tick.
        }
    }

    // MARK: Building a graph

    private func buildGraph(
        config: CaptureConfig, target: TapTarget, mic: AudioObjectID?,
        processor: CaptureIOProcessor
    ) throws -> CaptureGraph {
        let id = lock.withLock { () -> Int in
            graphCounter += 1
            return graphCounter
        }
        var graph = CaptureGraph(id: id)
        var complete = false
        defer { if !complete { graph.teardown() } }

        let output = HAL.defaultOutputDevice()
        graph.outputDevice = output
        graph.micDevice = mic
        graph.processes = target.processes

        let tapDescription: CATapDescription
        switch config.source {
        case .allExceptSelf:
            tapDescription = CATapDescription(
                stereoGlobalTapButExcludeProcesses: target.processes)
        case .apps:
            tapDescription = CATapDescription(stereoMixdownOfProcesses: target.processes)
        }
        let tapUID = UUID()
        tapDescription.uuid = tapUID
        tapDescription.name = "LocTranscriptor"
        tapDescription.isPrivate = true
        tapDescription.muteBehavior = .unmuted
        var tap = AudioObjectID(0)
        var status = AudioHardwareCreateProcessTap(tapDescription, &tap)
        guard status == noErr, tap != 0 else {
            log.error("tap create failed \(status, privacy: .public)")
            throw CaptureFailure.startFailed
        }
        graph.tap = tap

        let outputUID = output.flatMap(HAL.uid)
        let micUID = mic.flatMap(HAL.uid)
        if mic != nil, micUID == nil { throw CaptureFailure.startFailed }

        // Prefer the output device as the clock. If an aggregate with it cannot be made (an
        // odd virtual device), fall back to the microphone or the tap alone.
        var plan = BufferPlan(subDeviceBuffers: [], micSubDevice: nil)
        var aggregate = AudioObjectID(0)
        for includeOutput in outputUID != nil ? [true, false] : [false] {
            var subDevices: [[String: Any]] = []
            var candidate = BufferPlan(subDeviceBuffers: [], micSubDevice: nil)
            var main: String?
            if includeOutput, let output, let outputUID {
                subDevices.append([kAudioSubDeviceUIDKey: outputUID])
                candidate.subDeviceBuffers.append(HAL.inputBuffers(output).count)
                main = outputUID
                if micUID == outputUID { candidate.micSubDevice = 0 }
            }
            if let mic, let micUID, candidate.micSubDevice == nil {
                subDevices.append([
                    kAudioSubDeviceUIDKey: micUID,
                    kAudioSubDeviceDriftCompensationKey: main == nil ? 0 : 1,
                ])
                candidate.subDeviceBuffers.append(HAL.inputBuffers(mic).count)
                candidate.micSubDevice = subDevices.count - 1
                if main == nil { main = micUID }
            }
            var description: [String: Any] = [
                kAudioAggregateDeviceNameKey: "LocTranscriptor Capture",
                kAudioAggregateDeviceUIDKey: captureDeviceUIDPrefix + UUID().uuidString,
                kAudioAggregateDeviceIsPrivateKey: true,
                kAudioAggregateDeviceIsStackedKey: false,
                kAudioAggregateDeviceTapAutoStartKey: true,
                kAudioAggregateDeviceSubDeviceListKey: subDevices,
                kAudioAggregateDeviceTapListKey: [
                    [
                        kAudioSubTapUIDKey: tapUID.uuidString,
                        kAudioSubTapDriftCompensationKey: true,
                    ]
                ],
            ]
            if let main { description[kAudioAggregateDeviceMainSubDeviceKey] = main }
            status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &aggregate)
            if status == noErr, aggregate != 0 {
                plan = candidate
                graph.outputIncluded = includeOutput
                break
            }
            log.error("aggregate create failed \(status, privacy: .public)")
            aggregate = 0
        }
        guard aggregate != 0 else { throw CaptureFailure.startFailed }
        graph.aggregate = aggregate

        // A new aggregate can take a moment to publish its streams.
        for _ in 0..<10 where HAL.inputBuffers(aggregate).isEmpty {
            Thread.sleep(forTimeInterval: 0.05)
        }
        guard let rate = HAL.nominalRate(aggregate) ?? HAL.tapRate(tap) else {
            log.error("no sample rate")
            throw CaptureFailure.startFailed
        }
        graph.sampleRate = rate

        let graphId = graph.id
        let inputRate = Int(rate.rounded())
        var proc: AudioDeviceIOProcID?
        status = AudioDeviceCreateIOProcIDWithBlock(&proc, aggregate, io) {
            [processor, plan] _, input, _, _, _ in
            processor.handle(
                graph: graphId, plan: plan, rate: inputRate, input: input,
                now: ProcessInfo.processInfo.systemUptime)
        }
        guard status == noErr, let proc else {
            log.error("ioproc create failed \(status, privacy: .public)")
            throw CaptureFailure.startFailed
        }
        graph.ioProc = proc
        complete = true
        return graph
    }

    // MARK: Lookups

    /// The processes to tap (an app) or to leave out (everything but this app).
    private static func resolve(_ source: CaptureSource) -> TapTarget {
        let me = getpid()
        let list = HAL.processes()
        switch source {
        case .allExceptSelf:
            let own = list.filter { $0.pid == me }
            return TapTarget(
                processes: own.map(\.id).sorted(),
                outputting: list.contains { $0.pid != me && $0.outputting },
                current: own.map { CaptureProcessRef(id: $0.id, outputting: $0.outputting) })
        case .apps(let bundleIds):
            let matched = list.filter {
                $0.pid != me && CaptureAppGrouping.belongs($0.bundleId, to: bundleIds)
            }
            return TapTarget(
                processes: matched.map(\.id).sorted(),
                outputting: matched.contains(where: \.outputting),
                current: matched.map { CaptureProcessRef(id: $0.id, outputting: $0.outputting) })
        }
    }

    /// The chosen input device, or the system default when it is missing.
    private static func micDevice(for uid: String?) -> AudioObjectID? {
        if let uid, let device = HAL.aliveDevice(uid: uid) { return device }
        return HAL.defaultInputDevice()
    }

    /// Waits until the default output device and its format stop changing (at most 2.5 s).
    private static func waitForStableOutput(maxWait: Double = 2.5, settle: Double = 0.4) {
        func snapshot() -> [Double] {
            let output = HAL.defaultOutputDevice()
            return [
                Double(output ?? 0), output.flatMap(HAL.nominalRate) ?? 0,
                Double(output.map { HAL.inputBuffers($0).count } ?? 0),
            ]
        }
        let begin = ProcessInfo.processInfo.systemUptime
        var last = snapshot()
        var stableSince = begin
        while true {
            let now = ProcessInfo.processInfo.systemUptime
            if now - begin >= maxWait || now - stableSince >= settle { return }
            Thread.sleep(forTimeInterval: 0.1)
            let next = snapshot()
            if next != last {
                last = next
                stableSince = ProcessInfo.processInfo.systemUptime
            }
        }
    }
}

// MARK: - Pieces

private enum RebuildReason {
    /// Output or input device changed, or callbacks stopped: retried with backoff.
    case device
    /// The source's processes changed.
    case processes
    /// The tap went silent while its source plays.
    case watchdog
}

private struct TapTarget: Equatable {
    /// Process objects to tap (one app) or to exclude (everything else), sorted.
    var processes: [AudioObjectID]
    var outputting: Bool
    /// The source's process objects now: the app's (one app) or this app's own (everything
    /// else), with their output state.
    var current: [CaptureProcessRef]
}

/// How the device's input buffers are laid out before it starts.
private struct BufferPlan: Sendable {
    var subDeviceBuffers: [Int]
    var micSubDevice: Int?
}

/// Everything one capture device owns, released in reverse order of creation.
private struct CaptureGraph: @unchecked Sendable {
    let id: Int
    var tap = AudioObjectID(0)
    var aggregate = AudioObjectID(0)
    var ioProc: AudioDeviceIOProcID?
    /// The default output device when the graph was built.
    var outputDevice: AudioObjectID?
    var outputIncluded = false
    var micDevice: AudioObjectID?
    var sampleRate: Double = 0
    var processes: [AudioObjectID] = []

    init(id: Int) { self.id = id }

    /// Stop the IOProc, destroy it, destroy the aggregate, destroy the tap.
    func teardown() {
        if aggregate != 0, let ioProc {
            AudioDeviceStop(aggregate, ioProc)
            AudioDeviceDestroyIOProcID(aggregate, ioProc)
        }
        if aggregate != 0 { AudioHardwareDestroyAggregateDevice(aggregate) }
        if tap != 0 { AudioHardwareDestroyProcessTap(tap) }
    }
}

/// State of one recording, used only on the control queue after `start` hands it over.
private final class CaptureRun: @unchecked Sendable {
    let generation: Int
    let config: CaptureConfig
    let processor: CaptureIOProcessor
    var graph: CaptureGraph
    var target: TapTarget
    var monitor: CaptureHealthMonitor
    var backoff = CaptureBackoff()
    var timer: DispatchSourceTimer?
    var ticks = 0
    var rebuildPending = false
    var pendingReason = RebuildReason.device
    var over = false

    init(
        generation: Int, config: CaptureConfig, processor: CaptureIOProcessor,
        graph: CaptureGraph, target: TapTarget, start: Double
    ) {
        self.generation = generation
        self.config = config
        self.processor = processor
        self.graph = graph
        self.target = target
        monitor = CaptureHealthMonitor(micEnabled: config.micEnabled, start: start)
    }
}

/// Runs a microphone with an empty IOProc while the capture device is being built.
private final class MicWarmup {
    private let device: AudioObjectID
    private var proc: AudioDeviceIOProcID?
    private static let queue = DispatchQueue(label: "local.loctranscriptor.capture.warmup")

    init(device: AudioObjectID) {
        self.device = device
        var proc: AudioDeviceIOProcID?
        let created = AudioDeviceCreateIOProcIDWithBlock(&proc, device, Self.queue) { _, _, _, _, _ in }
        guard created == noErr, let proc else { return }
        if AudioDeviceStart(device, proc) == noErr {
            self.proc = proc
        } else {
            AudioDeviceDestroyIOProcID(device, proc)
        }
    }

    func stop() {
        guard let proc else { return }
        AudioDeviceStop(device, proc)
        AudioDeviceDestroyIOProcID(device, proc)
        self.proc = nil
    }
}

/// Turns IOProc buffers into Int16 blocks. Its conversion state lives on the IO queue; the
/// active graph and the statistics are shared with the control queue under a lock.
private final class CaptureIOProcessor: @unchecked Sendable {
    private let micEnabled: Bool
    private let deliver: @Sendable ([Int16], Int) -> Void

    // IO queue only.
    private var pipeline: CapturePipeline
    private var system: [Float] = []
    private var mic: [Float] = []
    private var startUptime: Double?
    private var lastGraph = -1
    private var finished = false

    // Guarded by `lock`.
    private let lock = NSLock()
    private var activeGraph = -1
    private var systemStats = CaptureSignalStats()
    private var micStats = CaptureSignalStats()

    init(micEnabled: Bool, deliver: @escaping @Sendable ([Int16], Int) -> Void) {
        self.micEnabled = micEnabled
        self.deliver = deliver
        pipeline = CapturePipeline(micEnabled: micEnabled)
    }

    /// Only callbacks of this graph are used; -1 drops everything.
    func activate(graph: Int) {
        lock.withLock { activeGraph = graph }
    }

    func takeStats() -> (system: CaptureSignalStats, mic: CaptureSignalStats) {
        lock.withLock { (systemStats.take(), micStats.take()) }
    }

    func handle(
        graph: Int, plan: BufferPlan, rate: Int, input: UnsafePointer<AudioBufferList>,
        now: Double
    ) {
        guard !finished, rate > 0, lock.withLock({ activeGraph }) == graph else { return }
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard
            let layout = CaptureStreamLayout.resolve(
                buffers: list.count, subDeviceBuffers: plan.subDeviceBuffers,
                micSubDevice: micEnabled ? plan.micSubDevice : nil)
        else { return }

        let tapBuffer = list[layout.tap]
        let tapChannels = max(1, Int(tapBuffer.mNumberChannels))
        let frames = Int(tapBuffer.mDataByteSize) / (MemoryLayout<Float>.size * tapChannels)
        guard frames > 0 else { return }
        Self.reset(&system, frames: frames)
        system.withUnsafeMutableBufferPointer { out in
            if let data = tapBuffer.mData {
                CaptureMixdown.addMono(
                    data.assumingMemoryBound(to: Float.self), channels: tapChannels,
                    frames: frames, into: out)
            }
        }
        if micEnabled {
            Self.reset(&mic, frames: frames)
            if let range = layout.mic {
                let buffers = range.filter { $0 < list.count }.map { list[$0] }
                let total = buffers.reduce(0) { $0 + Int($1.mNumberChannels) }
                mic.withUnsafeMutableBufferPointer { out in
                    for buffer in buffers {
                        let channels = Int(buffer.mNumberChannels)
                        guard channels > 0, total > 0, let data = buffer.mData else { continue }
                        let available = Int(buffer.mDataByteSize) / (MemoryLayout<Float>.size * channels)
                        CaptureMixdown.addMono(
                            data.assumingMemoryBound(to: Float.self), channels: channels,
                            frames: min(frames, available), into: out,
                            weight: Float(channels) / Float(total))
                    }
                }
            }
        }

        var blocks: [[Int16]] = []
        if graph != lastGraph {
            let bufferStart = now - Double(frames) / Double(rate)
            if let startUptime {
                // A rebuilt graph: fill the time it took with silence.
                let gap = CaptureTimeline.gapFrames(
                    elapsed: bufferStart - startUptime, produced: pipeline.producedFrames)
                blocks += pipeline.appendSilence(frames: gap)
            } else {
                startUptime = bufferStart
            }
            lastGraph = graph
        }

        system.withUnsafeBufferPointer { sys in
            mic.withUnsafeBufferPointer { m in
                lock.withLock {
                    systemStats.add(sys)
                    if micEnabled { micStats.add(m) }
                }
                blocks += pipeline.process(
                    system: sys, mic: micEnabled ? m : nil, inputRate: rate)
            }
        }
        let channels = pipeline.channels
        for block in blocks { deliver(block, channels) }
    }

    /// Delivers what is left and ignores every later callback. On the IO queue.
    func finish() {
        guard !finished else { return }
        finished = true
        lock.withLock { activeGraph = -1 }
        if let tail = pipeline.flush() { deliver(tail, pipeline.channels) }
    }

    private static func reset(_ buffer: inout [Float], frames: Int) {
        if buffer.count != frames {
            buffer = [Float](repeating: 0, count: frames)
        } else {
            buffer.withUnsafeMutableBufferPointer { $0.update(repeating: 0) }
        }
    }
}
