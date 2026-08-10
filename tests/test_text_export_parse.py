"""Pro Tools' "Session Info as Text" export is the only way this backend
can see clips at all, so how it's parsed is load-bearing.

The export is keyed by TRACK NAME and carries no per-clip identity, which
is fine until two tracks share a name.
"""
from dawbridge.protools_backend import ProToolsBackend

_EXPORT = "\n".join([
    "SESSION NAME:\tShady Grove",
    "SAMPLE RATE:\t44100.000000",
    "",
    "O N L I N E   C L I P S   I N   S E S S I O N",
    "",
    "CLIP NAME\tFILE NAME",
    "Verse #11111111\tverse.wav",
    "Chorus #22222222\tchorus.wav",
    "Solo #33333333\tsolo.wav",
    "",
    "T R A C K   L I S T I N G",
    "TRACK NAME:\tGtr #aaaaaaaa (Stereo)",
    "COMMENTS:\t",
    "STATE:\t",
    "CHANNEL\tEVENT\tCLIP NAME\tSTART TIME\tEND TIME\tDURATION\tSTATE",
    "1\t1\tVerse #11111111\t0\t44100\t44100\tUnmuted",
    "1\t2\tChorus #22222222\t44100\t88200\t44100\tUnmuted",
    "",
    "TRACK NAME:\tGtr #bbbbbbbb (Stereo)",
    "COMMENTS:\t",
    "STATE:\tMuted",
    "CHANNEL\tEVENT\tCLIP NAME\tSTART TIME\tEND TIME\tDURATION\tSTATE",
    "1\t1\tSolo #33333333\t88200\t132300\t44100\tUnmuted",
    "",
])


class _Builder:
    def include_clip_list(self): pass
    def include_track_edls(self): pass
    def all_tracks(self): pass
    def time_type(self, _v): pass
    def export_string(self): return _EXPORT


class _Engine:
    def session_sample_rate(self): return 44100
    def session_path(self): return r"C:\sessions\Shady Grove\Shady Grove.ptx"
    def export_session_as_text(self): return _Builder()


def test_parser_reads_positions_in_samples_not_frames():
    # The timecode columns round to whole frames (~33ms at 30fps), which
    # silently quantized every position on a Pro Tools pull. Samples are
    # exact.
    clips, _muted = ProToolsBackend()._pull_clips_via_text_export(_Engine())

    verse = clips["Gtr #aaaaaaaa"][0]
    assert verse["start_seconds"] == 0.0
    assert verse["length_seconds"] == 1.0


def test_parser_reads_mute_state_per_track_not_per_shared_name():
    _clips, muted = ProToolsBackend()._pull_clips_via_text_export(_Engine())
    assert muted == {"Gtr #aaaaaaaa": False, "Gtr #bbbbbbbb": True}


def test_two_tracks_with_one_visible_name_keep_separate_clip_lists():
    """The collision, and the fix.

    "Gtr #aaaaaaaa" and "Gtr #bbbbbbbb" are distinct tracks with distinct
    identities. The parse used to strip the tag before bucketing, so all
    three clips landed under "Gtr": pull() then gave BOTH canonical
    tracks all three clips, and push() credited each track with the
    other's clips, so plan_clips believed they were already present and
    imported nothing. Neither outcome reported anything.

    Reaper allows duplicate track names freely, so this arrived from the
    other side of the bridge without anyone doing anything unusual. It
    needs no native clip ids to fix - the tag was already in the name and
    the parser was throwing it away.
    """
    clips, _muted = ProToolsBackend()._pull_clips_via_text_export(_Engine())

    assert list(clips) == ["Gtr #aaaaaaaa", "Gtr #bbbbbbbb"], "one bucket per real track"
    assert [c["name"] for c in clips["Gtr #aaaaaaaa"]] == ["Verse #11111111", "Chorus #22222222"]
    assert [c["name"] for c in clips["Gtr #bbbbbbbb"]] == ["Solo #33333333"]


def test_the_format_annotation_is_stripped_so_both_sides_agree():
    # The export writes "Gtr #aaaaaaaa (Stereo)"; track_list() reports
    # "Gtr #aaaaaaaa". The lookup only works if both reduce to one key.
    from dawbridge.protools_backend import _clip_bucket_key

    assert _clip_bucket_key("Gtr #aaaaaaaa (Stereo)") == "Gtr #aaaaaaaa"
    assert _clip_bucket_key("Gtr #aaaaaaaa") == "Gtr #aaaaaaaa"
    assert _clip_bucket_key("Gtr (DI) #aaaaaaaa (Mono)") == "Gtr (DI) #aaaaaaaa", (
        "a parenthesis in the musician's own name must survive"
    )


def test_source_files_resolve_against_the_sessions_audio_folder():
    clips, _muted = ProToolsBackend()._pull_clips_via_text_export(_Engine())
    verse = clips["Gtr #aaaaaaaa"][0]
    assert verse["source_file"].endswith(r"Audio Files\verse.wav")


def test_a_failed_export_is_reported_as_unknown_not_as_empty():
    """The distinction the whole pull now hangs on: every clip this
    backend can see comes from this export, so reading a failure as "no
    clips" published every track with its clips stripped off.
    """
    class _Failing(_Engine):
        def export_session_as_text(self):
            class _B(_Builder):
                def export_string(self):
                    raise RuntimeError("Assertion in UExportSessionTextCommand.cpp")
            return _B()

    warnings: list[str] = []
    clips, muted = ProToolsBackend()._pull_clips_via_text_export(_Failing(), warnings)

    assert clips is None, "None means 'could not read', {} would mean 'nothing there'"
    assert muted == {}
    assert len(warnings) == 1 and "left exactly as they were" in warnings[0]
