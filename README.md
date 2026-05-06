# dedupe-images

A small Python CLI that finds duplicate images across **three tiers**, in order of cost. Each tier catches dupes the previous tier misses.

| Tier | Catches | How |
|------|---------|-----|
| 1 | Byte-identical files | SHA-256 of the file (what most "find duplicates" tools do) |
| 2 | Same pixels, different metadata | SHA-256 of the **decoded pixel buffer**. Catches the case where two JPEGs render to the same image but have different EXIF, embedded thumbnails, color profile, orientation tag, etc. |
| 3 | Perceptually similar | dHash + pHash with a Hamming-distance threshold. Catches re-encodes, slight quality changes, resaved versions, even minor crops. |

Most generic dedup tools only do tier 1. The reason two of your iPad screenshots from the Photos library showed up as "different" in Deduplicate File but looked identical: their JPEG bitstreams are the same but the EXIF blocks differ. Tier 2 catches that exactly.

### How clusters are labeled

Each duplicate cluster appears under exactly one tier. The label is the **weakest** tier that joined any pair of files inside the cluster - your confidence floor.

Example: A and C are byte-identical, A/B/C are pixel-identical, and D is perceptually similar to A. All four end up in one cluster. The cluster reports under **Tier 3**, because Tier 3 is the weakest link (D's connection). Inside the cluster you still get exactly one keeper and three dupes, but the label tells you "be skeptical: at least one pair in here is only a perceptual match, so review by eye before destructive action."

If you want only the higher-confidence relationships, run `--skip-tier3`. The same A/B/C/D set then reports A/B/C as a Tier 2 cluster and D drops out entirely.

## Web review UI

Run with `--review` to eyeball each cluster in your browser before any files move:

```bash
uv run ~/dedupe-images/dedupe_images.py --review --quarantine ~/dupe-quarantine --flat ~/Pictures
```

This pops a local web page where you scroll through clusters, see all images side-by-side, and decide what to keep. The default `--keep` strategy pre-marks one file per cluster; you override with a click or keystroke.

**Keyboard shortcuts:**
- `1`–`9` mark the Nth image as keep, others as dupe
- `a` mark all in cluster as keep · `d` mark all as dupe
- `←` / `→` (or space) navigate clusters
- `c` commit moves
- click any image to zoom

The server runs on a random local port (override with `--port N`) and shuts down after you commit. Nothing is moved until you click commit.

## Default behavior is safe

- **Nothing is deleted.** Default action is a printed report. Add `--quarantine <dir>` to **move** all-but-one of each duplicate group into a holding directory you can review and empty yourself.
- **Photos library derivatives are skipped by default.** `*.photoslibrary/resources/derivatives/` is Photos.app's auto-generated cache. Deleting from it either gets regenerated (no space saved) or breaks the library. Pass `--allow-photos-internals` to override.

## Install and run

The script has [PEP 723 inline metadata](https://peps.python.org/pep-0723/), so the simplest way is `uv`:

```bash
uv run dedupe_images.py ~/Pictures
```

That auto-installs Pillow, imagehash, and pillow-heif into a per-script cache. Don't have `uv`? `brew install uv`.

Plain pip works too:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./dedupe_images.py ~/Pictures
```

## Usage

```
dedupe_images.py [options] PATH [PATH ...]

  --threshold N              Hamming distance for perceptual match. Default 8.
                             0 = pixel-equivalent after rescale; 4-8 = visibly
                             the same; >12 = loose.
  --allow-photos-internals   Scan inside *.photoslibrary/resources/derivatives/.
                             Off by default.
  --quarantine DIR           Move all-but-one of each duplicate group to DIR.
                             Absolute paths preserved underneath. No deletes.
  --keep STRATEGY            Which file to keep in each group:
                             oldest, newest, largest (default), smallest, first.
  --json PATH                Write a JSON report instead of printing groups.
  --min-size BYTES           Skip files smaller than this. Default 1024.
  --skip-tier3               Skip perceptual hashing. Much faster on huge sets.
  --dry-run                  With --quarantine, show what would move.
```

## Examples

Plain report on a folder:

```bash
uv run dedupe_images.py ~/Pictures/Exports
```

Just the cheap tiers (no perceptual), JSON to a file:

```bash
uv run dedupe_images.py --skip-tier3 --json /tmp/dupes.json ~/Pictures
```

Move every dupe to a holding pen, keeping the largest of each group:

```bash
uv run dedupe_images.py --quarantine ~/dupe-quarantine --keep largest ~/Pictures
```

Tighter perceptual threshold (only near-identical re-encodes):

```bash
uv run dedupe_images.py --threshold 4 ~/Pictures
```

Dry-run a quarantine move first:

```bash
uv run dedupe_images.py --quarantine ~/dupe-quarantine --dry-run ~/Pictures
```

## Output

Without `--json`, the script prints one block per tier with each group spelled out:

```
=== TIER 2 - same pixels, metadata differs ===
  [KEEP]  182.2 KB  /Users/me/.../4F18374F-...jpeg
  [DUPE]  180.4 KB  /Users/me/.../768112CD-...jpeg
```

The `[KEEP]` marker reflects your `--keep` strategy. With `--quarantine`, the `[DUPE]` files are the ones that move.

With `--json`, you get a structured report keyed by tier, with each group's files and their sizes. Suitable for piping into other tooling.

## Performance notes

- Tier 1 (file SHA) reads every byte of every file. Speed is bound by your disk - SSDs handle ~50K mid-size JPEGs/min easily.
- Tier 2 only runs on tier-1 representatives (one file per byte-identical cluster), so duplicates don't get decoded twice.
- Tier 3 is bucketed by the top nibble of the dHash to cut the comparison count, but it's still O(n^2) in the worst case. For libraries above ~50K unique images, use `--skip-tier3`, or run tier 3 on a narrower path.

## Caveats

- Perceptual hashing is content-based. Two photos of the same scene from a slightly different angle may match at threshold 12+ even though they're genuinely different shots. Default threshold is 8, which is conservative.
- Two files that are visually different but happen to hash to similar values (collision) is rare but possible. Always review tier-3 groups by eye before any destructive action - which is why this tool's destructive action is "move to quarantine" instead of "delete."
- HEIC support requires `pillow-heif`, included by default. If pillow-heif fails to install on your machine, the script still runs against JPEG/PNG/WebP/etc.

## License

MIT.
