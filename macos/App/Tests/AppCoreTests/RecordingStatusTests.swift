import Foundation
import Testing

@testable import AppCore

@Suite struct RecordingStatusTests {
    @Test func idleStatusShape() throws {
        let object = RecordingStatus.idle.jsonObject
        let data = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        let text = String(decoding: data, as: UTF8.self)
        #expect(text == #"{"audioSaved":null,"elapsedS":0,"error":null,"keepAudio":false,"level":{"mic":0,"system":0},"meetingId":null,"mic":false,"source":{"kind":"all"},"state":"idle","warning":null}"#)
    }

    @Test func recordingStatusShape() throws {
        var status = RecordingStatus()
        status.state = .recording
        status.elapsedS = 12.5
        status.mic = true
        status.source = RecordingSourceInfo(kind: .app, bundleIds: ["a.b"], name: "Zoom")
        status.levelMic = 0.5
        status.levelSystem = 0.25
        status.warning = .silentMic
        status.error = .diskLow
        status.meetingId = 4
        status.keepAudio = true
        status.audioSaved = false
        let json = try #require(
            try JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject: status.jsonObject))
                as? [String: Any])
        #expect(json["state"] as? String == "recording")
        #expect(json["elapsedS"] as? Double == 12.5)
        #expect(json["mic"] as? Bool == true)
        #expect((json["source"] as? [String: Any])?["name"] as? String == "Zoom")
        #expect((json["source"] as? [String: Any])?["kind"] as? String == "app")
        #expect((json["level"] as? [String: Double]) == ["mic": 0.5, "system": 0.25])
        #expect(json["warning"] as? String == "silent_mic")
        #expect(json["error"] as? String == "disk_low")
        #expect(json["meetingId"] as? Int == 4)
        #expect(json["keepAudio"] as? Bool == true)
        #expect(json["audioSaved"] as? Bool == false)
    }

    @Test func codesMatchTheContract() {
        #expect(Set(RecordingError.allCases.map(\.rawValue)) == [
            "already_recording", "mic_denied", "no_source", "source_gone", "start_failed", "disk_low",
            "finalize_failed", "enqueue_failed", "max_duration",
        ])
        #expect(Set(RecordingWarning.allCases.map(\.rawValue)) == [
            "silent_system", "silent_mic", "device_changed", "disk_low",
        ])
        #expect(RecordingError(.sourceGone) == .sourceGone)
        #expect(RecordingWarning(.deviceChanged) == .deviceChanged)
    }

    @Test func startCommandIsValidated() {
        let ok: [String: Any] = [
            "type": "recording.start",
            "source": ["kind": "app", "bundleIds": ["us.zoom.xos"], "name": "Zoom"],
            "mic": ["enabled": true, "id": NSNull()],
            "language": "ru", "title": "  Weekly  ",
        ]
        guard case .start(.success(let request))? = RecordingCommand(body: ok) else {
            Issue.record("start should parse")
            return
        }
        #expect(request.source == RecordingSourceInfo(kind: .app, bundleIds: ["us.zoom.xos"], name: "Zoom"))
        #expect(request.micEnabled && request.micId == nil)
        #expect(request.language == "ru")
        #expect(request.title == "Weekly")
        #expect(request.channels == 2)

        func start(_ change: (inout [String: Any]) -> Void) -> RecordingCommand? {
            var copy = ok
            change(&copy)
            return RecordingCommand(body: copy)
        }
        #expect(start { $0["source"] = ["kind": "app", "bundleIds": [String]()] } == .start(.failure(.noSource)))
        #expect(start { $0["source"] = ["kind": "tab"] } == .start(.failure(.noSource)))
        #expect(start { $0["language"] = "de" } == .start(.failure(.startFailed)))
        #expect(start { $0["mic"] = ["enabled": "yes"] } == .start(.failure(.startFailed)))
        let allSources = start { $0["source"] = ["kind": "all"]; $0["title"] = NSNull() }
        if case .start(.success(let all))? = allSources {
            #expect(all.source == .all && all.title == nil)
        } else {
            Issue.record("all should parse")
        }
    }

    @Test func keepAudioIsOptionalAndOffByDefault() {
        let base: [String: Any] = [
            "type": "recording.start", "source": ["kind": "all"], "mic": ["enabled": false, "id": NSNull()],
            "language": "en", "title": NSNull(),
        ]
        func parsed(_ extra: [String: Any]) -> RecordingStartRequest? {
            if case .start(.success(let request))? = RecordingCommand(body: base.merging(extra) { $1 }) { return request }
            return nil
        }
        #expect(parsed([:])?.keepAudio == false)
        #expect(parsed(["keepAudio": true])?.keepAudio == true)
        #expect(parsed(["keepAudio": false])?.keepAudio == false)
        // Anything that is not a boolean counts as off.
        #expect(parsed(["keepAudio": "yes"])?.keepAudio == false)
        #expect(parsed(["keepAudio": 1])?.keepAudio == false)
    }

    @Test func keepAudioIsRememberedWithTheLastSettings() throws {
        let suite = "recording-test-" + UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        #expect(RecordingLastSettings.standard.keepAudio == false)
        #expect(RecordingLastSettings.standard.jsonObject["keepAudio"] as? Bool == false)
        let request = RecordingStartRequest(
            source: .all, micEnabled: true, micId: nil, language: "ru", title: nil, keepAudio: true)
        RecordingLastSettings(request).save(to: defaults)
        let loaded = try #require(RecordingLastSettings.load(from: defaults))
        #expect(loaded.keepAudio == true)
        #expect(loaded.jsonObject["keepAudio"] as? Bool == true)
        #expect(loaded.request(language: "en").keepAudio == true)
        // Settings saved before the switch existed load with it off.
        let old = #"{"sourceKind":"all","bundleIds":[],"micEnabled":true}"#
        defaults.set(Data(old.utf8), forKey: RecordingLastSettings.defaultsKey)
        #expect(RecordingLastSettings.load(from: defaults)?.keepAudio == false)
    }

    @Test func enqueueBodyCarriesTheAudioRefOnlyWhenThereIsOne() throws {
        var payload = RecordingEnqueuePayload(
            ref: "rec" + String(repeating: "0", count: 24) + ".wav", title: nil, startedAt: Date(timeIntervalSince1970: 0),
            language: "ru", layout: "mono", durationS: 1)
        func json(_ payload: RecordingEnqueuePayload) throws -> [String: Any] {
            let data = try #require(RecordingRequests.body(payload))
            return try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        }
        let plain = try json(payload)
        #expect(plain["audio_ref"] == nil)
        payload.audioRef = "rec" + String(repeating: "0", count: 24) + ".m4a"
        let withAudio = try json(payload)
        #expect(withAudio["audio_ref"] as? String == payload.audioRef)
    }

    @Test func otherCommands() {
        #expect(RecordingCommand(body: ["type": "recording.sources"]) == .sources)
        #expect(RecordingCommand(body: ["type": "recording.stop"]) == .stop)
        #expect(RecordingCommand(body: ["type": "recording.status"]) == .status)
        #expect(RecordingCommand(body: ["type": "recording.openSettings", "pane": "mic"]) == .openSettings(.mic))
        #expect(RecordingCommand(body: ["type": "recording.openSettings", "pane": "systemAudio"])
            == .openSettings(.systemAudio))
        #expect(RecordingCommand(body: ["type": "recording.openSettings", "pane": "../x"]) == nil)
        #expect(RecordingCommand(body: ["type": "recording.rm"]) == nil)
        #expect(RecordingCommand(body: ["type": "other"]) == nil)
        #expect(RecordingCommand(body: ["cmd": "pick"]) == nil)
        #expect(RecordingCommand(body: "recording.stop") == nil)
        // The existing commands are not touched.
        #expect(BridgeCommand(body: ["cmd": "pick"]) == .pick)
    }

    @Test func settingsPanes() {
        #expect(RecordingSettingsPane.mic.url.absoluteString
            == "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
        #expect(RecordingSettingsPane.systemAudio.url.absoluteString
            == "x-apple.systempreferences:com.apple.preference.security?Privacy_AudioCapture")
    }

    @Test func lastSettingsPersistAndFallBack() throws {
        let suite = "recording-test-" + UUID().uuidString
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        #expect(RecordingLastSettings.load(from: defaults) == nil)
        let request = RecordingStartRequest(
            source: RecordingSourceInfo(kind: .app, bundleIds: ["a", "b"], name: "Meet"), micEnabled: false,
            micId: "uid-1", language: "en", title: "private title")
        RecordingLastSettings(request).save(to: defaults)
        let loaded = try #require(RecordingLastSettings.load(from: defaults))
        #expect(loaded.request(language: "ru").source == request.source)
        #expect(loaded.micEnabled == false && loaded.micId == "uid-1")
        // The title is not part of what is remembered.
        #expect(loaded.request(language: "ru").title == nil)
        let json = loaded.jsonObject
        #expect((json["source"] as? [String: Any])?["bundleIds"] as? [String] == ["a", "b"])
        #expect(json["micId"] as? String == "uid-1")
        // Defaults: all system audio, microphone on, no id.
        #expect(RecordingLastSettings.standard.request(language: "en")
            == RecordingStartRequest(source: .all, micEnabled: true, micId: nil, language: "en", title: nil))
        #expect(RecordingLastSettings.standard.jsonObject["micId"] is NSNull)
    }

    @Test func sourcesReplyShape() throws {
        let reply = RecordingSourcesReply.make(
            apps: [AudioAppInfo(bundleIds: ["a", "a.helper"], name: "A", outputting: true)],
            microphones: [MicrophoneInfo(id: "m1", name: "Built-in", isDefault: true)],
            last: .standard, permission: .undetermined)
        let json = try #require(
            try JSONSerialization.jsonObject(with: JSONSerialization.data(withJSONObject: reply)) as? [String: Any])
        #expect(Set(json.keys) == ["apps", "microphones", "last", "micPermission"])
        let app = try #require((json["apps"] as? [[String: Any]])?.first)
        #expect(Set(app.keys) == ["bundleIds", "name", "outputting"])
        let mic = try #require((json["microphones"] as? [[String: Any]])?.first)
        #expect(Set(mic.keys) == ["id", "name", "isDefault"])
        #expect(json["micPermission"] as? String == "undetermined")
        #expect(Set((json["last"] as? [String: Any])?.keys.map { $0 } ?? []) == ["source", "micEnabled", "micId", "keepAudio"])
    }

    @Test func statusScriptTakesNoInterpolatedValues() {
        #expect(PageScript.recordingStatus.contains("onStatus(status)"))
        #expect(!PageScript.recordingStatus.contains("\\("))
    }

    @Test func oneShotReleasesTheWaiterOnce() async {
        let gate = OneShot()
        gate.fire()
        gate.fire()
        await gate.wait()
        let later = OneShot()
        Task {
            try? await Task.sleep(for: .milliseconds(20))
            later.fire()
        }
        await later.wait()
    }
}

@Suite struct RecordingStringsTests {
    @Test func everyRecordingStringFollowsTheWritingRules() {
        var strings: [String] = []
        for lang in Lang.allCases {
            for error in RecordingError.allCases { strings.append(L10n.recordingError(error, lang)) }
            strings.append(L10n.clock(3725))
        }
        for text in [Text.recordingStart, .recordingStop, .recordingStarting, .recordingSaved, .recordingRecovered,
                     .recordingSaving, .openSystemSettings] {
            strings.append(text.string(.en))
            strings.append(text.string(.ru))
        }
        for string in strings {
            #expect(!string.isEmpty)
            #expect(!string.contains(";") && !string.contains("!") && !string.contains("\n"), "\(string)")
            #expect(!string.contains("—") && !string.contains(" – "), "\(string)")
        }
        for error in RecordingError.allCases {
            let ru = L10n.recordingError(error, .ru)
            #expect(!ru.lowercased().hasPrefix("мы ") && !ru.contains(" мы "), "\(error)")
        }
    }

    @Test func menuLine() {
        var status = RecordingStatus()
        #expect(L10n.recordingLine(status, .en) == nil)
        status.state = .recording
        status.elapsedS = 3725.9
        #expect(L10n.recordingLine(status, .en) == "Recording · 01:02:05")
        #expect(L10n.recordingLine(status, .ru) == "Идёт запись · 01:02:05")
        status.state = .queued
        status.error = .sourceGone
        #expect(L10n.recordingLine(status, .en) == "The app you were recording closed. What was recorded is saved.")
        status.error = .maxDuration
        #expect(L10n.recordingLine(status, .ru)?.contains("12 часов") == true)
        status.error = nil
        status.warning = .diskLow
        #expect(L10n.recordingLine(status, .en)?.hasPrefix("The disk is almost full") == true)
        status.warning = nil
        #expect(L10n.recordingLine(status, .en) == "Recording added to the queue")
        status.state = .failed
        status.error = .enqueueFailed
        #expect(L10n.recordingLine(status, .en)?.hasPrefix("The recording is saved") == true)
        status.state = .queued
        status.error = nil
        #expect(L10n.recordingLine(status, .ru) == "Запись добавлена в очередь")
    }
}
