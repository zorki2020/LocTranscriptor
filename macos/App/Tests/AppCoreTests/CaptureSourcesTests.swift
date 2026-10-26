import Foundation
import Testing

@testable import AppCore

private typealias App = CaptureAppGrouping.RunningApp
private typealias Proc = CaptureAppGrouping.AudioProcess

@Suite struct CaptureAppGroupingTests {
    private let apps = [
        App(bundleId: "com.google.Chrome", name: "Google Chrome"),
        App(bundleId: "com.apple.Safari", name: "Safari"),
        App(bundleId: "us.zoom.xos", name: "zoom.us"),
        App(bundleId: "com.spotify.client", name: "Spotify"),
        App(bundleId: "com.apple.TextEdit", name: "TextEdit"),
        App(bundleId: "company.thebrowser.Browser", name: "Arc"),
        App(bundleId: "org.mozilla.firefox", name: "Firefox"),
        App(bundleId: "local.loctranscriptor", name: "LocTranscriptor"),
    ]

    @Test func ownerByPrefixAndKnownHelpers() {
        let ids = apps.map(\.bundleId)
        #expect(CaptureAppGrouping.owner(of: "com.google.Chrome.helper", among: ids) == "com.google.Chrome")
        #expect(
            CaptureAppGrouping.owner(of: "com.google.Chrome.helper.Renderer", among: ids)
                == "com.google.Chrome")
        #expect(CaptureAppGrouping.owner(of: "com.apple.WebKit.GPU", among: ids) == "com.apple.Safari")
        #expect(
            CaptureAppGrouping.owner(of: "company.thebrowser.browser.helper", among: ids)
                == "company.thebrowser.Browser")
        #expect(
            CaptureAppGrouping.owner(of: "org.mozilla.plugincontainer", among: ids)
                == "org.mozilla.firefox")
        #expect(CaptureAppGrouping.owner(of: "us.zoom.xos", among: ids) == "us.zoom.xos")
        // Not a dotted prefix, unknown, empty.
        #expect(CaptureAppGrouping.owner(of: "com.google.ChromeCanary", among: ids) == nil)
        #expect(CaptureAppGrouping.owner(of: "com.apple.coreaudiod", among: ids) == nil)
        #expect(CaptureAppGrouping.owner(of: "", among: ids) == nil)
        // Safari's WebKit processes need Safari to be running.
        #expect(CaptureAppGrouping.owner(of: "com.apple.WebKit.GPU", among: ["com.apple.Mail"]) == nil)
    }

    @Test func longestPrefixWins() {
        let ids = ["com.example", "com.example.app"]
        #expect(CaptureAppGrouping.owner(of: "com.example.app.helper", among: ids) == "com.example.app")
        #expect(CaptureAppGrouping.owner(of: "com.example.other", among: ids) == "com.example")
    }

    @Test func belongsMatchesHelpersThatStartLater() {
        let chrome = ["com.google.Chrome"]
        #expect(CaptureAppGrouping.belongs("com.google.Chrome", to: chrome))
        #expect(CaptureAppGrouping.belongs("com.google.chrome.helper", to: chrome))
        #expect(!CaptureAppGrouping.belongs("com.google.ChromeCanary", to: chrome))
        #expect(!CaptureAppGrouping.belongs("", to: chrome))
        let safari = ["com.apple.Safari"]
        #expect(CaptureAppGrouping.belongs("com.apple.WebKit.GPU", to: safari))
        #expect(CaptureAppGrouping.belongs("com.apple.WebKit.WebContent", to: safari))
        #expect(!CaptureAppGrouping.belongs("com.apple.Music", to: safari))
        #expect(
            CaptureAppGrouping.belongs("org.mozilla.plugincontainer", to: ["org.mozilla.firefox"]))
    }

    @Test func catalogGroupsAndFilters() {
        let processes = [
            Proc(bundleId: "com.google.Chrome", outputting: false),
            Proc(bundleId: "com.google.Chrome.helper", outputting: true),
            Proc(bundleId: "com.apple.WebKit.GPU", outputting: false),
            Proc(bundleId: "com.spotify.client", outputting: true),
            Proc(bundleId: "com.apple.TextEdit", outputting: false),
            Proc(bundleId: "local.loctranscriptor", outputting: true),
            Proc(bundleId: "com.apple.coreaudiod", outputting: true),
        ]
        let list = CaptureAppGrouping.catalog(
            apps: apps, processes: processes, selfBundleId: "local.loctranscriptor")
        // Playing first (by name), then pinned but silent ones. TextEdit is neither, the app
        // itself is never offered.
        #expect(
            list.map(\.name) == ["Google Chrome", "Spotify", "Arc", "Firefox", "Safari", "zoom.us"])
        #expect(list[0].bundleIds == ["com.google.Chrome", "com.google.Chrome.helper"])
        #expect(list[0].outputting)
        #expect(list[1].bundleIds == ["com.spotify.client"])
        let safari = list.first { $0.name == "Safari" }!
        #expect(safari.bundleIds == ["com.apple.Safari", "com.apple.WebKit.GPU"])
        #expect(!safari.outputting)
    }

    @Test func emptyWhenNothingRuns() {
        #expect(CaptureAppGrouping.catalog(apps: [], processes: [], selfBundleId: nil).isEmpty)
    }
}

@Suite struct CaptureDeviceListTests {
    @Test func defaultFirstWithoutDuplicates() {
        let list = CaptureDeviceList.microphones(
            [("a", "Built-in"), ("b", "USB"), ("", "Broken"), ("b", "USB again"), ("c", "Headset")],
            defaultUID: "c")
        #expect(list.map(\.id) == ["c", "a", "b"])
        #expect(list.map(\.isDefault) == [true, false, false])
        #expect(CaptureDeviceList.microphones([("a", "A")], defaultUID: nil).map(\.isDefault) == [false])
    }
}

@Suite struct CaptureStreamLayoutTests {
    @Test func speakersWithoutInputAndASeparateMic() {
        // Output (no inputs), microphone (1 buffer), tap.
        let layout = CaptureStreamLayout.resolve(buffers: 2, subDeviceBuffers: [0, 1], micSubDevice: 1)
        #expect(layout == CaptureStreamLayout(mic: 0..<1, tap: 1))
    }

    @Test func headsetOutputWithItsOwnInput() {
        // Output device has an input buffer too, mic is another device.
        let layout = CaptureStreamLayout.resolve(buffers: 3, subDeviceBuffers: [1, 1], micSubDevice: 1)
        #expect(layout == CaptureStreamLayout(mic: 1..<2, tap: 2))
        // Mic and output are the same device.
        let same = CaptureStreamLayout.resolve(buffers: 2, subDeviceBuffers: [1], micSubDevice: 0)
        #expect(same == CaptureStreamLayout(mic: 0..<1, tap: 1))
    }

    @Test func tapOnly() {
        #expect(
            CaptureStreamLayout.resolve(buffers: 1, subDeviceBuffers: [0], micSubDevice: nil)
                == CaptureStreamLayout(mic: nil, tap: 0))
        #expect(CaptureStreamLayout.resolve(buffers: 0, subDeviceBuffers: [], micSubDevice: nil) == nil)
    }

    @Test func unexpectedCountFallsBack() {
        #expect(
            CaptureStreamLayout.resolve(buffers: 4, subDeviceBuffers: [0, 1], micSubDevice: 1)
                == CaptureStreamLayout(mic: 0..<1, tap: 3))
        #expect(
            CaptureStreamLayout.resolve(buffers: 1, subDeviceBuffers: [0, 1], micSubDevice: 1)
                == CaptureStreamLayout(mic: nil, tap: 0))
    }
}

@Suite struct CaptureRetapTests {
    private func refs(_ items: [(UInt32, Bool)]) -> [CaptureProcessRef] {
        items.map { CaptureProcessRef(id: $0.0, outputting: $0.1) }
    }

    @Test func oneAppFollowsItsProcesses() {
        // Unchanged, order does not matter.
        #expect(
            !CaptureRetap.needsRebuild(
                excludesOwnProcess: false, tapped: [3, 5], current: refs([(5, true), (3, false)])))
        // A helper started (browser audio service) or the app restarted.
        #expect(
            CaptureRetap.needsRebuild(
                excludesOwnProcess: false, tapped: [3], current: refs([(3, false), (9, true)])))
        #expect(
            CaptureRetap.needsRebuild(
                excludesOwnProcess: false, tapped: [3], current: refs([(11, false)])))
        // Every process gone: keep the old tap until the app is back.
        #expect(!CaptureRetap.needsRebuild(excludesOwnProcess: false, tapped: [3], current: []))
    }

    @Test func ownProcessAppearingIsIgnoredUntilItPlays() {
        // Capture started before this app had a process object; it appears with the capture.
        #expect(
            !CaptureRetap.needsRebuild(
                excludesOwnProcess: true, tapped: [], current: refs([(42, false)])))
        // This app starts playing a sound: exclude it now.
        #expect(
            CaptureRetap.needsRebuild(
                excludesOwnProcess: true, tapped: [], current: refs([(42, true)])))
        // Already excluded, or gone: nothing to do.
        #expect(
            !CaptureRetap.needsRebuild(
                excludesOwnProcess: true, tapped: [42], current: refs([(42, true)])))
        #expect(!CaptureRetap.needsRebuild(excludesOwnProcess: true, tapped: [42], current: []))
    }
}
