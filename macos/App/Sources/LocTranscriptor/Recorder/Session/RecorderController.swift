// The app side of recording: owns the session with the real capture engine, remembers the
// last settings and answers the page's questions. Capture itself lives in Recorder/Capture.
// Privacy: no names, titles or paths are logged.

import AppCore
import AppKit

@MainActor
final class RecorderController {
    private let session: RecordingSession
    private let catalog: any SourceCatalog
    private let defaults: UserDefaults
    private let lang: @MainActor () -> Lang

    /// Every status change, for the page and the menu bar.
    var onStatus: ((RecordingStatus) -> Void)?

    var status: RecordingStatus { session.status }

    init(
        dataDirectory: URL,
        connection: @escaping RecordingEnqueueClient.Connection,
        lang: @escaping @MainActor () -> Lang,
        defaults: UserDefaults = .standard
    ) {
        let catalog = CoreAudioSourceCatalog()
        self.catalog = catalog
        self.defaults = defaults
        self.lang = lang
        session = RecordingSession(
            engine: CoreAudioCaptureEngine(), catalog: catalog,
            enqueuer: RecordingEnqueueClient(connection: connection),
            dataDirectory: dataDirectory, lang: lang)
        session.onStatus = { [weak self] status in self?.onStatus?(status) }
    }

    /// The reply of `recording.sources`. The catalog asks Core Audio, so it runs off the
    /// main thread.
    func sources() async -> [String: Any] {
        let catalog = self.catalog
        let (apps, microphones, permission) = await Task.detached {
            (catalog.apps(), catalog.microphones(), catalog.micPermission())
        }.value
        return RecordingSourcesReply.make(
            apps: apps, microphones: microphones, last: RecordingLastSettings.load(from: defaults) ?? .standard,
            permission: permission)
    }

    func start(_ request: RecordingStartRequest) async -> RecordingError? {
        let error = await session.start(request)
        if error == nil { RecordingLastSettings(request).save(to: defaults) }
        return error
    }

    /// Menu bar: the saved settings, or all system audio with the microphone on.
    func startFromMenu() async -> RecordingError? {
        let saved = RecordingLastSettings.load(from: defaults) ?? .standard
        return await start(saved.request(language: lang().rawValue))
    }

    func stop() async { await session.stop() }

    /// The service is up: finish recordings an earlier run left behind.
    func serviceBecameAvailable() {
        Task { await session.recoverLeftovers() }
    }

    func openSettings(_ pane: RecordingSettingsPane) {
        if !NSWorkspace.shared.open(pane.url) { NSWorkspace.shared.open(RecordingSettingsPane.fallbackURL) }
    }
}
