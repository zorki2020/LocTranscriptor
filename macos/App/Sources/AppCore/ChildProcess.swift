// A child process started with posix_spawn: a clean environment, a new process group
// (so a stuck service can be killed together with its children), no inherited
// descriptors except stdin, stdout and stderr.

import Darwin
import Foundation

public enum FirstLine: Equatable, Sendable {
    case line(String)
    case endOfFile
    case timeout
}

/// Resumes a continuation once; later calls are ignored.
final class Once<T: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<T, Never>?

    init(_ continuation: CheckedContinuation<T, Never>) { self.continuation = continuation }

    func resume(_ value: T) {
        lock.lock()
        let pending = continuation
        continuation = nil
        lock.unlock()
        pending?.resume(returning: value)
    }
}

public final class ChildProcess: @unchecked Sendable {
    public enum SpawnError: Error { case pipe(Int32), spawn(Int32) }

    public let pid: pid_t
    private let stdoutFD: Int32
    private let exitTask: Task<Int32, Never>
    private let lock = NSLock()
    private var exited = false

    /// Starts `executable`. `input` is written to its stdin, then stdin is closed.
    /// `stderrFD` (or /dev/null) becomes its stderr; the caller keeps ownership of it.
    public static func spawn(
        executable: String,
        arguments: [String],
        environment: [String: String],
        input: Data,
        stderrFD: Int32?,
        workingDirectory: String = "/"
    ) throws -> ChildProcess {
        var stdinPipe: [Int32] = [-1, -1]
        var stdoutPipe: [Int32] = [-1, -1]
        guard pipe(&stdinPipe) == 0 else { throw SpawnError.pipe(errno) }
        guard pipe(&stdoutPipe) == 0 else {
            close(stdinPipe[0]); close(stdinPipe[1])
            throw SpawnError.pipe(errno)
        }
        // The parent's ends must not leak into other children.
        _ = fcntl(stdinPipe[1], F_SETFD, FD_CLOEXEC)
        _ = fcntl(stdoutPipe[0], F_SETFD, FD_CLOEXEC)

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        posix_spawn_file_actions_adddup2(&actions, stdinPipe[0], 0)
        posix_spawn_file_actions_adddup2(&actions, stdoutPipe[1], 1)
        if let stderrFD {
            posix_spawn_file_actions_adddup2(&actions, stderrFD, 2)
        } else {
            posix_spawn_file_actions_addopen(&actions, 2, "/dev/null", O_WRONLY, 0)
        }
        posix_spawn_file_actions_addchdir_np(&actions, workingDirectory)

        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        // New process group; close every descriptor not set up above; default signals.
        posix_spawnattr_setflags(
            &attributes, Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGDEF
                | POSIX_SPAWN_SETSIGMASK))
        posix_spawnattr_setpgroup(&attributes, 0)
        var all = sigset_t()
        sigfillset(&all)
        posix_spawnattr_setsigdefault(&attributes, &all)
        var none = sigset_t()
        sigemptyset(&none)
        posix_spawnattr_setsigmask(&attributes, &none)

        let argv = ([executable] + arguments).map { strdup($0) } + [nil]
        let envp = environment.sorted { $0.key < $1.key }.map { strdup("\($0.key)=\($0.value)") } + [nil]
        defer {
            argv.forEach { free($0) }
            envp.forEach { free($0) }
        }

        var pid: pid_t = 0
        let status = posix_spawn(&pid, executable, &actions, &attributes, argv, envp)
        close(stdinPipe[0])
        close(stdoutPipe[1])
        guard status == 0 else {
            close(stdinPipe[1])
            close(stdoutPipe[0])
            throw SpawnError.spawn(status)
        }

        // A few bytes fit in the pipe buffer: the write doesn't block.
        input.withUnsafeBytes { raw in
            var offset = 0
            while offset < raw.count {
                let written = write(stdinPipe[1], raw.baseAddress! + offset, raw.count - offset)
                if written < 0 {
                    if errno == EINTR { continue }
                    break
                }
                offset += written
            }
        }
        close(stdinPipe[1])
        return ChildProcess(pid: pid, stdoutFD: stdoutPipe[0])
    }

    private init(pid: pid_t, stdoutFD: Int32) {
        self.pid = pid
        self.stdoutFD = stdoutFD
        let waiting = Task<Int32, Never>.detached {
            await withCheckedContinuation { continuation in
                Thread.detachNewThread {
                    var status: Int32 = 0
                    while waitpid(pid, &status, 0) == -1 {
                        if errno != EINTR {
                            continuation.resume(returning: -1)
                            return
                        }
                    }
                    continuation.resume(returning: ChildProcess.exitCode(status))
                }
            }
        }
        exitTask = waiting
        Task.detached { [weak self] in
            _ = await waiting.value
            self?.markExited()
        }
    }

    private func markExited() {
        lock.lock()
        exited = true
        lock.unlock()
    }

    /// True once the process has been reaped: its pid may belong to someone else then.
    public var hasExited: Bool {
        lock.lock()
        defer { lock.unlock() }
        return exited
    }

    /// Exit code, or 128 + signal number when killed by a signal.
    public static func exitCode(_ status: Int32) -> Int32 {
        let signal = status & 0x7f
        return signal == 0 ? (status >> 8) & 0xff : 128 + signal
    }

    public func waitForExit() async -> Int32 { await exitTask.value }

    /// The exit code, or nil if the process is still running after `timeout` seconds.
    public func waitForExit(timeout: TimeInterval) async -> Int32? {
        let task = exitTask
        return await withCheckedContinuation { (continuation: CheckedContinuation<Int32?, Never>) in
            let once = Once(continuation)
            Task.detached { once.resume(await task.value) }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { once.resume(nil) }
        }
    }

    /// Reads the first stdout line (up to 4 KB). The rest of stdout is drained in the
    /// background and discarded, so the child never blocks on a full pipe.
    public func readFirstLine(timeout: TimeInterval) async -> FirstLine {
        let fd = stdoutFD
        return await withCheckedContinuation { (continuation: CheckedContinuation<FirstLine, Never>) in
            let once = Once(continuation)
            Thread.detachNewThread {
                var line = [UInt8]()
                var byte: UInt8 = 0
                var result = FirstLine.endOfFile
                while line.count < 4096 {
                    let n = read(fd, &byte, 1)
                    if n < 0, errno == EINTR { continue }
                    if n <= 0 { break }
                    if byte == UInt8(ascii: "\n") {
                        result = .line(String(decoding: line, as: UTF8.self))
                        break
                    }
                    line.append(byte)
                }
                once.resume(result)
                var buffer = [UInt8](repeating: 0, count: 4096)
                while true {
                    let n = read(fd, &buffer, buffer.count)
                    if n < 0, errno == EINTR { continue }
                    if n <= 0 { break }
                }
                close(fd)
            }
            DispatchQueue.global().asyncAfter(deadline: .now() + timeout) { once.resume(.timeout) }
        }
    }

    /// SIGTERM to the service only: it stops its worker and cleans up by itself.
    public func terminate() {
        guard !hasExited else { return }
        kill(pid, SIGTERM)
    }

    /// SIGKILL to the whole process group (the service and everything it started).
    public func killGroup() {
        guard !hasExited else { return }
        killpg(pid, SIGKILL)
    }
}
