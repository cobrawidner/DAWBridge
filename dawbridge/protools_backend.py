"""Pro Tools-side backend, driving a *running* Pro Tools instance live via
PTSL (Avid's Pro Tools Scripting SDK) through the py-ptsl Python wrapper
(https://github.com/iluvcapra/py-ptsl).

Validated against a real, running Pro Tools 2025 instance (PTSL version
2025) on a throwaway scratch session. Several things the original version
of this file had to guess turned out wrong or incomplete once checked
against the real API - notable surprises, in case py-ptsl's own API shifts
again in a future release:

  - `engine.import_data()` is NOT for importing audio files - it imports
    *another Pro Tools session's* data (like AAF/session-data import). The
    real call for bringing a raw audio file in is `engine.import_audio()`,
    but that wrapper is itself broken as of py-ptsl 601.1.0: it builds a
    `CId_Import` (session-data) operation instead of the dedicated
    `CId_ImportAudioToClipList` command, and fails with
    `OS_NoFilePathFound` no matter what's passed. Bypassed here by
    constructing the `CId_ImportAudioToClipList` operation directly - see
    `_import_audio_to_clip_list()`.
  - There is no `Spot` Engine wrapper (confirmed missing, matching this
    file's original caveat) - the real placement command is
    `SpotClipsByID`, also not wrapped, called directly via
    `_spot_clip_by_id()`. It takes a clip id (returned by the clip-list
    import above) and a destination track id, with the position given as
    a nested `TimelineLocation{location, time_type}` message - a flat
    `location_value` string alone is rejected with a bare
    `PT_UnknownError (location)`.
  - `engine.track_list()` returns a plain `list[ptsl.PTSL_pb2.Track]`, not
    a dict - iterate it directly, no `.get("track_list", ...)` needed.
  - `engine.rename_target_track()` takes `old_name=`/`new_name=`, not
    `track_name=`.
  - The real text-export method is `engine.export_session_as_text()`,
    which returns a mutable *builder* (`.include_track_edls()`,
    `.all_tracks()`, `.time_type("tc")`, then `.export_string()`) - not a
    single call taking `include_clip_list=`/`include_track_list=` kwargs.
  - Import needs `audio_operations=AudioOperations.ConvertAudio` if the
    source file's sample rate doesn't match the session's, or it fails
    with `PT_CannotBeDone (No audio files were imported.)`. But
    `ConvertAudio` always re-encodes, even when nothing needs converting
    - confirmed live, on a real multi-minute file it failed outright with
    an empty `CommandError()` (likely an internal timeout). Only use it
    when the source's sample rate actually differs from the session's;
    `AudioOperations.AddAudio` is instant for the common matching-rate
    case.
  - A track's channel width (mono/stereo) has to match on both sides -
    Pro Tools refuses to spot a clip onto a track of the wrong width. See
    `Track.channels` in model.py.
  - **Never hand Pro Tools a direct path into the shared folder.**
    `AddAudio` imports by reference rather than copying (confirmed live),
    and `rename_target_clip` defaults to `rename_file=True`, which
    renames *and rewrites the header of* the underlying file on disk.
    Combined, importing straight from the shared folder silently renamed
    and modified a real shared audio file in place. Always copy into the
    session's own Audio Files folder first and import that copy (see
    `_import_audio_to_clip_list()`), and pass `rename_file=False`
    explicitly regardless.
  - A stereo interleaved file comes back from `ImportAudioToClipList` as
    **two** linked mono-channel clip ids (displayed as `<name>.L` /
    `<name>.R`), not one - both must be passed to `SpotClipsByID` in the
    same call to move as a pair (confirmed live: spotting only one id
    places just that channel). The text-export parser has to collapse
    these two rows back into one logical clip on pull, or it would double
    every stereo clip.

KNOWN GAP: automation curves are not exposed by PTSL as of this writing
(no Get/SetAutomation-style command in the protocol's command table), so
this backend does not attempt to read or write automation. See the
feasibility notes for what that means for scope.

MARKERS: memory locations are exposed properly - `get_memory_locations()`
returns structured messages and `create_memory_location()` /
`edit_memory_location()` write them, so this backend reads and writes
markers. `CreateMemoryLocationRequestBody` carries no time-type field
(unlike `SpotClipsByID` and `set_timeline_selection`, which both take an
explicit `TimelineLocationType`), which looked like an unresolvable
ambiguity about what unit the position string is in. Settled by live
test against Pro Tools 2025:
  - reads are ALWAYS in samples, whatever the main counter is set to
    (checked against Bars|Beats, TimeCode and Min:Secs in turn - the same
    marker read back '480000' every time). This matters because
    `_use_bars_beats_counter()` changes the counter on every push.
  - writes accept several formats and resolve them correctly:
    "00:00:10:00", "480000" and "0:10.000" all landed at exactly 480000
    samples in a 48kHz session, cross-checked against the text export.
So samples are exact in both directions and nothing has to be guessed.

CANNOT MOVE AN EXISTING CLIP: `_push_clips` warns rather than re-applying
a moved clip's position, and that is a real limit of PTSL 2025, not
laziness. `CId_GetClipList` exists in the protocol but returns an empty
list even with clips on the timeline and a selection made (confirmed
live). And re-spotting a clip's own ids with `SpotClipsByID` does not
move it - it places a SECOND copy (confirmed live: one clip at 4s became
two, at 4s and 10s). So there is no read path to a timeline clip's
identity and no write path that moves one; a naive "just spot it again"
fix would silently duplicate every moved clip.

KNOWN CAVEAT: the text-export clip-name column is a fixed display width;
a very long tagged clip name could in principle be truncated in the
export, which would corrupt the trailing `#<id>` tag. Not hit in testing,
but worth knowing about if pull ever silently fails to recognize a clip
it should already know.

One-time setup (see README):
    - Download/accept the Pro Tools Scripting SDK from developer.avid.com
    - Enable scripting access in Pro Tools' preferences if prompted
    - pip install py-ptsl
"""
from __future__ import annotations

import re
from pathlib import Path

from . import audiofile
from .backend import Backend, LiveClip, LiveMarker, LiveTrack, missing_client_library_reason
from .model import Clip, Marker, Session, Track, new_id
from .sync import (
    claim_live_id,
    describe_dropped_tracks,
    describe_publish_sample_rate_change,
    describe_sample_rate_mismatch,
    duplicate_adoption_warning,
    find_overlapping_clips,
    merge_pulled_track,
    plan_clips,
    plan_markers,
    plan_tracks,
    renumber_tracks,
)
from .tagging import parse_tag, strip_tag, tag

_COMPANY = "DAWBridge"
_APP = "DAWBridge Agent"


def _is_grpc_unavailable(exc: BaseException) -> bool:
    """Whether `exc` is gRPC's "nothing is listening there" status.

    Read through duck-typing rather than `except grpc.RpcError` so this
    file still needs no direct grpc import - grpc arrives only as a
    dependency of py-ptsl, and importing it here would turn "py-ptsl
    isn't installed" into a second, different ImportError at module
    load, which is precisely the confusion this code exists to remove.
    """
    code = getattr(exc, "code", None)
    if not callable(code):
        return False
    try:
        return getattr(code(), "name", "") == "UNAVAILABLE"
    except Exception:
        return False


class ProToolsBackend(Backend):
    name = "protools"

    def is_available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Which of the three unavailable cases this actually is - see
        Backend.unavailable_reason.

        The three are told apart by where the attempt fails:

          - `import ptsl` raises          -> the library isn't installed.
          - the gRPC call comes back      -> nothing is listening on
            UNAVAILABLE                      localhost:31416.
          - anything else                 -> something answered and then
                                             refused us, so Pro Tools is
                                             up and the problem is the
                                             scripting connection itself.

        Verified against a closed Pro Tools on 2026-08-11: the second
        case is a `grpc._channel._InactiveRpcError` whose `code()` is
        `StatusCode.UNAVAILABLE` ("ConnectEx: Connection refused"). The
        third is unverified against a live refusal - it is the honest
        default for "the port answered but the command didn't", not a
        case anyone has reproduced.
        """
        try:
            from ptsl import open_engine
        except ImportError as exc:
            return missing_client_library_reason("Pro Tools", "ptsl", "py-ptsl", exc)

        try:
            with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
                engine.ptsl_version()
            return None
        except Exception as exc:
            if _is_grpc_unavailable(exc):
                return (
                    "Pro Tools isn't answering on localhost:31416, so it doesn't look "
                    "like it's running - open your session in Pro Tools and try again."
                )
            return (
                f"Pro Tools is running but refused DAWBridge's scripting connection "
                f"({type(exc).__name__}: {exc}). Check that Pro Tools' scripting access "
                f"is enabled and that no dialog is waiting for you in Pro Tools."
            )

    def project_identity(self) -> str:
        from ptsl import open_engine

        try:
            with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
                return engine.session_path() or ""
        except Exception:
            return ""

    # ---- read: what's in Pro Tools right now ----------------------------

    def read_live_state(self) -> list[LiveTrack]:
        from ptsl import open_engine

        with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
            return self._read_live_state(engine)

    def _read_live_state(self, engine) -> list[LiveTrack]:
        """Engine-reusing variant: push already holds an open engine and
        opening a second one per call is both slow and needless.
        """
        clips_by_track, muted_by_track = self._pull_clips_via_text_export(engine)
        clips_by_track = clips_by_track or {}

        live = []
        for native_track in engine.track_list():
            base_name, bridge_id = parse_tag(native_track.name)
            key = _clip_bucket_key(native_track.name)
            clips = []
            for info in clips_by_track.get(key, []):
                clip_base, clip_id = parse_tag(info["name"])
                clips.append(
                    LiveClip(
                        bridge_id=clip_id,
                        name=clip_base,
                        start_seconds=info["start_seconds"],
                        length_seconds=info["length_seconds"],
                    )
                )
            live.append(
                LiveTrack(
                    bridge_id=bridge_id,
                    name=base_name,
                    channels=_track_channels(native_track),
                    muted=muted_by_track.get(key, False),
                    clips=clips,
                    native=native_track,
                )
            )
        return live

    # ---- markers (Pro Tools memory locations) ---------------------------

    def read_live_markers(self, warnings: list[str] | None = None) -> list[LiveMarker] | None:
        from ptsl import open_engine

        with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
            return self._read_live_markers(engine, warnings)

    def _read_live_markers(self, engine, warnings: list[str] | None = None) -> list[LiveMarker] | None:
        """Memory locations that are markers, as LiveMarkers, or None if
        Pro Tools can't be asked.

        PTSL does expose these properly (get_memory_locations returns
        structured messages, not display text), so unlike clips there's
        no text export to parse. What it does NOT expose is the unit of
        the position string - see _parse_pt_time. A location whose
        position can't be converted exactly is dropped WITH a warning
        rather than converted approximately.
        """
        from ptsl.PTSL_pb2 import TimeProperties

        try:
            locations = engine.get_memory_locations()
            sample_rate = int(engine.session_sample_rate())
        except Exception as exc:
            if warnings is not None:
                warnings.append(f"could not read Pro Tools' memory locations ({exc})")
            return None

        markers: list[LiveMarker] = []
        unconvertible: list[str] = []
        for location in locations:
            # A memory location can be a marker, a selection (a range) or
            # neither. Only a marker is a point in time, which is all the
            # schema can hold.
            if location.time_properties != TimeProperties.TP_Marker:
                continue
            seconds = _parse_pt_time(location.start_time, sample_rate)
            if seconds is None:
                unconvertible.append(f"{location.name!r} at {location.start_time!r}")
                continue
            base_name, bridge_id = parse_tag(location.name)
            markers.append(
                LiveMarker(bridge_id=bridge_id, name=base_name,
                           time_seconds=seconds, native=location.number)
            )

        if unconvertible and warnings is not None:
            warnings.append(
                f"{len(unconvertible)} Pro Tools memory location(s) report their position in a "
                f"format DAWBridge cannot convert exactly ({', '.join(unconvertible[:3])}"
                f"{', ...' if len(unconvertible) > 3 else ''}). Set Pro Tools' main counter to "
                f"Min:Secs or Samples and pull again - they were left out rather than published "
                f"at an approximate position"
            )
        return markers

    def _push_markers(self, engine, session: Session, warnings: list[str]) -> None:
        from ptsl.PTSL_pb2 import MemoryLocationReference, TimeProperties

        live = self._read_live_markers(engine, warnings)
        if live is None:
            return
        live_by_id = {m.bridge_id: m for m in live if m.bridge_id}
        plan = plan_markers(session.markers, set(live_by_id))

        if not plan.to_add and not plan.to_update:
            for orphan_id in plan.orphaned_ids:
                orphan = live_by_id.get(orphan_id)
                warnings.append(
                    f"marker {orphan.name if orphan else orphan_id!r} is in Pro Tools but no "
                    f"longer in the shared session; left in place, review manually"
                )
            return

        sample_rate = int(engine.session_sample_rate())

        # Every memory location needs its own number, and Pro Tools will
        # NOT pick one for you: left to itself it handed out 32000 for the
        # first marker and then rejected every one after it with "Such a
        # memory location number is already used" (confirmed live - a
        # three-marker push produced one marker and two warnings). Numbers
        # already in the session belong to the user; take the free ones.
        # Every location counts here, not just the markers in `live`: a
        # selection or a window-configuration location holds a number too
        # and would collide just the same.
        try:
            used_numbers = {loc.number for loc in engine.get_memory_locations()}
        except Exception:
            used_numbers = {m.native for m in live if isinstance(m.native, int)}
        next_number = 1

        def _claim_number() -> int:
            nonlocal next_number
            while next_number in used_numbers:
                next_number += 1
            used_numbers.add(next_number)
            return next_number

        for marker in plan.to_add:
            start = _format_pt_time(marker.time_seconds, sample_rate)
            if start is None:
                warnings.append(f"marker {marker.name!r} could not be positioned in Pro Tools; skipped")
                continue
            try:
                engine.create_memory_location(
                    memory_number=_claim_number(),
                    start_time=start,
                    name=tag(marker.name, marker.id),
                    time_properties=TimeProperties.TP_Marker,
                    reference=MemoryLocationReference.MLR_Absolute,
                )
            except Exception as exc:
                warnings.append(f"marker {marker.name!r} failed to create in Pro Tools: {exc}; skipped")

        for marker in plan.to_update:
            existing = live_by_id[marker.id]
            start = _format_pt_time(marker.time_seconds, sample_rate)
            if start is None:
                continue
            try:
                engine.edit_memory_location(
                    location_number=existing.native,
                    name=tag(marker.name, marker.id),
                    start_time=start,
                    end_time=start,
                    time_properties=TimeProperties.TP_Marker,
                    reference=MemoryLocationReference.MLR_Absolute,
                    general_properties=None,
                    comments="",
                )
            except Exception as exc:
                warnings.append(f"marker {marker.name!r} failed to move in Pro Tools: {exc}; skipped")

        for orphan_id in plan.orphaned_ids:
            orphan = live_by_id.get(orphan_id)
            warnings.append(
                f"marker {orphan.name if orphan else orphan_id!r} is in Pro Tools but no longer "
                f"in the shared session; left in place, review manually"
            )

    def _pull_markers(self, engine, session: Session, warnings: list[str]) -> None:
        live = self._read_live_markers(engine, warnings)
        if live is None:
            return

        markers_before = list(session.markers)
        pulled: list[Marker] = []
        claimed: set[str] = set()

        for observed in live:
            bridge_id = claim_live_id(observed.bridge_id, claimed)
            if observed.bridge_id and bridge_id is None:
                warnings.append(duplicate_adoption_warning("marker", observed.name, observed.bridge_id))

            existing = next((m for m in session.markers if m.id == bridge_id), None) if bridge_id else None
            if existing is not None:
                existing.name = observed.name
                existing.time_seconds = observed.time_seconds
                pulled.append(existing)
                continue

            marker = Marker(id=bridge_id or new_id(), name=observed.name,
                            time_seconds=observed.time_seconds)
            pulled.append(marker)
            if bridge_id is None:
                # Adopt: stamp the id into the name, same as tracks and
                # clips, so the next pull recognises it instead of
                # minting a second copy.
                try:
                    engine.edit_memory_location(
                        location_number=observed.native,
                        name=tag(marker.name, marker.id),
                        start_time=_format_pt_time(
                            observed.time_seconds, int(engine.session_sample_rate())
                        ),
                        end_time="",
                        time_properties=None,
                        reference=None,
                        general_properties=None,
                        comments="",
                    )
                except Exception as exc:
                    warnings.append(
                        f"marker {marker.name!r} could not be tagged in Pro Tools ({exc}), so the "
                        f"next pull will treat it as a new marker and your partner will collect a "
                        f"duplicate - rename it by hand to {tag(marker.name, marker.id)!r}"
                    )

        session.markers = pulled

        lost = [m.name for m in markers_before if m.id not in {x.id for x in pulled}]
        if lost:
            warnings.append(
                f"{len(lost)} marker(s) in the shared session are not in this Pro Tools session "
                f"and this publish removes them ({', '.join(repr(n) for n in lost[:5])}"
                f"{', ...' if len(lost) > 5 else ''})"
            )

    # ---- pull: live Pro Tools session -> canonical Session --------------

    def capture(self, session: Session, store, warnings: list[str] | None = None) -> Session:
        from ptsl import open_engine

        with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
            return self._pull(engine, session, store, warnings)

    def _pull(self, engine, session: Session, store, warnings: list[str] | None = None) -> Session:
        """Engine-reusing variant, mirroring _read_live_state - and the
        only way any of this is testable without a running Pro Tools.
        """
        if warnings is None:
            warnings = []
        tracks_before = list(session.tracks)
        rate_before = session.sample_rate

        native_tracks = engine.track_list()
        clips_by_track, muted_by_track = self._pull_clips_via_text_export(engine, warnings)

        # None (not {}) means the text export failed rather than came back
        # empty, and the difference is everything: every clip this backend
        # can see comes from that export, so treating a failure as "no
        # clips" published a session with every track emptied - a total,
        # silent wipe of the shared arrangement, from a DAW that still had
        # all of it on screen. Keep what canonical already knows instead.
        export_failed = clips_by_track is None
        clips_by_track = clips_by_track or {}

        # Replace canonical's track list (and each track's clip list)
        # wholesale with what's live right now - see sync.py's module
        # docstring for why pull doesn't merge across different
        # source projects. A track/clip already known by id keeps its
        # existing identity via merge_pulled_track/clip_by_id; one
        # not present in this pull is simply not carried forward.
        # Session.sample_rate was never written by any backend, so it
        # claimed 48000 forever regardless of what either DAW was
        # actually running at. Pro Tools reports its own rate, and
        # this is the one place we're certain of the value.
        try:
            rate = int(engine.session_sample_rate())
            if rate > 0:
                session.sample_rate = rate
        except Exception:
            pass

        for name, count in _duplicate_native_names(native_tracks).items():
            warnings.append(
                f"{count} Pro Tools tracks are called {name!r}. Clips are matched to tracks by "
                f"name here, so DAWBridge cannot tell theirs apart - rename one and pull again"
            )

        pulled_tracks = []
        claimed_track_ids: set[str] = set()

        for native_track in native_tracks:
            raw_name = native_track.name
            base_name, parsed_id = parse_tag(raw_name)
            # Two tracks claiming one id would put the same canonical
            # Track object into the list twice, and the second one's
            # clip list would overwrite the first's - see
            # sync.claim_live_id.
            bridge_id = claim_live_id(parsed_id, claimed_track_ids)
            if parsed_id and bridge_id is None:
                warnings.append(duplicate_adoption_warning("track", base_name, parsed_id))
            key = _clip_bucket_key(raw_name)
            channels = _track_channels(native_track)
            track = merge_pulled_track(session, base_name, bridge_id, kind="audio", channels=channels)
            pulled_tracks.append(track)
            # Unlike channels, mute always reflects the live DAW -
            # captured fresh on every pull, not just on first adopt.
            track.muted = muted_by_track.get(key, False)
            if bridge_id is None:
                # Same exposure as the clip rename below - a refusal here
                # must not cost the whole publish.
                try:
                    engine.rename_target_track(old_name=raw_name, new_name=tag(base_name, track.id))
                except Exception as exc:
                    warnings.append(
                        f"track {base_name!r} could not be tagged in Pro Tools ({exc}), so the "
                        f"next pull will treat it as a new track and your partner will collect a "
                        f"duplicate - rename it by hand to {tag(base_name, track.id)!r}"
                    )

            if export_failed:
                # Nothing was read, so nothing is known to have changed.
                # Leave this track's clips exactly as canonical has them.
                continue

            pulled_clips = []
            claimed_clip_ids: set[str] = set()
            for clip_info in clips_by_track.get(key, []):
                clip_base, parsed_clip_id = parse_tag(clip_info["name"])
                clip_id = claim_live_id(parsed_clip_id, claimed_clip_ids)
                if parsed_clip_id and clip_id is None:
                    warnings.append(duplicate_adoption_warning("clip", clip_base, parsed_clip_id))

                existing_clip = track.clip_by_id(clip_id) if clip_id else None
                if existing_clip is not None:
                    # Refresh position/length/name from the live DAW -
                    # see the matching comment in reaper_backend's
                    # _pull_clips. Skipping this meant edits to an
                    # already-tagged clip never left Pro Tools.
                    existing_clip.name = clip_base
                    existing_clip.start_seconds = clip_info["start_seconds"]
                    # Don't let Pro Tools shorten a looped Reaper item.
                    # Pro Tools can't loop, so it only ever holds one
                    # iteration and reporting that back as the real
                    # length destroys the loop - confirmed live, a
                    # 204.26s looped item came back as 30.63s and that
                    # truncation then got pushed into Reaper.
                    shrinks = clip_info["length_seconds"] < existing_clip.length_seconds
                    if not (existing_clip.loop_source and shrinks):
                        existing_clip.length_seconds = clip_info["length_seconds"]
                    pulled_clips.append(existing_clip)
                    continue

                audio_file = self._import_clip_audio(clip_info, store)
                if not audio_file:
                    warnings.append(
                        f"clip {clip_base!r} on track {base_name!r} has no source file Pro Tools "
                        f"could point at, so it was published without audio - your partner will "
                        f"be told it's missing rather than getting a silent clip"
                    )
                new_clip = Clip(
                    id=clip_id or new_id(),
                    name=clip_base,
                    audio_file=audio_file,
                    start_seconds=clip_info["start_seconds"],
                    length_seconds=clip_info["length_seconds"],
                )
                pulled_clips.append(new_clip)
                if clip_id is None:
                    # Adoption stamps the bridge id into the clip's name.
                    # Confirmed live against Pro Tools 2025: this can fail
                    # outright - PT_InvalidParameter ("Can't found clip:
                    # stereo_probe") when the name is ambiguous on the
                    # timeline - and unguarded it took the whole publish
                    # down with it, writing nothing at all. push() had
                    # this same failure fixed once already; the pull side
                    # never did. The clip is still published; it just
                    # won't be recognised next time, which is a warning,
                    # not a catastrophe.
                    try:
                        # rename_file=False is NOT optional. py-ptsl
                        # defaults it to True, which renames and rewrites
                        # the underlying audio file on disk - confirmed
                        # live here, not theorised: one pull turned
                        # "stereo_probe.wav" into "stereo_probe
                        # #fd13bc6f.wav" and grew it from 864044 to
                        # 870160 bytes. Pro Tools imports by reference,
                        # so the file it rewrites can be anywhere,
                        # including the shared folder. The push path was
                        # fixed for this long ago (see the module
                        # docstring); this one never was.
                        engine.rename_target_clip(
                            clip_name=clip_info["name"],
                            new_name=tag(clip_base, new_clip.id),
                            rename_file=False,
                        )
                    except Exception as exc:
                        warnings.append(
                            f"clip {clip_base!r} on track {base_name!r} could not be tagged in "
                            f"Pro Tools ({exc}), so the next pull will treat it as a new clip and "
                            f"your partner will collect a duplicate - rename it by hand to "
                            f"{tag(clip_base, new_clip.id)!r}"
                        )
            track.clips = pulled_clips

        session.tracks = pulled_tracks
        # Track.order only becomes meaningful here, with the list in
        # Pro Tools' own order - see sync.renumber_tracks.
        renumber_tracks(session.tracks)

        try:
            self._pull_markers(engine, session, warnings)
        except Exception as exc:
            warnings.append(f"markers could not be read from Pro Tools ({exc}); none were published")

        # Publishing replaces the shared session's track list; say what
        # that removes before the person who owns it finds out the hard
        # way.
        warnings.extend(describe_dropped_tracks(tracks_before, session.tracks))
        rate_change = describe_publish_sample_rate_change(rate_before, session.sample_rate)
        if rate_change:
            warnings.append(rate_change)

        # Tags stamped above only live in the running Pro Tools
        # instance until the session is saved - confirmed live,
        # closing Pro Tools without saving loses them, and the next
        # pull/push then treats everything as brand new and
        # duplicates the whole session. Save immediately so identity
        # actually persists.
        engine.save_session()

        return session

    def _use_bars_beats_counter(self, engine, warnings: list[str]) -> None:
        """Switch Pro Tools' main counter (and so the Grid) to Bars|Beats.

        A new Pro Tools session defaults to Min:Secs, which is close to
        useless for music work - the grid you snap to doesn't line up with
        the bars anyone is playing to. Reaper sessions are bars/beats by
        default, so leaving Pro Tools on Min:Secs means the two people are
        looking at different rulers for the same song.

        Only changes it when it isn't already Bars|Beats, so it never
        stomps a deliberate choice on repeat pushes.
        """
        from ptsl.PTSL_pb2 import TimelineLocationType, TrackOffsetOptions

        try:
            current = engine.get_main_counter_format().current_setting
            if current == TrackOffsetOptions.BarsBeats:
                return
            engine.set_main_counter_format(TimelineLocationType.TLType_BarsBeats)
            warnings.append(
                f"switched Pro Tools' main counter from "
                f"{TrackOffsetOptions.Name(current)} to Bars|Beats"
            )
        except Exception as exc:
            warnings.append(f"could not switch Pro Tools' counter to Bars|Beats: {exc}")

    def _import_clip_audio(self, clip_info: dict, store) -> str:
        """Bring a pulled clip's audio into the shared store, recombining
        Pro Tools' separate per-channel mono files into one interleaved
        file first. Returns the store-relative name, or "" if there's
        nothing usable.
        """
        import tempfile

        sources = [Path(p) for p in clip_info.get("source_files") or [] if p]
        sources = [p for p in sources if p.exists()]
        if not sources:
            single = clip_info.get("source_file", "")
            return store.import_audio_file(Path(single)) if single and Path(single).exists() else ""

        if len(sources) == 1:
            return store.import_audio_file(sources[0])

        # Multiple channel files - interleave into one stereo/multichannel
        # file so canonical keeps a single, complete clip. Falls back to
        # the first channel only if the parts can't be combined, which is
        # at least playable, and the caller's warning explains the rest.
        tmp_dir = Path(tempfile.mkdtemp(prefix="dawbridge_interleave_"))
        try:
            base = _split_channel_suffix(sources[0].stem)[0]
            combined = tmp_dir / f"{base}.wav"
            if audiofile.write_wav_interleaved(sources, combined):
                return store.import_audio_file(combined)
            return store.import_audio_file(sources[0])
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)

    def _pull_clips_via_text_export(
        self, engine, warnings: list[str] | None = None
    ) -> tuple[dict[str, list[dict]] | None, dict[str, bool]]:
        """Parse Pro Tools' "Session Info as Text" export for per-track
        clip name/position/duration, plus a separate clip-name -> source
        file name lookup from the "Online Clips" section, resolved to an
        absolute path via the session's Audio Files folder. Also returns
        per-track mute state, read from this same export's "STATE:" line
        (track_list()'s own track_attributes.is_muted was tried first and
        found unreliable live - it didn't flip after actually muting a
        track - so this reuses the one text-export mechanism already
        proven to work rather than adding a second, flakier source).

        Returns (None, {}) - not ({}, {}) - when Pro Tools refuses to
        produce the export at all. Callers have to be able to tell "there
        are no clips" from "I could not find out", because every clip
        this backend can see comes from here: a pull that reads a failure
        as an empty session publishes every track with its clips removed.

        Buckets are keyed by the track's FULL native name, bridge tag and
        all, deliberately. Keying by the tag-stripped name (as this used
        to) merged any two tracks whose human-readable names matched -
        "Gtr #aaaaaaaa" and "Gtr #bbbbbbbb" both reduced to "Gtr" - and
        the tag is the one thing guaranteed to tell them apart.

        Validated against a real PT 2025 export - see the module
        docstring for the differences from the original guessed format.
        """
        # Samples, not timecode. The export's timecode columns are rounded
        # to whole frames (30fps here = ~33ms), which silently quantized
        # every position on a Pro Tools pull - confirmed live, Reaper's
        # 17.87233560s came back as 17.8667s and that error then got
        # pushed back into Reaper. Samples are exact: 788170/44100 matches
        # the Reaper value to full precision.
        sample_rate = engine.session_sample_rate()
        audio_files_dir = Path(engine.session_path()).parent / "Audio Files"

        builder = engine.export_session_as_text()
        builder.include_clip_list()
        builder.include_track_edls()
        builder.all_tracks()
        builder.time_type("samples")
        try:
            text = builder.export_string()
        except Exception as exc:
            # Pro Tools can fail this outright - confirmed live, a session
            # with no tracks raised an internal assertion inside
            # UExportSessionTextCommand.cpp. That's the state a fresh
            # session is in right before someone's FIRST push, so letting
            # it propagate turned the most common starting point into a
            # traceback. Report it as "couldn't read", never as "empty".
            if warnings is not None:
                warnings.append(
                    f"Pro Tools would not export its session text ({exc}), which is the only "
                    f"way DAWBridge can see clips here. No clip was read, so the shared "
                    f"session's clips were left exactly as they were rather than being "
                    f"replaced with nothing"
                )
            return None, {}

        # logical clip name -> {channel suffix: source file name}
        name_to_source_file: dict[str, dict[str, str]] = {}
        clips_by_track: dict[str, list[dict]] = {}
        muted_by_track: dict[str, bool] = {}
        seen_clips: set[tuple[str, str, str]] = set()  # (track, logical clip name, start) dedup for .L/.R pairs

        current_track = None
        in_online_clips = False
        in_edl_rows = False

        for line in text.splitlines():
            stripped = line.strip()

            if stripped.startswith("O N L I N E") and "C L I P S" in stripped:
                in_online_clips = True
                in_edl_rows = False
                continue
            if stripped.startswith("T R A C K") and "L I S T I N G" in stripped:
                in_online_clips = False
                continue

            if in_online_clips:
                if not stripped or stripped.startswith("CLIP NAME"):
                    continue
                fields = [f.strip() for f in line.split("\t")]
                if len(fields) >= 2 and fields[1]:
                    # Keep EVERY channel's source file, not just the last
                    # one seen. Pro Tools records a stereo track as two
                    # separate mono files (.L/.R); collapsing them to one
                    # entry threw away a channel and turned a stereo track
                    # into mono - confirmed live on a "Synth" track that
                    # arrived in the shared folder as a lone .R file.
                    base, suffix = _split_channel_suffix(fields[0])
                    name_to_source_file.setdefault(base, {})[suffix] = fields[1]
                continue

            if line.startswith("TRACK NAME:"):
                raw_track_name = line.split("\t", 1)[1].strip() if "\t" in line else ""
                # Pro Tools appends a trailing "(Stereo)"/"(Mono)" format
                # annotation to this line (confirmed live - only seen on
                # non-mono tracks, which is why an earlier mono-only test
                # missed it) - strip it before the tag, or strip_tag's
                # anchored regex silently fails to match at all and every
                # stereo track's clips end up keyed under the wrong,
                # untagged dict entry.
                # Full name, tag included - see this method's docstring
                # for why stripping it here merged same-named tracks.
                current_track = _clip_bucket_key(raw_track_name)
                clips_by_track.setdefault(current_track, [])
                muted_by_track[current_track] = False
                in_edl_rows = False
                continue

            if line.startswith("STATE:") and current_track is not None:
                muted_by_track[current_track] = "Muted" in line
                continue

            if line.startswith("CHANNEL"):
                in_edl_rows = True
                continue

            if not in_edl_rows or current_track is None:
                continue

            if not stripped:
                in_edl_rows = False
                continue

            fields = [f.strip() for f in line.split("\t")]
            if len(fields) < 6:
                continue

            raw_clip_name, start_samples, _end_samples, duration_samples = (
                fields[2], fields[3], fields[4], fields[5]
            )
            clip_name = _strip_channel_suffix(raw_clip_name)

            dedup_key = (current_track, clip_name, start_samples)
            if dedup_key in seen_clips:
                continue  # the other channel of the same stereo clip - already recorded
            seen_clips.add(dedup_key)

            start_seconds = _samples_to_seconds(start_samples, sample_rate)
            length_seconds = _samples_to_seconds(duration_samples, sample_rate)
            if start_seconds is None or length_seconds is None:
                continue  # not a parseable EDL row

            # Channel order matters: interleaving R before L would swap the
            # stereo image. Sort by the suffix Pro Tools reports.
            by_channel = name_to_source_file.get(clip_name, {})
            ordered = [by_channel[k] for k in sorted(by_channel, key=_channel_sort_key)]
            source_paths = [str(audio_files_dir / n) for n in dict.fromkeys(ordered)]

            clips_by_track[current_track].append(
                {
                    "name": clip_name,
                    "start_seconds": start_seconds,
                    "length_seconds": length_seconds,
                    # One entry per channel when Pro Tools split the track
                    # into separate mono files; pull() recombines them.
                    "source_files": source_paths,
                    "source_file": source_paths[0] if source_paths else "",
                }
            )

        return clips_by_track, muted_by_track

    # ---- push: canonical Session -> live Pro Tools session --------------

    def apply(self, session: Session, store) -> list[str]:
        from ptsl import open_engine
        from ptsl.PTSL_pb2 import TrackFormat, TrackTimebase, TrackType

        warnings: list[str] = []

        with open_engine(company_name=_COMPANY, application_name=_APP) as engine:
            self._use_bars_beats_counter(engine, warnings)

            # PTSL has no tempo/meter command whatsoever (verified against
            # the full 2025 command table), so this can only be reported.
            # It matters more than it sounds: a wrong session tempo means
            # a wrong grid, and bars/beats editing silently lands in the
            # wrong place even though every clip is sample-accurate.
            warnings.append(
                f"set Pro Tools' tempo/meter by hand to {session.tempo_bpm:g} BPM, "
                f"{session.time_signature_numerator}/{session.time_signature_denominator} "
                f"(Pro Tools' scripting API exposes no tempo command, so DAWBridge cannot set it)"
            )

            try:
                rate = describe_sample_rate_mismatch(session, engine.session_sample_rate(), "Pro Tools")
                if rate:
                    warnings.append(rate)
            except Exception:
                pass

            native_tracks = engine.track_list()

            # Everything below recovers which clips are on which track by
            # parsing the session text export, which can only be keyed by
            # the track's name. Two tracks with the SAME full native name
            # would be indistinguishable - Pro Tools doesn't allow that,
            # and the bridge tag makes it doubly unlikely, but if it ever
            # happens the failure is silent (each track credited with the
            # other's clips), so it's worth the two lines.
            for name, count in _duplicate_native_names(native_tracks).items():
                warnings.append(
                    f"{count} Pro Tools tracks are called {name!r}. Clips are matched to tracks "
                    f"by name here, so DAWBridge cannot tell theirs apart - rename one and push "
                    f"again"
                )

            local_tag_to_track = {}
            for native_track in native_tracks:
                _base, bridge_id = parse_tag(native_track.name)
                if bridge_id:
                    local_tag_to_track[bridge_id] = native_track

            track_plan = plan_tracks(session, {k: v.name for k, v in local_tag_to_track.items()})

            # Pro Tools doesn't expose clip-level identity through
            # track_list() - the only way to recover which clips already
            # exist (and are tagged) on a track is the same text-export
            # parse pull() uses. Run it once up front so push is
            # idempotent instead of re-importing every clip every time.
            clips_by_track, _muted_by_track = self._pull_clips_via_text_export(engine, warnings)
            if clips_by_track is None:
                # Failure, not emptiness. Every track would look like it
                # has no clips, so every clip would be imported again -
                # the "-01/-02/-03 duplicates" symptom, across the whole
                # session at once. Stop instead.
                warnings.append(
                    "push stopped: without the session text export DAWBridge cannot tell which "
                    "clips are already in Pro Tools, and pushing anyway would import a duplicate "
                    "of every one of them. Nothing was changed"
                )
                return warnings
            clips_by_track = clips_by_track or {}

            for track in track_plan.to_create:
                # Every track is independent - one track failing outright
                # (e.g. a rejected create_new_tracks call) must not stop
                # the rest of the project from populating.
                try:
                    # Reaper tracks are always >=2 channels at the track
                    # level regardless of whether the content on them is
                    # actually mono (I_NCHAN doesn't reflect clip
                    # content), so track.channels alone is a poor signal
                    # for a brand new Pro Tools track's format. Prefer
                    # the first clip's real audio file when readable.
                    first_clip_channels = None
                    if track.clips:
                        first_clip_channels = audiofile.channel_count(
                            store.resolve_audio_path(track.clips[0].audio_file)
                        )
                    channels = first_clip_channels if first_clip_channels is not None else track.channels

                    engine.create_new_tracks(
                        number_of_tracks=1,
                        track_name=tag(track.name, track.id),
                        track_type=TrackType.TT_Audio,
                        track_format=TrackFormat.TF_Stereo if channels >= 2 else TrackFormat.TF_Mono,
                        track_timebase=TrackTimebase.TTB_Samples,
                    )
                    native_track = next(t for t in engine.track_list() if t.name == tag(track.name, track.id))
                    local_tag_to_track[track.id] = native_track
                    engine.set_track_mute_state(track_names=[native_track.name], new_state=track.muted)
                    self._push_clips(engine, native_track, track, store, warnings, local_clip_ids=set())
                except Exception as exc:
                    warnings.append(f"track {track.name!r} failed to create in Pro Tools: {exc}; skipped")

            for track, _native_name in track_plan.to_update:
                try:
                    native_track = local_tag_to_track[track.id]
                    # Capture the pre-rename base name: clips_by_track was
                    # built from a text export taken BEFORE any renames in
                    # this loop, so it's keyed by the OLD name. Looking it
                    # up by the new canonical name silently missed, left
                    # local_clip_ids empty, and re-imported every clip on
                    # the track as if it were new - confirmed live, a
                    # renamed track came back with -01/-02/-03 duplicates.
                    native_key = _clip_bucket_key(native_track.name)
                    native_base_name = strip_tag(native_track.name)
                    if native_base_name != track.name:
                        engine.rename_target_track(old_name=native_track.name, new_name=tag(track.name, track.id))
                    # No track-width check here: track.channels reflects
                    # Reaper's I_NCHAN, which is a routing-width concept
                    # (almost always >=2) unrelated to what a track's
                    # actual clip content is - not a reliable signal to
                    # compare against Pro Tools' real mono/stereo track
                    # format. The check that actually matters is
                    # per-clip, in _push_clips, against the clip's real
                    # audio file.
                    existing_clips = clips_by_track.get(native_key, [])
                    local_clip_ids = {cid for cid in (parse_tag(c["name"])[1] for c in existing_clips) if cid}
                    # tag(track.name, track.id), not native_track.name: if
                    # the rename above just ran, native_track is a local
                    # snapshot from before it and its .name is now stale.
                    engine.set_track_mute_state(track_names=[tag(track.name, track.id)], new_state=track.muted)
                    self._push_clips(engine, native_track, track, store, warnings, local_clip_ids)
                except Exception as exc:
                    warnings.append(f"track {track.name!r} failed to update in Pro Tools: {exc}; skipped")

            try:
                self._push_markers(engine, session, warnings)
            except Exception as exc:
                warnings.append(
                    f"markers could not be written into Pro Tools ({exc}); the tracks and clips "
                    f"in this push were applied, the markers were not"
                )

            # Same reasoning as pull(): tags stamped on newly-created
            # tracks/clips only live in-memory until saved.
            engine.save_session()

        return warnings

    def _push_clips(
        self, engine, native_track, track: Track, store, warnings: list[str], local_clip_ids: set[str]
    ) -> None:
        for earlier, later in find_overlapping_clips(track.clips):
            warnings.append(
                f"clips {earlier.name!r} and {later.name!r} overlap on track {track.name!r} "
                f"({later.start_seconds:.2f}s starts before {earlier.name!r} ends at "
                f"{earlier.start_seconds + earlier.length_seconds:.2f}s). Reaper layers overlapping "
                f"items; Pro Tools has one playlist per track and will overwrite/split them. Review "
                f"this track in Pro Tools"
            )

        clip_plan = plan_clips(track.clips, local_clip_ids)

        for clip in clip_plan.to_add:
            # One clip's failure (missing/corrupt audio file, a spot
            # rejection, anything) must never abort the rest of the push -
            # confirmed live, an unhandled exception here used to kill
            # every remaining track in the same push() call, which is
            # exactly the "only creates one track per click" symptom this
            # was built to fix. Every path through this block either
            # succeeds or appends a warning and moves on.
            try:
                self._push_one_clip(engine, native_track, track, clip, store, warnings)
            except Exception as exc:
                warnings.append(f"clip {clip.name!r} on track {track.name!r} failed to push: {exc}; skipped")

        for clip in clip_plan.to_update:
            # Already present and tagged, per the text-export parse above.
            # Moving/resizing an existing Pro Tools clip in place would
            # need its native clip id, which the text export doesn't
            # provide (only names) - re-spotting by name risks matching
            # the wrong clip if names collide. Left as a manual step for
            # now rather than guessing; unlike Reaper, nothing here is
            # silently skipped without being surfaced.
            warnings.append(
                f"clip {clip.name!r} on Pro Tools track {track.name!r} already exists; position/length "
                f"changes are not re-applied on push yet - move it manually if it changed"
            )

        for orphan_id in clip_plan.orphaned_ids:
            warnings.append(
                f"clip {orphan_id} on Pro Tools track {track.name!r} is no longer in the "
                f"canonical session; left in place, review manually"
            )

    def _push_one_clip(self, engine, native_track, track: Track, clip: Clip, store, warnings: list[str]) -> None:
        from ptsl.PTSL_pb2 import TrackFormat

        audio_path = store.resolve_audio_path(clip.audio_file)
        if not audio_path.exists():
            warnings.append(
                f"clip {clip.name!r} on track {track.name!r} references missing audio "
                f"{clip.audio_file!r} in the shared folder; skipped"
            )
            return

        # Pro Tools refuses to spot a clip onto a track of the wrong
        # channel width (confirmed live: mono audio onto a stereo track
        # fails the spot outright). Check up front where possible so we
        # skip cleanly instead of leaving an imported-but-unplaced clip
        # sitting in Pro Tools' clip list.
        clip_channels = audiofile.channel_count(audio_path)
        track_channels = 2 if native_track.format == TrackFormat.TF_Stereo else 1
        if clip_channels is not None and clip_channels != track_channels:
            warnings.append(
                f"clip {clip.name!r}'s audio is {clip_channels}ch but track {track.name!r} is "
                f"{track_channels}ch in Pro Tools - Pro Tools requires an exact match to place a "
                f"clip; skipped, resolve manually"
            )
            return

        source_seconds = audiofile.duration_seconds(audio_path)

        # Reaper's "loop source" repeats the audio to fill the item; Pro
        # Tools has no equivalent, so a looped clip arrives as a single
        # iteration and is shorter than the canonical length says. Say so
        # rather than let the timeline quietly differ between the two.
        if clip.loop_source and source_seconds is not None:
            if clip.length_seconds > source_seconds + 0.01:
                repeats = clip.length_seconds / source_seconds
                warnings.append(
                    f"clip {clip.name!r} on track {track.name!r} is a looped Reaper item "
                    f"({source_seconds:.2f}s source repeated to {clip.length_seconds:.2f}s, ~{repeats:.1f}x). "
                    f"Pro Tools has no loop-to-length, so it got a single {source_seconds:.2f}s iteration - "
                    f"duplicate it there, or consolidate the item in Reaper and re-pull"
                )

        import_result = self._import_audio_to_clip_list(engine, audio_path, clip)
        if not import_result:
            warnings.append(
                f"clip {clip.name!r} on track {track.name!r} failed to import into Pro Tools "
                f"(see logs); skipped"
            )
            return
        clip_ids, native_clip_name = import_result

        try:
            self._spot_clip_by_id(engine, clip_ids, native_track.id, clip.start_seconds)
        except Exception as exc:
            warnings.append(
                f"clip {clip.name!r} on track {track.name!r} imported but could not be spotted "
                f"(likely a track-width mismatch between the clip's audio and the track): {exc}"
            )
            return

        # No trim step here on purpose - the audio was sliced to the exact
        # region before import (see _import_audio_to_clip_list), because
        # Pro Tools' trim_to_selection works on the edit selection and was
        # confirmed live to delete a neighbouring clip on the same track.

        # Pro Tools truncates clip names on import to some internal limit
        # (confirmed live: a 39-char computed name came back truncated by
        # a few characters) - renaming FROM our own computed name doesn't
        # match the clip's real current name, and rename_target_clip
        # silently no-ops on a non-match rather than raising, so this
        # would otherwise fail invisibly and leave every clip untagged.
        # Look up the clip's actual current name by re-scanning the
        # track for whatever's sitting at the position we just spotted.
        # Keyed by the track's full native name, which after any rename in
        # this push is tag(track.name, track.id) - native_track.name may be
        # a pre-rename snapshot, same trap as the mute call above.
        actual_name = (
            self._find_clip_name_at_position(engine, tag(track.name, track.id), clip.start_seconds)
            or native_clip_name
        )

        # rename_file=False: this clip name comes from a copy inside Pro
        # Tools' own session-local Audio Files folder (see
        # _import_audio_to_clip_list), but pass it explicitly anyway
        # rather than relying on that - rename_target_clip defaults to
        # ALSO renaming the underlying file on disk, which is
        # catastrophic if this is ever pointed at a real shared file
        # instead of a local copy (confirmed live: it silently renamed
        # and rewrote a real shared audio file when import had
        # referenced one directly).
        engine.rename_target_clip(clip_name=actual_name, new_name=tag(clip.name, clip.id), rename_file=False)

    def _find_clip_name_at_position(self, engine, track_name: str, start_seconds: float) -> str | None:
        """Re-scan the track listing for whatever clip is sitting closest
        to `start_seconds` on `track_name`, and return its real (possibly
        truncated) current name - see _push_one_clip for why this can't
        just be the name we computed before import.
        """
        clips_by_track, _muted_by_track = self._pull_clips_via_text_export(engine)
        candidates = (clips_by_track or {}).get(_clip_bucket_key(track_name), [])
        if not candidates:
            return None
        closest = min(candidates, key=lambda c: abs(c["start_seconds"] - start_seconds))
        if abs(closest["start_seconds"] - start_seconds) > 0.2:
            return None
        return closest["name"]

    def _import_audio_to_clip_list(self, engine, audio_path: Path, clip: Clip | None = None):
        """Import a file into the session's clip list (not yet placed on
        any track). Returns (clip_ids, native_clip_name) - clip_ids is a
        list because a stereo interleaved file comes back as two linked
        mono-channel clip ids (confirmed live: `.L`/`.R`), both of which
        need to be passed to SpotClipsByID together to move as a pair.
        Returns None on failure. Bypasses py-ptsl's broken
        `engine.import_audio()` - see module docstring.

        Copies the file into the session's own "Audio Files" folder
        first and imports *that* copy, never the caller's real path
        directly. Pro Tools' AddAudio imports "by reference" (confirmed
        live - it does not make its own copy), and rename_target_clip
        defaults to renaming the underlying file on disk too. Handed a
        direct path into the shared folder, that combination silently
        renamed *and rewrote the header of* a real shared audio file.
        Pro Tools sessions are expected to own their Audio Files folder
        content, so that's the right permanent home for this copy -
        deleting it after import would leave the new clip pointing at a
        file that no longer exists.
        """
        import shutil

        import ptsl.ops as ops
        from ptsl.PTSL_pb2 import AudioOperations

        class CId_ImportAudioToClipList(ops.Operation):
            pass

        audio_files_dir = Path(engine.session_path()).parent / "Audio Files"
        audio_files_dir.mkdir(parents=True, exist_ok=True)

        # Slice to exactly the region this clip uses, so Pro Tools never
        # needs a post-spot trim (which is destructive to neighbours) and
        # so Clip.source_offset is actually honoured. A distinct name per
        # (offset, length) keeps two clips sharing one source file from
        # colliding.
        source_seconds = audiofile.duration_seconds(audio_path)
        needs_slice = clip is not None and (
            clip.source_offset_seconds > 0.001
            or (source_seconds is not None and clip.length_seconds < source_seconds - 0.01)
        )

        dest_path = audio_files_dir / audio_path.name
        if needs_slice:
            stem, suffix = audio_path.stem, audio_path.suffix
            slice_name = f"{stem}_{clip.source_offset_seconds:.3f}-{clip.length_seconds:.3f}{suffix}"
            sliced_path = audio_files_dir / slice_name
            if sliced_path.exists() or audiofile.write_wav_slice(
                audio_path, sliced_path, clip.source_offset_seconds, clip.length_seconds
            ):
                dest_path = sliced_path
            elif not dest_path.exists():
                shutil.copy2(audio_path, dest_path)  # unparseable; fall back to whole file
        elif not dest_path.exists():
            shutil.copy2(audio_path, dest_path)

        # ConvertAudio always re-encodes, even when nothing needs
        # converting - confirmed live: it fails outright (empty
        # CommandError, likely a timeout) on a real multi-minute file.
        # AddAudio (no conversion) is instant when the file already
        # matches the session's sample rate, which is the common case
        # since these files came from a dawbridge pull.
        source_rate = audiofile.sample_rate(dest_path)
        session_rate = engine.session_sample_rate()
        operation = (
            AudioOperations.AddAudio
            if source_rate is not None and source_rate == session_rate
            else AudioOperations.ConvertAudio
        )

        op = CId_ImportAudioToClipList(file_list=[str(dest_path)], audio_operations=operation)
        engine.client.run(op)

        # Collect clip ids across EVERY destination_file_list entry, not
        # just the first. The two import modes report a stereo file
        # differently (confirmed live): AddAudio returns one entry with
        # two clip ids, while ConvertAudio splits the file into separate
        # mono `.L`/`.R` files - two entries, one clip id each. Reading
        # only the first entry meant a converted stereo file spotted a
        # lone mono clip onto a stereo track, which Pro Tools rejects
        # with a confusing "incompatibilities between selected clips and
        # destination track" error.
        clip_ids: list[str] = []
        native_name = None
        for entry in op.response.file_list:
            for file_entry in entry.destination_file_list:
                clip_ids.extend(file_entry.clip_id_list)
                if native_name is None and file_entry.clip_id_list:
                    native_name = _strip_channel_suffix(Path(file_entry.file_path).stem)

        if not clip_ids:
            return None
        return clip_ids, native_name

    def _spot_clip_by_id(self, engine, clip_ids: list[str], track_id: str, start_seconds: float) -> None:
        """clip_ids is normally one id, but a stereo interleaved import
        produces two linked mono-channel clip ids (`.L`/`.R`) that must
        be spotted together in one call to move as a pair - confirmed
        live, spotting only the first id alone would place just one
        channel.
        """
        import ptsl.ops as ops
        from ptsl.PTSL_pb2 import SpotLocationType, TimelineLocationType

        class CId_SpotClipsByID(ops.Operation):
            pass

        op = CId_SpotClipsByID(
            src_clips=clip_ids,
            dst_track_id=track_id,
            dst_location_data={
                "location_type": SpotLocationType.Start,
                "location": {
                    "location": str(start_seconds),
                    "time_type": TimelineLocationType.TLType_Seconds,
                },
            },
        )
        engine.client.run(op)

    def _trim_clip_to_length(
        self, engine, native_track, start_seconds: float, length_seconds: float, source_seconds: float | None
    ) -> None:
        """Trim the just-spotted clip down to its canonical length, but
        ONLY when it's actually too long.

        `trim_to_selection` acts on the edit selection, not on one clip,
        so it will happily delete OTHER clips on the same track that fall
        outside the selection. Confirmed live: pushing a clip at 25.5s
        silently destroyed the neighbouring clip sitting at 0-25.5s,
        which then vanished from canonical on the next pull. So: skip the
        trim entirely when the imported clip is already the right length
        (the common case, since the audio came from a dawbridge pull of
        exactly this clip), and when a trim IS needed, first narrow the
        track selection to just this track to bound the blast radius.
        """
        from ptsl.PTSL_pb2 import TimelineLocationType

        # Nothing to trim: the source is already at (or shorter than) the
        # canonical length, and Pro Tools can't fabricate extra audio.
        if source_seconds is not None and source_seconds <= length_seconds + 0.01:
            return

        try:
            engine.select_tracks_by_name(names=[native_track.name])
            engine.set_timeline_selection(
                in_time=str(start_seconds),
                out_time=str(start_seconds + length_seconds),
                location_type=TimelineLocationType.TLType_Seconds,
            )
            engine.trim_to_selection()
        except Exception:
            pass


#: A memory location's position is a bare string with no unit attached -
#: CreateMemoryLocationRequestBody has no time-type field, unlike
#: SpotClipsByID and set_timeline_selection which both carry an explicit
#: TimelineLocationType. That looked like an unresolvable ambiguity, so
#: this backend used to learn the format from an existing marker and
#: refuse to write when there wasn't one.
#:
#: SETTLED BY LIVE TEST against Pro Tools 2025, and the answer is much
#: better than the guess:
#:   - get_memory_locations() ALWAYS reports start_time in samples,
#:     regardless of the main counter format. Verified by setting the
#:     counter to Bars|Beats, TimeCode and Min:Secs in turn and re-reading
#:     the same marker: '480000' every time. That matters because
#:     _use_bars_beats_counter() changes the counter on every push.
#:   - Writing accepts several formats and resolves them correctly:
#:     "00:00:10:00", "480000" and "0:10.000" all landed at exactly
#:     480000 samples in a 48kHz session, cross-checked against the
#:     session text export in samples.
#: So samples are exact in both directions and there is nothing to guess.
_PT_SAMPLES_RE = re.compile(r"^\d+$")
_PT_MINSECS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:\.\d+)?)$")


def _parse_pt_time(text: str, sample_rate: int) -> float | None:
    """Memory location position string -> seconds, or None if it isn't a
    format that converts exactly.

    Samples is what Pro Tools 2025 actually reports (confirmed live).
    Min:Secs is accepted too because it costs nothing and would otherwise
    be a silent drop if a future version changed its mind. Anything else
    - timecode (quantised to whole frames), bars|beats (needs a tempo map
    canonical doesn't have) - returns None so the caller can say so
    instead of placing the marker approximately.
    """
    text = (text or "").strip()
    if _PT_SAMPLES_RE.match(text):
        return int(text) / float(sample_rate) if sample_rate else None
    match = _PT_MINSECS_RE.match(text)
    if match:
        hours, minutes, seconds = match.groups()
        return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)
    return None


def _format_pt_time(seconds: float, sample_rate: int) -> str | None:
    """Seconds -> the sample-count string Pro Tools reports and accepts."""
    if seconds < 0 or not sample_rate:
        return None
    return str(int(round(seconds * sample_rate)))


def _clip_bucket_key(track_name: str) -> str:
    """The key clips are filed under for a given track.

    The track's full native name with only Pro Tools' "(Stereo)"/"(Mono)"
    display annotation removed - the bridge tag STAYS. That tag is what
    distinguishes two tracks a musician gave the same name, and stripping
    it here was the whole of the collapse described in
    _pull_clips_via_text_export.

    Used on both sides of the lookup - the export's "TRACK NAME:" line
    (which carries the annotation) and track_list()'s name (which doesn't)
    - so both reduce to the same string.
    """
    return _strip_track_format_suffix(track_name).strip()


def _track_channels(native_track) -> int:
    """2 for a stereo Pro Tools track, 1 otherwise.

    A function rather than an inline comparison so the PTSL enum import
    lives in one place and tests can substitute it without a running Pro
    Tools.
    """
    from ptsl.PTSL_pb2 import TrackFormat

    return 2 if native_track.format == TrackFormat.TF_Stereo else 1


def _duplicate_native_names(native_tracks) -> dict[str, int]:
    """{name: count} for full native track names that appear more than
    once - tracks the text-export lookup genuinely cannot tell apart.

    Pro Tools enforces unique track names, so this should always be
    empty; it's here because if that ever stops being true the failure is
    silent (each track credited with the other's clips) rather than loud.
    """
    counts: dict[str, int] = {}
    for native_track in native_tracks:
        key = _clip_bucket_key(native_track.name)
        counts[key] = counts.get(key, 0) + 1
    return {name: count for name, count in counts.items() if count > 1 and name}


_CHANNEL_ORDER = {"L": 0, "R": 1, "": 0}


def _split_channel_suffix(clip_name: str) -> tuple[str, str]:
    """("Synth_01", "L") for "Synth_01.L"; ("Synth_01", "") when unsuffixed."""
    if len(clip_name) > 2 and clip_name[-2] == "." and clip_name[-1] in ("L", "R"):
        return clip_name[:-2], clip_name[-1]
    return clip_name, ""


def _channel_sort_key(suffix: str) -> tuple[int, str]:
    """L before R; anything unexpected sorts after both, alphabetically,
    so an unfamiliar multichannel layout stays deterministic instead of
    silently shuffling channels between pulls.
    """
    return (_CHANNEL_ORDER.get(suffix, 99), suffix)


def _strip_channel_suffix(clip_name: str) -> str:
    """Pro Tools reports a stereo interleaved clip as two rows, one per
    channel, with the display name suffixed `.L`/`.R` - confirmed live.
    Strip that so both rows collapse to the same logical clip name.
    """
    if clip_name.endswith(".L") or clip_name.endswith(".R"):
        return clip_name[:-2]
    return clip_name

def _strip_track_format_suffix(track_name: str) -> str:
    """Pro Tools appends a trailing "(Stereo)"/"(Mono)" format annotation
    to the TRACK NAME line in the text export (confirmed live) - strip it
    before tag parsing, or the anchored tag regex never matches.
    """
    return re.sub(r"\s*\([^)]*\)\s*$", "", track_name)

def _samples_to_seconds(samples: str, sample_rate: int) -> float | None:
    """Sample-count column from the text export -> seconds. None when the
    field isn't a plain integer, which is how non-EDL rows get skipped.
    """
    text = samples.strip().replace(",", "")
    if not text.isdigit() or not sample_rate:
        return None
    return int(text) / float(sample_rate)
