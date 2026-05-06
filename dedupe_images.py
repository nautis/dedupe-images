#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "Pillow>=10.0",
#     "imagehash>=4.3",
#     "pillow-heif>=0.16",
#     "Flask>=3.0",
#     "rawpy>=0.21",
#     "PyMuPDF>=1.24",
# ]
# ///
"""
dedupe-images: three-tier image deduplication with optional web review UI.

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
group into a holding directory; nothing is ever deleted. --review opens a local
web UI where you eyeball each cluster and decide what to keep.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import shutil
import sys
import time
import threading
import urllib.parse
import webbrowser
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

try:
    import rawpy
    HAVE_RAWPY = True
except ImportError:
    HAVE_RAWPY = False

try:
    import fitz  # PyMuPDF
    HAVE_FITZ = True
except ImportError:
    HAVE_FITZ = False

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".heic", ".heif",
    ".webp", ".tiff", ".tif", ".bmp", ".gif",
}
RAW_EXTS = {".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2",
            ".dng", ".raf", ".rw2", ".orf", ".pef", ".rwl", ".x3f"}
PDF_EXTS = {".pdf"}
SCAN_EXTS = IMAGE_EXTS | RAW_EXTS | PDF_EXTS

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
        img = open_canonical_image(path)
        try:
            if hasattr(img, "load"):
                img.load()
            mode = "RGBA" if img.mode in ("RGBA", "LA", "PA") else "RGB"
            canonical = img.convert(mode) if img.mode != mode else img
            h = hashlib.sha256()
            h.update(f"{mode}:{canonical.size[0]}x{canonical.size[1]}:".encode())
            h.update(canonical.tobytes())
            return h.hexdigest()
        finally:
            if hasattr(img, "close"):
                img.close()
    except Exception:
        return None


def perceptual_hashes(path: Path) -> tuple[int, int] | tuple[None, None]:
    try:
        img = open_canonical_image(path)
        try:
            gray = img.convert("L")
            return (
                int(str(imagehash.dhash(gray)), 16),
                int(str(imagehash.phash(gray)), 16),
            )
        finally:
            if hasattr(img, "close"):
                img.close()
    except Exception:
        return (None, None)


def exif_datetime_unix(path: Path) -> int | None:
    """Return EXIF DateTimeOriginal as a unix timestamp, or None if absent.

    Reads via Pillow's _getexif. No timezone handling - EXIF datetimes are
    naive, treated as local time. Good enough for "are these two photos
    within N seconds of each other."
    """
    try:
        with Image.open(path) as img:
            exif = img.getexif()
            if not exif:
                return None
            # 36867 = DateTimeOriginal, 306 = DateTime, 36868 = DateTimeDigitized
            for tag in (36867, 36868, 306):
                v = exif.get(tag)
                if v:
                    break
            else:
                return None
            # Format: "YYYY:MM:DD HH:MM:SS"
            from datetime import datetime
            try:
                dt = datetime.strptime(str(v).strip(), "%Y:%m:%d %H:%M:%S")
                return int(dt.timestamp())
            except ValueError:
                return None
    except Exception:
        return None


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def _glob_to_regex(pattern: str) -> "re.Pattern[str]":
    """Convert a glob pattern with ** support to a compiled regex matching
    full paths. * matches any chars except /, ? matches one char except /,
    ** matches any number of path segments (including zero)."""
    import re as _re
    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*" and i + 1 < len(pattern) and pattern[i + 1] == "*":
            parts.append(r".*")
            i += 2
            if i < len(pattern) and pattern[i] == "/":
                i += 1
        elif c == "*":
            parts.append(r"[^/]*")
            i += 1
        elif c == "?":
            parts.append(r"[^/]")
            i += 1
        else:
            parts.append(_re.escape(c))
            i += 1
    return _re.compile("^" + "".join(parts) + "$")


def is_locked(path: Path, lock_patterns: list["re.Pattern[str]"]) -> bool:
    if not lock_patterns:
        return False
    s = str(path)
    return any(p.match(s) is not None for p in lock_patterns)


# ---------- union-find ----------

class UF:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))
        self.tier = [0] * n

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
        self.parent[ra] = rb
        self.tier[rb] = max(self.tier[ra], self.tier[rb], tier)


# ---------- helpers ----------

def walk_images(roots: list[Path], allow_photos: bool):
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            if not allow_photos and PHOTOS_DERIV_MARKER in dirpath:
                dirnames[:] = []
                continue
            for name in filenames:
                if Path(name).suffix.lower() in SCAN_EXTS:
                    yield Path(dirpath) / name


def open_canonical_image(path: Path) -> "Image.Image":
    """Open path and return a Pillow Image in RGB or RGBA mode.

    Dispatches by extension:
      - RAW (.cr2/.nef/.arw/...): try embedded JPEG preview via rawpy first
        (fast); fall back to libraw render if no preview.
      - PDF: render page 1 at 150 DPI via PyMuPDF.
      - Everything else: Pillow direct.
    """
    ext = path.suffix.lower()

    if ext in RAW_EXTS:
        if not HAVE_RAWPY:
            raise RuntimeError(
                f"RAW file {path} requires rawpy (auto-installed via uv run)")
        with rawpy.imread(str(path)) as raw:
            try:
                # Embedded JPEG preview is much faster than full demosaic.
                thumb = raw.extract_thumb()
                if thumb.format == rawpy.ThumbFormat.JPEG:
                    return Image.open(io.BytesIO(thumb.data)).convert("RGB")
            except (rawpy.LibRawNoThumbnailError, AttributeError):
                pass
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
            return Image.fromarray(rgb, mode="RGB")

    if ext in PDF_EXTS:
        if not HAVE_FITZ:
            raise RuntimeError(
                f"PDF file {path} requires PyMuPDF (auto-installed via uv run)")
        doc = fitz.open(str(path))
        try:
            page = doc.load_page(0)
            mat = fitz.Matrix(150 / 72, 150 / 72)  # 150 DPI
            pix = page.get_pixmap(matrix=mat, alpha=False)
            mode = "RGB" if pix.n == 3 else "RGBA"
            return Image.frombytes(mode, (pix.width, pix.height), pix.samples)
        finally:
            doc.close()

    return Image.open(path)


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


# ---------- core: compute clusters ----------

def compute_clusters(
    roots: list[Path],
    *,
    threshold: int = 8,
    allow_photos: bool = False,
    min_size: int = 1024,
    skip_tier3: bool = False,
    time_gap: int = 0,
    log_progress: bool = True,
) -> dict[int, list[list[tuple[Path, int]]]]:
    """Scan roots, run 3-tier dedup, return {tier: [cluster, ...]} where
    each cluster is a list of (path, size) tuples and tier is the weakest
    joining tier.

    time_gap: if > 0, also unions files whose EXIF DateTimeOriginal is within
    `time_gap` seconds of another file's. Cluster gets labeled tier 4.
    """

    if log_progress:
        print(f"Scanning {len(roots)} root(s)...", file=sys.stderr)
    files: list[tuple[Path, int]] = []
    for f in walk_images(roots, allow_photos):
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        if sz < min_size:
            continue
        files.append((f, sz))
    if log_progress:
        print(f"Found {len(files)} candidate images", file=sys.stderr)
    if not files:
        return {1: [], 2: [], 3: []}

    n = len(files)
    uf = UF(n)

    # Tier 1
    by_sha: dict[str, list[int]] = defaultdict(list)
    for i, (f, _) in enumerate(files):
        try:
            by_sha[sha256_file(f)].append(i)
        except OSError as e:
            if log_progress:
                print(f"\n  read error on {f}: {e}", file=sys.stderr)
        if log_progress and ((i + 1) % 25 == 0 or i == n - 1):
            progress("Tier 1 (file SHA)    ", i + 1, n)
    for indices in by_sha.values():
        for j in indices[1:]:
            uf.union(indices[0], j, tier=1)

    # Tier 2
    tier1_reps = [grp[0] for grp in by_sha.values()]
    by_pixels: dict[str, list[int]] = defaultdict(list)
    for k, idx in enumerate(tier1_reps, 1):
        f, _ = files[idx]
        ph = pixel_sha256(f)
        if ph is not None:
            by_pixels[ph].append(idx)
        if log_progress and (k % 25 == 0 or k == len(tier1_reps)):
            progress("Tier 2 (pixel SHA)   ", k, len(tier1_reps))
    for indices in by_pixels.values():
        for j in indices[1:]:
            uf.union(indices[0], j, tier=2)

    # Tier 3
    if not skip_tier3:
        rep_for_cluster: dict[int, int] = {}
        for i in range(n):
            r = uf.find(i)
            if r not in rep_for_cluster:
                rep_for_cluster[r] = i
        cluster_reps = list(rep_for_cluster.values())

        candidates: list[tuple[int, int, int]] = []
        for k, idx in enumerate(cluster_reps, 1):
            f, _ = files[idx]
            d, p = perceptual_hashes(f)
            if d is not None:
                candidates.append((idx, d, p))
            if log_progress and (k % 25 == 0 or k == len(cluster_reps)):
                progress("Tier 3 (perceptual)  ", k, len(cluster_reps))

        buckets: dict[int, list[int]] = defaultdict(list)
        for ci, (_, d, _) in enumerate(candidates):
            buckets[d >> 60].append(ci)

        for ci, (idx_a, d1, p1) in enumerate(candidates):
            top = d1 >> 60
            adjacent = {b for b in (top - 1, top, top + 1) if 0 <= b <= 0xF}
            for b in adjacent:
                for cj in buckets.get(b, []):
                    if cj <= ci:
                        continue
                    idx_b, d2, p2 = candidates[cj]
                    if hamming(d1, d2) <= threshold and hamming(p1, p2) <= threshold:
                        uf.union(idx_a, idx_b, tier=3)

    # Tier 4: Series of Shots — union files within `time_gap` seconds.
    if time_gap > 0:
        timestamps: list[tuple[int, int]] = []  # (unix_ts, file_idx)
        for i, (f, _) in enumerate(files):
            ts = exif_datetime_unix(f)
            if ts is not None:
                timestamps.append((ts, i))
            if log_progress and ((i + 1) % 50 == 0 or i == n - 1):
                progress("Tier 4 (EXIF time)   ", i + 1, n)
        # Sort by timestamp; sweep with a moving window.
        timestamps.sort()
        for k in range(1, len(timestamps)):
            ts_a, idx_a = timestamps[k - 1]
            ts_b, idx_b = timestamps[k]
            if ts_b - ts_a <= time_gap:
                uf.union(idx_a, idx_b, tier=4)

    # Build clusters
    clusters_by_root: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        clusters_by_root[uf.find(i)].append(i)

    by_tier: dict[int, list[list[tuple[Path, int]]]] = {1: [], 2: [], 3: [], 4: []}
    for root, idxs in clusters_by_root.items():
        if len(idxs) < 2:
            continue
        members = [files[i] for i in idxs]
        members.sort(key=lambda fs: str(fs[0]))
        tier = uf.tier[root] or 1
        by_tier[tier].append(members)

    # Sort clusters within each tier by total size, descending
    for t in by_tier:
        by_tier[t].sort(key=lambda g: -sum(sz for _, sz in g))

    return by_tier


# ---------- review server ----------

REVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>dedupe-images review</title>
<style>
  :root {
    --bg: #1a1a1a; --fg: #eee; --muted: #888;
    --keep: #2d8a3e; --dupe: #a83232; --skip: #555;
    --accent: #4a9eff;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif;
         background: var(--bg); color: var(--fg); }
  header { padding: 12px 24px; background: #222; display: flex;
           justify-content: space-between; align-items: center;
           border-bottom: 1px solid #333; position: sticky; top: 0; z-index: 10; }
  header h1 { margin: 0; font-size: 16px; font-weight: 500; }
  header .meta { font-size: 13px; color: var(--muted); }
  header button { background: var(--accent); color: white; border: 0;
                  padding: 8px 16px; border-radius: 6px; cursor: pointer;
                  font-size: 14px; }
  header button:disabled { background: var(--skip); cursor: not-allowed; }
  header button:hover:not(:disabled) { opacity: 0.9; }
  .nav { display: flex; gap: 8px; align-items: center; }
  .nav button { background: #333; color: var(--fg); padding: 6px 12px; }
  .cluster-info { padding: 16px 24px; font-size: 14px; color: var(--muted); }
  .cluster-info b { color: var(--fg); }
  .grid { display: grid; gap: 16px; padding: 0 24px 24px;
          grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); }
  .card { background: #222; border-radius: 8px; overflow: hidden;
          border: 3px solid transparent; transition: border-color 0.15s; }
  .card.keep { border-color: var(--keep); }
  .card.dupe { border-color: var(--dupe); }
  .card img { width: 100%; height: 360px; object-fit: contain;
              background: #111; cursor: zoom-in; display: block; }
  .card .info { padding: 10px 12px; font-size: 12px; }
  .card .info .num { font-weight: bold; color: var(--accent); margin-right: 6px; }
  .card .info .path { color: var(--muted); word-break: break-all;
                      font-family: ui-monospace, monospace; font-size: 11px; }
  .card .info .size { color: var(--fg); }
  .card .actions { display: flex; }
  .card .actions button { flex: 1; border: 0; padding: 10px;
                          background: #333; color: var(--fg); cursor: pointer;
                          font-size: 13px; }
  .card .actions button:disabled { color: #555; cursor: not-allowed;
                                   background: #2a2a2a; }
  .card .actions .keep-btn:hover:not(:disabled),
  .card.keep .actions .keep-btn { background: var(--keep); }
  .card .actions .dupe-btn:hover:not(:disabled),
  .card.dupe .actions .dupe-btn { background: var(--dupe); }
  .card.locked { border-color: #b8860b; }
  .card .lock-badge { position: absolute; top: 8px; right: 8px;
                      background: rgba(184, 134, 11, 0.9); color: white;
                      padding: 4px 8px; border-radius: 4px; font-size: 11px;
                      font-weight: bold; pointer-events: none; }
  .card { position: relative; }
  .empty { padding: 80px 24px; text-align: center; color: var(--muted); }
  .lightbox { position: fixed; inset: 0; background: rgba(0,0,0,0.95);
              display: none; align-items: center; justify-content: center;
              z-index: 100; cursor: zoom-out; }
  .lightbox.show { display: flex; }
  .lightbox img { max-width: 95vw; max-height: 95vh; object-fit: contain; }
  .help { font-size: 11px; color: var(--muted); padding: 0 24px 16px;
          font-family: ui-monospace, monospace; }
  .help kbd { background: #333; padding: 2px 6px; border-radius: 3px;
              border: 1px solid #444; }
</style>
</head>
<body>
<header>
  <div>
    <h1>dedupe-images review</h1>
    <div class="meta" id="meta">loading...</div>
  </div>
  <div class="nav">
    <button id="prev" title="Previous (←)">←</button>
    <button id="next" title="Next (→ or space)">→</button>
    <button id="commit" title="Commit (c)">Commit moves</button>
  </div>
</header>

<div class="cluster-info" id="clusterInfo"></div>
<div class="help">
  <kbd>1</kbd>–<kbd>9</kbd> mark Nth as keep ·
  <kbd>a</kbd> all keep · <kbd>d</kbd> all dupe ·
  <kbd>←</kbd>/<kbd>→</kbd> nav · <kbd>space</kbd> next · <kbd>c</kbd> commit ·
  click image to zoom
</div>
<div class="grid" id="grid"></div>

<div class="lightbox" id="lightbox" onclick="this.classList.remove('show')">
  <img id="lightboxImg">
</div>

<script>
let clusters = [];
let cur = 0;
let decisions = {};  // path -> "keep" | "dupe"

async function load() {
  const r = await fetch('/api/clusters');
  const data = await r.json();
  clusters = data.clusters;
  decisions = data.defaults;
  if (!clusters.length) {
    document.getElementById('grid').innerHTML =
      '<div class="empty">No duplicate clusters found. Nothing to review.</div>';
    document.getElementById('meta').textContent = '0 clusters';
    document.getElementById('commit').disabled = true;
    return;
  }
  render();
}

function render() {
  const c = clusters[cur];
  const meta = document.getElementById('meta');
  meta.textContent = `cluster ${cur + 1} of ${clusters.length} · tier ${c.tier} · ${c.files.length} files`;

  const info = document.getElementById('clusterInfo');
  info.innerHTML = `<b>${tierLabel(c.tier)}</b> · pick what to keep, the rest move to <b>${c.quarantine}</b>`;

  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  c.files.forEach((f, i) => {
    const card = document.createElement('div');
    let cls = 'card ' + (decisions[f.path] || 'keep');
    if (f.locked) cls += ' locked';
    card.className = cls;
    const lockBadge = f.locked ? '<div class="lock-badge">LOCKED</div>' : '';
    card.innerHTML = `
      ${lockBadge}
      <img loading="lazy" src="/api/thumb?path=${encodeURIComponent(f.path)}&w=720"
           data-full="/api/image?path=${encodeURIComponent(f.path)}">
      <div class="info">
        <div><span class="num">${i + 1}</span><span class="size">${fmtBytes(f.size)}</span></div>
        <div class="path">${f.path}</div>
      </div>
      <div class="actions">
        <button class="keep-btn">Keep</button>
        <button class="dupe-btn"${f.locked ? ' disabled title="locked by --lock-glob"' : ''}>Dupe</button>
      </div>`;
    card.querySelector('.keep-btn').onclick = () => mark(f.path, 'keep');
    if (!f.locked) {
      card.querySelector('.dupe-btn').onclick = () => mark(f.path, 'dupe');
    }
    card.querySelector('img').onclick = (e) => {
      const lb = document.getElementById('lightbox');
      document.getElementById('lightboxImg').src = e.target.dataset.full;
      lb.classList.add('show');
    };
    grid.appendChild(card);
  });
  updateCommit();
}

function tierLabel(t) {
  return {
    1: 'Tier 1 — byte-identical',
    2: 'Tier 2 — same pixels, metadata differs',
    3: 'Tier 3 — perceptually similar (eyeball this)',
    4: 'Tier 4 — series of shots (eyeball this; subjects may differ)',
  }[t] || 'cluster';
}

function fmtBytes(n) {
  const u = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + ' ' + u[i];
}

function mark(path, action) {
  decisions[path] = action;
  render();
}

function markAll(action) {
  clusters[cur].files.forEach(f => {
    if (f.locked) { decisions[f.path] = 'keep'; return; }
    decisions[f.path] = action;
  });
  render();
}

function pickOnly(idx) {
  const c = clusters[cur];
  c.files.forEach((f, i) => {
    if (f.locked) { decisions[f.path] = 'keep'; return; }
    decisions[f.path] = (i === idx) ? 'keep' : 'dupe';
  });
  render();
}

function go(delta) {
  cur = Math.max(0, Math.min(clusters.length - 1, cur + delta));
  render();
}

function updateCommit() {
  const total = Object.values(decisions).filter(d => d === 'dupe').length;
  document.getElementById('commit').textContent =
    total > 0 ? `Commit (${total} moves)` : 'Commit (nothing to move)';
  document.getElementById('commit').disabled = total === 0;
}

async function commit() {
  if (!confirm(`Move ${Object.values(decisions).filter(d => d === 'dupe').length} files to quarantine?`)) return;
  const r = await fetch('/api/commit', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({decisions}),
  });
  const data = await r.json();
  document.body.innerHTML = `<div class="empty"><h2>Done.</h2>
    <p>${data.moved} file(s) moved · ${data.bytes} bytes reclaimable</p>
    <p>Quarantine: <code>${data.quarantine}</code></p>
    <p>You can close this tab.</p></div>`;
  setTimeout(() => fetch('/api/shutdown', {method: 'POST'}).catch(()=>{}), 500);
}

document.getElementById('prev').onclick = () => go(-1);
document.getElementById('next').onclick = () => go(1);
document.getElementById('commit').onclick = commit;

document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (document.getElementById('lightbox').classList.contains('show')) {
    if (e.key === 'Escape') document.getElementById('lightbox').classList.remove('show');
    return;
  }
  if (e.key === 'ArrowLeft') go(-1);
  else if (e.key === 'ArrowRight' || e.key === ' ') { e.preventDefault(); go(1); }
  else if (e.key === 'a') markAll('keep');
  else if (e.key === 'd') markAll('dupe');
  else if (e.key === 'c') commit();
  else if (e.key >= '1' && e.key <= '9') {
    const idx = parseInt(e.key) - 1;
    if (clusters[cur] && idx < clusters[cur].files.length) pickOnly(idx);
  }
});

load();
</script>
</body>
</html>"""


def serialize_clusters_for_review(
    by_tier: dict[int, list[list[tuple[Path, int]]]],
    keep_strategy: str,
    quarantine: Path,
    lock_patterns: list,
) -> tuple[list[dict], dict[str, str]]:
    """Flatten by_tier into a list of cluster dicts and a default-decisions map.
    Locked files (matching --lock-glob) are always defaulted to keep."""
    clusters = []
    defaults: dict[str, str] = {}
    for tier in (1, 2, 3, 4):
        for group in by_tier[tier]:
            locked_idx = [i for i, (f, _) in enumerate(group)
                          if is_locked(f, lock_patterns)]
            if locked_idx:
                keep_i = locked_idx[0]
            else:
                keep_i = keep_index(group, keep_strategy)
            files_payload = [
                {"path": str(f), "size": sz, "locked": is_locked(f, lock_patterns)}
                for f, sz in group
            ]
            clusters.append({
                "tier": tier,
                "files": files_payload,
                "quarantine": str(quarantine),
            })
            for i, (f, _) in enumerate(group):
                if is_locked(f, lock_patterns):
                    defaults[str(f)] = "keep"
                else:
                    defaults[str(f)] = "keep" if i == keep_i else "dupe"
    return clusters, defaults


def run_review_server(
    by_tier: dict[int, list[list[tuple[Path, int]]]],
    quarantine: Path,
    keep_strategy: str,
    flat: bool,
    port: int,
    lock_patterns: list | None = None,
) -> int:
    lock_patterns = lock_patterns or []
    try:
        from flask import Flask, abort, jsonify, request, Response
    except ImportError:
        print("Flask not available. Run via `uv run` to auto-install.", file=sys.stderr)
        return 1

    # Path whitelist: only paths from the actual scan can be served.
    allowed_paths: set[str] = set()
    for tier in (1, 2, 3, 4):
        for group in by_tier[tier]:
            for f, _ in group:
                allowed_paths.add(str(f.resolve()))

    clusters, defaults = serialize_clusters_for_review(
        by_tier, keep_strategy, quarantine, lock_patterns)
    locked_set: set[str] = {
        str(f.resolve()) for tier in by_tier.values()
        for group in tier for f, _ in group
        if is_locked(f, lock_patterns)
    }
    shutdown_event = threading.Event()

    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(REVIEW_HTML, mimetype="text/html; charset=utf-8")

    @app.route("/api/clusters")
    def api_clusters():
        return jsonify({"clusters": clusters, "defaults": defaults})

    def resolve_allowed(qpath: str) -> Path:
        try:
            p = Path(urllib.parse.unquote(qpath)).resolve()
        except Exception:
            abort(400)
        if str(p) not in allowed_paths:
            abort(403)
        return p

    @app.route("/api/image")
    def api_image():
        qpath = request.args.get("path", "")
        p = resolve_allowed(qpath)
        if not p.exists():
            abort(404)
        ext = p.suffix.lower()
        # RAW and PDF: render canonical as JPEG so the browser can display it.
        if ext in RAW_EXTS or ext in PDF_EXTS:
            try:
                img = open_canonical_image(p)
                try:
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG", quality=92)
                    return Response(buf.getvalue(), mimetype="image/jpeg",
                                    headers={"Cache-Control": "max-age=3600"})
                finally:
                    if hasattr(img, "close"):
                        img.close()
            except Exception as e:
                print(f"  image render error on {p}: {e}", file=sys.stderr)
                abort(500)
        ext_key = ext.lstrip(".")
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
                "tif": "image/tiff", "tiff": "image/tiff",
                "heic": "image/heic", "heif": "image/heif"}.get(
                    ext_key, "application/octet-stream")
        return Response(p.read_bytes(), mimetype=mime)

    @app.route("/api/thumb")
    def api_thumb():
        qpath = request.args.get("path", "")
        try:
            w = int(request.args.get("w", "720"))
        except ValueError:
            w = 720
        w = max(64, min(2048, w))
        p = resolve_allowed(qpath)
        if not p.exists():
            abort(404)
        try:
            img = open_canonical_image(p)
            try:
                img.thumbnail((w, w * 4), Image.Resampling.LANCZOS)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=82)
                return Response(buf.getvalue(), mimetype="image/jpeg",
                                headers={"Cache-Control": "max-age=3600"})
            finally:
                if hasattr(img, "close"):
                    img.close()
        except Exception as e:
            print(f"  thumb error on {p}: {e}", file=sys.stderr)
            abort(500)

    @app.route("/api/commit", methods=["POST"])
    def api_commit():
        payload = request.get_json(silent=True) or {}
        decisions = payload.get("decisions", {})
        moves: list[tuple[Path, int]] = []
        skipped_locked = 0
        for path_str, action in decisions.items():
            if action != "dupe":
                continue
            if path_str not in allowed_paths:
                continue
            resolved_str = str(Path(path_str).resolve())
            if resolved_str in locked_set:
                skipped_locked += 1
                continue
            p = Path(path_str)
            if not p.exists():
                continue
            try:
                moves.append((p, p.stat().st_size))
            except OSError:
                continue

        q = quarantine.expanduser().resolve()
        q.mkdir(parents=True, exist_ok=True)
        moved = 0
        moved_bytes = 0
        for f, sz in moves:
            target = q / f.name if flat else q / Path(*f.parts[1:])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                target = target.with_name(
                    f"{target.stem}.{int(time.time())}{target.suffix}"
                )
            try:
                shutil.move(str(f), str(target))
                moved += 1
                moved_bytes += sz
            except OSError as e:
                print(f"  move failed: {f}: {e}", file=sys.stderr)
        print(f"\nQuarantined {moved} file(s), {fmt_bytes(moved_bytes)} -> {q}",
              file=sys.stderr)
        return jsonify({"moved": moved, "bytes": fmt_bytes(moved_bytes),
                        "quarantine": str(q), "skipped_locked": skipped_locked})

    @app.route("/api/shutdown", methods=["POST"])
    def api_shutdown():
        shutdown_event.set()
        return jsonify({"ok": True})

    # Start server in background thread so we can wait on shutdown_event in main.
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, app, threaded=True)
    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/"

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    print(f"\n  Review UI: {url}", file=sys.stderr)
    print(f"  Open it in your browser to review {len(clusters)} cluster(s).", file=sys.stderr)
    print(f"  Press Ctrl-C in this terminal to stop the server.\n", file=sys.stderr)
    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        # Wait for either /api/shutdown or Ctrl-C
        while not shutdown_event.wait(timeout=0.5):
            pass
    except KeyboardInterrupt:
        print("\nShutting down.", file=sys.stderr)
    finally:
        server.shutdown()

    return 0


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Three-tier image deduplication with optional web review UI.",
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
                    help="Move all-but-one of each cluster to this dir (no deletes). "
                         "Required with --review.")
    ap.add_argument("--keep", choices=["oldest", "newest", "largest", "smallest", "first"],
                    default="largest", help="Which file in each cluster to keep (default: largest).")
    ap.add_argument("--json", type=Path, default=None, help="Write JSON report to this path.")
    ap.add_argument("--min-size", type=int, default=1024,
                    help="Skip files smaller than this many bytes (default: 1024).")
    ap.add_argument("--dry-run", action="store_true",
                    help="With --quarantine (CLI mode), print what would move without moving.")
    ap.add_argument("--flat", action="store_true",
                    help="With --quarantine, drop files at the top level instead of "
                         "preserving their absolute path.")
    ap.add_argument("--skip-tier3", action="store_true",
                    help="Skip perceptual hashing. Much faster on huge sets.")
    ap.add_argument("--time-gap", type=int, default=0, metavar="SECONDS",
                    help="Series of Shots: also cluster files whose EXIF "
                         "DateTimeOriginal is within SECONDS of another file. "
                         "Default 0 (off). Try 3 for burst photography.")
    ap.add_argument("--lock-glob", action="append", default=[], metavar="PATTERN",
                    help="Files matching this glob pattern (relative to a scan root, "
                         "supports **) are treated as 'locked' and cannot be moved. "
                         "Repeatable. Example: --lock-glob '**/keepers/*'")
    ap.add_argument("--review", action="store_true",
                    help="Open a web UI to eyeball each cluster and pick what to keep. "
                         "Requires --quarantine. Default --keep choice pre-selects, you override.")
    ap.add_argument("--port", type=int, default=0,
                    help="Port for --review server. Default 0 = auto-pick free port.")
    args = ap.parse_args()

    roots = [Path(p).expanduser().resolve() for p in args.paths]
    for r in roots:
        if not r.exists():
            print(f"Path not found: {r}", file=sys.stderr)
            return 1

    if args.review and args.quarantine is None:
        print("--review requires --quarantine to know where dupes go.", file=sys.stderr)
        return 1

    lock_patterns = [_glob_to_regex(p) for p in args.lock_glob]

    by_tier = compute_clusters(
        roots,
        threshold=args.threshold,
        allow_photos=args.allow_photos_internals,
        min_size=args.min_size,
        skip_tier3=args.skip_tier3,
        time_gap=args.time_gap,
    )

    if not any(by_tier.values()):
        print("\nNo duplicate clusters found.", file=sys.stderr)
        return 0

    # Summary
    def reclaimable(groups):
        return sum(sum(sz for _, sz in g) - max(sz for _, sz in g) for g in groups)

    print()
    labels = {
        1: "Tier 1 (byte-identical)              ",
        2: "Tier 2 (same pixels, metadata differs)",
        3: f"Tier 3 (perceptual <={args.threshold:>2d})              ",
        4: f"Tier 4 (series of shots <={args.time_gap}s)         ",
    }
    for t in (1, 2, 3, 4):
        if t == 3 and args.skip_tier3:
            continue
        if t == 4 and args.time_gap == 0:
            continue
        groups = by_tier[t]
        print(f"{labels[t]} {len(groups):4d} clusters, "
              f"{sum(len(g) - 1 for g in groups):4d} extras, "
              f"{fmt_bytes(reclaimable(groups)):>10s} reclaimable")

    # --review takes the interactive path and returns
    if args.review:
        return run_review_server(
            by_tier=by_tier,
            quarantine=args.quarantine,
            keep_strategy=args.keep,
            flat=args.flat,
            port=args.port,
            lock_patterns=lock_patterns,
        )

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
            "tier4_series_of_shots": [
                [{"path": str(f), "size": sz} for f, sz in g] for g in by_tier[4]
            ],
        }
        with open(args.json, "w") as fp:
            json.dump(payload, fp, indent=2)
        print(f"\nJSON report: {args.json}", file=sys.stderr)
    elif not args.quarantine:
        print()
        for t in (1, 2, 3, 4):
            groups = by_tier[t]
            if not groups:
                continue
            label = {1: "TIER 1 - byte-identical",
                     2: "TIER 2 - same pixels, metadata differs",
                     3: "TIER 3 - perceptually similar",
                     4: "TIER 4 - series of shots (EXIF time-window)"}[t]
            print(f"=== {label} ===")
            for g in groups:
                keep_i = keep_index(g, args.keep)
                for i, (f, sz) in enumerate(g):
                    marker = "KEEP" if i == keep_i else "DUPE"
                    print(f"  [{marker}] {fmt_bytes(sz):>9s}  {f}")
                print()

    # Quarantine (CLI mode, non-interactive)
    if args.quarantine:
        q = args.quarantine.expanduser().resolve()
        if not args.dry_run:
            q.mkdir(parents=True, exist_ok=True)
        moved = 0
        moved_bytes = 0
        skipped_locked = 0
        for t in (1, 2, 3, 4):
            for group in by_tier[t]:
                # Force locked files into the keep set; pick keep among them if any.
                locked_idx = [i for i, (f, _) in enumerate(group) if is_locked(f, lock_patterns)]
                if locked_idx:
                    keep_i = locked_idx[0]
                else:
                    keep_i = keep_index(group, args.keep)
                for i, (f, sz) in enumerate(group):
                    if i == keep_i:
                        continue
                    if is_locked(f, lock_patterns):
                        skipped_locked += 1
                        continue
                    target = q / f.name if args.flat else q / Path(*f.parts[1:])
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
        if skipped_locked:
            print(f"Skipped {skipped_locked} locked file(s) "
                  f"(matched --lock-glob).", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
