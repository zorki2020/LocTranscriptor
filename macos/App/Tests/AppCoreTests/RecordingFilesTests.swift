import Foundation
import Testing

@testable import AppCore

/// A fresh folder under the system temp directory, removed when the test ends.
func makeTempDir() throws -> URL {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("rec-test-" + UUID().uuidString)
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

func bytes(of samples: [Int16]) -> Data { samples.withUnsafeBytes { Data($0) } }

func mode(of url: URL) -> Int? {
    (try? FileManager.default.attributesOfItem(atPath: url.path))?[.posixPermissions] as? Int
}

/// Synthetic audio: a counting pattern, so any lost, doubled or reordered frame shows.
func pattern(frames: Int, channels: Int, from start: Int = 0) -> [Int16] {
    (0..<(frames * channels)).map { Int16(truncatingIfNeeded: (start * channels + $0) &* 31 &+ 5) }
}

@Suite struct WAVHeaderTests {
    @Test func monoHeaderIsExact() {
        let header = WAVFile.header(dataBytes: 32000, channels: 1)
        #expect(header.count == 44)
        var expected: [UInt8] = Array("RIFF".utf8)
        expected += [0x24, 0x7D, 0x00, 0x00]  // 36 + 32000
        expected += Array("WAVEfmt ".utf8)
        expected += [16, 0, 0, 0, 1, 0, 1, 0]  // fmt size, PCM, 1 channel
        expected += [0x80, 0x3E, 0, 0]  // 16000 Hz
        expected += [0x00, 0x7D, 0, 0]  // 32000 bytes per second
        expected += [2, 0, 16, 0]  // block align, bits
        expected += Array("data".utf8)
        expected += [0x00, 0x7D, 0, 0]
        #expect(Array(header) == expected)
    }

    @Test func stereoHeader() {
        let header = Array(WAVFile.header(dataBytes: 64000, channels: 2))
        #expect(header[22] == 2)  // channels
        #expect(Array(header[28..<32]) == [0x00, 0xFA, 0, 0])  // byte rate 64000
        #expect(header[32] == 4)  // block align
        #expect(header[34] == 16)
    }
}

@Suite struct ChunkWriterTests {
    @Test func rotatesAboutEveryChunkAndKeepsEverySample() async throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = RecordingChunkWriter(folder: dir, channels: 2, chunkSeconds: 1)
        // 2.5 s in uneven pieces that straddle chunk borders.
        let total = pattern(frames: 40000, channels: 2)
        var frame = 0
        for size in [7000, 9000, 16000, 8000] {
            writer.append(Array(total[(frame * 2)..<((frame + size) * 2)]), channels: 2)
            frame += size
        }
        let summary = await writer.close()
        #expect(summary.frames == 40000)
        #expect(!summary.failed)
        #expect(summary.parts.map(\.lastPathComponent) == ["part-0001.caf", "part-0002.caf", "part-0003.caf"])
        for part in summary.parts { #expect(mode(of: part) == 0o600) }
        let sizes = try summary.parts.map { try Data(contentsOf: $0).count - CAFChunk.headerSize }
        #expect(sizes == [16000 * 4, 16000 * 4, 8000 * 4])
    }

    @Test func dropsFramesWithAnotherChannelCount() async throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = RecordingChunkWriter(folder: dir, channels: 1)
        writer.append([1, 2, 3, 4], channels: 2)
        writer.append([1, 2, 3, 4], channels: 1)
        #expect(await writer.close().frames == 4)
    }

    @Test func chunkHeaderIsReadable() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("part-0001.caf")
        try (CAFChunk.header(channels: 2) + bytes(of: [1, 2, 3, 4])).write(to: url)
        let fd = open(url.path, O_RDONLY)
        defer { close(fd) }
        let info = try CAFChunk.info(fd: fd)
        #expect(info == CAFChunk.Info(channels: 2, dataOffset: Int64(CAFChunk.headerSize), dataLength: 8))
        #expect(CAFChunk.header(channels: 1).count == CAFChunk.headerSize)
    }

    @Test func garbageIsNotAChunk() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("part-0001.caf")
        try Data("not a caf file at all".utf8).write(to: url)
        let fd = open(url.path, O_RDONLY)
        defer { close(fd) }
        #expect(throws: RecordingFileError.self) { try CAFChunk.info(fd: fd) }
    }
}

@Suite struct FinalizerTests {
    func session(_ dir: URL, channels: Int) throws -> (folder: URL, meta: RecordingMeta) {
        let folder = RecordingStore.recordingsDirectory(dir).appendingPathComponent("0123456789abcdef")
        try PrivateFiles.ensureDirectory(folder)
        let meta = RecordingMeta(
            ref: "rec" + String(repeating: "a", count: 24) + ".wav", startedAt: "2026-10-23T10:00:00Z",
            language: "ru", channels: channels, title: nil)
        return (folder, meta)
    }

    @Test func finalFileIsOneWavWithTheSamplesExactly() async throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 2)
        let writer = RecordingChunkWriter(folder: folder, channels: 2, chunkSeconds: 1)
        let samples = pattern(frames: 37000, channels: 2)
        writer.append(samples, channels: 2)
        _ = await writer.close()

        let duration = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        #expect(abs(duration - 37000.0 / 16000.0) < 1e-9)
        let target = RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref)
        let file = try Data(contentsOf: target)
        #expect(file == WAVFile.header(dataBytes: UInt32(samples.count * 2), channels: 2) + bytes(of: samples))
        #expect(mode(of: target) == 0o600)
        #expect(mode(of: RecordingStore.uploadsDirectory(dir)) == 0o700)
        #expect(!FileManager.default.fileExists(atPath: folder.appendingPathComponent("final.tmp").path))
        let summary = try #require(WAVFile.summary(of: target))
        #expect(summary.channels == 2)
        #expect(abs(summary.durationS - duration) < 1e-9)
    }

    @Test func monoFileOfOneChunk() async throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 1)
        let writer = RecordingChunkWriter(folder: folder, channels: 1)
        let samples = pattern(frames: 1234, channels: 1)
        writer.append(samples, channels: 1)
        _ = await writer.close()
        _ = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref))
        #expect(file == WAVFile.header(dataBytes: 2468, channels: 1) + bytes(of: samples))
    }

    @Test func aChunkCutOffMidFrameLosesOnlyThePartialFrame() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 2)
        let samples = pattern(frames: 10, channels: 2)
        // Ten frames and three stray bytes, as after a crash in the middle of a write.
        try (CAFChunk.header(channels: 2) + bytes(of: samples) + Data([1, 2, 3]))
            .write(to: folder.appendingPathComponent("part-0001.caf"))
        let duration = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        #expect(abs(duration - 10.0 / 16000.0) < 1e-12)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref))
        #expect(file == WAVFile.header(dataBytes: 40, channels: 2) + bytes(of: samples))
    }

    @Test func brokenChunksAreSkippedAndEveryReadableOneIsKeptInOrder() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 1)
        let a = pattern(frames: 40, channels: 1)
        let b = pattern(frames: 30, channels: 1, from: 40)
        try (CAFChunk.header(channels: 1) + bytes(of: a)).write(to: folder.appendingPathComponent("part-0001.caf"))
        // A bad chunk in the middle, then a good one, then a zero-byte and a cut-header last chunk.
        try Data([1, 2, 3]).write(to: folder.appendingPathComponent("part-0002.caf"))
        try (CAFChunk.header(channels: 1) + bytes(of: b)).write(to: folder.appendingPathComponent("part-0003.caf"))
        try Data().write(to: folder.appendingPathComponent("part-0004.caf"))
        try CAFChunk.header(channels: 1).prefix(50).write(to: folder.appendingPathComponent("part-0005.caf"))
        let duration = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        #expect(abs(duration - 70.0 / 16000.0) < 1e-12)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref))
        #expect(file == WAVFile.header(dataBytes: 140, channels: 1) + bytes(of: a + b))
    }

    @Test func aZeroByteLastChunkDoesNotLoseTheRecording() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 2)
        let a = pattern(frames: 25, channels: 2)
        try (CAFChunk.header(channels: 2) + bytes(of: a)).write(to: folder.appendingPathComponent("part-0001.caf"))
        try Data().write(to: folder.appendingPathComponent("part-0002.caf"))
        _ = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref))
        #expect(file == WAVFile.header(dataBytes: 100, channels: 2) + bytes(of: a))
    }

    @Test func aTruncatedHeaderLastChunkDoesNotLoseTheRecording() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 1)
        let a = pattern(frames: 25, channels: 1)
        try (CAFChunk.header(channels: 1) + bytes(of: a)).write(to: folder.appendingPathComponent("part-0001.caf"))
        try CAFChunk.header(channels: 1).prefix(20).write(to: folder.appendingPathComponent("part-0002.caf"))
        _ = try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        let file = try Data(contentsOf: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref))
        #expect(file == WAVFile.header(dataBytes: 50, channels: 1) + bytes(of: a))
    }

    @Test func nothingAfterCloseCreatesAChunk() async throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let writer = RecordingChunkWriter(folder: dir, channels: 1, chunkSeconds: 1)
        writer.append(pattern(frames: 10, channels: 1), channels: 1)
        let summary = await writer.close()
        writer.append(pattern(frames: 20000, channels: 1), channels: 1)
        let again = await writer.close()
        #expect(summary.frames == 10 && again.frames == 10)
        #expect(RecordingStore.parts(in: dir).count == 1)
    }

    @Test func noAudioIsAnError() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 1)
        #expect(throws: RecordingFileError.badChunk) {
            try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        }
        #expect(WAVFile.summary(of: RecordingStore.uploadsDirectory(dir).appendingPathComponent(meta.ref)) == nil)
    }

    @Test func channelMismatchIsRefused() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 2)
        try (CAFChunk.header(channels: 1) + bytes(of: [1, 2, 3]))
            .write(to: folder.appendingPathComponent("part-0001.caf"))
        #expect(throws: RecordingFileError.badChunk) {
            try RecordingFinalizer.finalize(folder: folder, meta: meta, data: dir)
        }
    }

    @Test func namesAreChecked() {
        #expect(RecordingStore.isRef("rec" + String(repeating: "0f", count: 12) + ".wav"))
        #expect(!RecordingStore.isRef("rec" + String(repeating: "0F", count: 12) + ".wav"))
        #expect(!RecordingStore.isRef("rec0123.wav"))
        #expect(!RecordingStore.isRef("../" + String(repeating: "a", count: 28)))
        #expect(RecordingStore.isSessionId("0123456789abcdef"))
        #expect(!RecordingStore.isSessionId("0123456789abcdeg"))
        #expect((try? RecordingStore.newRef()).map(RecordingStore.isRef) == true)
        #expect((try? RecordingStore.newSessionId()).map(RecordingStore.isSessionId) == true)
    }

    @Test func metaRoundTripsAndRejectsABadRef() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let (folder, meta) = try session(dir, channels: 2)
        try RecordingStore.writeMeta(meta, in: folder)
        #expect(RecordingStore.readMeta(in: folder) == meta)
        #expect(mode(of: folder.appendingPathComponent("session.json")) == 0o600)
        var bad = meta
        bad.ref = "../../etc/passwd"
        try RecordingStore.writeMeta(bad, in: folder)
        #expect(RecordingStore.readMeta(in: folder) == nil)
    }
}
