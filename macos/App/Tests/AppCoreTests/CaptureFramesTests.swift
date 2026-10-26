import Foundation
import Testing

@testable import AppCore

@Suite struct CaptureFrameAssemblerTests {
    @Test func monoBlocks() {
        var assembler = CaptureFrameAssembler(channels: 1, blockFrames: 4)
        assembler.append([1, 2, 3], channel: 0)
        #expect(assembler.takeBlocks().isEmpty)
        assembler.append([4, 5, 6, 7, 8, 9], channel: 0)
        #expect(assembler.takeBlocks() == [[1, 2, 3, 4], [5, 6, 7, 8]])
        #expect(assembler.flush() == [9])
        #expect(assembler.flush() == nil)
    }

    @Test func interleavesMicFirst() {
        var assembler = CaptureFrameAssembler(channels: 2, blockFrames: 3)
        assembler.append([1, 2, 3, 4], channel: 0)  // mic
        #expect(assembler.takeBlocks().isEmpty)  // waits for the system channel
        assembler.append([-1, -2, -3], channel: 1)  // system
        #expect(assembler.takeBlocks() == [[1, -1, 2, -2, 3, -3]])
        // The leftover mic frame waits for its partner and is padded at the end.
        #expect(assembler.flush() == [4, 0])
    }

    @Test func skewIsBoundedWithSilence() {
        var assembler = CaptureFrameAssembler(channels: 2, blockFrames: 2, maxSkew: 4)
        assembler.append([1, 1, 1, 1, 1, 1], channel: 1)
        let blocks = assembler.takeBlocks()
        #expect(blocks.count == 3)
        #expect(blocks.allSatisfy { $0 == [0, 1, 0, 1] })
    }
}

@Suite struct CapturePipelineTests {
    private func run(_ pipeline: inout CapturePipeline, system: [Float], mic: [Float]?, rate: Int)
        -> [[Int16]]
    {
        system.withUnsafeBufferPointer { s in
            if let mic {
                return mic.withUnsafeBufferPointer { pipeline.process(system: s, mic: $0, inputRate: rate) }
            }
            return pipeline.process(system: s, mic: nil, inputRate: rate)
        }
    }

    @Test func monoAtSixteenKilohertz() {
        var pipeline = CapturePipeline(micEnabled: false)
        var blocks: [[Int16]] = []
        // 1 s at 48 kHz in 10 ms callbacks.
        for _ in 0..<100 {
            blocks += run(&pipeline, system: [Float](repeating: 0.25, count: 480), mic: nil, rate: 48_000)
        }
        #expect(blocks.allSatisfy { $0.count == 1_600 })
        #expect(blocks.count == 9)  // 10th block is short by the filter latency
        let tail = pipeline.flush() ?? []
        #expect(blocks.count * 1_600 + tail.count == pipeline.producedFrames)
        #expect(pipeline.producedFrames > 15_950 && pipeline.producedFrames <= 16_000)
        #expect(abs(Int(blocks[5][800]) - 8_192) <= 2)
    }

    @Test func twoChannelsMicFirstAndAligned() {
        var pipeline = CapturePipeline(micEnabled: true)
        var blocks: [[Int16]] = []
        for _ in 0..<50 {
            blocks += run(
                &pipeline, system: [Float](repeating: -0.5, count: 441),
                mic: [Float](repeating: 0.25, count: 441), rate: 44_100)
        }
        #expect(pipeline.channels == 2)
        #expect(!blocks.isEmpty)
        for block in blocks {
            #expect(block.count == 3_200)
        }
        let mid = blocks[1]
        #expect(abs(Int(mid[100]) - 8_192) <= 2)  // channel 0: microphone
        #expect(abs(Int(mid[101]) + 16_384) <= 2)  // channel 1: system
    }

    @Test func missingMicIsSilence() {
        var pipeline = CapturePipeline(micEnabled: true)
        var blocks: [[Int16]] = []
        for _ in 0..<20 {
            blocks += run(&pipeline, system: [Float](repeating: 0.5, count: 480), mic: nil, rate: 48_000)
        }
        let block = blocks[0]
        #expect(stride(from: 0, to: block.count, by: 2).allSatisfy { block[$0] == 0 })
    }

    @Test func loudInputIsLimitedNotClipped() {
        var pipeline = CapturePipeline(micEnabled: false)
        var blocks: [[Int16]] = []
        let loud = (0..<4_800).map { Float(sin(Double($0) * 0.05)) * 1.6 }
        for _ in 0..<5 { blocks += run(&pipeline, system: loud, mic: nil, rate: 48_000) }
        let peak = blocks.flatMap { $0 }.map { abs(Int($0)) }.max() ?? 0
        #expect(peak <= 32_113)
        #expect(peak > 25_000)
    }

    @Test func rateChangeKeepsGoing() {
        var pipeline = CapturePipeline(micEnabled: false)
        _ = run(&pipeline, system: [Float](repeating: 0.1, count: 48_000), mic: nil, rate: 48_000)
        let before = pipeline.producedFrames
        _ = run(&pipeline, system: [Float](repeating: 0.1, count: 24_000), mic: nil, rate: 24_000)
        let added = pipeline.producedFrames - before
        #expect(added > 15_900 && added <= 16_000)
    }

    @Test func silenceIsAddedToEveryChannel() {
        var pipeline = CapturePipeline(micEnabled: true)
        let blocks = pipeline.appendSilence(frames: 3_300)
        #expect(blocks.count == 2)
        #expect(blocks.allSatisfy { $0.count == 3_200 && $0.allSatisfy { $0 == 0 } })
        #expect(pipeline.producedFrames == 3_300)
        #expect(pipeline.flush()?.count == 200)
    }
}

@Suite struct CaptureTimelineTests {
    @Test func fillsRealGapsOnly() {
        // 10 s elapsed, 9.5 s produced: half a second of silence.
        #expect(CaptureTimeline.gapFrames(elapsed: 10, produced: 152_000) == 8_000)
        // Jitter below 50 ms is ignored.
        #expect(CaptureTimeline.gapFrames(elapsed: 10, produced: 159_500) == 0)
        // Ahead of the clock: nothing.
        #expect(CaptureTimeline.gapFrames(elapsed: 10, produced: 170_000) == 0)
        // Capped at 30 s.
        #expect(CaptureTimeline.gapFrames(elapsed: 100, produced: 0) == 480_000)
        #expect(CaptureTimeline.gapFrames(elapsed: .nan, produced: 0) == 0)
        #expect(CaptureTimeline.gapFrames(elapsed: -1, produced: 0) == 0)
    }
}
