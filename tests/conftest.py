"""Pytest fixtures for dedupe-images tests.

Builds small synthetic JPEGs in a tmp dir per test:
- A and B: same pixels, different EXIF (Tier 2)
- C: byte-identical to A (Tier 1)
- D: re-encoded at lower quality (Tier 3)
- E and F: distinct controls (no match)
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import piexif
    HAVE_PIEXIF = True
except ImportError:
    HAVE_PIEXIF = False


def _screenshot_image() -> Image.Image:
    img = Image.new("RGB", (640, 400), (240, 240, 240))
    d = ImageDraw.Draw(img)
    for i in range(0, 640, 8):
        d.line([(i, 0), (i, 400)], fill=(160, 100, 50), width=1)
    d.text((40, 60), "Test Subject A", fill=(20, 20, 20))
    d.text((40, 120), "Some Caption", fill=(180, 30, 30))
    d.rectangle([(440, 220), (600, 320)], outline=(220, 50, 50), width=4)
    return img


def _solid_image(color: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (640, 400), color)
    d = ImageDraw.Draw(img)
    d.ellipse([(200, 100), (440, 300)], fill=(255, 255, 255))
    d.text((240, 180), "CONTROL", fill=(0, 0, 0))
    return img


def _noise_image() -> Image.Image:
    import random
    rng = random.Random(42)
    img = Image.new("RGB", (640, 400))
    px = img.load()
    for y in range(400):
        for x in range(640):
            px[x, y] = (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
    return img


def _save_with_exif(img: Image.Image, path: Path, software: str, datetime_str: str):
    if not HAVE_PIEXIF:
        img.save(path, "JPEG", quality=92)
        return
    exif_dict = {
        "0th": {
            piexif.ImageIFD.Software: software.encode("utf-8"),
            piexif.ImageIFD.DateTime: datetime_str.encode("utf-8"),
            piexif.ImageIFD.Make: b"FixtureCam",
            piexif.ImageIFD.Model: b"Synthetic",
        },
        "Exif": {
            piexif.ExifIFD.DateTimeOriginal: datetime_str.encode("utf-8"),
        },
    }
    img.save(path, "JPEG", quality=92, exif=piexif.dump(exif_dict))


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    """Build the canonical 6-file fixture set in a tmp dir."""
    out = tmp_path / "fixtures"
    out.mkdir()

    img1 = _screenshot_image()
    _save_with_exif(img1, out / "fixture_a.jpg",
                    "Photos 9.0", "2026:05:06 00:51:49")
    _save_with_exif(img1, out / "fixture_b.jpg",
                    "Preview 12.1", "2026:05:06 00:52:18")
    shutil.copy2(out / "fixture_a.jpg", out / "fixture_c.jpg")
    img1.save(out / "fixture_d.jpg", "JPEG", quality=55)
    _solid_image((50, 80, 120)).save(out / "fixture_e.jpg", "JPEG", quality=92)
    _noise_image().save(out / "fixture_f.jpg", "JPEG", quality=92)
    return out


@pytest.fixture
def burst_dir(tmp_path: Path) -> Path:
    """Build a burst sequence with EXIF DateTimeOriginal at 1-second intervals.

    Three "subjects" (different solid-color backgrounds), 3 frames each, all
    timestamped within a short window so --time-gap can group them.

    Frame timestamps:
      burst1_*: 2026:05:06 10:00:00, 10:00:01, 10:00:02   (subject 1)
      burst2_*: 2026:05:06 10:00:30, 10:00:31, 10:00:32   (subject 2)
      burst3_*: 2026:05:06 10:01:30, 10:01:31, 10:01:32   (subject 3)
    """
    out = tmp_path / "burst"
    out.mkdir()
    if not HAVE_PIEXIF:
        pytest.skip("piexif required for burst fixtures")

    times = [
        ("burst1_a.jpg", "2026:05:06 10:00:00", (200, 50, 50)),
        ("burst1_b.jpg", "2026:05:06 10:00:01", (210, 50, 50)),
        ("burst1_c.jpg", "2026:05:06 10:00:02", (220, 50, 50)),
        ("burst2_a.jpg", "2026:05:06 10:00:30", (50, 200, 50)),
        ("burst2_b.jpg", "2026:05:06 10:00:31", (50, 210, 50)),
        ("burst2_c.jpg", "2026:05:06 10:00:32", (50, 220, 50)),
        ("burst3_a.jpg", "2026:05:06 10:01:30", (50, 50, 200)),
        ("burst3_b.jpg", "2026:05:06 10:01:31", (50, 50, 210)),
        ("burst3_c.jpg", "2026:05:06 10:01:32", (50, 50, 220)),
    ]
    for name, ts, color in times:
        img = _solid_image(color)
        _save_with_exif(img, out / name, "TestCam", ts)
    return out
