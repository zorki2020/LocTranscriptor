import Foundation
import Testing

@testable import AppCore

private func stats(_ samples: [Float]) -> CaptureSignalStats {
    var s = CaptureSignalStats()
    s.add(samples)
    return s
}

private let zeros = stats([0, 0, 0, 0])
private let sound = stats([0, 0.2, -0.1, 0])
private let nothing = CaptureSignalStats()

/// Runs the monitor every 0.5 s from `from` to `to` and collects the actions with their times.
private func drive(
    _ monitor: inout CaptureHealthMonitor, from: Double, to: Double,
    system: CaptureSignalStats, mic: CaptureSignalStats = zeros, outputting: Bool
) -> [(Double, CaptureHealthMonitor.Action)] {
    var result: [(Double, CaptureHealthMonitor.Action)] = []
    var t = from
    while t <= to + 1e-9 {
        let tick = CaptureHealthMonitor.Tick(now: t, system: system, mic: mic, sourceOutputting: outputting)
        for action in monitor.observe(tick) { result.append((t, action)) }
        t += 0.5
    }
    return result
}

@Suite struct CaptureHealthMonitorTests {
    @Test func zerosFromTheStartWarnAfterFifteenSeconds() {
        // No permission for system audio: exact zeros even with nothing playing.
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        let actions = drive(&monitor, from: 0.5, to: 30, system: zeros, outputting: false)
        #expect(actions.count == 1)
        #expect(actions.first?.0 == 15)
        #expect(actions.first?.1 == .warn(.silentSystem))
    }

    @Test func silenceWhileNothingPlaysIsFineLater() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        #expect(drive(&monitor, from: 0.5, to: 5, system: sound, outputting: true).isEmpty)
        #expect(drive(&monitor, from: 5.5, to: 120, system: zeros, outputting: false).isEmpty)
    }

    @Test func zerosWhileTheSourcePlaysWarnOncePerEpisode() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        _ = drive(&monitor, from: 0.5, to: 2, system: sound, outputting: true)
        let quiet = drive(&monitor, from: 2.5, to: 40, system: zeros, outputting: true)
        let warnings = quiet.filter { $0.1 == .warn(.silentSystem) }
        #expect(warnings.count == 1)
        #expect(warnings.first!.0 >= 17 && warnings.first!.0 <= 17.5)
        // Sound returns, then a new silent episode warns again.
        _ = drive(&monitor, from: 40.5, to: 41, system: sound, outputting: true)
        let again = drive(&monitor, from: 41.5, to: 60, system: zeros, outputting: true)
        #expect(again.filter { $0.1 == .warn(.silentSystem) }.count == 1)
    }

    @Test func watchdogRestartsASilentTapWithCooldown() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        _ = drive(&monitor, from: 0.5, to: 10, system: sound, outputting: true)
        let quiet = drive(&monitor, from: 10.5, to: 100, system: zeros, outputting: true)
        let restarts = quiet.filter { $0.1 == .restartSilentTap }.map(\.0)
        // First after 4 s of zeros, then after 30 s, then 60 s.
        #expect(restarts == [14, 44, 104].filter { $0 <= 100 })
    }

    @Test func noWatchdogWithoutEverHearingTheTap() {
        // Probably a missing permission: rebuilding will not help.
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        let actions = drive(&monitor, from: 0.5, to: 60, system: zeros, outputting: true)
        #expect(!actions.contains { $0.1 == .restartSilentTap })
    }

    @Test func noWatchdogWhileTheSourceIsQuiet() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        _ = drive(&monitor, from: 0.5, to: 5, system: sound, outputting: true)
        let actions = drive(&monitor, from: 5.5, to: 60, system: zeros, outputting: false)
        #expect(actions.isEmpty)
    }

    @Test func micSilenceWarns() {
        var monitor = CaptureHealthMonitor(micEnabled: true, start: 0)
        let actions = drive(&monitor, from: 0.5, to: 20, system: sound, mic: zeros, outputting: true)
        #expect(actions.map(\.1) == [.warn(.silentMic)])
        #expect(actions.first?.0 == 15)
        var off = CaptureHealthMonitor(micEnabled: false, start: 0)
        #expect(drive(&off, from: 0.5, to: 20, system: sound, mic: zeros, outputting: true).isEmpty)
    }

    @Test func stalledCallbacksRestart() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        _ = drive(&monitor, from: 0.5, to: 3, system: sound, outputting: true)
        let stalled = drive(&monitor, from: 3.5, to: 7, system: nothing, outputting: true)
        #expect(stalled.map(\.0) == [5, 7])
        #expect(stalled.allSatisfy { $0.1 == .restartStalled })
        monitor.didRestart(at: 7.2)
        let resumed = drive(&monitor, from: 7.5, to: 12, system: sound, outputting: true)
        #expect(resumed.isEmpty)
    }

    @Test func restartResetsTheWatchdogClock() {
        var monitor = CaptureHealthMonitor(micEnabled: false, start: 0)
        _ = drive(&monitor, from: 0.5, to: 2, system: sound, outputting: true)
        _ = drive(&monitor, from: 2.5, to: 4, system: zeros, outputting: true)
        monitor.didRestart(at: 4)
        let after = drive(&monitor, from: 4.5, to: 7, system: zeros, outputting: true)
        #expect(after.isEmpty)
    }
}

@Suite struct CaptureBackoffTests {
    @Test func oneTwoFourThenGiveUp() {
        var backoff = CaptureBackoff()
        var delays: [Double] = []
        while let d = backoff.nextDelay() {
            delays.append(d)
            backoff.failed()
        }
        #expect(delays == [1, 2, 4])
        backoff.reset()
        #expect(backoff.nextDelay() == 1)
    }
}

/// A fake clock: `sleep` advances time.
private final class FakeClock {
    var t: Double = 100
    var sleeps = 0
}

@Suite struct CaptureWaitTests {
    @Test func findsAtFirstProbe() {
        let clock = FakeClock()
        let outcome = CaptureWait.poll(
            timeout: 60, now: { clock.t }, sleep: { clock.t += $0 }, cancelled: { false },
            probe: { 7 })
        guard case .found(7) = outcome else { Issue.record("not found"); return }
        #expect(clock.t == 100)
    }

    @Test func probesEverySecondUntilFound() {
        let clock = FakeClock()
        var probes: [Double] = []
        let outcome = CaptureWait.poll(
            timeout: 60, now: { clock.t }, sleep: { clock.t += $0 }, cancelled: { false },
            probe: { () -> Int? in
                probes.append((clock.t - 100).rounded())
                return clock.t - 100 >= 4.99 ? 1 : nil
            })
        guard case .found = outcome else { Issue.record("not found"); return }
        #expect(probes == [0, 1, 2, 3, 4, 5])
    }

    @Test func timesOutAfterSixtySeconds() {
        let clock = FakeClock()
        var probes = 0
        let outcome = CaptureWait.poll(
            timeout: 60, now: { clock.t }, sleep: { clock.t += $0 }, cancelled: { false },
            probe: { () -> Int? in
                probes += 1
                return nil
            })
        guard case .timedOut = outcome else { Issue.record("no timeout"); return }
        #expect(abs(clock.t - 160) < 0.01)
        #expect(probes == 61)
    }

    @Test func cancelStopsWithinAStep() {
        let clock = FakeClock()
        let outcome = CaptureWait.poll(
            timeout: 60, now: { clock.t }, sleep: { clock.t += $0 },
            cancelled: { clock.t >= 102.25 }, probe: { () -> Int? in nil })
        guard case .cancelled = outcome else { Issue.record("not cancelled"); return }
        #expect(clock.t < 102.4)
    }
}
