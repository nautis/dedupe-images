// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "DedupeImages",
    platforms: [.macOS(.v13)],
    targets: [
        .target(
            name: "DedupeCore",
            path: "Sources/DedupeCore"
        ),
        .executableTarget(
            name: "DedupeImagesCLI",
            dependencies: ["DedupeCore"],
            path: "Sources/DedupeImagesCLI"
        ),
        .executableTarget(
            name: "DedupeImagesApp",
            dependencies: ["DedupeCore"],
            path: "Sources/DedupeImagesApp"
        ),
    ]
)
