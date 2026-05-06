import SwiftUI
import AppKit
import DedupeCore

struct ReviewView: View {
    @ObservedObject var model: AppModel

    var cluster: Cluster? {
        guard !model.clusters.isEmpty else { return nil }
        return model.clusters[model.currentClusterIdx]
    }

    var body: some View {
        VStack(spacing: 0) {
            // Top bar
            HStack {
                VStack(alignment: .leading, spacing: 2) {
                    Text("DedupeImages review")
                        .font(.headline)
                    Text("Cluster \(model.currentClusterIdx + 1) of \(model.clusters.count) · \(model.clusters[model.currentClusterIdx].members.count) files")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button(action: model.goPrev) { Image(systemName: "chevron.left") }
                    .keyboardShortcut(.leftArrow, modifiers: [])
                    .disabled(model.currentClusterIdx == 0)
                Button(action: model.goNext) { Image(systemName: "chevron.right") }
                    .keyboardShortcut(.rightArrow, modifiers: [])
                    .disabled(model.currentClusterIdx >= model.clusters.count - 1)
                Button(model.dupeCount == 0
                       ? "Commit (nothing to move)"
                       : "Commit (\(model.dupeCount) move\(model.dupeCount == 1 ? "" : "s"))") {
                    let alert = NSAlert()
                    alert.messageText = "Move \(model.dupeCount) files to ~/dedupe-quarantine?"
                    alert.informativeText = "Files marked Dupe will move out of their current location. Nothing is deleted."
                    alert.addButton(withTitle: "Move")
                    alert.addButton(withTitle: "Cancel")
                    if alert.runModal() == .alertFirstButtonReturn {
                        model.commit()
                    }
                }
                .keyboardShortcut("c", modifiers: [.command])
                .disabled(model.dupeCount == 0)
                .buttonStyle(.borderedProminent)
            }
            .padding(.horizontal)
            .padding(.vertical, 10)
            .background(Color(NSColor.controlBackgroundColor))

            Divider()

            if let cluster = cluster {
                ClusterGrid(cluster: cluster, model: model)
            } else {
                Text("No clusters")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }
}

struct ClusterGrid: View {
    let cluster: Cluster
    @ObservedObject var model: AppModel

    private let columns = [GridItem(.adaptive(minimum: 320), spacing: 16)]

    var body: some View {
        VStack(spacing: 0) {
            // Tier label
            HStack {
                Text(tierLabel(cluster.tier))
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal)
            .padding(.top, 8)

            ScrollView {
                LazyVGrid(columns: columns, spacing: 16) {
                    ForEach(Array(cluster.members.enumerated()), id: \.element.url) {
                        idx, member in
                        FileCard(member: member, idx: idx, cluster: cluster,
                                 model: model)
                    }
                }
                .padding()
            }
        }
    }

    private func tierLabel(_ t: Int) -> String {
        switch t {
        case 1: return "Tier 1 · byte-identical"
        case 3: return "Tier 3 · perceptually similar (eyeball this)"
        default: return "Tier \(t)"
        }
    }
}

struct FileCard: View {
    let member: ScanFile
    let idx: Int
    let cluster: Cluster
    @ObservedObject var model: AppModel
    @State private var showLightbox = false

    var decision: String {
        model.decisions[member.url] ?? "keep"
    }

    var body: some View {
        VStack(spacing: 0) {
            ThumbnailView(url: member.url)
                .frame(height: 280)
                .frame(maxWidth: .infinity)
                .background(Color(NSColor.controlBackgroundColor))
                .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
                .onTapGesture {
                    showLightbox = true
                }

            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    Text("[\(idx + 1)]").bold().foregroundStyle(.tint)
                    Text(formatBytes(member.size))
                    Spacer()
                }
                Text(member.url.path)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .truncationMode(.middle)
            }
            .padding(8)

            HStack(spacing: 0) {
                Button {
                    model.mark(member.url, as: "keep")
                } label: {
                    Text("Keep")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .background(decision == "keep" ? Color.green.opacity(0.7) : Color(NSColor.controlBackgroundColor))
                        .foregroundColor(decision == "keep" ? .white : .primary)
                }
                .buttonStyle(.plain)

                Button {
                    model.mark(member.url, as: "dupe")
                } label: {
                    Text("Dupe")
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 8)
                        .background(decision == "dupe" ? Color.red.opacity(0.7) : Color(NSColor.controlBackgroundColor))
                        .foregroundColor(decision == "dupe" ? .white : .primary)
                }
                .buttonStyle(.plain)
            }
        }
        .background(Color(NSColor.windowBackgroundColor))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(decision == "keep" ? Color.green : Color.red, lineWidth: 2)
        )
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .sheet(isPresented: $showLightbox) {
            LightboxView(url: member.url, isPresented: $showLightbox)
        }
    }
}

struct ThumbnailView: View {
    let url: URL
    @State private var image: NSImage?

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
            } else {
                ProgressView()
            }
        }
        .task(id: url) {
            self.image = await loadThumbnail(url: url, maxDim: 720)
        }
    }
}

struct LightboxView: View {
    let url: URL
    @Binding var isPresented: Bool
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            Color.black.opacity(0.95)
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .aspectRatio(contentMode: .fit)
                    .padding(20)
            } else {
                ProgressView().tint(.white)
            }
        }
        .frame(minWidth: 800, minHeight: 600)
        .onTapGesture { isPresented = false }
        .task(id: url) {
            self.image = await loadFullImage(url: url)
        }
    }
}

func loadThumbnail(url: URL, maxDim: CGFloat) async -> NSImage? {
    await Task.detached(priority: .userInitiated) {
        let opts: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceThumbnailMaxPixelSize: Int(maxDim),
            kCGImageSourceCreateThumbnailWithTransform: true
        ]
        guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
              let cg = CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary)
        else { return nil }
        return NSImage(cgImage: cg, size: .zero)
    }.value
}

func loadFullImage(url: URL) async -> NSImage? {
    await Task.detached(priority: .userInitiated) {
        NSImage(contentsOf: url)
    }.value
}
