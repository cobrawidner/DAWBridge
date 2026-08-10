"""Publishing (pull) must not silently replace someone else's work.

The shared folder is the master copy both people work from, and a publish
REPLACES its track list rather than merging. So publishing while unaware
that the other person already published is straightforward data loss for
them - and invisible to the person causing it.
"""
import json

import pytest

from dawbridge import syncstate


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    """Keep tests off the real ~/.dawbridge_state.json."""
    monkeypatch.setattr(syncstate, "_STATE_PATH", tmp_path / "state.json")


def test_no_drift_reported_when_machine_is_up_to_date(tmp_path):
    syncstate.record_sync(tmp_path, "reaper", revision=7, action="pull")
    assert syncstate.describe_drift(tmp_path, "reaper", current_revision=7) is None


def test_drift_reported_when_shared_session_moved_ahead(tmp_path):
    syncstate.record_sync(tmp_path, "reaper", revision=5, action="pull")

    drift = syncstate.describe_drift(tmp_path, "reaper", current_revision=8)

    assert drift is not None
    assert "3 revisions ahead" in drift
    assert "r5" in drift and "r8" in drift


def test_first_ever_sync_is_called_out_rather_than_silently_passing(tmp_path):
    drift = syncstate.describe_drift(tmp_path, "reaper", current_revision=3)
    assert "never synced" in drift


def test_state_is_tracked_per_daw_not_just_per_folder(tmp_path):
    # One machine can drive both DAWs against the same shared folder, and
    # each side syncs on its own schedule - collapsing them would make
    # one DAW's sync look like the other's.
    syncstate.record_sync(tmp_path, "reaper", revision=9, action="pull")

    assert syncstate.describe_drift(tmp_path, "reaper", 9) is None
    assert "never synced" in syncstate.describe_drift(tmp_path, "protools", 9)


def test_state_survives_a_corrupt_state_file(tmp_path, monkeypatch):
    # A half-written state file must not break syncing - the breadcrumb
    # is a convenience, not something worth failing a publish over.
    bad = tmp_path / "state.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(syncstate, "_STATE_PATH", bad)

    assert syncstate.last_synced(tmp_path, "reaper") is None
    syncstate.record_sync(tmp_path, "reaper", revision=2, action="push")
    assert json.loads(bad.read_text(encoding="utf-8"))  # rewritten cleanly


def test_project_change_is_detected(tmp_path):
    # A pull replaces the shared session with whatever project is open,
    # so switching projects is a swap, not an edit. This exact situation
    # silently doubled every track during development.
    syncstate.record_sync(tmp_path, "reaper", revision=4, action="pull", project=r"D:\songs\A.rpp")

    same = syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\A.rpp")
    different = syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\B.rpp")

    assert same is None
    assert different is not None
    assert "A.rpp" in different and "B.rpp" in different


def test_project_change_silent_when_unknown(tmp_path):
    # Backends that can't report a project must not trigger false alarms.
    syncstate.record_sync(tmp_path, "reaper", revision=4, action="pull", project="")
    assert syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\A.rpp") is None
    assert syncstate.describe_project_change(tmp_path, "reaper", "") is None


def test_a_sync_that_does_not_report_a_project_does_not_forget_the_old_one(tmp_path):
    """"I didn't ask" is not "there is no project".

    The GUI - which is what these two people actually use - calls
    record_sync with no project on every single pull and push. Dropping
    the recorded project there switched describe_project_change off
    permanently: the guard against publishing a DIFFERENT project over
    the shared session, which is the thing that silently doubled every
    track during development, went quiet after the first GUI sync and
    stayed quiet for the CLI too.
    """
    syncstate.record_sync(tmp_path, "reaper", revision=4, action="pull", project=r"D:\songs\A.rpp")

    syncstate.record_sync(tmp_path, "reaper", revision=5, action="push")  # a GUI-style call

    assert syncstate.last_synced(tmp_path, "reaper")["project"] == r"D:\songs\A.rpp"
    assert syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\B.rpp") is not None


def test_a_reported_project_still_replaces_the_remembered_one(tmp_path):
    # Preserving the old one must not mean ignoring a new one - switching
    # projects on purpose has to be recorded, or the next switch back
    # would look like a change.
    syncstate.record_sync(tmp_path, "reaper", revision=4, action="pull", project=r"D:\songs\A.rpp")
    syncstate.record_sync(tmp_path, "reaper", revision=5, action="pull", project=r"D:\songs\B.rpp")

    assert syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\B.rpp") is None
    assert syncstate.describe_project_change(tmp_path, "reaper", r"D:\songs\A.rpp") is not None
