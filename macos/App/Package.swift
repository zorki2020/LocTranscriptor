// swift-tools-version: 6.0
// LocTranscriptor.app: the window, the menu bar icon and the supervisor of the local
// service. System frameworks only, no third-party dependencies.
import PackageDescription

let package = Package(
    name: "LocTranscriptorApp",
    platforms: [.macOS(.v15)],
    products: [
        .executable(name: "LocTranscriptor", targets: ["LocTranscriptor"]),
    ],
    targets: [
        // Pure logic with unit tests: no windows, no WebKit.
        .target(name: "AppCore"),
        .executableTarget(name: "LocTranscriptor", dependencies: ["AppCore"]),
        .testTarget(name: "AppCoreTests", dependencies: ["AppCore"]),
    ]
)
