from dawbridge.model import Clip, Session, Track
from dawbridge.sync import find_overlapping_clips, merge_pulled_track, plan_clips, plan_tracks


def test_find_overlapping_clips_detects_and_ignores_adjacency():
    a = Clip.new(name="A", audio_file="a.wav", start_seconds=0, length_seconds=10)
    b = Clip.new(name="B", audio_file="b.wav", start_seconds=5, length_seconds=10)   # overlaps A
    c = Clip.new(name="C", audio_file="c.wav", start_seconds=15, length_seconds=5)   # butts against B

    overlaps = find_overlapping_clips([a, b, c])

    assert [(x.name, y.name) for x, y in overlaps] == [("A", "B")]


def test_find_overlapping_clips_empty_for_sequential_clips():
    clips = [
        Clip.new(name="one", audio_file="a.wav", start_seconds=0, length_seconds=4),
        Clip.new(name="two", audio_file="b.wav", start_seconds=4, length_seconds=4),
    ]
    assert find_overlapping_clips(clips) == []


def test_find_overlapping_clips_tolerates_subsample_adjacency():
    # Butt-joined clips drift by a fraction of a sample through float
    # round trips; reporting that as an overlap is noise that trains the
    # user to ignore a warning that matters. Confirmed live on a real
    # project: 25.53191489 vs 25.53190476 (~1e-5s).
    clips = [
        Clip.new(name="a", audio_file="a.wav", start_seconds=0.0, length_seconds=25.53191489),
        Clip.new(name="b", audio_file="b.wav", start_seconds=25.53190476, length_seconds=10.0),
    ]
    assert find_overlapping_clips(clips) == []


def test_find_overlapping_clips_still_flags_real_overlap():
    clips = [
        Clip.new(name="a", audio_file="a.wav", start_seconds=0.0, length_seconds=10.0),
        Clip.new(name="b", audio_file="b.wav", start_seconds=9.0, length_seconds=5.0),
    ]
    assert [(x.name, y.name) for x, y in find_overlapping_clips(clips)] == [("a", "b")]


def test_plan_tracks_creates_unmatched_and_updates_matched():
    session = Session()
    matched = Track.new(name="Kick")
    unmatched = Track.new(name="Snare")
    session.tracks = [matched, unmatched]

    plan = plan_tracks(session, local_tag_to_name={matched.id: "Kick #" + matched.id})

    assert plan.to_create == [unmatched]
    assert [t for t, _ in plan.to_update] == [matched]


def test_plan_clips_add_update_orphan():
    kept = Clip.new(name="Kept", audio_file="a.wav", start_seconds=0, length_seconds=1)
    changed = Clip.new(name="Changed", audio_file="b.wav", start_seconds=5, length_seconds=2)
    new = Clip.new(name="New", audio_file="c.wav", start_seconds=10, length_seconds=1)
    canonical_clips = [kept, changed, new]

    # Locally we have `kept` and `changed` (by id) plus one the canonical
    # session no longer lists at all.
    local_ids = {kept.id, changed.id, "ffffffff"}

    plan = plan_clips(canonical_clips, local_ids)

    assert plan.to_add == [new]
    assert set(c.id for c in plan.to_update) == {kept.id, changed.id}
    assert plan.orphaned_ids == ["ffffffff"]


def test_merge_pulled_track_reuses_existing_tagged_track():
    session = Session()
    existing = Track.new(name="Bass")
    session.tracks.append(existing)

    result = merge_pulled_track(session, "Bass (renamed locally)", existing.id)

    assert result is existing


def test_merge_pulled_track_refreshes_name_of_existing_track():
    # A pull captures live DAW state: renaming a track in one DAW has to
    # reach the other. This regressed once - merge returned the existing
    # canonical track untouched, so renames were silently dropped and the
    # two sides drifted apart with no warning.
    session = Session()
    existing = Track.new(name="Bass")
    session.tracks.append(existing)

    result = merge_pulled_track(session, "Bass DI", existing.id)

    assert result.id == existing.id  # identity preserved
    assert result.name == "Bass DI"  # ...but name is refreshed


def test_merge_pulled_track_adopts_untagged_track():
    session = Session()

    result = merge_pulled_track(session, "Guitar DI", None)

    assert result.id not in (t.id for t in session.tracks)  # not yet appended by merge itself
    assert result.name == "Guitar DI"


def test_never_auto_deletes_orphaned_clips():
    # plan_clips should only ever *report* orphans, never remove them from
    # any structure - this test exists to make the non-destructive
    # guarantee explicit and regression-proof.
    plan = plan_clips(canonical_clips=[], local_clip_ids={"aaaa1111"})
    assert plan.orphaned_ids == ["aaaa1111"]
    assert plan.to_add == []
    assert plan.to_update == []
