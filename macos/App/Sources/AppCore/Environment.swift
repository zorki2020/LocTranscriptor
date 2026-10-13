// Where things live and how the service is started: paths, arguments, a clean
// environment, the access key, the location check.

import Foundation
import Security

public enum AppPaths {
    public static let dataFolderName = "LocTranscriptor"

    /// `TRANSCRIBATOR_DATA_DIR` when it is an absolute path, otherwise
    /// `~/Library/Application Support/LocTranscriptor`.
    public static func dataDirectory(environment: [String: String], home: URL) -> URL {
        if let raw = environment["TRANSCRIBATOR_DATA_DIR"], !raw.isEmpty {
            let expanded = (raw as NSString).expandingTildeInPath
            if expanded.hasPrefix("/") { return URL(fileURLWithPath: expanded, isDirectory: true) }
        }
        return home.appendingPathComponent("Library/Application Support/\(dataFolderName)", isDirectory: true)
    }

    public static func logDirectory(home: URL) -> URL {
        home.appendingPathComponent("Library/Logs/\(dataFolderName)", isDirectory: true)
    }

    public static func logFile(home: URL) -> URL {
        logDirectory(home: home).appendingPathComponent("service.log")
    }

    /// The embedded Python, or `TRANSCRIBATOR_APP_PYTHON` for development.
    public static func python(resources: URL?, environment: [String: String]) -> URL? {
        if let dev = environment["TRANSCRIBATOR_APP_PYTHON"], dev.hasPrefix("/") {
            return URL(fileURLWithPath: dev)
        }
        return resources?.appendingPathComponent("python/bin/python3.12")
    }

    /// `true` when the app runs translocated (Gatekeeper's read-only random path) or
    /// straight from a mounted disk image.
    public static func needsMoveToApplications(bundlePath: String) -> Bool {
        bundlePath.contains("/AppTranslocation/") || bundlePath.hasPrefix("/Volumes/")
    }
}

public enum ServiceCommand {
    public static func arguments(parentPID: Int32) -> [String] {
        ["-I", "-B", "-m", "transcribator", "serve", "--app", "--port", "0", "--parent-pid", String(parentPID)]
    }

    /// A clean environment: nothing is inherited from the app except HOME and TMPDIR.
    /// The languages are first-run defaults only; the user's choice lives in the
    /// service's settings. `developer` (TRANSCRIBATOR_APP_PYTHON set) also passes the
    /// tool paths from the app's environment when the bundle has no helpers.
    public static func environment(
        appEnvironment: [String: String],
        home: URL,
        dataDirectory: URL,
        fluidAudioCLI: URL?,
        audioTool: URL?,
        lang: Lang,
        developer: Bool
    ) -> [String: String] {
        var env: [String: String] = [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": home.path,
            "TMPDIR": appEnvironment["TMPDIR"].flatMap { $0.isEmpty ? nil : $0 } ?? NSTemporaryDirectory(),
            "LANG": "en_US.UTF-8",
            "TRANSCRIBATOR_DATA_DIR": dataDirectory.path,
            "TRANSCRIBATOR_UI_LANG": lang.rawValue,
            "TRANSCRIBATOR_LANGUAGE": lang.rawValue,
        ]
        if let fluidAudioCLI { env["TRANSCRIBATOR_FLUIDAUDIO_CLI"] = fluidAudioCLI.path }
        if let audioTool { env["TRANSCRIBATOR_AUDIO_TOOL"] = audioTool.path }
        if developer {
            for key in ["TRANSCRIBATOR_FLUIDAUDIO_CLI", "TRANSCRIBATOR_AUDIO_TOOL"] where env[key] == nil {
                if let value = appEnvironment[key], value.hasPrefix("/") { env[key] = value }
            }
            // ffmpeg from Homebrew for a source checkout without the audio helper.
            env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"
        }
        return env
    }
}

public enum AccessKey {
    public enum Failure: Error { case random(Int32) }

    /// 32 random bytes as 64 lowercase hex characters, new for every service start.
    public static func generate() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        let status = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        guard status == errSecSuccess else { throw Failure.random(status) }
        return hex(bytes)
    }

    public static func hex(_ bytes: [UInt8]) -> String {
        let digits = Array("0123456789abcdef")
        var out = ""
        out.reserveCapacity(bytes.count * 2)
        for byte in bytes {
            out.append(digits[Int(byte >> 4)])
            out.append(digits[Int(byte & 0x0f)])
        }
        return out
    }

    public static func isWellFormed(_ key: String) -> Bool {
        key.utf8.count == 64 && key.utf8.allSatisfy { (48...57).contains($0) || (97...102).contains($0) }
    }
}

public enum PrivateFiles {
    /// Creates the folder (and missing parents) with mode 0700. An existing folder is
    /// left as it is.
    public static func ensureDirectory(_ url: URL) throws {
        var isDir: ObjCBool = false
        if FileManager.default.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue { return }
        try FileManager.default.createDirectory(
            at: url, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        chmod(url.path, 0o700)
    }

    /// Opens the service log for appending (folder 0700, file 0600). A log over
    /// `maxBytes` is emptied first. Returns the descriptor; the caller closes it.
    public static func openLog(_ url: URL, maxBytes: Int = 1_000_000) throws -> Int32 {
        try ensureDirectory(url.deletingLastPathComponent())
        let path = url.path
        var info = stat()
        if lstat(path, &info) == 0, (info.st_mode & S_IFMT) == S_IFREG, info.st_size > maxBytes {
            truncate(path, 0)
        }
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0o600)
        guard fd >= 0 else { throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO) }
        fchmod(fd, 0o600)
        return fd
    }
}
