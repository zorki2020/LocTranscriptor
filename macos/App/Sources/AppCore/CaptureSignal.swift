// Small sample-level helpers for the capture engine: down-mixing device buffers to mono, a
// peak limiter in front of the Int16 conversion, and per-interval signal statistics.

import Foundation

public enum CaptureMixdown {
    /// Adds the average of `channels` interleaved channels of `frames` frames into `output`
    /// (which must hold at least `frames` samples), scaled by `weight`.
    public static func addMono(
        _ interleaved: UnsafePointer<Float>, channels: Int, frames: Int,
        into output: UnsafeMutableBufferPointer<Float>, weight: Float = 1
    ) {
        guard channels > 0, frames > 0 else { return }
        let n = min(frames, output.count)
        if channels == 1 {
            for i in 0..<n { output[i] += interleaved[i] * weight }
            return
        }
        let scale = weight / Float(channels)
        for i in 0..<n {
            var sum: Float = 0
            let base = i * channels
            for c in 0..<channels { sum += interleaved[base + c] }
            output[i] += sum * scale
        }
    }

    /// Mono average of an interleaved array, for tests and simple callers.
    public static func mono(_ interleaved: [Float], channels: Int) -> [Float] {
        guard channels > 0 else { return [] }
        let frames = interleaved.count / channels
        var out = [Float](repeating: 0, count: frames)
        interleaved.withUnsafeBufferPointer { src in
            out.withUnsafeMutableBufferPointer { dst in
                if let base = src.baseAddress {
                    addMono(base, channels: channels, frames: frames, into: dst)
                }
            }
        }
        return out
    }
}

/// Peak limiter with instant attack and exponential release. Apps sometimes output peaks above
/// 1.0 (and the resampler can overshoot a little): without it they would clip hard in Int16.
public struct CaptureLimiter: Sendable {
    public let ceiling: Float
    public private(set) var gain: Float = 1
    private let release: Float

    public init(sampleRate: Int = 16_000, ceiling: Float = 0.98, releaseSeconds: Double = 0.25) {
        self.ceiling = ceiling
        release = Float(1 - exp(-1 / (releaseSeconds * Double(sampleRate))))
    }

    public mutating func process(_ x: Float) -> Float {
        guard x.isFinite else { return 0 }
        let a = abs(x)
        if a * gain > ceiling { gain = ceiling / a }
        let y = x * gain
        gain += (1 - gain) * release
        return y
    }

    public static func int16(_ y: Float) -> Int16 {
        guard y.isFinite else { return 0 }
        return Int16((max(-1, min(1, y)) * 32767).rounded())
    }

    /// Limits and converts a block.
    public mutating func convert(_ samples: [Float]) -> [Int16] {
        var out = [Int16](repeating: 0, count: samples.count)
        for i in samples.indices { out[i] = Self.int16(process(samples[i])) }
        return out
    }
}

/// What one channel did during an interval: loudest sample and whether anything but exact
/// digital zero arrived. Exact zeros are what a tap without permission (or a muted input) gives.
public struct CaptureSignalStats: Sendable, Equatable {
    public var peak: Float = 0
    public var nonZero = false
    public var frames = 0

    public init() {}

    public mutating func add(_ samples: UnsafeBufferPointer<Float>) {
        var p = peak
        var any = nonZero
        for s in samples {
            let a = abs(s)
            if a != 0 { any = true }
            if a > p { p = a }
        }
        if !p.isFinite { p = 1 }
        peak = p
        nonZero = any
        frames += samples.count
    }

    public mutating func add(_ samples: [Float]) {
        samples.withUnsafeBufferPointer { add($0) }
    }

    /// Level for the interface, 0...1.
    public var level: Float { min(1, max(0, peak)) }

    /// Returns the statistics so far and starts a new interval.
    public mutating func take() -> CaptureSignalStats {
        let current = self
        self = CaptureSignalStats()
        return current
    }
}
