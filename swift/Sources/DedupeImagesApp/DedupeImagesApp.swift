// DedupeImagesApp - native SwiftUI macOS app for image deduplication.

import SwiftUI
import AppKit

@main
struct DedupeImagesApp: App {
    var body: some Scene {
        WindowGroup("DedupeImages") {
            ContentView()
                .frame(minWidth: 900, minHeight: 600)
        }
        .windowResizability(.contentSize)
        .commands {
            CommandGroup(replacing: .newItem) { }
        }
    }
}
