import Foundation
import Testing

@testable import AppCore

private func sine(_ freq: Double, rate: Int, count: Int, amplitude: Float = 0.5) -> [Float] {
    (0..<count).map { amplitude * Float(sin(2 * Double.pi * freq * Double($0) / Double(rate))) }
}

private func rms(_ x: ArraySlice<Float>) -> Float {
    guard !x.isEmpty else { return 0 }
    return (x.reduce(0) { $0 + $1 * $1 } / Float(x.count)).squareRoot()
}

/// Deterministic noise in -1...1.
private func noise(_ count: Int, seed: UInt64 = 42) -> [Float] {
    var state = seed
    return (0..<count).map { _ in
        state = state &* 6_364_136_223_846_793_005 &+ 1_442_695_040_888_963_407
        return Float(Int64(bitPattern: state >> 1) % 2_000_000) / 1_000_000 - 1
    }
}

@Suite struct CaptureResamplerTests {
    @Test(arguments: [48_000, 44_100, 96_000, 24_000])
    func keepsASpeechTone(inputRate: Int) {
        var resampler = CaptureResampler(inputRate: inputRate, outputRate: 16_000)
        let out = resampler.process(sine(1_000, rate: inputRate, count: inputRate))
        // Output sample n sits at input time n / 16 kHz: compare with the ideal tone.
        let expected = sine(1_000, rate: 16_000, count: out.count)
        let skip = 100
        let worst = zip(out, expected).dropFirst(skip).map { abs($0 - $1) }.max() ?? 1
        #expect(worst < 0.01, "rate \(inputRate): \(worst)")
    }

    @Test func outputCountFollowsTheRatio() {
        var resampler = CaptureResampler(inputRate: 48_000, outputRate: 16_000)
        let out = resampler.process([Float](repeating: 0.1, count: 48_000))
        // One second of input, minus the filter latency still in the buffer.
        #expect(out.count <= 16_000)
        #expect(out.count >= 16_000 - 30)
        var r44 = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        var total = 0
        for _ in 0..<10 { total += r44.process([Float](repeating: 0, count: 4_410)).count }
        #expect(abs(total - 16_000) <= 30)
    }

    @Test func dcGainIsOne() {
        var resampler = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        let out = resampler.process([Float](repeating: 0.5, count: 44_100))
        for v in out.dropFirst(50) { #expect(abs(v - 0.5) < 1e-4) }
    }

    @Test func removesFrequenciesAboveTheNewNyquist() {
        var resampler = CaptureResampler(inputRate: 48_000, outputRate: 16_000)
        let out = resampler.process(sine(12_000, rate: 48_000, count: 48_000, amplitude: 1))
        #expect(rms(out.dropFirst(100)) < 0.003)
        var r441 = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        let out441 = r441.process(sine(10_000, rate: 44_100, count: 44_100, amplitude: 1))
        #expect(rms(out441.dropFirst(100)) < 0.003)
    }

    @Test func chunkingDoesNotChangeTheResult() {
        let input = noise(30_000)
        var whole = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        let expected = whole.process(input)
        var chunked = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        var got: [Float] = []
        var i = 0
        let sizes = [1, 7, 512, 471, 1_024, 3, 2_048]
        var k = 0
        while i < input.count {
            let n = min(sizes[k % sizes.count], input.count - i)
            got += chunked.process(Array(input[i..<(i + n)]))
            i += n
            k += 1
        }
        #expect(got == expected)
    }

    @Test func twoChannelsFedAlikeStayAligned() {
        var a = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        var b = CaptureResampler(inputRate: 44_100, outputRate: 16_000)
        var countA = 0
        var countB = 0
        for n in [441, 512, 1_000, 13, 4_096] {
            countA += a.process(noise(n, seed: 1)).count
            countB += b.process(noise(n, seed: 2)).count
            #expect(countA == countB)
        }
    }

    @Test func sameRatePassesThrough() {
        var resampler = CaptureResampler(inputRate: 16_000, outputRate: 16_000)
        let input = noise(1_000)
        #expect(resampler.process(input) == input)
    }
}

@Suite struct CaptureLimiterTests {
    @Test func quietSignalIsUntouched() {
        var limiter = CaptureLimiter()
        let input = sine(440, rate: 16_000, count: 16_000, amplitude: 0.5)
        for x in input { #expect(limiter.process(x) == x) }
    }

    @Test func peaksAboveOneNeverExceedTheCeiling() {
        var limiter = CaptureLimiter()
        let input = sine(440, rate: 16_000, count: 16_000, amplitude: 1.8)
        for x in input { #expect(abs(limiter.process(x)) <= limiter.ceiling + 1e-6) }
    }

    @Test func gainRecoversAfterASpike() {
        var limiter = CaptureLimiter(releaseSeconds: 0.25)
        _ = limiter.process(4)
        #expect(limiter.gain < 0.3)
        for _ in 0..<16_000 { _ = limiter.process(0.1) }
        #expect(limiter.gain > 0.98)
    }

    @Test func int16Conversion() {
        #expect(CaptureLimiter.int16(0) == 0)
        #expect(CaptureLimiter.int16(1) == 32_767)
        #expect(CaptureLimiter.int16(-1) == -32_767)
        #expect(CaptureLimiter.int16(5) == 32_767)
        #expect(CaptureLimiter.int16(.nan) == 0)
        #expect(CaptureLimiter.int16(0.5) == 16_384)
        var limiter = CaptureLimiter()
        let out = limiter.convert([2, -3, .infinity, .nan, 0])
        #expect(out[0] <= 32_113 && out[0] > 30_000)  // limited to the ceiling, not clipped
        #expect(out[1] >= -32_113 && out[1] < -30_000)
        #expect(out[2] == 0 && out[3] == 0)
    }
}

@Suite struct CaptureMixdownTests {
    @Test func averagesInterleavedChannels() {
        #expect(CaptureMixdown.mono([1, 0, 0.5, 0.5, -1, 1], channels: 2) == [0.5, 0.5, 0])
        #expect(CaptureMixdown.mono([0.25, 0.5], channels: 1) == [0.25, 0.5])
        #expect(CaptureMixdown.mono([1, 1, 1, 0, 0, 0], channels: 3) == [1, 0])
    }

    @Test func addsWithWeight() {
        var out: [Float] = [1, 1]
        let src: [Float] = [1, 1, 0, 0]
        src.withUnsafeBufferPointer { s in
            out.withUnsafeMutableBufferPointer { o in
                CaptureMixdown.addMono(s.baseAddress!, channels: 2, frames: 2, into: o, weight: 0.5)
            }
        }
        #expect(out == [1.5, 1])
    }
}

@Suite struct CaptureSignalStatsTests {
    @Test func peakAndExactZeros() {
        var stats = CaptureSignalStats()
        stats.add([0, 0, 0])
        #expect(!stats.nonZero && stats.peak == 0 && stats.frames == 3)
        stats.add([0, -0.4, 0.2])
        #expect(stats.nonZero && stats.peak == 0.4 && stats.frames == 6)
        let taken = stats.take()
        #expect(taken.peak == 0.4)
        #expect(stats == CaptureSignalStats())
    }

    @Test func levelIsClampedAndTinyNoiseCounts() {
        var stats = CaptureSignalStats()
        stats.add([1e-9])
        #expect(stats.nonZero)
        stats.add([3])
        #expect(stats.level == 1)
        var inf = CaptureSignalStats()
        inf.add([.infinity])
        #expect(inf.level == 1)
    }
}
