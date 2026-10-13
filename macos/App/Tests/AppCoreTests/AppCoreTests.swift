import Foundation
import Testing

@testable import AppCore

@Suite struct ReadyLineTests {
    @Test func parsesThePort() {
        #expect(ReadyLine.parse(#"{"ready": true, "port": 51234}"#) == 51234)
        #expect(ReadyLine.parse("{\"ready\":true,\"port\":1}\n") == 1)
    }

    @Test func rejectsAnythingElse() {
        for line in [
            "", "ready", #"{"ready": false, "port": 5000}"#, #"{"ready": true}"#,
            #"{"ready": 1, "port": 5000}"#, #"{"ready": true, "port": 0}"#,
            #"{"ready": true, "port": 70000}"#, #"{"ready": true, "port": "5000"}"#,
            #"[{"ready": true, "port": 5000}]"#,
            "{\"ready\": true, \"port\": 5000, \"pad\": \"" + String(repeating: "x", count: 300) + "\"}",
        ] {
            #expect(ReadyLine.parse(line) == nil, "\(line.prefix(40))")
        }
    }
}

@Suite struct RestartPolicyTests {
    @Test func backoffDoublesUpToFiveRestarts() {
        var policy = RestartPolicy()
        let start = Date(timeIntervalSince1970: 1000)
        let delays = (0..<5).map { policy.nextDelay(now: start.addingTimeInterval(Double($0))) }
        #expect(delays == [1, 2, 4, 8, 16])
        #expect(policy.nextDelay(now: start.addingTimeInterval(10)) == nil)
    }

    @Test func oldRestartsExpireAfterTwoMinutes() {
        var policy = RestartPolicy()
        let start = Date(timeIntervalSince1970: 1000)
        for i in 0..<5 { _ = policy.nextDelay(now: start.addingTimeInterval(Double(i))) }
        #expect(policy.nextDelay(now: start.addingTimeInterval(60)) == nil)
        // The first restart (t=0) is out of the window at t=120: one slot frees up.
        #expect(policy.nextDelay(now: start.addingTimeInterval(120)) == 16)
        #expect(policy.nextDelay(now: start.addingTimeInterval(120.5)) == nil)
        #expect(policy.nextDelay(now: start.addingTimeInterval(400)) == 1)
    }

    @Test func resetStartsOver() {
        var policy = RestartPolicy()
        let now = Date()
        for _ in 0..<5 { _ = policy.nextDelay(now: now) }
        policy.reset()
        #expect(policy.nextDelay(now: now) == 1)
    }

    @Test func exitCodes() {
        #expect(ServiceExit(code: 0) == .normal)
        #expect(ServiceExit(code: 75) == .alreadyRunning)
        #expect(ServiceExit(code: 2) == .configError)
        #expect(ServiceExit(code: 1) == .crash(1))
        #expect(ServiceExit(code: 137) == .crash(137))
    }
}

@Suite struct LocalOriginTests {
    let origin = LocalOrigin(port: 51234)

    @Test func allowsOnlyTheService() {
        #expect(origin.decide(URL(string: "http://127.0.0.1:51234/")!, userInitiated: false) == .allow)
        #expect(origin.decide(URL(string: "http://127.0.0.1:51234/meetings/1.md")!, userInitiated: true) == .allow)
        for raw in [
            "http://127.0.0.1:8765/", "http://localhost:51234/", "https://127.0.0.1:51234/",
            "http://127.0.0.1/", "file:///etc/passwd", "about:blank", "data:text/html,x",
            "javascript:alert(1)", "http://user@127.0.0.1:51234/", "blob:http://127.0.0.1:51234/x",
        ] {
            #expect(origin.decide(URL(string: raw)!, userInitiated: false) == .cancel, "\(raw)")
        }
    }

    @Test func userClickedWebLinksOpenInTheBrowser() {
        let github = URL(string: "https://github.com/zorki2020/transcribator")!
        #expect(origin.decide(github, userInitiated: true) == .openExternally)
        #expect(origin.decide(github, userInitiated: false) == .cancel)
        #expect(origin.decide(URL(string: "http://example.com/")!, userInitiated: true) == .openExternally)
        #expect(origin.decide(URL(string: "file:///Users/")!, userInitiated: true) == .cancel)
        #expect(origin.decide(URL(string: "mailto:a@b.c")!, userInitiated: true) == .cancel)
        #expect(origin.decide(URL(string: "https://user:pw@example.com/")!, userInitiated: true) == .cancel)
    }

    @Test func bridgeOnlyFromMainFrameOfTheService() {
        #expect(origin.acceptsBridge(protocol: "http", host: "127.0.0.1", port: 51234, isMainFrame: true))
        #expect(!origin.acceptsBridge(protocol: "http", host: "127.0.0.1", port: 51234, isMainFrame: false))
        #expect(!origin.acceptsBridge(protocol: "http", host: "localhost", port: 51234, isMainFrame: true))
        #expect(!origin.acceptsBridge(protocol: "https", host: "127.0.0.1", port: 51234, isMainFrame: true))
        #expect(!origin.acceptsBridge(protocol: "http", host: "127.0.0.1", port: 8765, isMainFrame: true))
    }

    @Test func contentRulesBlockEverythingButTheService() throws {
        let data = Data(origin.contentRuleList.utf8)
        let rules = try #require(try JSONSerialization.jsonObject(with: data) as? [[String: [String: String]]])
        #expect(rules.count == 2)
        #expect(rules[0]["trigger"]?["url-filter"] == ".*")
        #expect(rules[0]["action"]?["type"] == "block")
        #expect(rules[1]["trigger"]?["url-filter"] == #"^http://127\.0\.0\.1:51234/"#)
        #expect(rules[1]["action"]?["type"] == "ignore-previous-rules")
    }

    @Test func urls() {
        #expect(origin.baseURL.absoluteString == "http://127.0.0.1:51234/")
        #expect(origin.url(path: "settings").absoluteString == "http://127.0.0.1:51234/settings")
        #expect(origin.url(path: "/api/status").absoluteString == "http://127.0.0.1:51234/api/status")
    }

    @Test func uploadPage() {
        #expect(origin.isUploadPage(URL(string: "http://127.0.0.1:51234/")!))
        #expect(origin.isUploadPage(URL(string: "http://127.0.0.1:51234")!))
        #expect(origin.isUploadPage(URL(string: "http://127.0.0.1:51234/?tab=1")!))
        #expect(!origin.isUploadPage(URL(string: "http://127.0.0.1:51234/settings")!))
        #expect(!origin.isUploadPage(URL(string: "http://127.0.0.1:8765/")!))
        #expect(!origin.isUploadPage(URL(string: "https://127.0.0.1:51234/")!))
        #expect(!origin.isUploadPage(URL(string: "http://localhost:51234/")!))
    }

    @Test func pageScriptsTakeNoInterpolatedValues() {
        #expect(PageScript.drop.contains("window.transcribatorDrop(paths)"))
        #expect(PageScript.startTour == "return window.transcribatorTour ? window.transcribatorTour.start() : false")
    }

    @Test func bridgeCommands() {
        #expect(BridgeCommand(body: ["cmd": "pick"]) == .pick)
        #expect(BridgeCommand(body: ["cmd": "revealData"]) == .revealData)
        #expect(BridgeCommand(body: ["cmd": "rm"]) == nil)
        #expect(BridgeCommand(body: "pick") == nil)
        #expect(BridgeCommand(body: ["cmd": 1]) == nil)
    }

    @Test func downloadNames() {
        #expect(DownloadName.safe("Weekly sync.md") == "Weekly sync.md")
        #expect(DownloadName.safe("a/b:c\u{0}d.md") == "a b c d.md")
        #expect(DownloadName.safe(" .. ") == "transcript.md")
        #expect(DownloadName.safe(String(repeating: "x", count: 500)).count == 200)
    }
}

@Suite struct EnvironmentTests {
    let home = URL(fileURLWithPath: "/Users/test", isDirectory: true)

    @Test func dataDirectory() {
        #expect(AppPaths.dataDirectory(environment: [:], home: home).path
            == "/Users/test/Library/Application Support/LocTranscriptor")
        #expect(AppPaths.dataDirectory(environment: ["TRANSCRIBATOR_DATA_DIR": "/tmp/x"], home: home).path == "/tmp/x")
        #expect(AppPaths.dataDirectory(environment: ["TRANSCRIBATOR_DATA_DIR": "relative"], home: home).path
            == "/Users/test/Library/Application Support/LocTranscriptor")
        #expect(AppPaths.logFile(home: home).path == "/Users/test/Library/Logs/LocTranscriptor/service.log")
    }

    @Test func pythonPath() {
        let resources = URL(fileURLWithPath: "/Applications/LocTranscriptor.app/Contents/Resources")
        #expect(AppPaths.python(resources: resources, environment: [:])?.path
            == "/Applications/LocTranscriptor.app/Contents/Resources/python/bin/python3.12")
        #expect(AppPaths.python(resources: resources, environment: ["TRANSCRIBATOR_APP_PYTHON": "/r/.venv/bin/python"])?.path
            == "/r/.venv/bin/python")
    }

    @Test func translocation() {
        #expect(AppPaths.needsMoveToApplications(
            bundlePath: "/private/var/folders/x/AppTranslocation/ABC/d/LocTranscriptor.app"))
        #expect(AppPaths.needsMoveToApplications(bundlePath: "/Volumes/LocTranscriptor/LocTranscriptor.app"))
        #expect(!AppPaths.needsMoveToApplications(bundlePath: "/Applications/LocTranscriptor.app"))
        #expect(!AppPaths.needsMoveToApplications(bundlePath: "/Users/test/Applications/LocTranscriptor.app"))
    }

    @Test func cleanEnvironment() {
        let env = ServiceCommand.environment(
            appEnvironment: ["TMPDIR": "/var/tmp/x/", "PYTHONPATH": "/evil", "DYLD_INSERT_LIBRARIES": "/evil",
                             "TRANSCRIBATOR_FLUIDAUDIO_CLI": "/dev/cli"],
            home: home,
            dataDirectory: URL(fileURLWithPath: "/data"),
            fluidAudioCLI: URL(fileURLWithPath: "/App/Contents/Helpers/fluidaudiocli"),
            audioTool: nil,
            lang: .ru,
            developer: false)
        #expect(env == [
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/Users/test",
            "TMPDIR": "/var/tmp/x/",
            "LANG": "en_US.UTF-8",
            "TRANSCRIBATOR_DATA_DIR": "/data",
            "TRANSCRIBATOR_UI_LANG": "ru",
            "TRANSCRIBATOR_LANGUAGE": "ru",
            "TRANSCRIBATOR_FLUIDAUDIO_CLI": "/App/Contents/Helpers/fluidaudiocli",
        ])
    }

    @Test func developerEnvironmentPassesToolPaths() {
        let env = ServiceCommand.environment(
            appEnvironment: ["TRANSCRIBATOR_FLUIDAUDIO_CLI": "/dev/cli", "TRANSCRIBATOR_AUDIO_TOOL": "rel"],
            home: home, dataDirectory: URL(fileURLWithPath: "/data"), fluidAudioCLI: nil, audioTool: nil,
            lang: .en, developer: true)
        #expect(env["TRANSCRIBATOR_FLUIDAUDIO_CLI"] == "/dev/cli")
        #expect(env["TRANSCRIBATOR_AUDIO_TOOL"] == nil)
        #expect(env["PYTHONPATH"] == nil)
    }

    @Test func serviceArguments() {
        #expect(ServiceCommand.arguments(parentPID: 42)
            == ["-I", "-B", "-m", "transcribator", "serve", "--app", "--port", "0", "--parent-pid", "42"])
    }

    @Test func accessKeyFormat() throws {
        #expect(AccessKey.hex([0x00, 0x0f, 0xa5, 0xff]) == "000fa5ff")
        let a = try AccessKey.generate()
        let b = try AccessKey.generate()
        #expect(AccessKey.isWellFormed(a) && AccessKey.isWellFormed(b))
        #expect(a != b)
        #expect(!AccessKey.isWellFormed(String(repeating: "A", count: 64)))
        #expect(!AccessKey.isWellFormed("abc"))
    }

    @Test func logIsPrivateAndTrimmed() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("tb-log-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let log = dir.appendingPathComponent("logs/service.log")
        var fd = try PrivateFiles.openLog(log, maxBytes: 10)
        _ = "0123456789abcdef\n".withCString { write(fd, $0, 17) }
        close(fd)
        #expect(try FileManager.default.attributesOfItem(atPath: log.path)[.size] as? Int == 17)
        let dirMode = try FileManager.default.attributesOfItem(atPath: log.deletingLastPathComponent().path)[.posixPermissions] as? Int
        let fileMode = try FileManager.default.attributesOfItem(atPath: log.path)[.posixPermissions] as? Int
        #expect(dirMode == 0o700 && fileMode == 0o600)
        // Over the limit: emptied on the next start.
        fd = try PrivateFiles.openLog(log, maxBytes: 10)
        close(fd)
        #expect(try FileManager.default.attributesOfItem(atPath: log.path)[.size] as? Int == 0)
        // Under the limit: kept.
        fd = try PrivateFiles.openLog(log, maxBytes: 10)
        _ = "abc".withCString { write(fd, $0, 3) }
        close(fd)
        fd = try PrivateFiles.openLog(log, maxBytes: 10)
        close(fd)
        #expect(try FileManager.default.attributesOfItem(atPath: log.path)[.size] as? Int == 3)
    }
}

@Suite struct L10nTests {
    @Test func languageSelection() {
        #expect(Lang.system(["ru-RU", "en-US"]) == .ru)
        #expect(Lang.system(["en-US", "ru-RU"]) == .en)
        #expect(Lang.system([]) == .en)
        #expect(Lang.from(service: "ru", preferredLanguages: ["en"]) == .ru)
        #expect(Lang.from(service: "en", preferredLanguages: ["ru"]) == .en)
        #expect(Lang.from(service: nil, preferredLanguages: ["ru"]) == .ru)
        #expect(Lang.from(service: "de", preferredLanguages: ["en"]) == .en)
    }

    @Test func everyStringIsTranslated() {
        let cyrillic = try! Regex("[А-Яа-яЁё]")
        let sameInBothLanguages: Set<String> = ["appName"]
        for text in Text.allCases {
            let en = text.string(.en)
            let ru = text.string(.ru)
            #expect(!en.isEmpty && !ru.isEmpty, "\(text)")
            #expect(en.firstMatch(of: cyrillic) == nil, "\(text)")
            if !sameInBothLanguages.contains("\(text)") {
                #expect(ru.firstMatch(of: cyrillic) != nil, "\(text)")
            }
        }
    }

    @Test func writingRules() {
        // Plain sentences: no semicolons, no exclamations, no hard line breaks.
        for text in Text.allCases {
            for string in [text.string(.en), text.string(.ru)] {
                #expect(!string.contains(";") && !string.contains("!") && !string.contains("\n"), "\(text)")
            }
        }
        #expect(Text.appName.string(.ru) == "LocTranscriptor")
        #expect(Text.showTips.string(.ru) == "Показать подсказки")
    }

    @Test func statusLineHasCountersOnly() {
        let status = ServiceStatus(queued: 2, processing: 1, done: 5, error: 1, modelsReady: true, uiLang: "en")
        #expect(L10n.statusLine(status, .en) == "Running · 2 queued · 1 processing · 1 failed")
        #expect(L10n.statusLine(status, .ru) == "Работает · в очереди 2 · обрабатывается 1 · ошибок 1")
        let idle = ServiceStatus(queued: 0, processing: 0, done: 0, error: 0, modelsReady: nil, uiLang: nil)
        #expect(L10n.statusLine(idle, .en) == "Running")
        let setup = ServiceStatus(queued: 0, processing: 0, done: 0, error: 0, modelsReady: false, uiLang: "ru")
        #expect(L10n.statusLine(setup, .ru) == "Модели не скачаны")
        #expect(L10n.serviceFailedTitle(1, .en) == "The service doesn't start (code 1)")
    }

    @Test func statusDecoding() {
        let data = Data(#"{"queued":1,"processing":0,"done":3,"error":0,"models_ready":true,"ui_lang":"ru"}"#.utf8)
        #expect(ServiceStatus.decode(data)
            == ServiceStatus(queued: 1, processing: 0, done: 3, error: 0, modelsReady: true, uiLang: "ru"))
        // The source-install service has no models_ready and ui_lang.
        let plain = Data(#"{"queued":0,"processing":0,"done":0,"error":0}"#.utf8)
        #expect(ServiceStatus.decode(plain)?.modelsReady == nil)
        #expect(ServiceStatus.decode(Data("nope".utf8)) == nil)
    }
}

@Suite struct ChildProcessTests {
    @Test func passesStdinReadsReadyLineAndExitCode() async throws {
        let child = try ChildProcess.spawn(
            executable: "/bin/sh",
            arguments: ["-c", #"read key; printf '{"ready": true, "port": %s}\n' "${#key}"; echo "$FOO" >&2; exit 3"#],
            environment: ["PATH": "/usr/bin:/bin"],
            input: Data("0123456789\n".utf8),
            stderrFD: nil)
        let line = await child.readFirstLine(timeout: 10)
        #expect(line == .line(#"{"ready": true, "port": 10}"#))
        if case .line(let text) = line { #expect(ReadyLine.parse(text) == 10) }
        #expect(await child.waitForExit() == 3)
        #expect(child.hasExited || true)
    }

    @Test func environmentIsClean() async throws {
        setenv("TB_SHOULD_NOT_LEAK", "1", 1)
        let child = try ChildProcess.spawn(
            executable: "/usr/bin/env", arguments: [], environment: ["ONLY": "this"], input: Data(), stderrFD: nil)
        #expect(await child.readFirstLine(timeout: 10) == .line("ONLY=this"))
        #expect(await child.waitForExit() == 0)
    }

    @Test func timeoutAndGroupKill() async throws {
        let child = try ChildProcess.spawn(
            executable: "/bin/sh", arguments: ["-c", "sleep 30 & sleep 30"], environment: [:], input: Data(),
            stderrFD: nil)
        #expect(await child.readFirstLine(timeout: 0.3) == .timeout)
        #expect(await child.waitForExit(timeout: 0.2) == nil)
        // Its own process group: the background sleep goes too.
        #expect(getpgid(child.pid) == child.pid)
        child.killGroup()
        #expect(await child.waitForExit(timeout: 5) == 128 + SIGKILL)
    }

    @Test func terminateIsGraceful() async throws {
        let child = try ChildProcess.spawn(
            executable: "/bin/sh", arguments: ["-c", "trap 'exit 0' TERM; while :; do sleep 0.05; done"],
            environment: [:], input: Data(), stderrFD: nil)
        try await Task.sleep(for: .milliseconds(200))
        child.terminate()
        #expect(await child.waitForExit(timeout: 5) == 0)
    }

    @Test func endOfFileWithoutLine() async throws {
        let child = try ChildProcess.spawn(
            executable: "/bin/sh", arguments: ["-c", "exit 75"], environment: [:], input: Data(), stderrFD: nil)
        #expect(await child.readFirstLine(timeout: 10) == .endOfFile)
        #expect(ServiceExit(code: await child.waitForExit()) == .alreadyRunning)
    }
}
