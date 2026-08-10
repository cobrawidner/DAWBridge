"""Reaper-side backend, driving a *running* Reaper instance live via
python-reapy (https://python-reapy.readthedocs.io/).

NOT TESTED against a real Reaper instance - there's no Reaper available in
the sandbox this was written in. The ReaScript function names used below
(RPR_* calls, accessed through reapy.reascript_api) are long-stable parts
of the ReaScript API and are used here in their well-documented forms, but
reapy's exact argument conventions for buffer-style get/set calls can
differ slightly by reapy version. Treat this file as a strong starting
point to run and adjust against your actual installed reapy, not as
guaranteed-correct on the first try.

One-time setup (see README):
    pip install python-reapy
    python -c "import reapy; reapy.configure_reaper()"
    (then restart Reaper)
"""
from __future__ import annotations

from pathlib import Path

from .backend import Backend, LiveClip, LiveMarker, LiveTrack
from .model import Clip, Marker, Session, Track, new_id
from .sync import (
    claim_live_id,
    plan_markers,
    describe_dropped_tracks,
    describe_meter_mismatch,
    describe_publish_sample_rate_change,
    describe_sample_rate_mismatch,
    describe_tempo_map_flattening,
    duplicate_adoption_warning,
    merge_pulled_track,
    missing_audio_warning,
    plan_clips,
    plan_tracks,
    renumber_tracks,
    should_reimport_audio,
)
from .tagging import parse_tag, strip_tag, tag


#: EnumProjectMarkers2 fills out-parameters, and reapy's wrapper returns
#: them as a tuple. Which slot holds what depends on whether that version
#: prepends the C return value - this file's module docstring already
#: warns that reapy's buffer-style conventions differ by version, and
#: guessing wrong here would read a marker's position out of the field
#: holding its region end, i.e. put every marker at 0.
#: (retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)
_MARKER_ROW_LAYOUTS = ((3, 4, 6, 7), (2, 3, 5, 6))


def _decode_marker_row(row) -> tuple[bool, float, str, int] | None:
    """(is_region, position_seconds, name, marker_index) from one
    EnumProjectMarkers2 row, or None if the row makes no sense.

    Picks the layout by checking the shape of what it finds rather than
    trusting the length alone: the name field must be a string and the
    position a number. A row that matches neither layout is skipped by
    the caller with a warning, because a misread marker is worse than a
    missing one.
    """
    for isrgn_at, pos_at, name_at, index_at in _MARKER_ROW_LAYOUTS:
        if len(row) <= index_at:
            continue
        name, position = row[name_at], row[pos_at]
        if isinstance(name, str) and isinstance(position, (int, float)) and not isinstance(position, bool):
            try:
                return bool(row[isrgn_at]), float(position), name, int(row[index_at])
            except (TypeError, ValueError):
                continue
    return None


def _import_audio(store, source_path: str, warnings=None, clip_name: str = "", track_name: str = "") -> str:
    """Copy a take's source into the shared store, or "" if it isn't there.

    Reaper keeps items whose media has gone offline (a renamed folder, an
    external drive that isn't plugged in), and hashing a file that doesn't
    exist raised straight out of the middle of the pull - one offline item
    aborted the whole publish with a traceback and nothing was written.
    Returning "" instead lets the rest of the session publish, and the
    warning says which clip went out without audio; preview/push then
    refuse to place it rather than dropping a silent item on the
    timeline (sync.missing_audio_warning).
    """
    if not source_path:
        return ""
    path = Path(source_path)
    try:
        if path.is_file():
            return store.import_audio_file(path)
    except OSError:
        pass
    if warnings is not None:
        warnings.append(
            f"clip {clip_name!r} on track {track_name!r} plays {path.name!r}, which Reaper "
            f"can't find on disk - it was published with no audio, so your partner will be "
            f"told it's missing rather than getting a silent clip. Relink it in Reaper and "
            f"publish again"
        )
    return ""


def _take_source_path(RPR, take) -> str:
    """Absolute path of a take's source file, or "" if unavailable."""
    try:
        source = RPR.GetMediaItemTake_Source(take.id)
        return RPR.GetMediaSourceFileName(source, "", 4096)[1]
    except Exception:
        return ""


class ReaperBackend(Backend):
    name = "reaper"

    def is_available(self) -> bool:
        try:
            import reapy

            return reapy.is_inside_reaper() or reapy.dist_api_is_enabled()
        except Exception:
            return False

    def project_identity(self) -> str:
        from reapy import reascript_api as RPR

        try:
            return RPR.EnumProjects(-1, "", 4096)[2] or ""
        except Exception:
            return ""

    # ---- read: what's in Reaper right now ------------------------------

    def read_live_state(self) -> list[LiveTrack]:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()
        live = []
        for idx in range(project.n_tracks):
            native_track = project.tracks[idx]
            base_name, bridge_id = parse_tag(native_track.name)
            clips = []
            for i in range(native_track.n_items):
                item = native_track.items[i]
                take = item.active_take
                if take is None:
                    continue
                clip_base, clip_id = parse_tag(take.name)
                clips.append(
                    LiveClip(
                        bridge_id=clip_id,
                        name=clip_base,
                        start_seconds=item.position,
                        length_seconds=item.length,
                        source_path=_take_source_path(RPR, take),
                        native=item,
                    )
                )
            live.append(
                LiveTrack(
                    bridge_id=bridge_id,
                    name=base_name,
                    channels=int(RPR.GetMediaTrackInfo_Value(native_track.id, "I_NCHAN")),
                    muted=native_track.is_muted,
                    clips=clips,
                    native=native_track,
                )
            )
        return live

    # ---- markers -------------------------------------------------------

    def read_live_markers(
        self, warnings: list[str] | None = None, for_publish: bool = False
    ) -> list[LiveMarker]:
        """Project markers, tagged and untagged. Regions are deliberately
        not included - the schema models a marker as a point in time and
        has nowhere to put a region's end, so publishing one would round
        it to its start and silently destroy the range.

        `for_publish` gates the regions warning to the pull path, where
        it's true that they aren't crossing the bridge. Repeating it on
        every push would be noise about something that push isn't doing,
        and a warning people learn to skim is worse than none.
        """
        from reapy import reascript_api as RPR

        _retval, _proj, n_markers, n_regions = RPR.CountProjectMarkers(0, 0, 0)
        markers: list[LiveMarker] = []
        regions = 0
        unreadable = 0

        for index in range(int(n_markers) + int(n_regions)):
            decoded = _decode_marker_row(RPR.EnumProjectMarkers2(0, index, 0, 0, 0, "", 0))
            if decoded is None:
                unreadable += 1
                continue
            is_region, position, raw_name, marker_index = decoded
            if is_region:
                regions += 1
                continue
            base_name, bridge_id = parse_tag(raw_name)
            markers.append(
                LiveMarker(bridge_id=bridge_id, name=base_name,
                           time_seconds=position, native=marker_index)
            )

        if warnings is not None and for_publish and regions:
            warnings.append(
                f"this project has {regions} region(s), which DAWBridge does not sync - the "
                f"shared session models a marker as a single point and has nowhere to keep a "
                f"region's end, so publishing one would quietly flatten it to its start. "
                f"Markers cross the bridge; regions stay put"
            )
        if warnings is not None and unreadable:
            warnings.append(
                f"{unreadable} marker(s) could not be read from Reaper (unrecognised ReaScript "
                f"reply) and were left out of the publish rather than published at a guessed "
                f"position"
            )
        return markers

    def _pull_markers(self, session: Session, warnings: list[str]) -> None:
        """Replace canonical's marker list with what's in Reaper now.

        Wholesale, like tracks and clips - see sync.py's docstring. An
        untagged marker is adopted (stamped with a new id) so the next
        pull recognises it instead of adding a second copy.
        """
        from reapy import reascript_api as RPR

        markers_before = list(session.markers)
        pulled: list[Marker] = []
        claimed: set[str] = set()

        for live in self.read_live_markers(warnings, for_publish=True):
            bridge_id = claim_live_id(live.bridge_id, claimed)
            if live.bridge_id and bridge_id is None:
                warnings.append(duplicate_adoption_warning("marker", live.name, live.bridge_id))

            existing = next((m for m in session.markers if m.id == bridge_id), None) if bridge_id else None
            if existing is not None:
                existing.name = live.name
                existing.time_seconds = live.time_seconds
                pulled.append(existing)
                continue

            marker = Marker(id=bridge_id or new_id(), name=live.name, time_seconds=live.time_seconds)
            pulled.append(marker)
            if live.bridge_id is None or bridge_id is None:
                # Adopt it: stamp the id into the name so it's recognised
                # next time. Same reasoning as tracks and clips - without
                # this every pull would mint a new id and the other DAW
                # would collect a duplicate marker per sync.
                RPR.SetProjectMarker2(
                    0, live.native, False, live.time_seconds, 0, tag(marker.name, marker.id)
                )

        session.markers = pulled

        lost = [m.name for m in markers_before if m.id not in {x.id for x in pulled}]
        if lost:
            warnings.append(
                f"{len(lost)} marker(s) in the shared session are not in this project and this "
                f"publish removes them ({', '.join(repr(n) for n in lost[:5])}"
                f"{', ...' if len(lost) > 5 else ''})"
            )

    def _push_markers(self, session: Session, warnings: list[str]) -> None:
        """Apply canonical's markers into Reaper. Never deletes."""
        from reapy import reascript_api as RPR

        live = self.read_live_markers(warnings)
        live_by_id = {m.bridge_id: m for m in live if m.bridge_id}
        plan = plan_markers(session.markers, set(live_by_id))

        for marker in plan.to_add:
            RPR.AddProjectMarker2(
                0, False, marker.time_seconds, 0, tag(marker.name, marker.id), -1, 0
            )

        for marker in plan.to_update:
            existing = live_by_id[marker.id]
            RPR.SetProjectMarker2(
                0, existing.native, False, marker.time_seconds, 0, tag(marker.name, marker.id)
            )

        for orphan_id in plan.orphaned_ids:
            orphan = live_by_id.get(orphan_id)
            warnings.append(
                f"marker {orphan.name if orphan else orphan_id!r} is in your Reaper project but "
                f"no longer in the shared session; left in place, review manually"
            )

    # ---- pull: live Reaper project -> canonical Session ----------------

    def pull(self, session: Session, store, warnings: list[str] | None = None) -> Session:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()
        if warnings is None:
            warnings = []  # local sink, so every append below is unconditional
        tracks_before = list(session.tracks)
        rate_before = session.sample_rate

        # Session tempo/meter drives the grid on both sides, so capture it
        # even though Pro Tools can only be told about it in a warning
        # (PTSL has no tempo command at all).
        _p, _t, num, denom, tempo = RPR.TimeMap_GetTimeSigAtTime(0, 0.0, 0, 0, 0)
        session.tempo_bpm = float(tempo)
        session.time_signature_numerator = int(num)
        session.time_signature_denominator = int(denom)

        # Canonical holds ONE tempo. A project with tempo changes through
        # it publishes only the value at the start, and until now nothing
        # said so - the push side refuses to overwrite a tempo map, but
        # the publish had already dropped everything past bar one.
        try:
            flattened = describe_tempo_map_flattening(
                RPR.CountTempoTimeSigMarkers(0), session.tempo_bpm
            )
            if flattened:
                warnings.append(flattened)
        except Exception:
            pass

        # Session.sample_rate has been in the schema since day one and was
        # never written by anything, so it read 48000 forever - including
        # in the real shared session, which carries 44.1k audio. Capture
        # it so the two sides can at least be told they disagree (see
        # describe_sample_rate_mismatch). PROJECT_SRATE only applies when
        # PROJECT_SRATE_USE is on; when it's off Reaper follows the audio
        # device, and claiming a rate we didn't really read would be worse
        # than leaving the previous one alone.
        try:
            if RPR.GetSetProjectInfo(0, "PROJECT_SRATE_USE", 0, False):
                rate = int(RPR.GetSetProjectInfo(0, "PROJECT_SRATE", 0, False))
                if rate > 0:
                    session.sample_rate = rate
        except Exception:
            pass

        # Replace canonical's track list wholesale with what's live right
        # now - see sync.py's module docstring for why pull doesn't merge
        # across different source projects. A track already known by id
        # keeps its existing clip history via merge_pulled_track; a track
        # not present in this pull is simply not carried forward.
        pulled_tracks = []
        # Duplicating a track in Reaper copies its name, bridge tag
        # included, so two tracks can claim one identity. Whoever gets
        # there first keeps it; the copy is adopted as a new track below.
        claimed_track_ids: set[str] = set()

        for idx in range(project.n_tracks):
            native_track = project.tracks[idx]
            raw_name = native_track.name
            base_name, parsed_id = parse_tag(raw_name)
            bridge_id = claim_live_id(parsed_id, claimed_track_ids)
            if parsed_id and bridge_id is None:
                warnings.append(duplicate_adoption_warning("track", base_name, parsed_id))
            n_channels = int(RPR.GetMediaTrackInfo_Value(native_track.id, "I_NCHAN"))
            track = merge_pulled_track(session, base_name, bridge_id, kind="audio", channels=n_channels)
            pulled_tracks.append(track)
            # Unlike channels, mute always reflects the live DAW - captured
            # fresh on every pull, not just when the track is first adopted.
            track.muted = native_track.is_muted
            if bridge_id is None:
                # Adopt this local-only track: stamp it with its new id so
                # it's recognized next time.
                RPR.GetSetMediaTrackInfo_String(
                    native_track.id, "P_NAME", tag(base_name, track.id), True
                )

            self._pull_clips(native_track, track, session, store, warnings)

        session.tracks = pulled_tracks
        # Track.order is what `dawbridge status` and the GUI sort by; it
        # only becomes true here, once the list is in Reaper's order.
        renumber_tracks(session.tracks)

        try:
            self._pull_markers(session, warnings)
        except Exception as exc:
            # Markers are worth having but not worth losing a publish
            # over - the tracks and clips in hand are the valuable part.
            warnings.append(f"markers could not be read from Reaper ({exc}); none were published")

        # Publishing replaces the shared session's track list. Say which
        # tracks that removes, before the person who owns them finds out
        # by not finding them.
        warnings.extend(describe_dropped_tracks(tracks_before, session.tracks))
        rate_change = describe_publish_sample_rate_change(rate_before, session.sample_rate)
        if rate_change:
            warnings.append(rate_change)

        # Tags stamped above only live in the running Reaper instance
        # until the project is saved - confirmed live, closing Reaper
        # without saving loses them, and the next pull then treats every
        # track as brand new and duplicates the whole session. Save
        # immediately so identity actually persists.
        project.save()

        return session

    def _pull_clips(self, native_track, track: Track, session: Session, store, warnings: list[str]) -> None:
        from reapy import reascript_api as RPR

        # Replace track.clips wholesale with what's live now - same
        # reasoning as pull()'s track-list replacement (see sync.py).
        pulled_clips = []
        # Same story as tracks: "duplicate items" copies the take name and
        # so the bridge tag with it.
        claimed_clip_ids: set[str] = set()

        n_items = native_track.n_items
        for i in range(n_items):
            item = native_track.items[i]
            take = item.active_take
            if take is None:
                continue
            raw_name = take.name
            base_name, parsed_clip_id = parse_tag(raw_name)
            clip_id = claim_live_id(parsed_clip_id, claimed_clip_ids)
            if parsed_clip_id and clip_id is None:
                warnings.append(duplicate_adoption_warning("clip", base_name, parsed_clip_id))

            source_offset = RPR.GetMediaItemTakeInfo_Value(take.id, "D_STARTOFFS")
            loop_source = bool(RPR.GetMediaItemInfo_Value(item.id, "B_LOOPSRC"))
            # Fades were in the schema and written on push, but nothing
            # ever read them, so every clip published from Reaper claimed
            # 0.0 and the crossfades between butt-joined clips arrived on
            # the other side as clicks.
            fade_in = float(RPR.GetMediaItemInfo_Value(item.id, "D_FADEINLEN") or 0.0)
            fade_out = float(RPR.GetMediaItemInfo_Value(item.id, "D_FADEOUTLEN") or 0.0)
            live_source_path = _take_source_path(RPR, take)

            existing_clip = track.clip_by_id(clip_id) if clip_id else None
            if existing_clip is not None:
                # Refresh position/length/name from the live DAW. This
                # used to `continue` here on the theory that the push
                # side would compare positions - it doesn't and can't,
                # because canonical was never updated to differ. The
                # effect was that moving or resizing an already-tagged
                # clip was silently invisible to the other DAW forever.
                existing_clip.name = base_name
                existing_clip.start_seconds = item.position
                existing_clip.length_seconds = item.length
                existing_clip.source_offset_seconds = source_offset
                existing_clip.loop_source = loop_source
                existing_clip.fade_in_seconds = fade_in
                existing_clip.fade_out_seconds = fade_out
                # Replacing the audio under an already-tagged clip - a
                # re-record, a comp, "apply track FX to items" - used to
                # publish as no change at all, and the next push then
                # re-pointed the take back to canonical's older file,
                # silently undoing the user's own edit. See
                # sync.should_reimport_audio.
                if should_reimport_audio(live_source_path, existing_clip, store):
                    existing_clip.audio_file = (
                        _import_audio(store, live_source_path, warnings, base_name, track.name)
                        or existing_clip.audio_file
                    )
                pulled_clips.append(existing_clip)
                continue

            audio_file = _import_audio(store, live_source_path, warnings, base_name, track.name)

            new_clip = Clip(
                id=clip_id or new_id(),
                name=base_name,
                audio_file=audio_file,
                start_seconds=item.position,
                length_seconds=item.length,
                source_offset_seconds=source_offset,
                fade_in_seconds=fade_in,
                fade_out_seconds=fade_out,
                loop_source=loop_source,
            )
            pulled_clips.append(new_clip)
            if clip_id is None:
                RPR.GetSetMediaItemTakeInfo_String(
                    take.id, "P_NAME", tag(base_name, new_clip.id), True
                )

        track.clips = pulled_clips

    # ---- push: canonical Session -> live Reaper project -----------------

    def push(self, session: Session, store) -> list[str]:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()

        warnings: list[str] = []

        # Apply session tempo, but never silently flatten a tempo MAP:
        # canonical only models one tempo, so a project with multiple
        # tempo/time-sig markers would lose them. Leave those alone and
        # say so instead.
        n_tempo_markers = RPR.CountTempoTimeSigMarkers(0)
        _p, _t, cur_num, cur_denom, cur_tempo = RPR.TimeMap_GetTimeSigAtTime(0, 0.0, 0, 0, 0)
        if n_tempo_markers > 1:
            warnings.append(
                f"this Reaper project has {n_tempo_markers} tempo/time-signature markers; DAWBridge "
                f"models a single session tempo, so the tempo map was left untouched "
                f"(canonical tempo is {session.tempo_bpm:g} BPM)"
            )
        elif abs(float(cur_tempo) - session.tempo_bpm) > 1e-6:
            RPR.SetCurrentBPM(0, session.tempo_bpm, True)

        # The tempo above is applied; the METER never is - SetCurrentBPM
        # doesn't touch it and there's no meter write anywhere in this
        # backend. Pull captures it, so a 6/8 song crossing the bridge
        # arrives at the right BPM on a 4/4 grid, silently. Say so rather
        # than let the two people edit against different rulers.
        meter = describe_meter_mismatch(session, cur_num, cur_denom)
        if meter:
            warnings.append(meter)

        try:
            if RPR.GetSetProjectInfo(0, "PROJECT_SRATE_USE", 0, False):
                rate = describe_sample_rate_mismatch(
                    session, int(RPR.GetSetProjectInfo(0, "PROJECT_SRATE", 0, False)), "Reaper"
                )
                if rate:
                    warnings.append(rate)
        except Exception:
            pass

        # Same reading the preview uses, so a previewed change and the
        # change actually applied here can't disagree.
        local_tag_to_track = {t.bridge_id: t.native for t in self.read_live_state() if t.bridge_id}

        track_plan = plan_tracks(session, {k: v.name for k, v in local_tag_to_track.items()})

        for track in track_plan.to_create:
            new_index = project.n_tracks
            project.add_track(new_index, name=tag(track.name, track.id))
            native_track = project.tracks[new_index]
            RPR.SetMediaTrackInfo_Value(native_track.id, "I_NCHAN", max(2, track.channels))
            native_track.is_muted = track.muted
            local_tag_to_track[track.id] = native_track
            self._push_clips(native_track, track, store, warnings)

        for track, _native_name in track_plan.to_update:
            native_track = local_tag_to_track[track.id]
            if strip_tag(native_track.name) != track.name:
                RPR.GetSetMediaTrackInfo_String(
                    native_track.id, "P_NAME", tag(track.name, track.id), True
                )
            native_track.is_muted = track.muted
            self._push_clips(native_track, track, store, warnings)

        try:
            self._push_markers(session, warnings)
        except Exception as exc:
            warnings.append(
                f"markers could not be written into Reaper ({exc}); the tracks and clips in this "
                f"push were applied, the markers were not"
            )

        # Items created through the API don't get peak files built the way
        # a manual import/drag does, so pushed clips render as empty
        # outlines with no waveform until something forces a build
        # (confirmed live - the audio was fine and played, it just looked
        # blank). 40047 = "Peaks: Build any missing peaks".
        reapy.perform_action(40047)

        # Same reasoning as pull(): tags stamped on newly-created tracks
        # only live in-memory until saved.
        project.save()

        return warnings

    def _push_clips(self, native_track, track: Track, store, warnings: list[str]) -> None:
        from reapy import reascript_api as RPR

        local_clip_ids = set()
        native_items_by_id = {}
        for i in range(native_track.n_items):
            item = native_track.items[i]
            take = item.active_take
            if take is None:
                continue
            _base, clip_id = parse_tag(take.name)
            if clip_id:
                local_clip_ids.add(clip_id)
                native_items_by_id[clip_id] = item

        clip_plan = plan_clips(track.clips, local_clip_ids)

        for clip in clip_plan.to_add:
            # Pro Tools already refused to place a clip whose audio isn't
            # in the shared folder. Reaper handed the path to
            # PCM_Source_CreateFromFile regardless, which produces an item
            # with no source: it looks like a clip, sits at the right
            # place, and plays silence. That is exactly what a partner's
            # still-uploading 90MB stem looks like from this side.
            missing = missing_audio_warning(clip, track.name, store)
            if missing:
                warnings.append(missing + " (not created in Reaper)")
                continue

            audio_path = store.resolve_audio_path(clip.audio_file)
            item_id = RPR.AddMediaItemToTrack(native_track.id)
            take_id = RPR.AddTakeToMediaItem(item_id)
            source = RPR.PCM_Source_CreateFromFile(str(audio_path))
            RPR.SetMediaItemTake_Source(take_id, source)
            RPR.SetMediaItemInfo_Value(item_id, "D_POSITION", clip.start_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_LENGTH", clip.length_seconds)
            RPR.SetMediaItemTakeInfo_Value(take_id, "D_STARTOFFS", clip.source_offset_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_FADEINLEN", clip.fade_in_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_FADEOUTLEN", clip.fade_out_seconds)
            # Set the loop flag before length: a non-looped item can't be
            # longer than its source, so length would otherwise clamp.
            RPR.SetMediaItemInfo_Value(item_id, "B_LOOPSRC", 1 if clip.loop_source else 0)
            RPR.SetMediaItemInfo_Value(item_id, "D_LENGTH", clip.length_seconds)
            RPR.GetSetMediaItemTakeInfo_String(take_id, "P_NAME", tag(clip.name, clip.id), True)

        for clip in clip_plan.to_update:
            item = native_items_by_id[clip.id]
            take = item.active_take

            # Re-point the source if canonical now references different
            # audio for this clip. Without this, replacing a clip's audio
            # on one side never reached the other - confirmed live, a
            # stereo repair changed audio_file in the shared session and
            # Reaper happily kept playing the old mono file, reporting
            # "no changes" the whole time.
            if take is not None and clip.audio_file:
                wanted = store.resolve_audio_path(clip.audio_file)
                if _take_source_path(RPR, take) != str(wanted) and wanted.exists():
                    source = RPR.PCM_Source_CreateFromFile(str(wanted))
                    RPR.SetMediaItemTake_Source(take.id, source)

            RPR.SetMediaItemInfo_Value(item.id, "D_POSITION", clip.start_seconds)
            RPR.SetMediaItemInfo_Value(item.id, "B_LOOPSRC", 1 if clip.loop_source else 0)
            RPR.SetMediaItemInfo_Value(item.id, "D_LENGTH", clip.length_seconds)
            if take is not None:
                RPR.SetMediaItemTakeInfo_Value(take.id, "D_STARTOFFS", clip.source_offset_seconds)

        for orphan_id in clip_plan.orphaned_ids:
            warnings.append(
                f"clip {orphan_id} on Reaper track {track.name!r} is no longer in the "
                f"canonical session; left in place, review manually"
            )
