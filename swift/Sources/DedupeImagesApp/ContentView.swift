import SwiftUI
import AppKit
import DedupeCore

enum AppPhase {
    case idle
    case scanning(label: String, cur: Int, total: Int)
    case reviewing
    case committed(moved: Int, bytesMoved: Int64)
}

@MainActor
final class AppModel: ObservableObject {
    @Published var phase: AppPhase = .idle
    @Published var clusters: [Cluster] = []
    @Published var currentClusterIdx: Int = 0
    @Published var decisions: [URL: String] = [:]   // "keep" | "dupe"
    @Published var selectedFolder: URL? = nil

    var quarantineDir: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("dedupe-quarantine")
    }

    func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose folder to dedupe"
        if panel.runModal() == .OK, let url = panel.url {
            selectedFolder = url
            startScan(root: url)
        }
    }

    func startScan(root: URL) {
        phase = .scanning(label: "Listing files", cur: 0, total: 0)
        Task.detached(priority: .userInitiated) {
            let files = walkImages(at: root)
            await MainActor.run {
                self.phase = .scanning(label: "Hashing", cur: 0, total: files.count)
            }
            let clusters = computeClusters(files: files) { label, cur, total in
                Task { @MainActor in
                    self.phase = .scanning(label: label, cur: cur, total: total)
                }
            }
            await MainActor.run {
                self.clusters = clusters
                if clusters.isEmpty {
                    self.phase = .committed(moved: 0, bytesMoved: 0)
                    return
                }
                self.currentClusterIdx = 0
                self.applyDefaultDecisions()
                self.phase = .reviewing
            }
        }
    }

    func applyDefaultDecisions() {
        for cluster in clusters {
            // Default keep = largest file by size.
            guard let largest = cluster.members.max(by: { $0.size < $1.size }) else {
                continue
            }
            for f in cluster.members {
                decisions[f.url] = (f.url == largest.url) ? "keep" : "dupe"
            }
        }
    }

    func mark(_ url: URL, as action: String) {
        decisions[url] = action
    }

    func pickOnly(_ url: URL, in cluster: Cluster) {
        for f in cluster.members {
            decisions[f.url] = (f.url == url) ? "keep" : "dupe"
        }
    }

    func goPrev() {
        currentClusterIdx = max(0, currentClusterIdx - 1)
    }
    func goNext() {
        currentClusterIdx = min(clusters.count - 1, currentClusterIdx + 1)
    }

    var dupeCount: Int {
        decisions.values.filter { $0 == "dupe" }.count
    }

    func commit() {
        let q = quarantineDir
        try? FileManager.default.createDirectory(at: q,
                                                  withIntermediateDirectories: true)
        var moved = 0
        var bytesMoved: Int64 = 0
        for cluster in clusters {
            for f in cluster.members {
                guard decisions[f.url] == "dupe" else { continue }
                let dest = q.appendingPathComponent(f.url.lastPathComponent)
                let finalDest: URL
                if FileManager.default.fileExists(atPath: dest.path) {
                    let ts = Int(Date().timeIntervalSince1970)
                    let stem = (f.url.lastPathComponent as NSString).deletingPathExtension
                    let ext = f.url.pathExtension
                    finalDest = q.appendingPathComponent("\(stem).\(ts).\(ext)")
                } else {
                    finalDest = dest
                }
                do {
                    try FileManager.default.moveItem(at: f.url, to: finalDest)
                    moved += 1
                    bytesMoved += f.size
                } catch {
                    NSLog("move failed: \(f.url): \(error)")
                }
            }
        }
        phase = .committed(moved: moved, bytesMoved: bytesMoved)
    }

    func reset() {
        phase = .idle
        clusters = []
        decisions = [:]
        selectedFolder = nil
        currentClusterIdx = 0
    }
}

struct ContentView: View {
    @StateObject private var model = AppModel()

    var body: some View {
        VStack(spacing: 0) {
            switch model.phase {
            case .idle:
                IdleView(model: model)
            case .scanning(let label, let cur, let total):
                ScanProgressView(label: label, cur: cur, total: total)
            case .reviewing:
                ReviewView(model: model)
            case .committed(let moved, let bytes):
                DoneView(model: model, moved: moved, bytesMoved: bytes)
            }
        }
        .background(Color(NSColor.windowBackgroundColor))
    }
}

struct IdleView: View {
    @ObservedObject var model: AppModel
    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "photo.stack")
                .font(.system(size: 80, weight: .light))
                .foregroundStyle(.tint)
            Text("Find duplicate images in a folder.")
                .font(.title2)
            Text("Click below to pick a folder. Detected duplicates will move to ~/dedupe-quarantine on commit (nothing is deleted).")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
                .frame(maxWidth: 500)
            Button {
                model.pickFolder()
            } label: {
                Label("Choose folder…", systemImage: "folder")
                    .font(.title3)
                    .padding(.horizontal, 16)
                    .padding(.vertical, 8)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct ScanProgressView: View {
    let label: String
    let cur: Int
    let total: Int
    var body: some View {
        VStack(spacing: 16) {
            ProgressView(value: total == 0 ? 0 : Double(cur), total: Double(max(total, 1)))
                .progressViewStyle(.linear)
                .frame(maxWidth: 400)
            Text("\(label): \(cur) of \(total)")
                .foregroundStyle(.secondary)
                .monospacedDigit()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct DoneView: View {
    @ObservedObject var model: AppModel
    let moved: Int
    let bytesMoved: Int64
    var body: some View {
        VStack(spacing: 20) {
            Image(systemName: moved > 0 ? "checkmark.circle.fill" : "info.circle")
                .font(.system(size: 80))
                .foregroundStyle(moved > 0 ? .green : .secondary)
            if moved > 0 {
                Text("Moved \(moved) file\(moved == 1 ? "" : "s") to ~/dedupe-quarantine")
                    .font(.title2)
                Text("\(formatBytes(bytesMoved)) reclaimable. Review and rm -rf the quarantine when you're satisfied.")
                    .foregroundStyle(.secondary)
            } else {
                Text("No duplicates found.")
                    .font(.title2)
            }
            Button("Run again") { model.reset() }
                .buttonStyle(.borderedProminent)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

func formatBytes(_ n: Int64) -> String {
    let f = ByteCountFormatter()
    f.allowedUnits = [.useMB, .useGB, .useKB]
    f.countStyle = .file
    return f.string(fromByteCount: n)
}
