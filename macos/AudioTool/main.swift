// transcribator-audio: checks a recording and converts it to mono 16 kHz 16-bit WAV with
// AVFoundation, without ffmpeg.
//
//   transcribator-audio probe <file>                   -> {"duration": <seconds>}
//   transcribator-audio convert <file> <wav>           -> {"ok": true}
//   transcribator-audio split <file> <wav0> <wav1>     -> {"ok": true}
//
// `split` takes a two-channel recording and writes each channel as its own mono 16 kHz
// 16-bit WAV file on the same timeline; anything but two channels is refused with
// "not_two_channels". `convert` of a two-channel file still gives a mono mix.
//
// On failure: exit code 1 and {"error": "<code>"} on stdout, nothing else. The codes match
// pipeline/audio.py. File names, paths and system error texts are never printed: they may
// contain the meeting title.
//
// The container is identified by the file's first bytes, not by its name: copies of dropped
// recordings have no extension. References from a movie to other files or URLs are refused.
// The caller runs this program under sandbox-exec without network (pipeline/audio.py).
//
// Build: scripts/build_app.sh (or swiftc -O -swift-version 6 main.swift).

import AVFoundation
import Foundation

enum AudioFailure: Error {
    case code(String)
}

func emit(_ object: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: object),
       let line = String(data: data, encoding: .utf8) {
        print(line)
    }
}

/// MIME type by magic bytes, or nil for an unknown or unsupported container.
func sniffMIME(_ head: [UInt8]) -> String? {
    func ascii(_ offset: Int, _ text: String) -> Bool {
        let bytes = Array(text.utf8)
        guard head.count >= offset + bytes.count else { return false }
        return Array(head[offset..<offset + bytes.count]) == bytes
    }
    if ascii(4, "ftyp") {
        if ascii(8, "M4A ") || ascii(8, "M4B ") { return "audio/mp4" }
        if ascii(8, "qt  ") { return "video/quicktime" }
        return "video/mp4"
    }
    if ascii(0, "RIFF") && ascii(8, "WAVE") { return "audio/wav" }
    if ascii(0, "FORM") && (ascii(8, "AIFF") || ascii(8, "AIFC")) { return "audio/aiff" }
    if ascii(0, "caff") { return "audio/x-caf" }
    if ascii(0, "fLaC") { return "audio/flac" }
    if ascii(0, "ID3") { return "audio/mpeg" }
    // Older QuickTime movies start with another top-level atom instead of "ftyp".
    for atom in ["moov", "mdat", "wide", "free", "skip", "pnot"] where ascii(4, atom) {
        return "video/quicktime"
    }
    // MP3 without an ID3 tag (or ADTS AAC), possibly after some padding: two frames in a
    // row, so that random bytes that merely look like one header don't count.
    for i in 0..<max(0, head.count - 4) {
        if let mime = frameChain(head, at: i) { return mime }
    }
    return nil
}

/// Length in bytes and type of an MPEG audio Layer III or ADTS frame starting at `i`.
func frame(_ h: [UInt8], at i: Int) -> (length: Int, mime: String)? {
    guard i + 6 <= h.count, h[i] == 0xFF, h[i + 1] & 0xE0 == 0xE0 else { return nil }
    let layer = (h[i + 1] >> 1) & 0x03
    if layer == 0 {  // ADTS: 12-bit sync, length in the header
        guard h[i + 1] & 0xF6 == 0xF0 else { return nil }
        let length = (Int(h[i + 3] & 0x03) << 11) | (Int(h[i + 4]) << 3) | (Int(h[i + 5]) >> 5)
        return length > 7 ? (length, "audio/aac") : nil
    }
    guard layer == 1 else { return nil }  // Layer III only
    let version = (h[i + 1] >> 3) & 0x03  // 3: MPEG-1, 2: MPEG-2, 0: MPEG-2.5
    let bitrateIndex = Int(h[i + 2] >> 4)
    let rateIndex = Int((h[i + 2] >> 2) & 0x03)
    guard version != 1, bitrateIndex != 0, bitrateIndex != 15, rateIndex != 3 else { return nil }
    let v1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
    let v2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
    let rates = version == 3 ? [44100, 48000, 32000] : version == 2 ? [22050, 24000, 16000] : [11025, 12000, 8000]
    let kbps = (version == 3 ? v1 : v2)[bitrateIndex]
    let padding = Int((h[i + 2] >> 1) & 0x01)
    let length = (version == 3 ? 144_000 : 72_000) * kbps / rates[rateIndex] + padding
    return (length, "audio/mpeg")
}

func frameChain(_ h: [UInt8], at i: Int) -> String? {
    guard let first = frame(h, at: i), let second = frame(h, at: i + first.length),
          first.mime == second.mime
    else { return nil }
    return first.mime
}

func openAsset(_ path: String) throws -> AVURLAsset {
    let url = URL(fileURLWithPath: path)
    guard let handle = FileHandle(forReadingAtPath: path) else { throw AudioFailure.code("probe_failed") }
    let head = [UInt8](handle.readData(ofLength: 4096))
    try? handle.close()
    guard let mime = sniffMIME(head) else { throw AudioFailure.code("probe_failed") }
    return AVURLAsset(url: url, options: [
        AVURLAssetOverrideMIMETypeKey: mime,
        AVURLAssetReferenceRestrictionsKey: AVAssetReferenceRestrictions.forbidAll.rawValue,
        AVURLAssetPreferPreciseDurationAndTimingKey: true,
    ])
}

func audioTrack(_ asset: AVURLAsset) async throws -> AVAssetTrack {
    let readable: Bool
    do {
        readable = try await asset.load(.isReadable)
    } catch {
        throw AudioFailure.code("probe_failed")
    }
    guard readable else { throw AudioFailure.code("probe_failed") }
    let tracks = (try? await asset.loadTracks(withMediaType: .audio)) ?? []
    guard let track = tracks.first else { throw AudioFailure.code("no_audio_stream") }
    return track
}

func probe(_ path: String) async throws -> Double {
    let asset = try openAsset(path)
    let track = try await audioTrack(asset)
    let range = (try? await track.load(.timeRange)) ?? .zero
    var seconds = range.duration.seconds
    if !seconds.isFinite || seconds <= 0 {
        seconds = (try? await asset.load(.duration))?.seconds ?? 0
    }
    guard seconds.isFinite, seconds > 0 else { throw AudioFailure.code("empty_audio") }
    return seconds
}

/// Starts decoding the track to 16 kHz 16-bit interleaved PCM with `channels` channels.
func startPCMReader(_ asset: AVURLAsset, _ track: AVAssetTrack, channels: Int,
                    failure: String) throws -> (AVAssetReader, AVAssetReaderTrackOutput) {
    let reader: AVAssetReader
    do {
        reader = try AVAssetReader(asset: asset)
    } catch {
        throw AudioFailure.code(failure)
    }
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVSampleRateKey: 16_000,
        AVNumberOfChannelsKey: channels,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false,
    ]
    let out = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
    out.alwaysCopiesSampleData = false
    guard reader.canAdd(out) else { throw AudioFailure.code(failure) }
    reader.add(out)
    guard reader.startReading() else { throw AudioFailure.code(failure) }
    return (reader, out)
}

/// A new private output file with a 44-byte placeholder for the WAV header.
func createWAV(_ path: String, failure: String) throws -> FileHandle {
    guard FileManager.default.createFile(atPath: path, contents: nil,
                                         attributes: [.posixPermissions: 0o600]),
          let file = FileHandle(forWritingAtPath: path)
    else { throw AudioFailure.code(failure) }
    try file.write(contentsOf: Data(count: 44))  // header placeholder, filled in at the end
    return file
}

/// The PCM bytes of one decoded buffer, or nil for a buffer without data.
func pcmBytes(_ buffer: CMSampleBuffer, failure: String) throws -> Data? {
    guard let block = CMSampleBufferGetDataBuffer(buffer) else { return nil }
    let length = CMBlockBufferGetDataLength(block)
    var chunk = Data(count: length)
    guard length > 0 else { return chunk }
    let status = chunk.withUnsafeMutableBytes { raw in
        CMBlockBufferCopyDataBytes(block, atOffset: 0, dataLength: length,
                                   destination: raw.baseAddress!)
    }
    guard status == kCMBlockBufferNoErr else { throw AudioFailure.code(failure) }
    return chunk
}

func finishWAV(_ file: FileHandle, dataBytes: UInt64) throws {
    try file.seek(toOffset: 0)
    try file.write(contentsOf: wavHeader(dataBytes: UInt32(dataBytes)))
}

/// Decodes the first audio track to 16 kHz mono 16-bit PCM and writes a WAV file.
func convert(_ path: String, to output: String) async throws {
    let asset = try openAsset(path)
    let track = try await audioTrack(asset)
    let (reader, out) = try startPCMReader(asset, track, channels: 1, failure: "normalize_failed")
    let file = try createWAV(output, failure: "normalize_failed")
    defer { try? file.close() }

    var dataBytes: UInt64 = 0
    while let buffer = out.copyNextSampleBuffer() {
        guard let chunk = try pcmBytes(buffer, failure: "normalize_failed") else { continue }
        try file.write(contentsOf: chunk)  // throws on a full disk instead of crashing
        dataBytes += UInt64(chunk.count)
    }
    guard reader.status == .completed, dataBytes > 0, dataBytes < UInt64(UInt32.max) - 36 else {
        throw AudioFailure.code("normalize_failed")
    }
    try finishWAV(file, dataBytes: dataBytes)
}

/// Number of channels of the track's audio format, 0 if unknown.
func channelCount(_ track: AVAssetTrack) async -> Int {
    let formats = (try? await track.load(.formatDescriptions)) ?? []
    for format in formats {
        if let description = CMAudioFormatDescriptionGetStreamBasicDescription(format) {
            return Int(description.pointee.mChannelsPerFrame)
        }
    }
    return 0
}

/// Decodes a two-channel track to 16 kHz 16-bit PCM and writes each channel to its own
/// mono WAV file: channel 0 to `output0`, channel 1 to `output1`.
func split(_ path: String, to output0: String, _ output1: String) async throws {
    let asset = try openAsset(path)
    let track = try await audioTrack(asset)
    guard await channelCount(track) == 2 else { throw AudioFailure.code("not_two_channels") }
    let (reader, out) = try startPCMReader(asset, track, channels: 2, failure: "split_failed")
    let file0 = try createWAV(output0, failure: "split_failed")
    defer { try? file0.close() }
    let file1 = try createWAV(output1, failure: "split_failed")
    defer { try? file1.close() }

    var dataBytes: UInt64 = 0  // per channel
    while let buffer = out.copyNextSampleBuffer() {
        guard let chunk = try pcmBytes(buffer, failure: "split_failed") else { continue }
        // Interleaved frames of 4 bytes: 2 bytes of channel 0, then 2 bytes of channel 1.
        guard chunk.count % 4 == 0 else { throw AudioFailure.code("split_failed") }
        let frames = chunk.count / 4
        var left = Data(count: frames * 2)
        var right = Data(count: frames * 2)
        chunk.withUnsafeBytes { (source: UnsafeRawBufferPointer) in
            left.withUnsafeMutableBytes { (l: UnsafeMutableRawBufferPointer) in
                right.withUnsafeMutableBytes { (r: UnsafeMutableRawBufferPointer) in
                    for i in 0..<frames {
                        l[2 * i] = source[4 * i]
                        l[2 * i + 1] = source[4 * i + 1]
                        r[2 * i] = source[4 * i + 2]
                        r[2 * i + 1] = source[4 * i + 3]
                    }
                }
            }
        }
        try file0.write(contentsOf: left)  // throws on a full disk instead of crashing
        try file1.write(contentsOf: right)
        dataBytes += UInt64(frames * 2)
    }
    guard reader.status == .completed, dataBytes > 0, dataBytes < UInt64(UInt32.max) - 36 else {
        throw AudioFailure.code("split_failed")
    }
    try finishWAV(file0, dataBytes: dataBytes)
    try finishWAV(file1, dataBytes: dataBytes)
}

func wavHeader(dataBytes: UInt32, rate: UInt32 = 16_000, channels: UInt16 = 1,
               bits: UInt16 = 16) -> Data {
    var header = Data()
    func put<T: FixedWidthInteger>(_ value: T) {
        withUnsafeBytes(of: value.littleEndian) { header.append(contentsOf: $0) }
    }
    let blockAlign = channels * bits / 8
    header.append(contentsOf: Array("RIFF".utf8)); put(UInt32(36) + dataBytes)
    header.append(contentsOf: Array("WAVE".utf8))
    header.append(contentsOf: Array("fmt ".utf8)); put(UInt32(16)); put(UInt16(1))
    put(channels); put(rate); put(rate * UInt32(blockAlign)); put(blockAlign); put(bits)
    header.append(contentsOf: Array("data".utf8)); put(dataBytes)
    return header
}

func run() async -> Int32 {
    let args = CommandLine.arguments
    do {
        switch (args.count, args.count > 1 ? args[1] : "") {
        case (3, "probe"):
            emit(["duration": try await probe(args[2])])
        case (4, "convert"):
            do {
                try await convert(args[2], to: args[3])
            } catch {
                // A half-written WAV holds part of the meeting: never leave it behind.
                try? FileManager.default.removeItem(atPath: args[3])
                throw error
            }
            emit(["ok": true])
        case (5, "split"):
            guard args[3] != args[4] else { throw AudioFailure.code("split_failed") }
            do {
                try await split(args[2], to: args[3], args[4])
            } catch {
                // Half-written channel files hold part of the meeting: never leave them.
                for output in [args[3], args[4]] {
                    try? FileManager.default.removeItem(atPath: output)
                }
                throw (error as? AudioFailure) ?? AudioFailure.code("split_failed")
            }
            emit(["ok": true])
        default:
            FileHandle.standardError.write(Data("usage: transcribator-audio probe <file> | convert <file> <wav> | split <file> <wav0> <wav1>\n".utf8))
            return 2
        }
        return 0
    } catch AudioFailure.code(let code) {
        emit(["error": code])
    } catch {
        emit(["error": "normalize_failed"])
    }
    return 1
}

exit(await run())
