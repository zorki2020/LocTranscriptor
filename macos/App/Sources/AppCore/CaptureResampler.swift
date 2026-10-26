// Sample-rate conversion for captured audio: device rate (44.1, 48, 96 kHz...) down to the
// 16 kHz the transcriber wants. A streaming windowed-sinc filter, so the result does not
// depend on how the input is cut into callbacks and two channels fed the same number of
// frames always produce the same number of frames.

import Foundation

public struct CaptureResampler: Sendable {
    public let inputRate: Int
    public let outputRate: Int

    /// Kernel half width in input samples (real and the integer bound used for indexing).
    private let halfWidth: Double
    private let reach: Int
    /// Kernel values by distance from the centre, `resolution` points per input sample.
    private let table: [Float]
    private let resolution: Double
    private let passthrough: Bool

    /// Pending input. `position` is the index of the input sample at or just before the time
    /// of the next output sample, `fraction / outputRate` the remaining part of that time.
    private var buffer: [Float]
    private var position: Int
    private var fraction: Int

    /// - Parameters:
    ///   - zeroCrossings: kernel length on each side, in zero crossings of the sinc. 16 gives
    ///     a stop band of about 70 dB with the Blackman window.
    ///   - cutoff: pass band edge as a share of the output Nyquist frequency.
    public init(inputRate: Int, outputRate: Int, zeroCrossings: Int = 16, cutoff: Double = 0.85) {
        precondition(inputRate > 0 && outputRate > 0, "sample rates must be positive")
        self.inputRate = inputRate
        self.outputRate = outputRate
        passthrough = inputRate == outputRate
        // Cut-off in cycles per input sample.
        let fc = 0.5 * min(1, Double(outputRate) / Double(inputRate)) * cutoff
        halfWidth = Double(zeroCrossings) / (2 * fc)
        reach = Int(halfWidth.rounded(.up))
        resolution = 256
        let count = Int((halfWidth * resolution).rounded(.up)) + 2
        var table = [Float](repeating: 0, count: count)
        for i in 0..<count {
            let d = Double(i) / resolution
            guard d < halfWidth else { break }
            let x = 2 * fc * d
            let sinc = x == 0 ? 1 : sin(Double.pi * x) / (Double.pi * x)
            let u = d / halfWidth
            let window = 0.42 + 0.5 * cos(Double.pi * u) + 0.08 * cos(2 * Double.pi * u)
            table[i] = Float(2 * fc * sinc * window)
        }
        self.table = table
        // Left history of zeros so the first output sample sits at input time zero.
        buffer = [Float](repeating: 0, count: reach)
        position = reach
        fraction = 0
    }

    /// Feeds input samples and returns every output sample that is complete. The output lags
    /// the input by the kernel half width (about 1 ms) and has no other delay.
    public mutating func process(_ input: UnsafeBufferPointer<Float>) -> [Float] {
        if passthrough { return Array(input) }
        buffer.append(contentsOf: input)
        var output: [Float] = []
        output.reserveCapacity(input.count * outputRate / inputRate + 2)
        buffer.withUnsafeBufferPointer { samples in
            table.withUnsafeBufferPointer { kernel in
                while position + reach < samples.count {
                    let frac = Double(fraction) / Double(outputRate)
                    var acc: Float = 0
                    var weights: Float = 0
                    for i in (position - reach + 1)...(position + reach) {
                        let d = abs(Double(i - position) - frac)
                        guard d < halfWidth else { continue }
                        let t = d * resolution
                        let j = Int(t)
                        let w = kernel[j] + (kernel[j + 1] - kernel[j]) * Float(t - Double(j))
                        acc += samples[i] * w
                        weights += w
                    }
                    output.append(weights != 0 ? acc / weights : 0)
                    fraction += inputRate
                    position += fraction / outputRate
                    fraction %= outputRate
                }
            }
        }
        let consumed = position - reach
        if consumed > 0 {
            buffer.removeFirst(consumed)
            position -= consumed
        }
        return output
    }

    public mutating func process(_ input: [Float]) -> [Float] {
        input.withUnsafeBufferPointer { process($0) }
    }
}
