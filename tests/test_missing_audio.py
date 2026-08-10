"""A clip whose audio isn't in the shared folder.

This is not an exotic state. session.json is a few KB and arrives in a
blink; a 90MB stem does not. For the minutes in between, the partner's
published session references audio that genuinely is not there yet - and
Reaper's push handed the path to PCM_Source_CreateFromFile anyway, which
produces an item with no source: right name, right position, plays
silence. Pro Tools has always skipped and said so; Reaper said nothing.
"""
from dawbridge.backend import LiveClip, LiveTrack
from dawbridge.model import Clip, Session, Track
from dawbridge.store import SharedStore
from dawbridge.sync import missing_audio_warning, preview_push


def _store_with(tmp_path, *names) -> SharedStore:
    store = SharedStore(tmp_path / "shared")
    store.ensure_layout()
    for name in names:
        store.resolve_audio_path(name).write_bytes(b"RIFF fake")
    return store


def test_warns_about_audio_that_has_not_synced_yet(tmp_path):
    store = _store_with(tmp_path)
    clip = Clip.new(name="Verse", audio_file="abc_stem.wav", start_seconds=0, length_seconds=8)

    warning = missing_audio_warning(clip, "Vox", store)

    assert warning is not None
    assert "abc_stem.wav" in warning
    assert "still be uploading" in warning, "name the likely cause, not just the symptom"


def test_warns_about_a_clip_with_no_audio_recorded_at_all(tmp_path):
    store = _store_with(tmp_path)
    clip = Clip.new(name="Verse", audio_file="", start_seconds=0, length_seconds=8)

    warning = missing_audio_warning(clip, "Vox", store)

    assert warning is not None and "no audio file recorded" in warning


def test_silent_when_the_audio_is_there(tmp_path):
    store = _store_with(tmp_path, "abc_stem.wav")
    clip = Clip.new(name="Verse", audio_file="abc_stem.wav", start_seconds=0, length_seconds=8)
    assert missing_audio_warning(clip, "Vox", store) is None


def test_check_is_stat_only_so_cloud_files_are_not_downloaded(tmp_path, monkeypatch):
    # Dropbox files can be cloud-only placeholders: exists() is free,
    # reading a byte forces a download of the whole file. A recursive read
    # over this folder once began hydrating an entire Dropbox account.
    store = _store_with(tmp_path, "abc_stem.wav")
    clip = Clip.new(name="Verse", audio_file="abc_stem.wav", start_seconds=0, length_seconds=8)

    def explode(*a, **kw):
        raise AssertionError("missing-audio check must never open an audio file")

    monkeypatch.setattr("builtins.open", explode)
    assert missing_audio_warning(clip, "Vox", store) is None


def test_preview_warns_before_the_push_places_a_silent_clip(tmp_path):
    store = _store_with(tmp_path)
    session = Session()
    track = Track.new(name="Vox")
    track.clips.append(Clip.new(name="Verse", audio_file="abc_stem.wav",
                                start_seconds=0, length_seconds=8))
    session.tracks.append(track)

    preview = preview_push(session, live_tracks=[], target="reaper", store=store)

    assert any("not in the shared folder" in w for w in preview.warnings)


def test_preview_does_not_warn_about_clips_it_will_not_place(tmp_path):
    # A clip already sitting in the DAW plays from whatever the DAW has
    # locally; whether the shared copy has arrived is not this push's
    # problem, and saying so on every preview is noise.
    store = _store_with(tmp_path)
    session = Session()
    track = Track.new(name="Vox")
    clip = Clip.new(name="Verse", audio_file="abc_stem.wav", start_seconds=0, length_seconds=8)
    track.clips.append(clip)
    session.tracks.append(track)

    live = [
        LiveTrack(
            bridge_id=track.id,
            name="Vox",
            clips=[LiveClip(bridge_id=clip.id, name="Verse", start_seconds=0.0, length_seconds=8.0)],
        )
    ]

    preview = preview_push(session, live, target="reaper", store=store)

    assert preview.warnings == []


def test_missing_audio_warnings_are_capped_so_they_stay_readable(tmp_path):
    store = _store_with(tmp_path)
    session = Session()
    track = Track.new(name="Vox")
    for i in range(9):
        track.clips.append(Clip.new(name=f"clip{i}", audio_file=f"missing{i}.wav",
                                    start_seconds=i * 10, length_seconds=5))
    session.tracks.append(track)

    warnings = preview_push(session, [], target="reaper", store=store).warnings

    assert len(warnings) == 6, "five lines plus an 'and N more'"
    assert "and 4 more" in warnings[-1]


def test_offline_media_does_not_abort_a_whole_publish(tmp_path):
    """Reaper keeps items whose media has gone offline - a renamed folder,
    an unplugged drive. Hashing a file that isn't there raised out of the
    middle of the pull loop, so one offline item aborted the entire
    publish and nothing was written at all.
    """
    from dawbridge.reaper_backend import _import_audio

    store = _store_with(tmp_path)

    assert _import_audio(store, str(tmp_path / "gone.wav")) == ""
    assert _import_audio(store, "") == ""

    present = tmp_path / "here.wav"
    present.write_bytes(b"RIFF real")
    assert _import_audio(store, str(present)).endswith("here.wav")


def test_no_store_means_no_claim(tmp_path):
    # The preview is callable without a store (tests, and any caller that
    # hasn't got one); it must not invent warnings it can't substantiate.
    session = Session()
    track = Track.new(name="Vox")
    track.clips.append(Clip.new(name="Verse", audio_file="abc.wav", start_seconds=0, length_seconds=8))
    session.tracks.append(track)

    assert preview_push(session, [], target="reaper", store=None).warnings == []
