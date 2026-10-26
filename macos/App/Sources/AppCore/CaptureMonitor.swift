// Decisions the capture engine takes twice a second: when to warn about silence, when a tap
// that went quiet should be rebuilt, and how long to wait between rebuild attempts.

import Foundation

public struct CaptureHealthMonitor: Sendable {
    public struct Thresholds: Sendable, Equatable {
        /// Exact digital silence this long gives `silent_system` or `silent_mic`.
        public var silentWarning: Double = 15
        /// Zeros this long from a source that says it is playing restart the tap.
        public var watchdogZeros: Double = 4
        /// No callbacks at all for this long: the device went away.
        public var stall: Double = 2
        /// Pause after a watchdog restart before the next one, doubling up to the maximum.
        public var watchdogCooldown: Double = 30
        public var watchdogCooldownMax: Double = 300

        public init() {}
    }

    /// One interval of observations.
    public struct Tick: Sendable, Equatable {
        public var now: Double
        public var system: CaptureSignalStats
        public var mic: CaptureSignalStats
        /// True when a tapped process reports that it is playing audio.
        public var sourceOutputting: Bool

        public init(
            now: Double, system: CaptureSignalStats, mic: CaptureSignalStats,
            sourceOutputting: Bool
        ) {
            self.now = now
            self.system = system
            self.mic = mic
            self.sourceOutputting = sourceOutputting
        }
    }

    public enum Action: Sendable, Equatable {
        case warn(CaptureWarning)
        /// The tap returns zeros although the source plays: rebuild it.
        case restartSilentTap
        /// Callbacks stopped: rebuild against the current devices.
        case restartStalled
    }

    public let micEnabled: Bool
    public let thresholds: Thresholds
    private let start: Double
    private var lastTick: Double
    private var lastFrames: Double
    private var everSystemSignal = false
    private var systemZeroRun: Double = 0
    private var watchdogZeroRun: Double = 0
    private var systemWarned = false
    private var micZeroRun: Double = 0
    private var micWarned = false
    private var nextWatchdog: Double
    private var cooldown: Double

    public init(micEnabled: Bool, start: Double, thresholds: Thresholds = Thresholds()) {
        self.micEnabled = micEnabled
        self.thresholds = thresholds
        self.start = start
        lastTick = start
        lastFrames = start
        nextWatchdog = start
        cooldown = thresholds.watchdogCooldown
    }

    public mutating func observe(_ tick: Tick) -> [Action] {
        let dt = max(0, tick.now - lastTick)
        lastTick = tick.now
        var actions: [Action] = []

        if tick.system.frames > 0 { lastFrames = tick.now }

        // System channel.
        if tick.system.nonZero {
            everSystemSignal = true
            systemZeroRun = 0
            watchdogZeroRun = 0
            systemWarned = false
        } else if tick.system.frames > 0, tick.sourceOutputting {
            systemZeroRun += dt
            watchdogZeroRun += dt
        } else if !tick.sourceOutputting {
            // Nothing is playing: silence is expected.
            systemZeroRun = 0
            watchdogZeroRun = 0
        }
        let neverHeard = !everSystemSignal && tick.now - start >= thresholds.silentWarning
        if !systemWarned, neverHeard || systemZeroRun >= thresholds.silentWarning {
            systemWarned = true
            actions.append(.warn(.silentSystem))
        }

        // Microphone.
        if micEnabled {
            if tick.mic.nonZero {
                micZeroRun = 0
                micWarned = false
            } else if tick.mic.frames > 0 {
                micZeroRun += dt
            }
            if !micWarned, micZeroRun >= thresholds.silentWarning {
                micWarned = true
                actions.append(.warn(.silentMic))
            }
        }

        // A tap that worked and went quiet while its source plays. Without any signal since
        // the start it is more likely a missing permission, which a rebuild will not fix.
        if everSystemSignal, tick.sourceOutputting, watchdogZeroRun >= thresholds.watchdogZeros,
            tick.now >= nextWatchdog
        {
            actions.append(.restartSilentTap)
            nextWatchdog = tick.now + cooldown
            cooldown = min(cooldown * 2, thresholds.watchdogCooldownMax)
            watchdogZeroRun = 0
        }

        if tick.now - lastFrames >= thresholds.stall {
            actions.append(.restartStalled)
            lastFrames = tick.now
        }
        return actions
    }

    /// The capture was rebuilt: give the new one time before judging it.
    public mutating func didRestart(at now: Double) {
        watchdogZeroRun = 0
        lastFrames = now
    }
}

/// Waits between attempts to rebuild after a device change: 1 s, 2 s, 4 s, then give up.
public struct CaptureBackoff: Sendable, Equatable {
    public let delays: [Double]
    public private(set) var failures = 0

    public init(delays: [Double] = [1, 2, 4]) {
        self.delays = delays
    }

    /// Delay before the next attempt, nil when the attempts are used up.
    public func nextDelay() -> Double? {
        failures < delays.count ? delays[failures] : nil
    }

    public mutating func failed() { failures += 1 }

    public mutating func reset() { failures = 0 }
}

/// Polls for something to appear (an app's audio process) with a timeout, checking for
/// cancellation often so `stop` takes effect at once.
public enum CaptureWait {
    public enum Outcome<T> {
        case found(T)
        case cancelled
        case timedOut
    }

    /// Probes now and then every `interval` seconds until `probe` returns a value, `timeout`
    /// passes or `cancelled` says so (checked every `step` seconds).
    public static func poll<T>(
        timeout: Double, interval: Double = 1, step: Double = 0.1,
        now: () -> Double, sleep: (Double) -> Void, cancelled: () -> Bool, probe: () -> T?
    ) -> Outcome<T> {
        let begin = now()
        var nextProbe = begin
        while true {
            if cancelled() { return .cancelled }
            let t = now()
            if t >= nextProbe {
                if let value = probe() { return .found(value) }
                nextProbe += interval
            }
            if t - begin >= timeout { return .timedOut }
            sleep(min(step, max(0, nextProbe - t)))
        }
    }
}
