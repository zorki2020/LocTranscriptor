// Keeping the audio of a recording: the final WAV is converted to AAC in an m4a container
// (16 kHz, same channel count, about 32 kbps per channel) in `<data>/audio/`. The WAV is
// read and written in blocks, so a long recording never sits in memory.
// Privacy: errors are codes, file names and contents never leave these functions.

import AVFoundation
import Foundation

public enum RecordingAudioError: Error, Equatable {
    case badRef
    case notARegularFile
    case badFolder
    case unreadable
    case encoderFailed
    case io(Int32)
}

public enum RecordingAudio {
    /// AAC bit rate per channel.
    public static let bitRatePerChannel = 32_000
    /// Frames converted per block (about one second).
    static let blockFrames: AVAudioFrameCount = 16_000
    /// Half-written files older than this are removed.
    static let staleTempAge: TimeInterval = 86_400
    static let tempPrefix = "tmp-"

    public static func audioDirectory(_ data: URL) -> URL { data.appendingPathComponent("audio", isDirectory: true) }

    /// `rec<24 hex>.wav` -> `rec<24 hex>.m4a`. Nil for any other name.
    public static func audioRef(forWavRef ref: String) -> String? {
        RecordingStore.isRef(ref) ? String(ref.dropLast(4)) + ".m4a" : nil
    }

    public static func isAudioRef(_ name: String) -> Bool {
        guard name.utf8.count == 31, name.hasPrefix("rec"), name.hasSuffix(".m4a") else { return false }
        return name.dropFirst(3).dropLast(4).utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    /// The m4a that belongs to a WAV ref, if it exists as a real file (no link) with content.
    public static func existingAudioRef(forWavRef ref: String, data: URL) -> String? {
        guard let audioRef = audioRef(forWavRef: ref) else { return nil }
        var st = stat()
        let path = audioDirectory(data).appendingPathComponent(audioRef).path
        guard lstat(path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_size > 0 else { return nil }
        return audioRef
    }

    public static func removeAudio(ref: String, data: URL) {
        guard isAudioRef(ref) else { return }
        unlink(audioDirectory(data).appendingPathComponent(ref).path)
    }

    /// Converts `uploads/<wavRef>` to `audio/<audioRef>` and returns the audio ref. The file
    /// is written under a temporary name in the same folder and renamed, so a reader never
    /// sees half a file. Any failure removes the temporary file and leaves no audio behind.
    @discardableResult
    public static func encode(wavRef: String, data: URL) throws -> String {
        guard let audioRef = audioRef(forWavRef: wavRef) else { throw RecordingAudioError.badRef }
        let source = RecordingStore.uploadsDirectory(data).appendingPathComponent(wavRef)
        let folder = audioDirectory(data)
        try ensureFolder(folder)
        try encodeFile(from: source, to: folder.appendingPathComponent(audioRef))
        return audioRef
    }

    /// `audio/` must be a real directory (a link or a plain file is refused), made with mode 700.
    static func ensureFolder(_ folder: URL) throws {
        var st = stat()
        if lstat(folder.path, &st) == 0 {
            guard (st.st_mode & S_IFMT) == S_IFDIR else { throw RecordingAudioError.badFolder }
        } else {
            try PrivateFiles.ensureDirectory(folder)
            guard lstat(folder.path, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR else {
                throw RecordingAudioError.badFolder
            }
        }
        chmod(folder.path, 0o700)
    }

    /// Removes leftovers of an encode that was cut short (a crash, a quit).
    public static func removeStaleTemps(data: URL, now: Date = Date()) {
        let folder = audioDirectory(data)
        for name in (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        where name.hasPrefix(tempPrefix) && name.hasSuffix(".m4a") {
            let url = folder.appendingPathComponent(name)
            var st = stat()
            guard lstat(url.path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG else { continue }
            if now.timeIntervalSince1970 - Double(st.st_mtimespec.tv_sec) > staleTempAge { unlink(url.path) }
        }
    }

    /// `block` is called before every block with its index (tests use it to fail midway).
    static func encodeFile(
        from source: URL, to target: URL, block: ((Int) throws -> Void)? = nil
    ) throws {
        var st = stat()
        guard lstat(source.path, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_size > RecordingFormat.wavHeaderSize
        else { throw RecordingAudioError.notARegularFile }

        // The extension is what makes AVAudioFile write an m4a container.
        let temp = target.deletingLastPathComponent()
            .appendingPathComponent(tempPrefix + (try RecordingStore.randomHex(bytes: 8)) + ".m4a")
        var succeeded = false
        defer { if !succeeded { unlink(temp.path) } }

        // Created private from the first byte; the encoder then writes into this file.
        let created = open(temp.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard created >= 0 else { throw RecordingAudioError.io(errno) }
        fchmod(created, 0o600)
        close(created)

        do {
            let input = try AVAudioFile(forReading: source, commonFormat: .pcmFormatFloat32, interleaved: false)
            let format = input.processingFormat
            let channels = Int(format.channelCount)
            guard (1...2).contains(channels), Int(format.sampleRate) == RecordingFormat.sampleRate else {
                throw RecordingAudioError.unreadable
            }
            let settings: [String: Any] = [
                AVFormatIDKey: kAudioFormatMPEG4AAC,
                AVSampleRateKey: RecordingFormat.sampleRate,
                AVNumberOfChannelsKey: channels,
                AVEncoderBitRateKey: bitRatePerChannel * channels,
            ]
            let output = try AVAudioFile(
                forWriting: temp, settings: settings, commonFormat: .pcmFormatFloat32, interleaved: false)
            guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: blockFrames) else {
                throw RecordingAudioError.encoderFailed
            }
            var index = 0
            while input.framePosition < input.length {
                try block?(index)
                index += 1
                try input.read(into: buffer, frameCount: blockFrames)
                if buffer.frameLength == 0 { break }
                try output.write(from: buffer)
            }
            // Closing finishes the AAC stream: the file is not complete before this.
            output.close()
            guard index > 0 else { throw RecordingAudioError.unreadable }
        } catch let error as RecordingAudioError {
            throw error
        } catch let error as RecordingFileError {
            throw error
        } catch {
            throw RecordingAudioError.encoderFailed
        }

        chmod(temp.path, 0o600)
        let fd = open(temp.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { throw RecordingAudioError.io(errno) }
        defer { close(fd) }
        var written = stat()
        guard fstat(fd, &written) == 0, written.st_size > 0 else { throw RecordingAudioError.encoderFailed }
        PosixIO.fsyncFully(fd)
        guard rename(temp.path, target.path) == 0 else { throw RecordingAudioError.io(errno) }
        chmod(target.path, 0o600)
        succeeded = true
    }
}
