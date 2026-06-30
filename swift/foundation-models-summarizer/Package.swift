// swift-tools-version: 6.2
//
// Optional, NOT built by default. This is a scaffold for a future
// on-device summarizer using Apple Intelligence's Foundation Models
// framework (macOS 15+/26, Apple Silicon). Build only if requested — see
// README.md in this directory for why this exists as a separate Swift
// package rather than Python code, and how it plugs into the server.

import PackageDescription

let package = Package(
    name: "foundation-models-summarizer",
    platforms: [.macOS(.v26)],
    targets: [
        .executableTarget(
            name: "foundation-models-summarizer"
        )
    ]
)
