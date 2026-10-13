// Starts the local service, waits for its ready line, restarts it after a crash and
// stops it on quit. The access key goes to the service through stdin only.
// Privacy: logs nothing; the service's stderr (codes only) goes to its log file.

import AppCore
import Foundation

enum ServiceState: Equatable {
    case starting
    case running(port: Int, key: String)
    case failed(code: Int32)
    case alreadyRunning
    case configError(code: Int32)
    case stopping
    case stopped

    var origin: LocalOrigin? {
        if case .running(let port, _) = self { return LocalOrigin(port: port) }
        return nil
    }

    var key: String? {
        if case .running(_, let key) = self { return key }
        return nil
    }
}

@MainActor
final class ServiceSupervisor {
    static let readyTimeout: TimeInterval = 30
    static let stopTimeout: TimeInterval = 90

    private(set) var state: ServiceState = .stopped {
        didSet { if state != oldValue { onChange?(state) } }
    }

    var onChange: ((ServiceState) -> Void)?
    /// First-run language defaults for the service (the user's choice lives in its settings).
    var language: () -> Lang = { Lang.system() }

    private var child: ChildProcess?
    private var policy = RestartPolicy()
    private var generation = 0
    private var stopRequested = false
    private let environment = ProcessInfo.processInfo.environment
    private let home = FileManager.default.homeDirectoryForCurrentUser

    var dataDirectory: URL { AppPaths.dataDirectory(environment: environment, home: home) }
    var logFile: URL { AppPaths.logFile(home: home) }

    /// "Try Again" from the error view.
    func retry() {
        policy.reset()
        start()
    }

    func start() {
        generation += 1
        let current = generation
        stopRequested = false
        child = nil
        state = .starting

        do {
            try PrivateFiles.ensureDirectory(dataDirectory)
        } catch {
            state = .configError(code: 2)
            return
        }
        let key: String
        let process: ChildProcess
        do {
            key = try AccessKey.generate()
            process = try spawn(key: key)
        } catch {
            finished(code: 127, generation: current)
            return
        }
        child = process

        Task { [weak self] in
            let first = await process.readFirstLine(timeout: Self.readyTimeout)
            guard let self, self.generation == current else { return }
            if case .line(let line) = first, let port = ReadyLine.parse(line) {
                self.state = .running(port: port, key: key)
            } else {
                // No ready line in time, or garbage: kill it; the exit is handled below.
                process.killGroup()
            }
        }
        Task { [weak self] in
            let code = await process.waitForExit()
            guard let self, self.generation == current else { return }
            self.child = nil
            self.finished(code: code, generation: current)
        }
    }

    private func spawn(key: String) throws -> ChildProcess {
        let resources = Bundle.main.resourceURL
        guard let python = AppPaths.python(resources: resources, environment: environment) else {
            throw CocoaError(.fileNoSuchFile)
        }
        let dataDir = dataDirectory
        let helpers = Bundle.main.bundleURL.appendingPathComponent("Contents/Helpers")
        let fluid = helpers.appendingPathComponent("fluidaudiocli")
        let audio = helpers.appendingPathComponent("transcribator-audio")
        let fm = FileManager.default
        let env = ServiceCommand.environment(
            appEnvironment: environment,
            home: home,
            dataDirectory: dataDir,
            fluidAudioCLI: fm.isExecutableFile(atPath: fluid.path) ? fluid : nil,
            audioTool: fm.isExecutableFile(atPath: audio.path) ? audio : nil,
            lang: language(),
            developer: environment["TRANSCRIBATOR_APP_PYTHON"] != nil)
        // Without a log the service still runs; its stderr then goes to /dev/null.
        let logFD = try? PrivateFiles.openLog(logFile)
        defer { if let logFD { close(logFD) } }
        return try ChildProcess.spawn(
            executable: python.path,
            arguments: ServiceCommand.arguments(parentPID: getpid()),
            environment: env,
            input: Data((key + "\n").utf8),
            stderrFD: logFD)
    }

    private func finished(code: Int32, generation current: Int) {
        if stopRequested {
            state = .stopped
            return
        }
        switch ServiceExit(code: code) {
        case .alreadyRunning:
            state = .alreadyRunning
        case .configError:
            state = .configError(code: code)
        case .normal, .crash:
            guard let delay = policy.nextDelay() else {
                state = .failed(code: code)
                return
            }
            state = .starting
            Task { [weak self] in
                try? await Task.sleep(for: .seconds(delay))
                guard let self, self.generation == current, !self.stopRequested else { return }
                self.start()
            }
        }
    }

    /// SIGTERM, up to 90 s for the service to stop its worker and clean up, then SIGKILL
    /// to the whole process group.
    func stop() async {
        stopRequested = true
        guard let process = child else {
            state = .stopped
            return
        }
        state = .stopping
        process.terminate()
        if await process.waitForExit(timeout: Self.stopTimeout) == nil {
            process.killGroup()
            _ = await process.waitForExit(timeout: 5)
        }
        child = nil
        state = .stopped
    }
}
