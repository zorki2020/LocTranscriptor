// Recording on disk: the CAF chunk format, the final WAV file, the session record that
// crash recovery reads. Everything streams: the whole recording is never in memory.
// Privacy: errors are codes, file names and contents never leave these functions.

import Foundation
import Security

public enum RecordingFormat {
    public static let sampleRate = 16000
    public static let bytesPerSample = 2
    /// About five minutes per chunk file.
    public static let chunkSeconds = 300
    public static let wavHeaderSize = 44
    /// A recording stops by itself after 12 hours.
    public static let maxDurationS: Double = 12 * 3600
    /// How long the "output device changed" notice stays in the status.
    public static let deviceChangedNoticeS: Double = 5
    /// Session folders that can never be finalized are deleted after this many days.
    public static let staleSessionDays = 7
}

public enum RecordingFileError: Error, Equatable {
    case io(Int32)
    case badChunk
    case tooLong
}

// MARK: - POSIX helpers

enum PosixIO {
    static func writeAll(_ fd: Int32, _ pointer: UnsafeRawPointer, _ count: Int) throws {
        var done = 0
        while done < count {
            let n = write(fd, pointer + done, count - done)
            if n < 0 {
                if errno == EINTR { continue }
                throw RecordingFileError.io(errno)
            }
            done += n
        }
    }

    static func readFull(_ fd: Int32, into buffer: UnsafeMutableRawPointer, count: Int, offset: Int64) throws -> Int {
        var done = 0
        while done < count {
            let n = pread(fd, buffer + done, count - done, off_t(offset) + off_t(done))
            if n < 0 {
                if errno == EINTR { continue }
                throw RecordingFileError.io(errno)
            }
            if n == 0 { break }
            done += n
        }
        return done
    }

    static func fsyncFully(_ fd: Int32) {
        // F_FULLFSYNC asks the drive to flush its cache too; plain fsync is the fallback.
        if fcntl(fd, F_FULLFSYNC) != 0 { fsync(fd) }
    }
}

// MARK: - CAF chunks

/// A CAF file with 16-bit little-endian PCM whose data chunk has the "size unknown" marker
/// (-1): a chunk cut off by a crash is still readable up to its last byte.
public enum CAFChunk {
    public static let headerSize = 68

    public static func header(channels: Int) -> Data {
        var data = Data()
        func be16(_ v: UInt16) { data.append(contentsOf: [UInt8(v >> 8), UInt8(v & 0xff)]) }
        func be32(_ v: UInt32) { for shift in stride(from: 24, through: 0, by: -8) { data.append(UInt8((v >> UInt32(shift)) & 0xff)) } }
        func be64(_ v: UInt64) { for shift in stride(from: 56, through: 0, by: -8) { data.append(UInt8((v >> UInt64(shift)) & 0xff)) } }
        data.append(contentsOf: Array("caff".utf8))
        be16(1)
        be16(0)
        data.append(contentsOf: Array("desc".utf8))
        be64(32)
        be64(Double(RecordingFormat.sampleRate).bitPattern)
        data.append(contentsOf: Array("lpcm".utf8))
        be32(2)  // little-endian integer samples
        be32(UInt32(channels * RecordingFormat.bytesPerSample))
        be32(1)
        be32(UInt32(channels))
        be32(16)
        data.append(contentsOf: Array("data".utf8))
        be64(UInt64.max)  // -1: runs to the end of the file
        be32(0)  // edit count
        return data
    }

    public struct Info: Equatable {
        public var channels: Int
        public var dataOffset: Int64
        public var dataLength: Int64
    }

    /// Parses the header of a chunk file. Throws `badChunk` for anything that is not the
    /// format written above.
    public static func info(fd: Int32) throws -> Info {
        var st = stat()
        guard fstat(fd, &st) == 0 else { throw RecordingFileError.io(errno) }
        let size = Int64(st.st_size)
        var head = [UInt8](repeating: 0, count: 8)
        guard try PosixIO.readFull(fd, into: &head, count: 8, offset: 0) == 8,
              Array(head[0..<4]) == Array("caff".utf8), head[4] == 0, head[5] == 1
        else { throw RecordingFileError.badChunk }
        var position: Int64 = 8
        var channels: Int?
        while position + 12 <= size {
            var chunkHead = [UInt8](repeating: 0, count: 12)
            guard try PosixIO.readFull(fd, into: &chunkHead, count: 12, offset: position) == 12 else {
                throw RecordingFileError.badChunk
            }
            let type = String(decoding: chunkHead[0..<4], as: UTF8.self)
            let length = chunkHead[4..<12].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) }
            let body = position + 12
            if type == "desc" {
                guard length >= 32 else { throw RecordingFileError.badChunk }
                var desc = [UInt8](repeating: 0, count: 32)
                guard try PosixIO.readFull(fd, into: &desc, count: 32, offset: body) == 32 else {
                    throw RecordingFileError.badChunk
                }
                func u32(_ at: Int) -> UInt32 { desc[at..<at + 4].reduce(UInt32(0)) { ($0 << 8) | UInt32($1) } }
                let rate = Double(bitPattern: desc[0..<8].reduce(UInt64(0)) { ($0 << 8) | UInt64($1) })
                guard String(decoding: desc[8..<12], as: UTF8.self) == "lpcm", rate == Double(RecordingFormat.sampleRate),
                      u32(12) & 1 == 0, u32(12) & 2 == 2, u32(28) == 16, (1...2).contains(Int(u32(24)))
                else { throw RecordingFileError.badChunk }
                channels = Int(u32(24))
            } else if type == "data" {
                guard let channels, length == UInt64.max || length >= 4 else { throw RecordingFileError.badChunk }
                let start = body + 4
                let available = max(0, size - start)
                let declared = length == UInt64.max ? available : min(available, Int64(clamping: length) - 4)
                return Info(channels: channels, dataOffset: start, dataLength: max(0, declared))
            }
            guard length != UInt64.max, length <= UInt64(Int64.max / 2) else { throw RecordingFileError.badChunk }
            position = body + Int64(length)
        }
        throw RecordingFileError.badChunk
    }
}

// MARK: - WAV

public enum WAVFile {
    /// RIFF/WAVE, PCM, 16-bit, 16 kHz. `dataBytes` is the size of the sample data.
    public static func header(dataBytes: UInt32, channels: Int) -> Data {
        var data = Data()
        func le16(_ v: UInt16) { data.append(contentsOf: [UInt8(v & 0xff), UInt8(v >> 8)]) }
        func le32(_ v: UInt32) { for shift in stride(from: 0, through: 24, by: 8) { data.append(UInt8((v >> UInt32(shift)) & 0xff)) } }
        let blockAlign = channels * RecordingFormat.bytesPerSample
        data.append(contentsOf: Array("RIFF".utf8))
        le32(36 &+ dataBytes)
        data.append(contentsOf: Array("WAVE".utf8))
        data.append(contentsOf: Array("fmt ".utf8))
        le32(16)
        le16(1)  // PCM
        le16(UInt16(channels))
        le32(UInt32(RecordingFormat.sampleRate))
        le32(UInt32(RecordingFormat.sampleRate * blockAlign))
        le16(UInt16(blockAlign))
        le16(16)
        data.append(contentsOf: Array("data".utf8))
        le32(dataBytes)
        return data
    }

    /// Channels and duration of a file made by `RecordingFinalizer`, read from its size.
    public static func summary(of url: URL) -> (channels: Int, durationS: Double)? {
        let fd = open(url.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var st = stat()
        guard fstat(fd, &st) == 0, (st.st_mode & S_IFMT) == S_IFREG, st.st_size > RecordingFormat.wavHeaderSize else {
            return nil
        }
        var head = [UInt8](repeating: 0, count: RecordingFormat.wavHeaderSize)
        guard (try? PosixIO.readFull(fd, into: &head, count: head.count, offset: 0)) == head.count,
              Array(head[0..<4]) == Array("RIFF".utf8), Array(head[8..<12]) == Array("WAVE".utf8) else { return nil }
        let channels = Int(head[22]) | Int(head[23]) << 8
        guard (1...2).contains(channels) else { return nil }
        let bytes = Double(st.st_size) - Double(RecordingFormat.wavHeaderSize)
        return (channels, bytes / Double(channels * RecordingFormat.bytesPerSample * RecordingFormat.sampleRate))
    }
}

// MARK: - Chunk writer

/// Appends interleaved Int16 frames to `part-0001.caf`, `part-0002.caf`, ... in a private
/// folder. Writing happens on its own serial queue, so the capture callbacks never wait for
/// the disk and the order of the samples is kept.
public final class RecordingChunkWriter: @unchecked Sendable {
    public struct Summary: Sendable, Equatable {
        public var frames: Int
        public var parts: [URL]
        public var failed: Bool
    }

    private let queue = DispatchQueue(label: "app.loctranscriptor.recording.writer")
    private let folder: URL
    private let channels: Int
    private let chunkFrames: Int
    private let syncFrames: Int
    private let onFailure: @Sendable () -> Void

    // Touched only on `queue`.
    private var fd: Int32 = -1
    private var index = 0
    private var framesInChunk = 0
    private var framesSinceSync = 0
    private var totalFrames = 0
    private var parts: [URL] = []
    private var failed = false
    private var closed = false

    /// `onFailure` runs once, on the writer's queue, when the disk refuses a write (full disk).
    public init(folder: URL, channels: Int, chunkSeconds: Int = RecordingFormat.chunkSeconds,
                onFailure: @escaping @Sendable () -> Void = {}) {
        self.folder = folder
        self.channels = channels
        self.chunkFrames = max(1, chunkSeconds * RecordingFormat.sampleRate)
        self.syncFrames = 5 * RecordingFormat.sampleRate
        self.onFailure = onFailure
    }

    public static func partName(_ index: Int) -> String { String(format: "part-%04d.caf", index) }

    /// Never blocks on the disk. Frames with another channel count are dropped.
    public func append(_ samples: [Int16], channels count: Int) {
        guard count == channels, !samples.isEmpty else { return }
        queue.async {
            // Nothing is written after close(): no new chunk appears behind a finished recording.
            guard !self.closed else { return }
            self.write(samples)
        }
    }

    /// Waits for everything queued, syncs and closes the last chunk.
    public func close() async -> Summary {
        await withCheckedContinuation { (cont: CheckedContinuation<Summary, Never>) in
            queue.async {
                self.closed = true
                self.closeChunk()
                cont.resume(returning: Summary(frames: self.totalFrames, parts: self.parts, failed: self.failed))
            }
        }
    }

    private func write(_ samples: [Int16]) {
        guard !failed else { return }
        let totalIn = samples.count / channels
        var done = 0
        while done < totalIn {
            if fd < 0, !openNext() { return }
            let take = min(chunkFrames - framesInChunk, totalIn - done)
            let ok = samples.withUnsafeBytes { raw -> Bool in
                let base = raw.baseAddress! + done * channels * RecordingFormat.bytesPerSample
                do { try PosixIO.writeAll(fd, base, take * channels * RecordingFormat.bytesPerSample) } catch { return false }
                return true
            }
            guard ok else { fail(); return }
            done += take
            framesInChunk += take
            framesSinceSync += take
            totalFrames += take
            if framesInChunk >= chunkFrames {
                closeChunk()
            } else if framesSinceSync >= syncFrames {
                PosixIO.fsyncFully(fd)
                framesSinceSync = 0
            }
        }
    }

    private func openNext() -> Bool {
        index += 1
        let url = folder.appendingPathComponent(Self.partName(index))
        let handle = open(url.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard handle >= 0 else { fail(); return false }
        fchmod(handle, 0o600)
        let head = CAFChunk.header(channels: channels)
        let ok = head.withUnsafeBytes { raw in (try? PosixIO.writeAll(handle, raw.baseAddress!, raw.count)) != nil }
        // The header must be on disk before audio follows it, and a chunk without a whole
        // header is removed at once, so a full disk never leaves a broken file behind.
        guard ok, fsync(handle) == 0 else {
            Darwin.close(handle)
            unlink(url.path)
            fail()
            return false
        }
        fd = handle
        framesInChunk = 0
        framesSinceSync = 0
        parts.append(url)
        return true
    }

    private func closeChunk() {
        guard fd >= 0 else { return }
        PosixIO.fsyncFully(fd)
        Darwin.close(fd)
        fd = -1
    }

    private func fail() {
        guard !failed else { return }
        failed = true
        closeChunk()
        onFailure()
    }
}

// MARK: - Session record and folders

/// `session.json` in a session folder: what crash recovery needs to finish the job.
public struct RecordingMeta: Codable, Sendable, Equatable {
    public var ref: String
    public var startedAt: String
    public var language: String
    public var channels: Int
    public var title: String?
    public var meetingId: Int?

    enum CodingKeys: String, CodingKey {
        case ref, language, channels, title
        case startedAt = "started_at"
        case meetingId = "meeting_id"
    }

    public init(ref: String, startedAt: String, language: String, channels: Int, title: String?, meetingId: Int? = nil) {
        self.ref = ref
        self.startedAt = startedAt
        self.language = language
        self.channels = channels
        self.title = title
        self.meetingId = meetingId
    }

    public var layout: String { channels == 2 ? "mic_system" : "mono" }
}

public enum RecordingStore {
    public static func recordingsDirectory(_ data: URL) -> URL { data.appendingPathComponent("recordings", isDirectory: true) }
    public static func uploadsDirectory(_ data: URL) -> URL { data.appendingPathComponent("uploads", isDirectory: true) }

    public static func randomHex(bytes: Int) throws -> String {
        var buffer = [UInt8](repeating: 0, count: bytes)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes, &buffer) == errSecSuccess else {
            throw RecordingFileError.io(EIO)
        }
        return AccessKey.hex(buffer)
    }

    public static func newSessionId() throws -> String { try randomHex(bytes: 8) }
    public static func newRef() throws -> String { "rec" + (try randomHex(bytes: 12)) + ".wav" }

    public static func isSessionId(_ name: String) -> Bool {
        name.utf8.count == 16 && name.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    public static func isRef(_ name: String) -> Bool {
        guard name.utf8.count == 31, name.hasPrefix("rec"), name.hasSuffix(".wav") else { return false }
        return name.dropFirst(3).dropLast(4).utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }

    static let metaName = "session.json"
    static let finalTempName = "final.tmp"

    public static func writeMeta(_ meta: RecordingMeta, in folder: URL) throws {
        let data = try JSONEncoder().encode(meta)
        let target = folder.appendingPathComponent(metaName)
        // A name of its own per call: two writers never share a temp file.
        let temp = folder.appendingPathComponent(metaName + "." + (try randomHex(bytes: 4)) + ".tmp")
        let fd = open(temp.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw RecordingFileError.io(errno) }
        defer { close(fd) }
        fchmod(fd, 0o600)
        do {
            try data.withUnsafeBytes { try PosixIO.writeAll(fd, $0.baseAddress!, $0.count) }
            PosixIO.fsyncFully(fd)
            guard rename(temp.path, target.path) == 0 else { throw RecordingFileError.io(errno) }
        } catch {
            unlink(temp.path)
            throw error
        }
    }

    public static func readMeta(in folder: URL) -> RecordingMeta? {
        guard let data = try? Data(contentsOf: folder.appendingPathComponent(metaName)),
              data.count < 10_000,
              let meta = try? JSONDecoder().decode(RecordingMeta.self, from: data),
              isRef(meta.ref), (1...2).contains(meta.channels) else { return nil }
        return meta
    }

    /// Chunk files of a folder in order.
    public static func parts(in folder: URL) -> [URL] {
        let names = (try? FileManager.default.contentsOfDirectory(atPath: folder.path)) ?? []
        return names.filter { name in
            name.hasPrefix("part-") && name.hasSuffix(".caf") && name.count >= 9
                && name.dropFirst(5).dropLast(4).allSatisfy(\.isASCII)
                && Int(name.dropFirst(5).dropLast(4)) != nil
        }
        .sorted()
        .map { folder.appendingPathComponent($0) }
    }

    /// Session folders under `recordings/`: real directories with a 16-hex name. Anything
    /// else stays untouched.
    public static func leftoverSessions(data: URL, excluding active: String?) -> [URL] {
        let root = recordingsDirectory(data)
        let names = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        return names.filter { isSessionId($0) && $0 != active }.sorted().compactMap { name in
            let url = root.appendingPathComponent(name, isDirectory: true)
            var st = stat()
            guard lstat(url.path, &st) == 0, (st.st_mode & S_IFMT) == S_IFDIR else { return nil }
            return url
        }
    }

    public static func removeSession(_ folder: URL) {
        // Only ever a folder this app made: the name is checked by the callers.
        guard isSessionId(folder.lastPathComponent) else { return }
        try? FileManager.default.removeItem(at: folder)
    }

    public static func isoString(_ date: Date) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        formatter.timeZone = TimeZone(identifier: "UTC")
        return formatter.string(from: date)
    }

    public static func date(fromISO value: String) -> Date? {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}

// MARK: - Finalize

public enum RecordingFinalizer {
    /// The chunks whose header is whole and has the expected channel count.
    public static func readableChunks(_ parts: [URL], channels: Int) -> [(URL, CAFChunk.Info)] {
        parts.compactMap { part in
            let fd = open(part.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
            guard fd >= 0 else { return nil }
            defer { close(fd) }
            guard let info = try? CAFChunk.info(fd: fd), info.channels == channels else { return nil }
            return (part, info)
        }
    }

    /// Stream-copies the chunks into one WAV at `destination` (mode 600, new file), then
    /// fixes the header. Returns the frame count. A partial last frame of a chunk is dropped.
    public static func concatenate(parts: [URL], channels: Int, to destination: URL) throws -> Int {
        // A crash or a full disk can leave a chunk without a whole header. Every chunk that
        // can be read is kept, in order. Nothing readable at all is `badChunk`.
        let readable = readableChunks(parts, channels: channels)
        guard !readable.isEmpty else { throw RecordingFileError.badChunk }
        let out = open(destination.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard out >= 0 else { throw RecordingFileError.io(errno) }
        defer { close(out) }
        fchmod(out, 0o600)
        let placeholder = WAVFile.header(dataBytes: 0, channels: channels)
        try placeholder.withUnsafeBytes { try PosixIO.writeAll(out, $0.baseAddress!, $0.count) }

        let frameBytes = Int64(channels * RecordingFormat.bytesPerSample)
        var totalBytes: Int64 = 0
        let bufferSize = 1 << 20
        let buffer = UnsafeMutableRawPointer.allocate(byteCount: bufferSize, alignment: 16)
        defer { buffer.deallocate() }

        for (part, info) in readable {
            let fd = open(part.path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
            guard fd >= 0 else { throw RecordingFileError.io(errno) }
            defer { close(fd) }
            var remaining = info.dataLength - info.dataLength % frameBytes
            var offset = info.dataOffset
            while remaining > 0 {
                let want = Int(min(Int64(bufferSize), remaining))
                let got = try PosixIO.readFull(fd, into: buffer, count: want, offset: offset)
                guard got > 0 else { break }
                try PosixIO.writeAll(out, buffer, got)
                offset += Int64(got)
                remaining -= Int64(got)
                totalBytes += Int64(got)
            }
        }
        guard totalBytes <= Int64(UInt32.max) - 36 else { throw RecordingFileError.tooLong }
        let header = WAVFile.header(dataBytes: UInt32(totalBytes), channels: channels)
        let wrote = header.withUnsafeBytes { pwrite(out, $0.baseAddress!, $0.count, 0) }
        guard wrote == header.count else { throw RecordingFileError.io(errno) }
        PosixIO.fsyncFully(out)
        return Int(totalBytes / frameBytes)
    }

    /// Makes `uploads/<meta.ref>` from the chunks of a session folder: temp file inside the
    /// session folder, then a rename into `uploads/` (private folder, mode 600). When the
    /// final file already exists (an earlier run got that far) nothing is written again.
    /// Returns the duration in seconds.
    public static func finalize(folder: URL, meta: RecordingMeta, data: URL) throws -> Double {
        let uploads = RecordingStore.uploadsDirectory(data)
        try PrivateFiles.ensureDirectory(uploads)
        let target = uploads.appendingPathComponent(meta.ref)
        if let existing = WAVFile.summary(of: target) { return existing.durationS }
        let temp = folder.appendingPathComponent(RecordingStore.finalTempName)
        unlink(temp.path)
        let frames: Int
        do {
            frames = try concatenate(parts: RecordingStore.parts(in: folder), channels: meta.channels, to: temp)
        } catch {
            unlink(temp.path)
            throw error
        }
        guard frames > 0 else {
            unlink(temp.path)
            throw RecordingFileError.badChunk
        }
        guard rename(temp.path, target.path) == 0 else {
            let code = errno
            unlink(temp.path)
            throw RecordingFileError.io(code)
        }
        chmod(target.path, 0o600)
        return Double(frames) / Double(RecordingFormat.sampleRate)
    }
}

public enum RecordingDisk {
    /// Below this many free bytes a recording stops.
    public static let minimumFreeBytes: Int64 = 2_000_000_000

    /// Free bytes on the volume of `url`, counting what the system can free on demand.
    public static func availableBytes(at url: URL) -> Int64? {
        let values = try? url.resourceValues(forKeys: [.volumeAvailableCapacityForImportantUsageKey])
        return values?.volumeAvailableCapacityForImportantUsage
    }
}
