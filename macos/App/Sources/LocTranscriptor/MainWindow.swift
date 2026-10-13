// The main window: native "Starting…" and error views, and the web view showing the
// service. The web view loads only http://127.0.0.1:<port>, keeps nothing on disk and
// talks to the app through the "transcribator" message handler (file dialog, Finder).
// Privacy: no paths, titles or text are logged.

import AppCore
import AppKit
import UniformTypeIdentifiers
import WebKit

/// A web view that takes files dropped from Finder by path: the page adds them with
/// POST /enqueue and nothing is copied. Drops without a path (attachments from other
/// apps) go to WebKit and the page uploads a copy, as in a browser.
final class AppWebView: WKWebView {
    var origin: LocalOrigin?

    override func performDragOperation(_ sender: any NSDraggingInfo) -> Bool {
        let urls = sender.draggingPasteboard.readObjects(
            forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
        let paths = urls.filter(\.isFileURL).map(\.path)
        guard !paths.isEmpty else { return super.performDragOperation(sender) }
        // Paths go only to our own page (the navigation policy keeps it there anyway).
        guard let origin, let current = url, origin.isLocal(current) else { return false }
        // Let WebKit end its drag session (the page gets dragleave), then hand over paths.
        super.draggingExited(sender)
        // Paths go as an argument, never pasted into the script text.
        callAsyncJavaScript(PageScript.drop, arguments: ["paths": paths], in: nil, in: .page, completionHandler: nil)
        return true
    }
}

/// Holds the window controller weakly: WKUserContentController retains its handlers.
final class BridgeProxy: NSObject, WKScriptMessageHandlerWithReply {
    weak var owner: MainWindowController?

    init(owner: MainWindowController) { self.owner = owner }

    @MainActor
    func userContentController(
        _ userContentController: WKUserContentController, didReceive message: WKScriptMessage,
        replyHandler: @escaping @MainActor @Sendable (Any?, String?) -> Void
    ) {
        guard let owner else {
            replyHandler(nil, "unavailable")
            return
        }
        owner.handleBridge(message, reply: replyHandler)
    }
}

@MainActor
final class MainWindowController: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate,
    WKDownloadDelegate
{
    let window: NSWindow
    var lang: Lang = .system() {
        didSet { if lang != oldValue { refreshNativeView() } }
    }

    var onRetry: (() -> Void)?
    var onShowLog: (() -> Void)?
    var onRevealData: (() -> Void)?
    var onPageLoaded: (() -> Void)?
    var onClose: (() -> Void)?
    private var closeAfterFullScreen = false

    private var webView: AppWebView?
    private var origin: LocalOrigin?
    private var titleObservation: NSKeyValueObservation?
    private var nativeView: NativeView = .loading
    private var loadGeneration = 0
    /// Help > Show Tips waits for the upload page to finish loading.
    private var tourPending = false

    private static let minimumContentSize = NSSize(width: 720, height: 520)

    private enum NativeView: Equatable {
        case loading
        case failed(Int32)
        case alreadyRunning
        case configError(Int32)
        case none
    }

    override init() {
        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1100, height: 760),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        super.init()
        window.isReleasedWhenClosed = false
        // Resizable both ways down to a width where the page layout still works.
        window.contentMinSize = Self.minimumContentSize
        window.collectionBehavior.insert(.fullScreenPrimary)
        window.title = Text.appName.string(lang)
        window.delegate = self
        window.tabbingMode = .disallowed
        window.center()
        // Remembers size and position between launches.
        window.setFrameAutosaveName("MainWindow2")
        clampToMinimumSize()
        showNative(.loading)
    }

    /// A restored frame smaller than the minimum grows to it, top-left corner kept.
    private func clampToMinimumSize() {
        let content = window.contentRect(forFrameRect: window.frame)
        let minimum = Self.minimumContentSize
        guard content.width < minimum.width || content.height < minimum.height else { return }
        let size = NSSize(width: max(content.width, minimum.width), height: max(content.height, minimum.height))
        var frame = window.frameRect(forContentRect: NSRect(origin: content.origin, size: size))
        frame.origin.y = window.frame.maxY - frame.height
        window.setFrame(window.constrainFrameRect(frame, to: window.screen ?? NSScreen.main), display: false)
    }

    // MARK: - Native views

    func showLoading() { showNative(.loading) }

    func show(state: ServiceState) {
        switch state {
        case .starting:
            // A restart after a crash: the old page talks to a dead port.
            dropWebView()
            showNative(.loading)
        case .stopping, .stopped:
            if webView == nil { showNative(.loading) }
        case .running:
            break
        case .failed(let code):
            dropWebView()
            showNative(.failed(code))
        case .alreadyRunning:
            dropWebView()
            showNative(.alreadyRunning)
        case .configError(let code):
            dropWebView()
            showNative(.configError(code))
        }
    }

    private func showNative(_ view: NativeView) {
        nativeView = view
        refreshNativeView()
    }

    private func refreshNativeView() {
        if webView == nil { window.title = Text.appName.string(lang) }
        switch nativeView {
        case .none:
            return
        case .loading:
            window.contentView = StatusViews.loading(text: Text.starting.string(lang))
        case .failed(let code):
            window.contentView = errorView(
                title: L10n.serviceFailedTitle(code, lang), text: Text.serviceFailedText.string(lang), log: true)
        case .alreadyRunning:
            window.contentView = errorView(
                title: Text.alreadyRunningTitle.string(lang), text: Text.alreadyRunningText.string(lang), log: false)
        case .configError(let code):
            window.contentView = errorView(
                title: L10n.configErrorTitle(code, lang), text: Text.configErrorText.string(lang), log: true)
        }
    }

    private func errorView(title: String, text: String, log: Bool) -> NSView {
        var buttons = [StatusViews.button(Text.tryAgain.string(lang), primary: true) { [weak self] in
            self?.showNative(.loading)
            self?.onRetry?()
        }]
        if log {
            buttons.append(StatusViews.button(Text.showLog.string(lang), primary: false) { [weak self] in
                self?.onShowLog?()
            })
        }
        return StatusViews.error(title: title, text: text, buttons: buttons)
    }

    // MARK: - Web view

    /// A fresh web view for every service start: new port, new key, new empty data store.
    func load(origin: LocalOrigin, key: String) {
        if self.origin == origin, webView != nil { return }
        dropWebView()
        self.origin = origin
        loadGeneration += 1
        let current = loadGeneration
        Task {
            let made = await makeWebView(origin: origin, key: key)
            guard current == loadGeneration else { return }
            guard let view = made else {
                // Without the block list the window would lose its second line of defence.
                showNative(.configError(L10n.webSetupErrorCode))
                return
            }
            webView = view
            nativeView = .none
            window.contentView = view
            titleObservation = view.observe(\.title, options: [.new]) { [weak self] _, change in
                // Page titles are fixed words (the page name), never meeting content.
                let title = change.newValue ?? nil
                MainActor.assumeIsolated {
                    guard let self else { return }
                    self.window.title = (title?.isEmpty == false ? title : nil) ?? Text.appName.string(self.lang)
                }
            }
            view.load(URLRequest(url: origin.baseURL))
        }
    }

    private func dropWebView() {
        titleObservation = nil
        tourPending = false
        if let webView {
            webView.stopLoading()
            webView.configuration.userContentController.removeAllScriptMessageHandlers()
            webView.navigationDelegate = nil
            webView.uiDelegate = nil
        }
        webView = nil
        origin = nil
        loadGeneration += 1
    }

    private func makeWebView(origin: LocalOrigin, key: String) async -> AppWebView? {
        let config = WKWebViewConfiguration()
        // Nothing on disk: no cookies, cache or local storage with transcript pages.
        let store = WKWebsiteDataStore.nonPersistent()
        config.websiteDataStore = store
        config.preferences.isFraudulentWebsiteWarningEnabled = false
        config.preferences.javaScriptCanOpenWindowsAutomatically = false
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        let controller = WKUserContentController()
        controller.addScriptMessageHandler(BridgeProxy(owner: self), contentWorld: .page, name: "transcribator")
        // A second line after CSP and the navigation policy: every other load is blocked.
        guard let rules = try? await WKContentRuleListStore.default().compileContentRuleList(
            forIdentifier: "local-only", encodedContentRuleList: origin.contentRuleList)
        else { return nil }
        controller.add(rules)
        config.userContentController = controller

        var properties: [HTTPCookiePropertyKey: Any] = [
            .name: "tb_key",
            .value: key,
            .domain: LocalOrigin.host,
            .path: "/",
            .sameSitePolicy: HTTPCookieStringPolicy.sameSiteStrict,
            .discard: "TRUE",
        ]
        properties[HTTPCookiePropertyKey("HttpOnly")] = "TRUE"
        if let cookie = HTTPCookie(properties: properties) {
            await store.httpCookieStore.setCookie(cookie)
        }

        let view = AppWebView(frame: window.contentLayoutRect, configuration: config)
        view.origin = origin
        view.allowsLinkPreview = false
        view.allowsBackForwardNavigationGestures = true
        view.isInspectable = ProcessInfo.processInfo.environment["TRANSCRIBATOR_APP_INSPECT"] == "1"
        view.navigationDelegate = self
        view.uiDelegate = self
        return view
    }

    /// Opens a page of the service (Settings… in the menu).
    func open(path: String) {
        guard let origin, let webView else { return }
        webView.load(URLRequest(url: origin.url(path: path)))
    }

    func reload() { webView?.reload() }

    /// Help > Show Tips: replays the onboarding tips of the upload page. Opens "/" first
    /// when another page is showing. Only on the service's own page; failures are ignored.
    func startTour() {
        guard let origin, let webView, let current = webView.url, origin.isLocal(current) else { return }
        if origin.isUploadPage(current) {
            if webView.isLoading {
                tourPending = true
            } else {
                runTour(in: webView)
            }
        } else {
            tourPending = true
            webView.load(URLRequest(url: origin.baseURL))
        }
    }

    private func runTour(in view: WKWebView) {
        guard let origin, view === webView, let current = view.url, origin.isUploadPage(current) else { return }
        view.callAsyncJavaScript(PageScript.startTour, arguments: [:], in: nil, in: .page, completionHandler: nil)
    }

    func zoom(_ step: Int) {
        guard let webView else { return }
        let levels: [CGFloat] = [0.5, 0.67, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5, 3]
        if step == 0 {
            webView.pageZoom = 1
            return
        }
        let current = levels.firstIndex { $0 >= webView.pageZoom - 0.001 } ?? 4
        let next = min(max(current + step, 0), levels.count - 1)
        webView.pageZoom = levels[next]
    }

    // MARK: - Window

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        // Closing the window keeps the app and the service: the menu bar icon stays.
        // A full-screen window leaves full screen first, otherwise its empty Space stays
        // behind with no window and no Dock icon to return to.
        if window.styleMask.contains(.fullScreen) {
            closeAfterFullScreen = true
            window.toggleFullScreen(nil)
            return false
        }
        hide()
        return false
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        guard closeAfterFullScreen else { return }
        closeAfterFullScreen = false
        hide()
    }

    private func hide() {
        window.orderOut(nil)
        onClose?()
    }

    // MARK: - Bridge

    func handleBridge(_ message: WKScriptMessage, reply: @escaping @MainActor @Sendable (Any?, String?) -> Void) {
        let frame = message.frameInfo
        let source = frame.securityOrigin
        guard let origin, message.webView === webView,
              origin.acceptsBridge(
                protocol: source.protocol, host: source.host, port: source.port, isMainFrame: frame.isMainFrame),
              let command = BridgeCommand(body: message.body)
        else {
            reply(nil, "refused")
            return
        }
        switch command {
        case .pick:
            let panel = NSOpenPanel()
            panel.allowsMultipleSelection = true
            panel.canChooseDirectories = false
            panel.canChooseFiles = true
            panel.allowedContentTypes = [.audio, .movie]
            panel.message = Text.pickMessage.string(lang)
            panel.prompt = Text.pickButton.string(lang)
            panel.beginSheetModal(for: window) { response in
                let paths = response == .OK ? panel.urls.filter(\.isFileURL).map(\.path) : []
                reply(["paths": paths], nil)
            }
        case .revealData:
            onRevealData?()
            reply(["ok": true], nil)
        }
    }

    // MARK: - Navigation

    func webView(
        _ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationActionPolicy) -> Void
    ) {
        guard let origin, let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        switch origin.decide(url, userInitiated: navigationAction.navigationType == .linkActivated) {
        case .allow:
            decisionHandler(navigationAction.shouldPerformDownload ? .download : .allow)
        case .openExternally:
            NSWorkspace.shared.open(url)
            decisionHandler(.cancel)
        case .cancel:
            decisionHandler(.cancel)
        }
    }

    func webView(
        _ webView: WKWebView, decidePolicyFor navigationResponse: WKNavigationResponse,
        decisionHandler: @escaping @MainActor @Sendable (WKNavigationResponsePolicy) -> Void
    ) {
        let disposition = (navigationResponse.response as? HTTPURLResponse)?
            .value(forHTTPHeaderField: "Content-Disposition")?.lowercased() ?? ""
        if disposition.hasPrefix("attachment") || !navigationResponse.canShowMIMEType {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(_ webView: WKWebView, navigationAction: WKNavigationAction, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, navigationResponse: WKNavigationResponse, didBecome download: WKDownload) {
        download.delegate = self
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if tourPending {
            tourPending = false
            runTour(in: webView)
        }
        onPageLoaded?()
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: any Error) {
        tourPending = false
    }

    func webView(
        _ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: any Error
    ) {
        tourPending = false
    }

    func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
        webView.reload()
    }

    // MARK: - UI: new windows and <input type=file>

    func webView(
        _ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        // target=_blank: links the user clicked open in the default browser, never in a new view.
        if let origin, let url = navigationAction.request.url, navigationAction.navigationType == .linkActivated {
            switch origin.decide(url, userInitiated: true) {
            case .allow: webView.load(URLRequest(url: url))
            case .openExternally: NSWorkspace.shared.open(url)
            case .cancel: break
            }
        }
        return nil
    }

    func webView(
        _ webView: WKWebView, runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping @MainActor @Sendable ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.canChooseFiles = true
        panel.beginSheetModal(for: window) { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    // MARK: - Downloads (.md export)

    func download(
        _ download: WKDownload, decideDestinationUsing response: URLResponse, suggestedFilename: String,
        completionHandler: @escaping @MainActor @Sendable (URL?) -> Void
    ) {
        let panel = NSSavePanel()
        panel.directoryURL = FileManager.default.urls(for: .downloadsDirectory, in: .userDomainMask).first
        panel.nameFieldStringValue = DownloadName.safe(suggestedFilename)
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        panel.beginSheetModal(for: window) { result in
            guard result == .OK, let url = panel.url else {
                completionHandler(nil)
                return
            }
            // The panel already asked about replacing; WebKit refuses an existing file. The old
            // file goes to the Trash, not away, in case the download then fails.
            if FileManager.default.fileExists(atPath: url.path) {
                try? FileManager.default.trashItem(at: url, resultingItemURL: nil)
            }
            completionHandler(url)
        }
    }

    func download(_ download: WKDownload, didFailWithError error: any Error, resumeData: Data?) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        alert.messageText = Text.saveFailed.string(lang)
        alert.beginSheetModal(for: window)
    }
}
