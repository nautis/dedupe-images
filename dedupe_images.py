#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "Pillow>=10.0",
#     "imagehash>=4.3",
#     "pillow-heif>=0.16",
# ]
# ///
"""
dedupe-images: three-tier image deduplication.

Tier 1 - byte-identical: SHA-256 of the file.
Tier 2 - same pixels, metadata differs: SHA-256 of the decoded pixel buffer.
         Catches the case where two JPEGs render to the same image but have
         different EXIF, thumbnails, color profile, etc.
Tier 3 - perceptually similar: dHash + pHash with Hamming-distance threshold.
         Catches re-encodes, slight quality changes, resaved versions.

Each file ends up in at most one cluster. The cluster is labeled with the
weakest tier that joined any pair of files inside it - so a cluster where some
files are byte-identical and others only pixel-identical is labeled Tier 2.

Default action is report-only. --quarantine moves all-but-one of each duplicate
group into a holding directory; nothing is ever deleted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections import defaultdict
from pathlib import Path

try:
    from PIL import Image
    import imagehash
except ImportError:
    sys.stderr.write(
        "Missing dependencies. Either:\n"
        "  uv run dedupe_images.py ...   (auto-installs from PEP 723 header)\n"
        "or:\n"
        "  pip install -r requirements.txt\n"
    )
    sys.exit(1)

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".webp", ".tiff", ".tif", ".bmp", ".gif",
}
PHOTOS_DERIV_MARKER = ".photoslibrary/resources/derivatives"


# ---------- hashing ----------

def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def pixel_sha256(path: Path) -> str | None:
    try:
        with Image.open(path) as img:
            img.load()
            mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
            canonical = img.convert(mode)
            h = hashlib.sha256()
            h.update(f"{mode}:{canonical.size[0]}x{canonical.size[1]}:".encode())
            h.update(canonical.tobytes())
            return h.hexdigest()
    except Exception:
        return None


def perceptual_hashes(path: Path) -> tuple[int, int] | tuple[None, None]:
    try:
        with Image.open(path) as img:
            gray = img.convert("L")
            return (
                int(str(imagehash.dhash(gray)), 16),
                int(str(imagehash.phash(gray)), 16),
            )
    except Exception:
        return (None, None)


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# ---------- union-find over file indexes ----------

class UF:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.tier = [0] * n  # weakest tier of any union touching this root

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int, tier: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            self.tier[ra] = max(self.tier[ra], tier)
            return
        # attach smaller-index root to larger; simple, stable
        self.parent[ra] = rb
        self.tier[rb] = max(self.tier[ra], self.tier[rb], tier)


# ---------- file walking and helpers ----------

def walk_images(roots: list[Path], allow_photos: bool):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if not allow_photos and PHOTOS_DERIV_MARKER in dirpath:
                dirnames[:] = []
                continue
            for name in filenames:
                if Path(name).suffix.lower() in IMAGE_EXTS:
                    yield Path(dirpath) / name


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def progress(label: str, cur: int, total: int) -> None:
    if total == 0:
        return
    width = 30
    filled = int(cur / total * width)
    bar = "█" * filled + "░" * (width - filled)
    sys.stderr.write(f"\r{label} [{bar}] {cur}/{total}")
    sys.stderr.flush()
    if cur == total:
        sys.stderr.write("\n")


def keep_index(group: list[tuple[Path, int]], strategy: str) -> int:
    if strategy == "first":
        return 0
    if strategy in ("oldest", "newest"):
        try:
            stats = [f.stat().st_mtime for f, _ in group]
        except OSError:
            return 0
        return (min if strategy == "oldest" else max)(range(len(group)), key=lambda i: stats[i])
    if strategy == "largest":
        return max(range(len(group)), key=lambda i: group[i][1])
    if strategy == "smallest":
        return min(range(len(group)), key=lambda i: group[i][1])
    return 0


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Three-tier image deduplication.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("paths", nargs="+", help="Directories to scan")
    ap.add_argument("--threshold", type=int, default=8,
                    help="Max Hamming distance for perceptual match (default: 8). "
                         "0 = pixel-equivalent after rescale; 4-8 = visibly the same; >12 = loose.")
    ap.add_argument("--allow-photos-internals", action="store_true",
                    help="Scan inside *.photoslibrary/resources/derivatives/. Off by default.")
    ap.add_argument("--quarantine", type=Path, default=None,
                    help="Move all-but-one of each cluster to this dir (no deletes).")
    ap.add_argument("--keep", choices=["oldest", "newest", "largest", "smallest", "first"],
                    default="largest", help="Which file in each cluster to keep (default: largest).")
    ap.add_argument("--json", type=Path, default=None, help="Write JSON report to this path.")
    ap.add_argument("--min-size", type=int, default=1024,
                    help="Skip files smaller than this many bytes (default: 1024).")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --quarantine, print what would move without moving anything.")
    ap.add_argument("--skip-tier3", action="store_true",
                    help="Skip perceptual hashing. Much faster on huge sets.")
    args = ap.parse_args()

    roots = [Path(p).expanduser().resolve() for p in args.paths]
    for r in roots:
        if not r.exists():
            print(f"Path not found: {r}", file=sys.stderr)
            return 1

    print(f"Scanning {len(roots)} root(s)...", file=sys.stderr)
    files: list[tuple[Path, int]] = []
    for f in walk_images(roots, args.allow_photos_internals):
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        if sz < args.min_size:
            continue
        files.append((f, sz))
    print(f"Found {len(files)} candidate images", file=sys.stderr)
    if not files:
        return 0

    n = len(files)
    uf = UF(n)

    # Tier 1: file SHA
    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, (f, _) in enumerate(files):
        try:
            by_sha[sha256_file(f)].append(i)
        except OSError as e:
            print(f"\n  read error on {f}: {e}", file=sys.stderr)
        if (i + 1) % 25 == 0 or i == n - 1:
            progress("Tier 1 (file SHA)    ", i + 1, n)
    for indices in by_sha.values():
        for j in indices[1:]:
            uf.union(indices[0], j, tier=1)

    # Tier 2: pixel SHA on one representative per tier-1 cluster.
    # Compute on UF roots that have at least one entry, but here we just iterate
    # representatives = one index per file SHA group (since multiple files with
    # the same file SHA are already unified at tier 1).
    tier1_reps = [grp[0] for grp in by_sha.values()]
    by_pixels: dict[str, list[int]] = defaultdict(list)
    for k, idx in enumerate(tier1_reps, 1):
        f, _ = files[idx]
        ph = pixel_sha256(f)
        if ph is not None:
            by_pixels[ph].append(idx)
        if k % 25 == 0 or k == len(tier1_reps):
            progress("Tier 2 (pixel SHA)   ", k, len(tier1_reps))
    for indices in by_pixels.values():
        for j in indices[1:]:
            uf.union(indices[0], j, tier=2)

    # Tier 3: perceptual hashing on one rep per current cluster.
    if not args.skip_tier3:
        # Pick one rep per UF cluster (smallest index is fine).
        rep_for_cluster: dict[int, int] = {}
        for i in range(n):
            r = uf.find(i)
            if r not in rep_for_cluster:
                rep_for_cluster[r] = i
        cluster_reps = list(rep_for_cluster.values())

        candidates: list[tuple[int, int, int]] = []  # (file_idx, dhash, phash)
        for k, idx in enumerate(cluster_reps, 1):
            f, _ = files[idx]
            d, p = perceptual_hashes(f)
            if d is not None:
                candidates.append((idx, d, p))
            if k % 25 == 0 or k == len(cluster_reps):
                progress("Tier 3 (perceptual)  ", k, len(cluster_reps))

        # Bucket by top nibble of dHash to reduce comparison count
        buckets: dict[int, list[int]] = defaultdict(list)
        for ci, (_, d, _) in enumerate(candidates):
            buckets[d >> 60].append(ci)

        thr = args.threshold
        for ci, (idx_a, d1, p1) in enumerate(candidates):
            top = d1 >> 60
            adjacent = {b for b in (top - 1, top, top + 1) if 0 <= b <= 0xF}
            for b in adjacent:
                for cj in buckets.get(b, []):
                    if cj <= ci:
                        continue
                    idx_b, d2, p2 = candidates[cj]
                    if hamming(d1, d2) <= thr and hamming(p1, p2) <= thr:
                        uf.union(idx_a, idx_b, tier=3)

    # Build clusters from UF.
    clusters_by_root: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters_by_root[uf.find(i)].append(i)

    # Filter to dupe clusters and bucket by tier.
    by_tier: dict[int, list[list[tuple[Path, int]]]] = {1: [], 2: [], 3: []}
    for root, idxs in clusters_by_root.items():
        if len(idxs) < 2:
            continue
        members = [files[i] for i in idxs]
        # Within a cluster, find the byte-identical sub-clusters for sub-reporting
        members.sort(key=lambda fs: str(fs[0]))
        tier = uf.tier[root]
        if tier == 0:
            tier = 1
        by_tier[tier].append(members)

    # Summary
    def reclaimable(groups: list[list[tuple[Path, int]]]) -> int:
        return sum(sum(sz for _, sz in g) - max(sz for _, sz in g) for g in groups)

    print()
    labels = {
        1: "Tier 1 (byte-identical)              ",
        2: "Tier 2 (same pixels, metadata differs)",
        3: f"Tier 3 (perceptual <={args.threshold:>2d})              ",
    }
    for t in (1, 2, 3):
        if t == 3 and args.skip_tier3:
            continue
        groups = by_tier[t]
        n_groups = len(groups)
        n_extras = sum(len(g) - 1 for g in groups)
        s = reclaimable(groups)
        print(f"{labels[t]} {n_groups:4d} clusters, {n_extras:4d} extras, "
              f"{fmt_bytes(s):>10s} reclaimable")

    # JSON report
    if args.json:
        payload = {
            "tier1_byte_identical": [
                [{"path": str(f), "size": sz} for f, sz in g] for g in by_tier[1]
            ],
            "tier2_pixel_identical_metadata_differs": [
                [{"path": str(f), "size": sz} for f, sz in g] for g in by_tier[2]
            ],
            "tier3_perceptually_similar": [
                [{"path": str(f), "size": sz} for f, sz in g] for g in by_tier[3]
            ],
        }
        with open(args.json, "w") as fp:
            json.dump(payload, fp, indent=2)
        print(f"\nJSON report: {args.json}", file=sys.stderr)
    elif any(by_tier.values()):
        print()
        for t in (1, 2, 3):
            groups = by_tier[t]
            if not groups:
                continue
            label = {1: "TIER 1 - byte-identical",
                     2: "TIER 2 - same pixels, metadata differs",
                     3: "TIER 3 - perceptually similar"}[t]
            print(f"=== {label} ===")
            for g in groups:
                keep_i = keep_index(g, args.keep)
                for i, (f, sz) in enumerate(g):
                    marker = "KEEP" if i == keep_i else "DUPE"
                    print(f"  [{marker}] {fmt_bytes(sz):>9s}  {f}")
                print()

    # Quarantine: walk every cluster exactly once.
    if args.quarantine:
        q = args.quarantine.expanduser().resolve()
        if not args.dry_run:
            q.mkdir(parents=True, exist_ok=True)
        moved = 0
        moved_bytes = 0
        for t in (1, 2, 3):
            for group in by_tier[t]:
                keep_i = keep_index(group, args.keep)
                for i, (f, sz) in enumerate(group):
                    if i == keep_i:
                        continue
                    target = q / Path(*f.parts[1:])
                    if args.dry_run:
                        print(f"[dry-run] would move {f} -> {target}", file=sys.stderr)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        if target.exists():
                            target = target.with_name(
                                f"{target.stem}.{int(time.time())}{target.suffix}"
                            )
                        try:
                            shutil.move(str(f), str(target))
                        except OSError as e:
                            print(f"  move failed: {f}: {e}", file=sys.stderr)
                            continue
                    moved += 1
                    moved_bytes += sz
        verb = "Would quarantine" if args.dry_run else "Quarantined"
        print(f"\n{verb} {moved} file(s), {fmt_bytes(moved_bytes)} -> {q}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
