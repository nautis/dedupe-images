// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DedupeImagesCLI",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "DedupeImagesCLI",
            path: "Sources/DedupeImagesCLI"
        )
    ]
)
