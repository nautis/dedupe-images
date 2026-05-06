// dedupe-images-swift: native Swift port of the Tier 1 + Tier 3 dedup pipeline.
//
// Tier 1: CryptoKit SHA-256 of file bytes - byte-identical detection.
// Tier 3: Vision-framework-free dHash equivalent on a downscaled grayscale
//         representation built via Core Graphics. Hamming distance threshold
//         decides if two images are perceptually similar.
//
// This is a partial port - it deliberately doesn't do Tier 2 (decoded pixel
// SHA), Tier 4 (EXIF time-window), or Tier 5 (video). Use the Python tool
// (../dedupe_images.py) for those tiers and for the web review UI.

import Foundation
import CryptoKit
import CoreGraphics
import ImageIO
import AppKit

// MARK: - File walking

let imageExtensions: Set<String> = [
    "jpg", "jpeg", "png", "heic", "heif",
    "webp", "tiff", "tif", "bmp", "gif"
]
let photosDerivMarker = ".photoslibrary/resources/derivatives"

func walkImages(at root: URL, allowPhotosInternals: Bool = false) -> [URL] {
    let fm = FileManager.default
    var results: [URL] = []
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
        if imageExtensions.contains(ext) {
            results.append(url)
        }
    }
    return results
}

// MARK: - Tier 1: file SHA-256

func sha256(of url: URL) -> String? {
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

// MARK: - Tier 3: dHash via CoreGraphics

/// Returns a 64-bit dHash by downscaling to 9x8 grayscale and comparing
/// adjacent horizontal pixel pairs.
func dHash(of url: URL) -> UInt64? {
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

func hamming(_ a: UInt64, _ b: UInt64) -> Int {
    return (a ^ b).nonzeroBitCount
}

// MARK: - Union-Find

class UnionFind {
    var parent: [Int]
    var tier: [Int]
    init(_ n: Int) {
        parent = Array(0..<n)
        tier = Array(repeating: 0, count: n)
    }
    func find(_ x: Int) -> Int {
        var x = x
        while parent[x] != x {
            parent[x] = parent[parent[x]]
            x = parent[x]
        }
        return x
    }
    func union(_ a: Int, _ b: Int, tier t: Int) {
        let ra = find(a), rb = find(b)
        if ra == rb {
            tier[ra] = max(tier[ra], t)
            return
        }
        parent[ra] = rb
        tier[rb] = max(tier[ra], tier[rb], t)
    }
}

// MARK: - stderr helper

struct StandardErrorStream: TextOutputStream {
    func write(_ string: String) {
        FileHandle.standardError.write(string.data(using: .utf8)!)
    }
}
var standardError = StandardErrorStream()

// MARK: - Driver

func usage() {
    print("""
    dedupe-images-swift: native Swift port (Tier 1 + Tier 3 only).

    USAGE: dedupe-images-swift [--threshold N] [--min-size BYTES] PATH [PATH ...]

      --threshold N    Hamming distance for perceptual match. Default 8.
      --min-size B     Skip files smaller than B bytes. Default 1024.

    For Tier 2 (pixel SHA), Tier 4 (EXIF time-window), Tier 5 (video), or
    the web review UI, use the Python tool ../dedupe_images.py.
    """)
}

var paths: [String] = []
var threshold = 8
var minSize: Int64 = 1024

let argList = Array(CommandLine.arguments.dropFirst())
var i = 0
while i < argList.count {
    let a = argList[i]
    switch a {
    case "--threshold":
        i += 1
        if i < argList.count, let v = Int(argList[i]) { threshold = v }
    case "--min-size":
        i += 1
        if i < argList.count, let v = Int64(argList[i]) { minSize = v }
    case "-h", "--help":
        usage(); exit(0)
    default:
        paths.append(a)
    }
    i += 1
}

if paths.isEmpty {
    usage()
    exit(1)
}

var files: [URL] = []
for p in paths {
    let u = URL(fileURLWithPath: (p as NSString).expandingTildeInPath).standardizedFileURL
    files.append(contentsOf: walkImages(at: u))
}

// Filter by min size
files = files.compactMap { url -> URL? in
    let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
    let size = (attrs?[.size] as? Int64) ?? 0
    return size >= minSize ? url : nil
}

print("Found \(files.count) candidate images", to: &standardError)

let n = files.count
let uf = UnionFind(n)

// Tier 1
var bySha: [String: [Int]] = [:]
for (idx, url) in files.enumerated() {
    if let h = sha256(of: url) {
        bySha[h, default: []].append(idx)
    }
    if (idx + 1) % 50 == 0 {
        let pct = Int(Double(idx + 1) / Double(n) * 100)
        print("\rTier 1 (file SHA)    \(pct)% (\(idx + 1)/\(n))", terminator: "", to: &standardError)
    }
}
print("", to: &standardError)
for indices in bySha.values where indices.count > 1 {
    for j in indices.dropFirst() {
        uf.union(indices[0], j, tier: 1)
    }
}

// Tier 3 - dHash on one rep per current cluster
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
    if let h = dHash(of: files[idx]) {
        dhashes.append((idx: idx, hash: h))
    }
    if (k + 1) % 25 == 0 {
        let pct = Int(Double(k + 1) / Double(clusterReps.count) * 100)
        print("\rTier 3 (perceptual)  \(pct)% (\(k + 1)/\(clusterReps.count))", terminator: "", to: &standardError)
    }
}
print("", to: &standardError)

// O(n^2) bucketed by top nibble
var buckets: [Int: [Int]] = [:]
for (ci, item) in dhashes.enumerated() {
    let bucket = Int(item.hash >> 60)
    buckets[bucket, default: []].append(ci)
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

// Build clusters
var clustersByRoot: [Int: [Int]] = [:]
for i in 0..<n {
    clustersByRoot[uf.find(i), default: []].append(i)
}
var clustersOut: [(tier: Int, members: [URL])] = []
for (root, idxs) in clustersByRoot where idxs.count >= 2 {
    let tier = uf.tier[root] == 0 ? 1 : uf.tier[root]
    clustersOut.append((tier: tier, members: idxs.map { files[$0] }))
}
clustersOut.sort { $0.members.count > $1.members.count }

print("")
print("Found \(clustersOut.count) duplicate cluster(s):")
for (i, cluster) in clustersOut.enumerated() {
    print("\n[\(i + 1)] Tier \(cluster.tier) - \(cluster.members.count) files")
    for url in cluster.members {
        print("    \(url.path)")
    }
}

