"""Publishing without replacing what you didn't touch.

Publishing has always replaced the shared session's track list wholesale.
That is where every "you overwrote my work" failure comes from: your
partner edits the bass, you publish the vocal you were working on, and
their bass goes with it - silently, because from your side nothing looked
wrong.

This decides WHICH TRACKS a publish may touch. It is emphatically not a
content merge: no clip, take or position is ever reconciled between two
versions, and inside any track that is touched, wholesale replacement
stays exactly as it is. A track is taken whole from one side or the other.

Three inputs, all of which already exist on disk:

  baseline  - the canonical revision this machine last agreed with
              (syncstate records the number, archive/ holds the content)
  canonical - the shared session as it is right now
  live      - what this DAW has, as just captured

A track you deleted and a track you never had look identical if you only
inspect the DAW. They stop looking identical against the baseline.
"""
from dawbridge.model import Clip, Session, Track
from dawbridge.sync import plan_publish


def _track(track_id, name, start=0.0, muted=False):
    t = Track(id=track_id, name=name, muted=muted)
    t.clips = [Clip(id=track_id + "c", name="take", audio_file="a.wav",
                    start_seconds=start, length_seconds=4.0)]
    return t


def _names(tracks):
    return [t.name for t in tracks]


# ---- the failure this exists to remove ---------------------------------

def test_a_track_only_your_partner_changed_is_preserved():
    """The headline. You never touched the bass; they moved its clip. Your
    publish must not put your older copy back over theirs.
    """
    baseline = [_track("aaaa", "Vox"), _track("bbbb", "Bass", start=0.0)]
    canonical = [_track("aaaa", "Vox"), _track("bbbb", "Bass", start=32.0)]  # they moved it
    live = [_track("aaaa", "Vox 2"), _track("bbbb", "Bass", start=0.0)]      # you still have the old

    plan = plan_publish(canonical, live, baseline)

    bass = next(t for t in plan.tracks if t.id == "bbbb")
    assert bass.clips[0].start_seconds == 32.0, "their edit must survive your publish"
    assert "Bass" in plan.kept_theirs
    assert "Vox 2" in plan.updated, "the track you did change is still published"


def test_a_track_you_changed_is_published():
    baseline = [_track("aaaa", "Vox", start=0.0)]
    canonical = [_track("aaaa", "Vox", start=0.0)]
    live = [_track("aaaa", "Vox", start=8.0)]

    plan = plan_publish(canonical, live, baseline)

    assert next(t for t in plan.tracks if t.id == "aaaa").clips[0].start_seconds == 8.0
    assert plan.updated == ["Vox"]


def test_both_of_you_changing_one_track_is_a_conflict_not_a_merge():
    """No reconciliation. The publisher's version wins, exactly as it
    always has - but it is said out loud, because it is the one case where
    somebody's work is genuinely being replaced.
    """
    baseline = [_track("bbbb", "Bass", start=0.0)]
    canonical = [_track("bbbb", "Bass", start=32.0)]
    live = [_track("bbbb", "Bass", start=64.0)]

    plan = plan_publish(canonical, live, baseline)

    assert next(t for t in plan.tracks if t.id == "bbbb").clips[0].start_seconds == 64.0
    assert plan.conflicts == ["Bass"]
    assert any("Bass" in w and "both" in w.lower() for w in plan.warnings)


def test_an_untouched_track_is_left_completely_alone():
    baseline = [_track("aaaa", "Vox")]
    canonical = [_track("aaaa", "Vox")]
    live = [_track("aaaa", "Vox")]

    plan = plan_publish(canonical, live, baseline)

    assert plan.unchanged == ["Vox"]
    assert plan.updated == [] and plan.removed == [] and plan.added == []


def test_subsample_drift_does_not_count_as_a_change():
    # A round trip through the other DAW's rounding must not make a track
    # look edited, or every publish would claim to change everything.
    baseline = [_track("aaaa", "Vox", start=10.0)]
    canonical = [_track("aaaa", "Vox", start=10.0)]
    live = [_track("aaaa", "Vox", start=10.0001)]

    assert plan_publish(canonical, live, baseline).unchanged == ["Vox"]


# ---- membership: the part that makes deletion possible -----------------

def test_a_track_you_deleted_is_removed():
    baseline = [_track("aaaa", "Vox"), _track("bbbb", "Scratch")]
    canonical = [_track("aaaa", "Vox"), _track("bbbb", "Scratch")]
    live = [_track("aaaa", "Vox")]

    plan = plan_publish(canonical, live, baseline)

    assert _names(plan.tracks) == ["Vox"]
    assert plan.removed == ["Scratch"]


def test_a_track_that_arrived_after_you_last_looked_is_not_deleted():
    """The case membership alone gets wrong. It is absent from your DAW for
    the same reason a deleted track is - you don't have it - but it was
    never yours to delete.
    """
    baseline = [_track("aaaa", "Vox")]
    canonical = [_track("aaaa", "Vox"), _track("cccc", "Their new fiddle")]
    live = [_track("aaaa", "Vox")]

    plan = plan_publish(canonical, live, baseline)

    assert "Their new fiddle" in _names(plan.tracks)
    assert plan.removed == []
    assert "Their new fiddle" in plan.kept_theirs


def test_a_new_local_track_is_added():
    baseline = [_track("aaaa", "Vox")]
    canonical = [_track("aaaa", "Vox")]
    live = [_track("aaaa", "Vox"), _track("dddd", "New guitar")]

    plan = plan_publish(canonical, live, baseline)

    assert "New guitar" in _names(plan.tracks)
    assert plan.added == ["New guitar"]


# ---- when the baseline isn't available ---------------------------------

def test_without_a_baseline_nothing_is_ever_removed():
    """If the last-seen revision has aged out of the archive, "I deleted it"
    and "it arrived while I was away" are indistinguishable, and they have
    opposite correct answers. So don't choose: preserve, and say so.
    Losing the ability to delete costs one extra step. Losing your
    partner's track is not recoverable.
    """
    canonical = [_track("aaaa", "Vox"), _track("bbbb", "Bass")]
    live = [_track("aaaa", "Vox")]

    plan = plan_publish(canonical, live, baseline_tracks=None)

    assert _names(plan.tracks) == ["Vox", "Bass"], "Bass preserved, not removed"
    assert plan.removed == []
    assert "Bass" in plan.kept_theirs
    assert any("aged out" in w or "never synced" in w for w in plan.warnings)


def test_without_a_baseline_your_own_edits_still_publish():
    # Conservative about deletion must not mean refusing to publish.
    canonical = [_track("aaaa", "Vox", start=0.0)]
    live = [_track("aaaa", "Vox", start=8.0)]

    plan = plan_publish(canonical, live, baseline_tracks=None)

    assert next(t for t in plan.tracks if t.id == "aaaa").clips[0].start_seconds == 8.0


# ---- ordering and shape ------------------------------------------------

def test_the_daws_track_order_is_kept_and_preserved_tracks_follow():
    baseline = [_track("aaaa", "Vox")]
    canonical = [_track("aaaa", "Vox"), _track("cccc", "Theirs")]
    live = [_track("dddd", "New"), _track("aaaa", "Vox")]

    plan = plan_publish(canonical, live, baseline)

    assert _names(plan.tracks)[:2] == ["New", "Vox"], "this DAW's order leads"
    assert _names(plan.tracks)[2:] == ["Theirs"]


def test_plan_is_empty_of_claims_when_nothing_happened():
    tracks = [_track("aaaa", "Vox")]
    plan = plan_publish(tracks, tracks, tracks)

    assert not plan.warnings
    assert plan.updated == [] and plan.added == [] and plan.removed == []
    assert plan.conflicts == []


# ---- getting the three inputs safely -----------------------------------

def test_a_snapshot_survives_capture_mutating_the_originals():
    """The trap in the wiring. capture() reuses canonical's Track objects
    and mutates them in place (that's how a track keeps its clip history),
    so `list(session.tracks)` taken beforehand is NOT a before-picture -
    every object in it changes underneath you. Comparing against it would
    report that nothing ever changed.
    """
    from dawbridge.sync import snapshot_tracks

    session = Session()
    session.tracks = [_track("aaaa", "Vox", start=0.0)]
    before = snapshot_tracks(session)

    session.tracks[0].name = "Vox 2"                    # what capture does
    session.tracks[0].clips[0].start_seconds = 32.0

    assert before[0].name == "Vox"
    assert before[0].clips[0].start_seconds == 0.0
    assert plan_publish(before, session.tracks, before).updated == ["Vox 2"]


def test_baseline_comes_from_the_archive_when_canonical_has_moved_on(tmp_path):
    from dawbridge.store import SharedStore
    from dawbridge.sync import baseline_tracks_for

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    mine = Session()
    mine.tracks = [_track("aaaa", "Vox")]
    store.save(mine, updated_by="me")               # r1 - what I last saw
    seen = mine.revision

    theirs = store.load()
    theirs.tracks.append(_track("cccc", "Their fiddle"))
    store.save(theirs, updated_by="them")           # r2 - they published after

    baseline = baseline_tracks_for(store, store.load(), seen)

    assert [t.name for t in baseline] == ["Vox"], "the revision I last agreed with"


def test_baseline_is_canonical_itself_when_nothing_has_moved(tmp_path):
    # The common case - you published, nobody else has since. That revision
    # is session.json, not an archive file.
    from dawbridge.store import SharedStore
    from dawbridge.sync import baseline_tracks_for

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    mine = Session()
    mine.tracks = [_track("aaaa", "Vox")]
    store.save(mine, updated_by="me")

    current = store.load()
    baseline = baseline_tracks_for(store, current, current.revision)

    assert [t.name for t in baseline] == ["Vox"]


def test_baseline_is_unknown_when_it_has_aged_out_or_never_existed(tmp_path):
    from dawbridge.store import SharedStore
    from dawbridge.sync import baseline_tracks_for

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    store.save(Session(), updated_by="me")
    current = store.load()

    assert baseline_tracks_for(store, current, None) is None, "never synced"
    assert baseline_tracks_for(store, current, 9999) is None, "aged out of the archive"


def test_baseline_is_detached_from_the_live_session(tmp_path):
    # Same trap as the snapshot: if the baseline handed back canonical's own
    # objects, capture() would mutate the baseline too.
    from dawbridge.store import SharedStore
    from dawbridge.sync import baseline_tracks_for

    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    mine = Session()
    mine.tracks = [_track("aaaa", "Vox")]
    store.save(mine, updated_by="me")

    current = store.load()
    baseline = baseline_tracks_for(store, current, current.revision)
    current.tracks[0].name = "mutated"

    assert baseline[0].name == "Vox"


# ---- what the preview has to be able to say ----------------------------

def test_summary_line_states_removals_and_preserved_tracks():
    baseline = [_track("aaaa", "Vox"), _track("bbbb", "Scratch"), _track("eeee", "Old")]
    canonical = baseline + [_track("cccc", "Theirs")]
    live = [_track("aaaa", "Vox", start=8.0), _track("dddd", "New")]

    line = plan_publish(canonical, live, baseline).summary_line()

    assert "2 track(s) removed" in line
    assert "1 of your partner's track(s) left alone" in line
    assert "1 new track(s)" in line and "1 track(s) updated" in line


def test_summary_line_says_so_when_a_publish_changes_nothing():
    tracks = [_track("aaaa", "Vox")]
    assert "no changes" in plan_publish(tracks, tracks, tracks).summary_line()
