// From device buffers to the frames the recording session receives: resample each channel to
// 16 kHz, limit, convert to Int16, interleave (0 = microphone, 1 = system audio) and hand out
// in blocks of about 100 ms.

import Foundation

/// Collects converted samples per channel and hands out interleaved blocks. Channels stay
/// aligned frame by frame: a block is emitted only when every channel has enough frames.
public struct CaptureFrameAssembler: Sendable {
    public let channels: Int
    public let blockFrames: Int
    /// If one channel runs ahead of the other by more than this, the other gets zeros so the
    /// two never drift apart (it should not happen: both come from the same callback).
    public let maxSkew: Int
    private var pending: [[Int16]]

    public init(channels: Int, blockFrames: Int = 1_600, maxSkew: Int = 16_000) {
        precondition(channels == 1 || channels == 2, "one or two channels")
        self.channels = channels
        self.blockFrames = blockFrames
        self.maxSkew = maxSkew
        pending = Array(repeating: [], count: channels)
    }

    public var pendingFrames: Int { pending.map(\.count).min() ?? 0 }

    public mutating func append(_ samples: [Int16], channel: Int) {
        pending[channel].append(contentsOf: samples)
    }

    /// Full blocks that are ready, oldest first.
    public mutating func takeBlocks() -> [[Int16]] {
        balanceSkew()
        var blocks: [[Int16]] = []
        while pendingFrames >= blockFrames {
            blocks.append(interleave(frames: blockFrames))
        }
        return blocks
    }

    /// Everything left, shorter channels padded with zeros. Nil when nothing is left.
    public mutating func flush() -> [Int16]? {
        let longest = pending.map(\.count).max() ?? 0
        guard longest > 0 else { return nil }
        for c in 0..<channels where pending[c].count < longest {
            pending[c].append(contentsOf: repeatElement(0, count: longest - pending[c].count))
        }
        return interleave(frames: longest)
    }

    private mutating func balanceSkew() {
        guard channels == 2 else { return }
        let a = pending[0].count
        let b = pending[1].count
        if a - b > maxSkew {
            pending[1].append(contentsOf: repeatElement(0, count: a - b))
        } else if b - a > maxSkew {
            pending[0].append(contentsOf: repeatElement(0, count: b - a))
        }
    }

    private mutating func interleave(frames: Int) -> [Int16] {
        if channels == 1 {
            let block = Array(pending[0].prefix(frames))
            pending[0].removeFirst(frames)
            return block
        }
        var block = [Int16](repeating: 0, count: frames * channels)
        for c in 0..<channels {
            for i in 0..<frames { block[i * channels + c] = pending[c][i] }
            pending[c].removeFirst(frames)
        }
        return block
    }
}

/// The whole per-callback conversion. Not thread safe: the engine keeps it on one queue.
public struct CapturePipeline: Sendable {
    public static let outputRate = 16_000

    public let micEnabled: Bool
    public var channels: Int { micEnabled ? 2 : 1 }
    /// Frames (per channel) produced so far, including the ones still waiting in a block.
    public private(set) var producedFrames = 0

    private var inputRate = 0
    private var systemResampler: CaptureResampler?
    private var micResampler: CaptureResampler?
    private var systemLimiter = CaptureLimiter(sampleRate: CapturePipeline.outputRate)
    private var micLimiter = CaptureLimiter(sampleRate: CapturePipeline.outputRate)
    private var assembler: CaptureFrameAssembler

    public init(micEnabled: Bool, blockFrames: Int = 1_600) {
        self.micEnabled = micEnabled
        assembler = CaptureFrameAssembler(channels: micEnabled ? 2 : 1, blockFrames: blockFrames)
    }

    /// Converts one callback worth of mono input at `inputRate`. `mic` is ignored when the
    /// microphone is off and treated as silence when it is on but missing.
    public mutating func process(
        system: UnsafeBufferPointer<Float>, mic: UnsafeBufferPointer<Float>?, inputRate rate: Int
    ) -> [[Int16]] {
        guard rate > 0 else { return [] }
        if rate != inputRate {
            // A new device format: start new filters. The few samples of filter latency left
            // in the old ones are dropped.
            inputRate = rate
            systemResampler = CaptureResampler(inputRate: rate, outputRate: Self.outputRate)
            micResampler = micEnabled
                ? CaptureResampler(inputRate: rate, outputRate: Self.outputRate) : nil
        }
        let sys = systemResampler!.process(system)
        assembler.append(systemLimiter.convert(sys), channel: micEnabled ? 1 : 0)
        if micEnabled {
            let m: [Float]
            if let mic {
                m = micResampler!.process(mic)
            } else {
                m = [Float](repeating: 0, count: system.count).withUnsafeBufferPointer {
                    micResampler!.process($0)
                }
            }
            assembler.append(micLimiter.convert(m), channel: 0)
        }
        producedFrames += sys.count
        return assembler.takeBlocks()
    }

    /// Digital silence on every channel, used to bridge a gap while the capture was rebuilt so
    /// the recording keeps wall-clock time.
    public mutating func appendSilence(frames: Int) -> [[Int16]] {
        guard frames > 0 else { return [] }
        let zeros = [Int16](repeating: 0, count: frames)
        for c in 0..<channels { assembler.append(zeros, channel: c) }
        producedFrames += frames
        return assembler.takeBlocks()
    }

    /// The last, shorter block.
    public mutating func flush() -> [Int16]? {
        assembler.flush()
    }
}

public enum CaptureTimeline {
    /// Frames of silence to insert before the first buffer of a rebuilt capture, so the
    /// recording stays in step with the clock. `elapsed` is the time from the start of the
    /// recording to the start of that buffer. Small differences (callback jitter, filter
    /// latency) are ignored, very long gaps are capped.
    public static func gapFrames(
        elapsed: Double, produced: Int, rate: Int = CapturePipeline.outputRate,
        tolerance: Double = 0.05, maxGap: Double = 30
    ) -> Int {
        guard elapsed.isFinite, elapsed > 0 else { return 0 }
        let expected = Int(elapsed * Double(rate))
        let gap = expected - produced
        guard Double(gap) > tolerance * Double(rate) else { return 0 }
        return min(gap, Int(maxGap * Double(rate)))
    }
}
