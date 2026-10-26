// Which processes belong to which app, which apps to offer as a source, and where the
// microphone and the tap sit in the buffers of the capture device. No Core Audio calls here.

import Foundation

public enum CaptureAppGrouping {
    /// Running conferencing apps and browsers are always offered, even while silent.
    public static let pinnedBundleIds: [String] = [
        "us.zoom.xos",  // Zoom
        "com.microsoft.teams2", "com.microsoft.teams",  // Teams
        "ru.yandex.telemost", "com.yandex.telemost",  // Telemost
        "com.tinyspeck.slackmacgap",  // Slack
        "com.hnc.Discord",  // Discord
        "Cisco-Systems.Spark", "com.cisco.webexmeetingsapp",  // Webex
        "com.google.Chrome", "com.apple.Safari", "org.mozilla.firefox",
        "com.microsoft.edgemac", "company.thebrowser.Browser",
        "ru.yandex.desktop.yandex-browser", "com.brave.Browser",
    ]

    /// Audio processes whose bundle id does not start with the app's own id.
    static let knownOwners: [String: String] = [
        "com.apple.webkit.gpu": "com.apple.safari",
        "com.apple.webkit.webcontent": "com.apple.safari",
        "org.mozilla.plugincontainer": "org.mozilla.firefox",
    ]

    /// The app (one of `apps`, returned as spelled there) that a process with this bundle id
    /// belongs to: the app itself, a known helper, or the longest app id that is a dotted
    /// prefix of it (`com.google.Chrome.helper` belongs to `com.google.Chrome`).
    public static func owner(of process: String, among apps: [String]) -> String? {
        let p = process.lowercased()
        guard !p.isEmpty else { return nil }
        if let exact = apps.first(where: { $0.lowercased() == p }) { return exact }
        if let known = knownOwners[p], let app = apps.first(where: { $0.lowercased() == known }) {
            return app
        }
        var best: String?
        for app in apps {
            let a = app.lowercased()
            if p.hasPrefix(a + "."), a.count > (best?.count ?? 0) { best = app }
        }
        return best
    }

    /// Whether a process belongs to a group chosen in the interface. Processes that started
    /// after the list was made (a browser's audio helper) still match their parent.
    public static func belongs(_ process: String, to group: [String]) -> Bool {
        let p = process.lowercased()
        guard !p.isEmpty else { return false }
        for member in group {
            let m = member.lowercased()
            if p == m || p.hasPrefix(m + ".") || knownOwners[p] == m { return true }
        }
        return false
    }

    public struct RunningApp: Sendable, Equatable {
        public var bundleId: String
        public var name: String

        public init(bundleId: String, name: String) {
            self.bundleId = bundleId
            self.name = name
        }
    }

    public struct AudioProcess: Sendable, Equatable {
        public var bundleId: String
        public var outputting: Bool

        public init(bundleId: String, outputting: Bool) {
            self.bundleId = bundleId
            self.outputting = outputting
        }
    }

    /// Apps to offer: those with a process playing audio now plus pinned ones that run. Each
    /// lists the app's own bundle id first, then its audio helpers. The app itself is left out.
    public static func catalog(
        apps: [RunningApp], processes: [AudioProcess], selfBundleId: String?
    ) -> [AudioAppInfo] {
        let me = selfBundleId?.lowercased()
        let candidates = apps.filter { $0.bundleId.lowercased() != me }
        let ids = candidates.map(\.bundleId)
        var helpers: [String: Set<String>] = [:]
        var outputting: [String: Bool] = [:]
        for process in processes {
            guard let owner = owner(of: process.bundleId, among: ids) else { continue }
            if process.bundleId.lowercased() != owner.lowercased() {
                helpers[owner, default: []].insert(process.bundleId)
            }
            if process.outputting { outputting[owner] = true }
        }
        let pinned = Set(pinnedBundleIds.map { $0.lowercased() })
        var seen = Set<String>()
        var result: [AudioAppInfo] = []
        for app in candidates where seen.insert(app.bundleId.lowercased()).inserted {
            let playing = outputting[app.bundleId] ?? false
            guard playing || pinned.contains(app.bundleId.lowercased()) else { continue }
            let group = [app.bundleId] + (helpers[app.bundleId] ?? []).sorted()
            result.append(AudioAppInfo(bundleIds: group, name: app.name, outputting: playing))
        }
        return result.sorted { a, b in
            if a.outputting != b.outputting { return a.outputting }
            return a.name.localizedStandardCompare(b.name) == .orderedAscending
        }
    }
}

public enum CaptureDeviceList {
    /// Input devices for the interface, the system default first, the rest in system order.
    public static func microphones(
        _ devices: [(uid: String, name: String)], defaultUID: String?
    ) -> [MicrophoneInfo] {
        var seen = Set<String>()
        var list: [MicrophoneInfo] = []
        for device in devices where !device.uid.isEmpty && seen.insert(device.uid).inserted {
            list.append(
                MicrophoneInfo(id: device.uid, name: device.name, isDefault: device.uid == defaultUID))
        }
        if let i = list.firstIndex(where: \.isDefault), i > 0 {
            list.insert(list.remove(at: i), at: 0)
        }
        return list
    }
}

/// Where the microphone and the tap are in the input buffer list of the capture device. The
/// device's sub-devices come first, in the order they were listed, then the tap.
public struct CaptureStreamLayout: Sendable, Equatable {
    /// Buffers of the microphone, nil when it is not part of the device.
    public var mic: Range<Int>?
    public var tap: Int

    public init(mic: Range<Int>?, tap: Int) {
        self.mic = mic
        self.tap = tap
    }

    /// - Parameters:
    ///   - buffers: number of input buffers the device delivers.
    ///   - subDeviceBuffers: input buffers of each sub-device, in list order.
    ///   - micSubDevice: index of the microphone in that list.
    /// If the count does not add up, the tap is taken as the last buffer and the microphone as
    /// the first one.
    public static func resolve(buffers: Int, subDeviceBuffers: [Int], micSubDevice: Int?)
        -> CaptureStreamLayout?
    {
        guard buffers > 0 else { return nil }
        let expected = subDeviceBuffers.reduce(0, +) + 1
        if buffers == expected {
            var mic: Range<Int>?
            if let m = micSubDevice, subDeviceBuffers.indices.contains(m), subDeviceBuffers[m] > 0 {
                let start = subDeviceBuffers[..<m].reduce(0, +)
                mic = start..<(start + subDeviceBuffers[m])
            }
            return CaptureStreamLayout(mic: mic, tap: buffers - 1)
        }
        let mic: Range<Int>? = micSubDevice != nil && buffers > 1 ? 0..<1 : nil
        return CaptureStreamLayout(mic: mic, tap: buffers - 1)
    }
}

/// A Core Audio process object as the engine sees it.
public struct CaptureProcessRef: Sendable, Equatable {
    public var id: UInt32
    public var outputting: Bool

    public init(id: UInt32, outputting: Bool) {
        self.id = id
        self.outputting = outputting
    }
}

public enum CaptureRetap {
    /// Whether the tap must be rebuilt because the source's processes changed.
    /// - Parameters:
    ///   - excludesOwnProcess: true for "everything except this app" (`current` are then the
    ///     app's own process objects), false for one app (`current` are its processes).
    ///   - tapped: process objects the running tap was built with (tapped or excluded).
    /// One app: rebuild when its set of processes changed and is not empty (with every
    /// process gone the old tap is kept until the app is back). Everything except this app:
    /// the app's own process object appears only once capture runs, and rebuilding for that
    /// alone is useless, so rebuild only when an own object that is not excluded yet plays.
    public static func needsRebuild(
        excludesOwnProcess: Bool, tapped: [UInt32], current: [CaptureProcessRef]
    ) -> Bool {
        let known = Set(tapped)
        if excludesOwnProcess {
            return current.contains { $0.outputting && !known.contains($0.id) }
        }
        let ids = Set(current.map(\.id))
        return !ids.isEmpty && ids != known
    }
}
