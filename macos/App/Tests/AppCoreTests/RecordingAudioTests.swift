import AVFoundation
import Foundation
import Testing

@testable import AppCore

/// A synthetic WAV (tones, no speech) written the way the recorder writes its final file.
func writeSyntheticWAV(at url: URL, seconds: Double, channels: Int) throws {
    let frames = Int(seconds * 16000)
    var samples = [Int16](repeating: 0, count: frames * channels)
    for frame in 0..<frames {
        for channel in 0..<channels {
            let t = Double(frame) / 16000
            let tone = sin(2 * .pi * (220 + 110 * Double(channel)) * t) + 0.5 * sin(2 * .pi * 660 * t)
            samples[frame * channels + channel] = Int16(tone * 6000)
        }
    }
    let data = WAVFile.header(dataBytes: UInt32(samples.count * 2), channels: channels) + bytes(of: samples)
    try data.write(to: url)
}

func fileSize(_ url: URL) -> Int {
    ((try? FileManager.default.attributesOfItem(atPath: url.path))?[.size] as? Int) ?? -1
}

@Suite struct RecordingAudioTests {
    let wavRef = "rec" + String(repeating: "a1", count: 12) + ".wav"

    private func prepare(seconds: Double, channels: Int) throws -> URL {
        let dir = try makeTempDir()
        try PrivateFiles.ensureDirectory(RecordingStore.uploadsDirectory(dir))
        try writeSyntheticWAV(at: RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef), seconds: seconds, channels: channels)
        return dir
    }

    @Test func namesFollowTheWavRef() {
        #expect(RecordingAudio.audioRef(forWavRef: wavRef) == "rec" + String(repeating: "a1", count: 12) + ".m4a")
        #expect(RecordingAudio.audioRef(forWavRef: "x.wav") == nil)
        #expect(RecordingAudio.audioRef(forWavRef: "../rec" + String(repeating: "a", count: 24) + ".wav") == nil)
        #expect(RecordingAudio.isAudioRef("rec" + String(repeating: "0", count: 24) + ".m4a"))
        #expect(!RecordingAudio.isAudioRef("rec" + String(repeating: "0", count: 24) + ".wav"))
        #expect(!RecordingAudio.isAudioRef("rec" + String(repeating: "G", count: 24) + ".m4a"))
    }

    @Test func monoIsEncodedSmallPrivateAndReadable() throws {
        // The container has a fixed overhead of a few tens of kilobytes, so the size check
        // needs a recording of some length.
        let dir = try prepare(seconds: 20, channels: 1)
        defer { try? FileManager.default.removeItem(at: dir) }
        let ref = try RecordingAudio.encode(wavRef: wavRef, data: dir)
        #expect(ref == RecordingAudio.audioRef(forWavRef: wavRef))
        let folder = RecordingAudio.audioDirectory(dir)
        let file = folder.appendingPathComponent(ref)
        #expect(mode(of: file) == 0o600)
        #expect(mode(of: folder) == 0o700)
        #expect(try FileManager.default.contentsOfDirectory(atPath: folder.path) == [ref])

        let opened = try AVAudioFile(forReading: file)
        #expect(opened.fileFormat.channelCount == 1)
        #expect(opened.fileFormat.sampleRate == 16000)
        let seconds = Double(opened.length) / opened.processingFormat.sampleRate
        #expect(abs(seconds - 20) < 0.3)
        let wav = fileSize(RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef))
        #expect(fileSize(file) > 0)
        #expect(fileSize(file) * 4 < wav)
        // The WAV is not touched.
        #expect(wav == 44 + 20 * 16000 * 2)
    }

    @Test func stereoKeepsBothChannels() throws {
        let dir = try prepare(seconds: 20, channels: 2)
        defer { try? FileManager.default.removeItem(at: dir) }
        let ref = try RecordingAudio.encode(wavRef: wavRef, data: dir)
        let file = RecordingAudio.audioDirectory(dir).appendingPathComponent(ref)
        let opened = try AVAudioFile(forReading: file)
        #expect(opened.fileFormat.channelCount == 2)
        #expect(abs(Double(opened.length) / opened.processingFormat.sampleRate - 20) < 0.3)
        let wav = fileSize(RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef))
        #expect(fileSize(file) * 4 < wav)
        #expect(mode(of: file) == 0o600)
    }

    @Test func aFailureMidwayLeavesNothingBehind() throws {
        let dir = try prepare(seconds: 3, channels: 1)
        defer { try? FileManager.default.removeItem(at: dir) }
        let folder = RecordingAudio.audioDirectory(dir)
        try PrivateFiles.ensureDirectory(folder)
        let source = RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef)
        let target = folder.appendingPathComponent(RecordingAudio.audioRef(forWavRef: wavRef)!)
        struct Boom: Error {}
        #expect(throws: RecordingAudioError.encoderFailed) {
            try RecordingAudio.encodeFile(from: source, to: target) { index in if index == 1 { throw Boom() } }
        }
        #expect(try FileManager.default.contentsOfDirectory(atPath: folder.path).isEmpty)
        #expect(FileManager.default.fileExists(atPath: source.path))
    }

    @Test func theTempFileIsPrivateWhileItIsWritten() throws {
        let dir = try prepare(seconds: 3, channels: 1)
        defer { try? FileManager.default.removeItem(at: dir) }
        let folder = RecordingAudio.audioDirectory(dir)
        try PrivateFiles.ensureDirectory(folder)
        let source = RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef)
        let target = folder.appendingPathComponent(RecordingAudio.audioRef(forWavRef: wavRef)!)
        var modes: [Int?] = []
        try RecordingAudio.encodeFile(from: source, to: target) { _ in
            let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
            modes += names.map { mode(of: folder.appendingPathComponent($0)) }
        }
        #expect(!modes.isEmpty)
        #expect(modes.allSatisfy { $0 == 0o600 })
    }

    @Test func aLinkOrFileInPlaceOfTheAudioFolderIsRefused() throws {
        let dir = try prepare(seconds: 1, channels: 1)
        defer { try? FileManager.default.removeItem(at: dir) }
        let elsewhere = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: elsewhere) }
        let folder = RecordingAudio.audioDirectory(dir)
        try FileManager.default.createSymbolicLink(at: folder, withDestinationURL: elsewhere)
        #expect(throws: RecordingAudioError.badFolder) { try RecordingAudio.encode(wavRef: wavRef, data: dir) }
        #expect(try FileManager.default.contentsOfDirectory(atPath: elsewhere.path).isEmpty)
        try FileManager.default.removeItem(at: folder)
        try Data([1]).write(to: folder)
        #expect(throws: RecordingAudioError.badFolder) { try RecordingAudio.encode(wavRef: wavRef, data: dir) }
    }

    @Test func notAWavFailsAndLeavesNothingBehind() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        try PrivateFiles.ensureDirectory(RecordingStore.uploadsDirectory(dir))
        try Data(repeating: 7, count: 5000).write(to: RecordingStore.uploadsDirectory(dir).appendingPathComponent(wavRef))
        #expect(throws: (any Error).self) { try RecordingAudio.encode(wavRef: wavRef, data: dir) }
        let folder = RecordingAudio.audioDirectory(dir)
        #expect(try FileManager.default.contentsOfDirectory(atPath: folder.path).isEmpty)
    }

    @Test func missingFileAndBadRefAreRefused() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        #expect(throws: RecordingAudioError.notARegularFile) { try RecordingAudio.encode(wavRef: wavRef, data: dir) }
        #expect(throws: RecordingAudioError.badRef) { try RecordingAudio.encode(wavRef: "../x.wav", data: dir) }
    }

    @Test func aLinkIsNotEncoded() throws {
        let dir = try prepare(seconds: 1, channels: 1)
        defer { try? FileManager.default.removeItem(at: dir) }
        let uploads = RecordingStore.uploadsDirectory(dir)
        let other = "rec" + String(repeating: "b2", count: 12) + ".wav"
        try FileManager.default.createSymbolicLink(
            at: uploads.appendingPathComponent(other), withDestinationURL: uploads.appendingPathComponent(wavRef))
        #expect(throws: RecordingAudioError.notARegularFile) { try RecordingAudio.encode(wavRef: other, data: dir) }
    }

    @Test func existingAudioNeedsARealNonEmptyFile() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let folder = RecordingAudio.audioDirectory(dir)
        try PrivateFiles.ensureDirectory(folder)
        let ref = RecordingAudio.audioRef(forWavRef: wavRef)!
        #expect(RecordingAudio.existingAudioRef(forWavRef: wavRef, data: dir) == nil)
        try Data().write(to: folder.appendingPathComponent(ref))
        #expect(RecordingAudio.existingAudioRef(forWavRef: wavRef, data: dir) == nil)
        try Data([1]).write(to: folder.appendingPathComponent(ref))
        #expect(RecordingAudio.existingAudioRef(forWavRef: wavRef, data: dir) == ref)
        RecordingAudio.removeAudio(ref: ref, data: dir)
        #expect(RecordingAudio.existingAudioRef(forWavRef: wavRef, data: dir) == nil)
    }

    @Test func staleTempFilesAreRemovedAndOthersStay() throws {
        let dir = try makeTempDir()
        defer { try? FileManager.default.removeItem(at: dir) }
        let folder = RecordingAudio.audioDirectory(dir)
        try PrivateFiles.ensureDirectory(folder)
        let temp = folder.appendingPathComponent("tmp-0011223344556677.m4a")
        let real = folder.appendingPathComponent(RecordingAudio.audioRef(forWavRef: wavRef)!)
        try Data([1]).write(to: temp)
        try Data([1]).write(to: real)
        RecordingAudio.removeStaleTemps(data: dir)
        #expect(FileManager.default.fileExists(atPath: temp.path))  // fresh: may be in use
        RecordingAudio.removeStaleTemps(data: dir, now: Date().addingTimeInterval(2 * 86400))
        #expect(!FileManager.default.fileExists(atPath: temp.path))
        #expect(FileManager.default.fileExists(atPath: real.path))
    }
}

final class SeenNames: @unchecked Sendable {
    private let lock = NSLock()
    private var value: [String] = []
    var names: [String] { lock.withLock { value } }
    func set(_ names: [String]) { lock.withLock { value = names } }
}

@MainActor
@Suite struct RecordingKeepAudioSessionTests {
    private func record(_ h: Harness, keepAudio: Bool, seconds: Int = 2) async throws {
        var request = h.request()
        request.keepAudio = keepAudio
        #expect(await h.session.start(request) == nil)
        h.engine.emit(pattern(frames: seconds * 16000, channels: 1), channels: 1)
        await h.session.stop()
    }

    private func audioFiles(_ h: Harness) -> [String] {
        let folder = RecordingAudio.audioDirectory(h.dir)
        return ((try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []).sorted()
    }

    @Test func withoutTheSwitchNothingChanges() async throws {
        let h = try Harness()
        try await record(h, keepAudio: false)
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.keepAudio == false)
        #expect(h.session.status.audioSaved == nil)
        #expect(h.enqueuer.payloads.first?.audioRef == nil)
        #expect(!FileManager.default.fileExists(atPath: RecordingAudio.audioDirectory(h.dir).path))
    }

    @Test func withTheSwitchTheM4aIsMadeBeforeTheEnqueueAndPassedOn() async throws {
        let h = try Harness()
        let seen = SeenNames()
        let folder = RecordingAudio.audioDirectory(h.dir)
        h.enqueuer.hook = {
            seen.set(((try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []).sorted())
        }
        try await record(h, keepAudio: true)
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.keepAudio == true)
        #expect(h.session.status.audioSaved == true)
        let payload = try #require(h.enqueuer.payloads.first)
        #expect(payload.audioRef == RecordingAudio.audioRef(forWavRef: payload.ref))
        #expect(audioFiles(h) == [try #require(payload.audioRef)])
        #expect(seen.names == audioFiles(h))
        let opened = try AVAudioFile(forReading: folder.appendingPathComponent(try #require(payload.audioRef)))
        #expect(opened.fileFormat.channelCount == 1)
        #expect(abs(Double(opened.length) / opened.processingFormat.sampleRate - 2) < 0.3)
    }

    @Test func aFailedEncodeStillQueuesTheRecording() async throws {
        let h = try Harness()
        // A plain file where the folder should be: the encoder cannot create `audio/`.
        try Data([1]).write(to: RecordingAudio.audioDirectory(h.dir))
        try await record(h, keepAudio: true)
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.audioSaved == false)
        #expect(h.session.status.keepAudio == true)
        #expect(h.enqueuer.payloads.first?.audioRef == nil)
        #expect(h.uploads.count == 1)
    }

    @Test func theServiceRefusingOnlyTheAudioGetsTheRecordingAgainWithoutIt() async throws {
        let h = try Harness(outcomes: [.rejected("audio_missing"), .queued(meetingId: 4)])
        try await record(h, keepAudio: true)
        #expect(h.session.status.state == .queued)
        #expect(h.session.status.meetingId == 4)
        #expect(h.session.status.audioSaved == false)
        #expect(h.enqueuer.payloads.count == 2)
        #expect(h.enqueuer.payloads[0].audioRef != nil)
        #expect(h.enqueuer.payloads[1].audioRef == nil)
        #expect(audioFiles(h).isEmpty)
    }

    @Test func theRetryAtLaunchSendsTheAudioRefWhenTheM4aExists() async throws {
        let h = try Harness(outcomes: [.unavailable])
        try await record(h, keepAudio: true)
        #expect(h.session.status.state == .failed)
        #expect(h.session.status.audioSaved == true)
        #expect(audioFiles(h).count == 1)

        let again = FakeEnqueuer([.queued(meetingId: 9)])
        let next = RecordingSession(
            engine: FakeEngine(), catalog: FakeCatalog(), enqueuer: again, dataDirectory: h.dir,
            sleepBlocker: FakeSleep(), lang: { .en })
        #expect(await next.recoverLeftovers() == 1)
        let payload = try #require(again.payloads.first)
        #expect(payload.audioRef == RecordingAudio.audioRef(forWavRef: payload.ref))
    }

    @Test func aRecoveryRefusedWithFileMissingDropsTheAudioCopyToo() async throws {
        let h = try Harness(outcomes: [.unavailable])
        try await record(h, keepAudio: true)
        #expect(audioFiles(h).count == 1)
        let again = FakeEnqueuer([.rejected("file_missing")])
        let next = RecordingSession(
            engine: FakeEngine(), catalog: FakeCatalog(), enqueuer: again, dataDirectory: h.dir,
            sleepBlocker: FakeSleep(), lang: { .en })
        #expect(await next.recoverLeftovers() == 0)
        #expect(again.payloads.first?.audioRef != nil)
        #expect(audioFiles(h).isEmpty)
        #expect(h.sessionFolders.isEmpty)
    }

    @Test func aRecoveredSessionWithoutAnM4aKeepsNoAudio() async throws {
        let h = try Harness(outcomes: [.unavailable])
        try await record(h, keepAudio: false)
        let again = FakeEnqueuer([.queued(meetingId: 9)])
        let next = RecordingSession(
            engine: FakeEngine(), catalog: FakeCatalog(), enqueuer: again, dataDirectory: h.dir,
            sleepBlocker: FakeSleep(), lang: { .en })
        #expect(await next.recoverLeftovers() == 1)
        #expect(again.payloads.first?.audioRef == nil)
        #expect(!FileManager.default.fileExists(atPath: RecordingAudio.audioDirectory(h.dir).path))
    }
}
