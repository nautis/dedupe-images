// DedupeCore — pure-Swift dedup algorithm, no I/O side effects.
//
// Tier 1: SHA-256 of file bytes (CryptoKit).
// Tier 3: 9x8 grayscale dHash via CoreGraphics, Hamming distance threshold.

import Foundation
import CryptoKit
import CoreGraphics
import ImageIO

public let imageExtensions: Set<String> = [
    "jpg", "jpeg", "png", "heic", "heif",
    "webp", "tiff", "tif", "bmp", "gif"
]
public let photosDerivMarker = ".photoslibrary/resources/derivatives"

public struct ScanFile: Hashable, Sendable {
    public let url: URL
    public let size: Int64
    public init(url: URL, size: Int64) {
        self.url = url
        self.size = size
    }
}

public struct Cluster: Identifiable, Sendable {
    public let id = UUID()
    public let tier: Int
    public let members: [ScanFile]
    public init(tier: Int, members: [ScanFile]) {
        self.tier = tier
        self.members = members
    }
}

public func walkImages(at root: URL,
                       allowPhotosInternals: Bool = false) -> [ScanFile] {
    let fm = FileManager.default
    var results: [ScanFile] = []
    guard let enumerator = fm.enumerator(
        at: root,
        includingPropertiesForKeys: [.isRegularFileKey, .fileSizeKey],
        options: [.skipsHiddenFiles]
    ) else { return [] }

    for case let url as URL in enumerator {
        if !allowPhotosInternals && url.path.contains(photosDerivMarker) {
            enumerator.skipDescendants()
            continue
        }
        let ext = url.pathExtension.lowercased()
        guard imageExtensions.contains(ext) else { continue }
        let attrs = try? fm.attributesOfItem(atPath: url.path)
        let size = (attrs?[.size] as? Int64) ?? 0
        if size >= 1024 {
            results.append(ScanFile(url: url, size: size))
        }
    }
    return results
}

public func sha256(of url: URL) -> String? {
    guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
    defer { try? handle.close() }
    var hasher = SHA256()
    while true {
        let chunk = handle.readData(ofLength: 64 * 1024)
        if chunk.isEmpty { break }
        hasher.update(data: chunk)
    }
    return hasher.finalize().compactMap { String(format: "%02x", $0) }.joined()
}

public func dHash(of url: URL) -> UInt64? {
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let img = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        return nil
    }
    let width = 9
    let height = 8
    let colorSpace = CGColorSpaceCreateDeviceGray()
    var pixels = [UInt8](repeating: 0, count: width * height)
    guard let ctx = CGContext(
        data: &pixels,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.none.rawValue
    ) else { return nil }
    ctx.interpolationQuality = .high
    ctx.draw(img, in: CGRect(x: 0, y: 0, width: width, height: height))

    var hash: UInt64 = 0
    var bit = 0
    for y in 0..<height {
        for x in 0..<(width - 1) {
            let left = pixels[y * width + x]
            let right = pixels[y * width + x + 1]
            if left > right {
                hash |= (UInt64(1) << bit)
            }
            bit += 1
        }
    }
    return hash
}

public func hamming(_ a: UInt64, _ b: UInt64) -> Int {
    return (a ^ b).nonzeroBitCount
}

public final class UnionFind {
    public var parent: [Int]
    public var tier: [Int]
    public init(_ n: Int) {
        parent = Array(0..<n)
        tier = Array(repeating: 0, count: n)
    }
    public func find(_ x: Int) -> Int {
        var x = x
        while parent[x] != x {
            parent[x] = parent[parent[x]]
            x = parent[x]
        }
        return x
    }
    public func union(_ a: Int, _ b: Int, tier t: Int) {
        let ra = find(a), rb = find(b)
        if ra == rb {
            tier[ra] = max(tier[ra], t)
            return
        }
        parent[ra] = rb
        tier[rb] = max(tier[ra], tier[rb], t)
    }
}

public func computeClusters(files: [ScanFile],
                            threshold: Int = 8,
                            progress: ((String, Int, Int) -> Void)? = nil
                            ) -> [Cluster] {
    let n = files.count
    let uf = UnionFind(n)

    var bySha: [String: [Int]] = [:]
    for (idx, f) in files.enumerated() {
        if let h = sha256(of: f.url) {
            bySha[h, default: []].append(idx)
        }
        progress?("Tier 1 (file SHA)", idx + 1, n)
    }
    for indices in bySha.values where indices.count > 1 {
        for j in indices.dropFirst() {
            uf.union(indices[0], j, tier: 1)
        }
    }

    var repForCluster: [Int: Int] = [:]
    for i in 0..<n {
        let r = uf.find(i)
        if repForCluster[r] == nil {
            repForCluster[r] = i
        }
    }
    let clusterReps = Array(repForCluster.values)
    var dhashes: [(idx: Int, hash: UInt64)] = []
    for (k, idx) in clusterReps.enumerated() {
        if let h = dHash(of: files[idx].url) {
            dhashes.append((idx: idx, hash: h))
        }
        progress?("Tier 3 (perceptual)", k + 1, clusterReps.count)
    }

    var buckets: [Int: [Int]] = [:]
    for (ci, item) in dhashes.enumerated() {
        buckets[Int(item.hash >> 60), default: []].append(ci)
    }
    for (ci, item) in dhashes.enumerated() {
        let top = Int(item.hash >> 60)
        let adjacent = Set([top - 1, top, top + 1].filter { $0 >= 0 && $0 <= 0xF })
        for b in adjacent {
            for cj in (buckets[b] ?? []) {
                if cj <= ci { continue }
                let other = dhashes[cj]
                if hamming(item.hash, other.hash) <= threshold {
                    uf.union(item.idx, other.idx, tier: 3)
                }
            }
        }
    }

    var clustersByRoot: [Int: [Int]] = [:]
    for i in 0..<n {
        clustersByRoot[uf.find(i), default: []].append(i)
    }
    var out: [Cluster] = []
    for (root, idxs) in clustersByRoot where idxs.count >= 2 {
        let tier = uf.tier[root] == 0 ? 1 : uf.tier[root]
        let members = idxs.map { files[$0] }
        out.append(Cluster(tier: tier, members: members))
    }
    out.sort { $0.members.count > $1.members.count }
    return out
}
