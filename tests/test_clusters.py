"""Tests for compute_clusters() three-tier dedup."""

from __future__ import annotations

from pathlib import Path

from dedupe_images import compute_clusters, _glob_to_regex, is_locked


def _all_paths(by_tier: dict) -> set[str]:
    out: set[str] = set()
    for tier in by_tier.values():
        for cluster in tier:
            for f, _ in cluster:
                out.add(str(f))
    return out


def test_finds_byte_pixel_and_perceptual_dupes(fixtures_dir: Path):
    by_tier = compute_clusters([fixtures_dir], log_progress=False)

    # All clusters across all tiers should be size >= 2.
    for tier_groups in by_tier.values():
        for cluster in tier_groups:
            assert len(cluster) >= 2

    # a, b, c, d should be in one merged cluster (Tier 3 = weakest joining tier).
    paths = _all_paths(by_tier)
    expected_dupes = {
        str(fixtures_dir / "fixture_a.jpg"),
        str(fixtures_dir / "fixture_b.jpg"),
        str(fixtures_dir / "fixture_c.jpg"),
        str(fixtures_dir / "fixture_d.jpg"),
    }
    assert expected_dupes <= paths

    # Controls e and f should NOT be in any cluster.
    assert str(fixtures_dir / "fixture_e.jpg") not in paths
    assert str(fixtures_dir / "fixture_f.jpg") not in paths


def test_skip_tier3_yields_only_pixel_match(fixtures_dir: Path):
    by_tier = compute_clusters([fixtures_dir], skip_tier3=True, log_progress=False)

    # Without tier 3, d (re-encoded) should drop out of clusters.
    # a + b + c remain merged via tier 1 (a-c byte) + tier 2 (b -> a/c pixels).
    paths = _all_paths(by_tier)
    assert str(fixtures_dir / "fixture_a.jpg") in paths
    assert str(fixtures_dir / "fixture_b.jpg") in paths
    assert str(fixtures_dir / "fixture_c.jpg") in paths
    assert str(fixtures_dir / "fixture_d.jpg") not in paths
    assert by_tier[3] == []


def test_cluster_label_is_weakest_tier(fixtures_dir: Path):
    by_tier = compute_clusters([fixtures_dir], log_progress=False)

    # The full a/b/c/d cluster has tier-3 joins, so it lives under by_tier[3].
    assert len(by_tier[3]) == 1
    cluster = by_tier[3][0]
    members = {f.name for f, _ in cluster}
    assert members == {"fixture_a.jpg", "fixture_b.jpg",
                       "fixture_c.jpg", "fixture_d.jpg"}


def test_threshold_is_monotonic(fixtures_dir: Path):
    # A higher threshold must find at least as many clustered files as a lower one.
    by_8 = compute_clusters([fixtures_dir], threshold=8, log_progress=False)
    by_0 = compute_clusters([fixtures_dir], threshold=0, log_progress=False)
    n_8 = sum(len(c) for clusters in by_8.values() for c in clusters)
    n_0 = sum(len(c) for clusters in by_0.values() for c in clusters)
    assert n_8 >= n_0


def test_empty_dir_returns_empty(tmp_path: Path):
    by_tier = compute_clusters([tmp_path], log_progress=False)
    assert by_tier == {1: [], 2: [], 3: []}


def test_time_gap_groups_burst_frames(burst_dir: Path):
    # Three subjects, 3 frames each, intra-burst spacing 1s, inter-burst 30s+.
    # With --time-gap 3, intra-burst should cluster but inter-burst should NOT.
    by_tier = compute_clusters([burst_dir], time_gap=3, log_progress=False,
                               skip_tier3=True)  # skip perceptual to isolate tier 4
    tier4 = by_tier[4]
    # Should produce 3 separate clusters of 3 files each.
    assert len(tier4) == 3, f"expected 3 burst clusters, got {len(tier4)}"
    for cluster in tier4:
        assert len(cluster) == 3
        # Members should all share the same "burstN_" prefix.
        prefixes = {f.name.split("_")[0] for f, _ in cluster}
        assert len(prefixes) == 1


def test_time_gap_zero_disables_tier4(burst_dir: Path):
    by_tier = compute_clusters([burst_dir], time_gap=0, log_progress=False,
                               skip_tier3=True)
    assert by_tier[4] == []


def test_glob_to_regex_basic():
    p = _glob_to_regex("**/keepers/*.jpg")
    assert p.match("/Users/me/Pictures/keepers/foo.jpg")
    assert p.match("/Users/me/keepers/x.jpg")
    assert not p.match("/Users/me/keepers/x.png")
    assert not p.match("/Users/me/other/x.jpg")


def test_glob_to_regex_star_doesnt_cross_slash():
    p = _glob_to_regex("/root/*.jpg")
    assert p.match("/root/x.jpg")
    assert not p.match("/root/sub/x.jpg")


def test_is_locked():
    p1 = _glob_to_regex("**/locked/*")
    p2 = _glob_to_regex("**/important.jpg")
    assert is_locked(Path("/a/b/locked/x.jpg"), [p1, p2])
    assert is_locked(Path("/a/b/important.jpg"), [p1, p2])
    assert not is_locked(Path("/a/b/c.jpg"), [p1, p2])
    assert not is_locked(Path("/a/b/c.jpg"), [])


def test_min_size_filter(tmp_path: Path):
    from PIL import Image
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    img.save(tmp_path / "small.jpg", "JPEG", quality=10)
    img.save(tmp_path / "small_dup.jpg", "JPEG", quality=10)

    # min_size larger than the file → no candidates → no clusters.
    by_tier = compute_clusters([tmp_path], min_size=10**9, log_progress=False)
    assert by_tier == {1: [], 2: [], 3: []}
