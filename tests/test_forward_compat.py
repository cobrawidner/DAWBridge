"""One collaborator updates DAWBridge, the other doesn't.

That will happen - two people, two machines, one shared folder, and no
mechanism that updates them together. The out-of-date machine has to cope
with a session.json written by a newer version:

  - Fields it has never heard of must not kill it, and must not be
    quietly dropped either. Publishing REPLACES the shared session, so an
    old client that strips a field it doesn't understand destroys the
    other person's data on every single publish - silently, and for good.
  - A genuinely incompatible format bump must produce a sentence a
    musician can act on, not a traceback.
"""
import json

import pytest

from dawbridge.model import Session, Track, UnsupportedSchemaVersion


def _future_session() -> dict:
    """What a newer DAWBridge might write: same schema version, extra
    fields at every level.
    """
    return {
        "schema_version": 1,
        "name": "Shady Grove",
        "sample_rate": 44100,
        "tempo_bpm": 94.0,
        "time_signature_numerator": 4,
        "time_signature_denominator": 4,
        "swing_percent": 12.5,  # unknown to this version
        "tracks": [
            {
                "id": "aaaaaaaa",
                "name": "Vox",
                "kind": "audio",
                "order": 0,
                "channels": 2,
                "muted": False,
                "input_gain_db": -3.0,  # unknown to this version
                "clips": [
                    {
                        "id": "bbbbbbbb",
                        "name": "Verse",
                        "audio_file": "abc_verse.wav",
                        "start_seconds": 1.0,
                        "length_seconds": 4.0,
                        "pitch_semitones": -2,  # unknown to this version
                    }
                ],
            }
        ],
        "markers": [{"id": "cccccccc", "name": "Chorus", "time_seconds": 32.0, "colour": "red"}],
        "revision": 7,
        "updated_by": "conner@protools",
        "updated_at": "2026-08-09T16:29:41+00:00",
    }


def test_a_newer_sessions_unknown_clip_field_does_not_crash_the_old_client():
    session = Session.from_dict(_future_session())
    assert session.tracks[0].clips[0].name == "Verse"


def test_unknown_fields_survive_a_round_trip_at_every_level():
    """The part that matters: an old client republishing must not strip
    what it couldn't read. A pull replaces the shared session wholesale,
    so anything dropped here is gone from the partner's copy too.
    """
    original = _future_session()

    reloaded = json.loads(Session.from_dict(original).to_json())

    assert reloaded["swing_percent"] == 12.5
    assert reloaded["tracks"][0]["input_gain_db"] == -3.0
    assert reloaded["tracks"][0]["clips"][0]["pitch_semitones"] == -2
    assert reloaded["markers"][0]["colour"] == "red"


def test_known_fields_still_win_and_are_still_editable():
    session = Session.from_dict(_future_session())
    session.tracks[0].name = "Lead Vocal"
    session.bump("travis@reaper")

    reloaded = json.loads(session.to_json())

    assert reloaded["tracks"][0]["name"] == "Lead Vocal"
    assert reloaded["revision"] == 8
    assert reloaded["tracks"][0]["input_gain_db"] == -3.0, "editing must not drop the unknowns"


def test_unknown_field_is_not_leaked_as_a_literal_extra_key():
    reloaded = json.loads(Session.from_dict(_future_session()).to_json())
    assert "extra" not in reloaded
    assert "extra" not in reloaded["tracks"][0]
    assert "extra" not in reloaded["tracks"][0]["clips"][0]


def test_a_plain_session_carries_no_extras_and_looks_unchanged():
    # The common case must produce byte-for-byte the same file it always
    # did, or every machine rewrites every session on first contact.
    session = Session(name="Song")
    session.tracks.append(Track.new(name="Kick"))

    reloaded = json.loads(session.to_json())

    assert set(reloaded) == {
        "schema_version", "name", "sample_rate", "tempo_bpm",
        "time_signature_numerator", "time_signature_denominator",
        "tracks", "markers", "revision", "updated_by", "updated_at",
    }


def test_a_too_new_schema_version_says_what_to_do_instead_of_raising_typeerror():
    data = _future_session()
    data["schema_version"] = 99

    with pytest.raises(UnsupportedSchemaVersion) as caught:
        Session.from_dict(data)

    message = str(caught.value)
    assert "99" in message and "update" in message.lower()
    assert "nothing has been changed" in message.lower(), "say the folder is untouched"
    assert "Traceback" not in message


def test_an_older_schema_version_is_still_readable():
    # Forward compatibility isn't an excuse to break backwards.
    data = _future_session()
    data["schema_version"] = 0
    assert Session.from_dict(data).name == "Shady Grove"


def test_a_missing_schema_version_is_assumed_current():
    data = _future_session()
    del data["schema_version"]
    assert Session.from_dict(data).revision == 7


def test_loading_a_too_new_file_from_disk_reports_the_same_sentence(tmp_path):
    path = tmp_path / "session.json"
    data = _future_session()
    data["schema_version"] = 2
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(UnsupportedSchemaVersion):
        Session.load(path)
