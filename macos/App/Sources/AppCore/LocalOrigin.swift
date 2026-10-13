// What the window may load: only the service at http://127.0.0.1:<port>. Links to other
// http(s) sites that the user clicks open in the default browser; everything else is
// cancelled.

import Foundation

public enum NavigationDecision: Equatable, Sendable {
    case allow
    case openExternally
    case cancel
}

public struct LocalOrigin: Equatable, Sendable {
    public static let host = "127.0.0.1"
    public let port: Int

    public init(port: Int) { self.port = port }

    public var baseURL: URL { URL(string: "http://\(Self.host):\(port)/")! }

    public func url(path: String) -> URL {
        let clean = path.hasPrefix("/") ? path : "/" + path
        return URL(string: "http://\(Self.host):\(port)\(clean)")!
    }

    /// The URL is the service itself: http, 127.0.0.1, our port, no user info.
    public func isLocal(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == "http", url.host == Self.host, url.port == port,
              url.user == nil, url.password == nil else { return false }
        return true
    }

    /// The upload page ("/") of the service, with or without a query.
    public func isUploadPage(_ url: URL) -> Bool {
        isLocal(url) && (url.path.isEmpty || url.path == "/")
    }

    public func decide(_ url: URL, userInitiated: Bool) -> NavigationDecision {
        if isLocal(url) { return .allow }
        let scheme = url.scheme?.lowercased()
        if userInitiated, scheme == "http" || scheme == "https", let host = url.host, !host.isEmpty,
           url.user == nil, url.password == nil {
            return .openExternally
        }
        return .cancel
    }

    /// The bridge accepts messages only from the main frame of the service's own page.
    public func acceptsBridge(protocol scheme: String, host: String, port: Int, isMainFrame: Bool) -> Bool {
        isMainFrame && scheme == "http" && host == Self.host && port == self.port
    }

    /// WKContentRuleList: block every load except the service (a second line after CSP
    /// and the navigation policy).
    public var contentRuleList: String {
        let rules: [[String: Any]] = [
            ["trigger": ["url-filter": ".*"], "action": ["type": "block"]],
            [
                "trigger": ["url-filter": "^http://127\\.0\\.0\\.1:\(port)/"],
                "action": ["type": "ignore-previous-rules"],
            ],
        ]
        let data = try! JSONSerialization.data(withJSONObject: rules, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }
}

/// Scripts the app runs in the page world of the service's own page. Values go in as
/// arguments, never pasted into the script text.
public enum PageScript {
    /// Hands dropped Finder paths (argument `paths`) to the upload page.
    public static let drop =
        "return typeof window.transcribatorDrop === 'function' ? window.transcribatorDrop(paths) : false"
    /// Replays the onboarding tips on the upload page (Help > Show Tips).
    public static let startTour =
        "return window.transcribatorTour ? window.transcribatorTour.start() : false"
}

/// Commands from the page (`window.webkit.messageHandlers.transcribator`).
public enum BridgeCommand: Equatable, Sendable {
    /// Native file dialog; the reply is `{paths: [...]}`.
    case pick
    /// Show the data folder in Finder.
    case revealData

    public init?(body: Any) {
        guard let dict = body as? [String: Any], let cmd = dict["cmd"] as? String else { return nil }
        switch cmd {
        case "pick": self = .pick
        case "revealData": self = .revealData
        default: return nil
        }
    }
}

/// File name for a download from the service, safe for the save panel.
public enum DownloadName {
    public static func safe(_ suggested: String, fallback: String = "transcript.md") -> String {
        let cleaned = suggested.unicodeScalars.map { scalar -> Character in
            if scalar.properties.generalCategory == .control || scalar == "/" || scalar == ":" { return " " }
            return Character(scalar)
        }
        let name = String(cleaned).split(whereSeparator: \.isWhitespace).joined(separator: " ")
            .trimmingCharacters(in: CharacterSet(charactersIn: " ."))
        guard !name.isEmpty else { return fallback }
        return String(name.prefix(200))
    }
}
