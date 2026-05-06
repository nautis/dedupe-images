// Native Swift CLI for dedupe-images. Tier 1 (file SHA) + Tier 3 (dHash).
// For Tier 2/4/5, Photos.app, Lightroom, batch rename, web review UI -
// use the Python tool dedupe_images.py.

import Foundation
import DedupeCore

struct StandardErrorStream: TextOutputStream {
    func write(_ string: String) {
        FileHandle.standardError.write(string.data(using: .utf8)!)
    }
}
var standardError = StandardErrorStream()

func usage() {
    print("""
    dedupe-images-swift: native Swift port (Tier 1 + Tier 3 only).

    USAGE: dedupe-images-swift [--threshold N] PATH [PATH ...]
    """)
}

let argList = Array(CommandLine.arguments.dropFirst())
var paths: [String] = []
var threshold = 8
var i = 0
while i < argList.count {
    let a = argList[i]
    switch a {
    case "--threshold":
        i += 1
        if i < argList.count, let v = Int(argList[i]) { threshold = v }
    case "-h", "--help":
        usage(); exit(0)
    default:
        paths.append(a)
    }
    i += 1
}
if paths.isEmpty { usage(); exit(1) }

var files: [ScanFile] = []
for p in paths {
    let u = URL(fileURLWithPath: (p as NSString).expandingTildeInPath)
        .standardizedFileURL
    files.append(contentsOf: walkImages(at: u))
}

print("Found \(files.count) candidate images", to: &standardError)

var lastLabel = ""
let clusters = computeClusters(files: files, threshold: threshold) {
    label, cur, total in
    if label != lastLabel {
        if !lastLabel.isEmpty { print("", to: &standardError) }
        lastLabel = label
    }
    if cur % 25 == 0 || cur == total {
        let pct = Int(Double(cur) / Double(total) * 100)
        print("\r\(label)    \(pct)% (\(cur)/\(total))",
              terminator: "", to: &standardError)
    }
}
print("", to: &standardError)

print("\nFound \(clusters.count) duplicate cluster(s):")
for (i, cluster) in clusters.enumerated() {
    print("\n[\(i + 1)] Tier \(cluster.tier) - \(cluster.members.count) files")
    for f in cluster.members {
        print("    \(f.url.path)")
    }
}
