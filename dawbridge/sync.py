"""DAW-independent diff/plan logic for applying a canonical Session to a
live DAW's current (tagged) state.

Backends are responsible for reading what's currently in the DAW and for
actually executing a plan; this module only decides *what should happen*,
so the decision logic is testable without a real Reaper or Pro Tools
instance.

Guiding rule for PUSH (per project scope decision): never destructive to
the live DAW by default.
  - A canonical track with no local match -> create it (empty, no FX).
  - A canonical track with a local match -> update its clips in place;
    never delete/recreate the track itself (that would blow away
    whatever effects chain the local user built on it).
  - A local clip that's no longer in canonical -> reported as orphaned,
    not auto-deleted. The human decides.

PULL is the opposite by design: it replaces canonical session.json's
track list wholesale with whatever's currently live in the DAW you just
pulled from - no merge across different source projects/files. If you
pull from a different .rpp than last time, canonical now reflects *that*
file, full stop; nothing from the old one lingers. This is a deliberate
simplification (per user decision) over trying to reconcile "is this
really the same project" across files dawbridge has no way to know are
related - the tradeoff is that a pull from the wrong project can replace
real canonical data in one command, which is what SharedStore.save()'s
single rolling backup exists to make recoverable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .model import Clip, Session, Track


@dataclass
class TrackSyncPlan:
    to_create: list[Track] = field(default_factory=list)  # canonical tracks with no local match
    to_update: list[tuple[Track, str]] = field(default_factory=list)  # (canonical track, local_native_name)


@dataclass
class ClipSyncPlan:
    to_add: list[Clip] = field(default_factory=list)
    to_update: list[Clip] = field(default_factory=list)  # id matches, some field changed
    orphaned_ids: list[str] = field(default_factory=list)  # present locally, no longer in canonical


def plan_tracks(canonical: Session, local_tag_to_name: dict[str, str]) -> TrackSyncPlan:
    """local_tag_to_name maps bridge-id -> current native track name, for
    every tagged track the backend found in the live DAW.
    """
    plan = TrackSyncPlan()
    for track in canonical.tracks:
        if track.id in local_tag_to_name:
            plan.to_update.append((track, local_tag_to_name[track.id]))
        else:
            plan.to_create.append(track)
    return plan


def plan_clips(canonical_clips: list[Clip], local_clip_ids: set[str]) -> ClipSyncPlan:
    plan = ClipSyncPlan()
    canonical_ids = set()
    for clip in canonical_clips:
        canonical_ids.add(clip.id)
        if clip.id in local_clip_ids:
            plan.to_update.append(clip)
        else:
            plan.to_add.append(clip)
    plan.orphaned_ids = sorted(local_clip_ids - canonical_ids)
    return plan


#: Overlaps shorter than this are treated as clips butting up against each
#: other, not as real overlap. Butt-joined clips routinely differ by a
#: fraction of a sample after a float round trip (confirmed live: an
#: adjacent pair differed by 1e-5s and was reported as overlapping), and
#: nothing that short is audible or actionable.
_OVERLAP_TOLERANCE_SECONDS = 0.001


def find_overlapping_clips(clips: list[Clip], tolerance: float = _OVERLAP_TOLERANCE_SECONDS) -> list[tuple[Clip, Clip]]:
    """Pairs of clips whose time ranges genuinely overlap, earliest first.

    Reaper happily layers overlapping items on one track; Pro Tools has a
    single playlist per track, so spotting a clip onto occupied timeline
    *overwrites and splits* what's already there. That's a silent,
    destructive difference - confirmed live, a clip moved into an
    overlapping position came back split into "-01"/"-02" fragments.
    Callers that can't represent overlap should warn rather than pretend
    the push was clean.
    """
    ordered = sorted(clips, key=lambda c: c.start_seconds)
    overlaps = []
    for i, clip in enumerate(ordered):
        clip_end = clip.start_seconds + clip.length_seconds
        for other in ordered[i + 1:]:
            if other.start_seconds >= clip_end - tolerance:
                break  # sorted, so nothing later can overlap either
            overlaps.append((clip, other))
    return overlaps


#: Position/length differences below this are treated as identical. A
#: round trip through another DAW's own rounding can shift a value by a
#: sample or two; flagging that as "moved" would bury real edits in noise.
_MOVE_TOLERANCE_SECONDS = 0.002


@dataclass
class ClipChange:
    """One clip's fate on push, in reviewable terms."""
    track_name: str
    clip_name: str
    kind: str  # "add" | "move" | "orphan"
    from_start: float | None = None
    to_start: float | None = None
    from_length: float | None = None
    to_length: float | None = None


@dataclass
class TrackChange:
    name: str
    kind: str  # "create" | "rename" | "mute" | "unmute"
    detail: str = ""


@dataclass
class PushPreview:
    """What a push would do, without doing any of it."""
    track_changes: list[TrackChange] = field(default_factory=list)
    clip_changes: list[ClipChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    untouched_tracks: int = 0
    untouched_clips: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.track_changes and not self.clip_changes

    def summary_line(self) -> str:
        creates = sum(1 for t in self.track_changes if t.kind == "create")
        renames = sum(1 for t in self.track_changes if t.kind == "rename")
        mutes = sum(1 for t in self.track_changes if t.kind in ("mute", "unmute"))
        adds = sum(1 for c in self.clip_changes if c.kind == "add")
        moves = sum(1 for c in self.clip_changes if c.kind == "move")
        reaudio = sum(1 for c in self.clip_changes if c.kind == "reaudio")
        orphans = sum(1 for c in self.clip_changes if c.kind == "orphan")
        parts = []
        if creates: parts.append(f"{creates} new track(s)")
        if renames: parts.append(f"{renames} rename(s)")
        if mutes: parts.append(f"{mutes} mute change(s)")
        if adds: parts.append(f"{adds} clip(s) added")
        if moves: parts.append(f"{moves} clip(s) moved/resized")
        if reaudio: parts.append(f"{reaudio} clip(s) re-pointed to different audio")
        if orphans: parts.append(f"{orphans} orphan(s) left alone")
        return ", ".join(parts) if parts else "no changes - the DAW already matches the shared session"


def preview_push(canonical: Session, live_tracks: list, target: str = "", store=None) -> PushPreview:
    """Diff canonical against what's live in a DAW, without touching it.

    Uses the same plan_tracks/plan_clips decisions the real push uses, so
    the preview and the push cannot disagree about what will happen.

    `target` is the destination DAW name; warnings are filtered to the
    ones that actually apply to it (Reaper handles loops and overlapping
    items natively, so warning about them there is pure noise). `store`,
    when given, is used to check real audio durations so a loop warning
    only fires when the clip genuinely repeats its source.
    """
    preview = PushPreview()
    live_by_id = {t.bridge_id: t for t in live_tracks if t.bridge_id}
    placing: list[tuple[str, Clip]] = []  # clips this push would import, for the audio check

    plan = plan_tracks(canonical, {tid: t.name for tid, t in live_by_id.items()})

    for track in plan.to_create:
        preview.track_changes.append(
            TrackChange(name=track.name, kind="create",
                        detail=f"{len(track.clips)} clip(s), {'stereo' if track.channels >= 2 else 'mono'}")
        )
        for clip in track.clips:
            preview.clip_changes.append(
                ClipChange(track_name=track.name, clip_name=clip.name, kind="add",
                           to_start=clip.start_seconds, to_length=clip.length_seconds)
            )
            placing.append((track.name, clip))

    for track, _native_name in plan.to_update:
        live = live_by_id[track.id]
        changed_here = False

        if live.name != track.name:
            preview.track_changes.append(
                TrackChange(name=track.name, kind="rename", detail=f"{live.name!r} -> {track.name!r}")
            )
            changed_here = True
        if live.muted != track.muted:
            preview.track_changes.append(
                TrackChange(name=track.name, kind="mute" if track.muted else "unmute")
            )
            changed_here = True

        live_clips_by_id = {c.bridge_id: c for c in live.clips if c.bridge_id}
        clip_plan = plan_clips(track.clips, set(live_clips_by_id))

        for clip in clip_plan.to_add:
            preview.clip_changes.append(
                ClipChange(track_name=track.name, clip_name=clip.name, kind="add",
                           to_start=clip.start_seconds, to_length=clip.length_seconds)
            )
            placing.append((track.name, clip))
            changed_here = True

        for clip in clip_plan.to_update:
            existing = live_clips_by_id[clip.id]
            moved = abs(existing.start_seconds - clip.start_seconds) > _MOVE_TOLERANCE_SECONDS
            resized = abs(existing.length_seconds - clip.length_seconds) > _MOVE_TOLERANCE_SECONDS
            reaudioed = _audio_differs(existing, clip, store)
            if reaudioed:
                preview.clip_changes.append(
                    ClipChange(track_name=track.name, clip_name=clip.name, kind="reaudio")
                )
                changed_here = True
            if moved or resized:
                preview.clip_changes.append(
                    ClipChange(track_name=track.name, clip_name=clip.name, kind="move",
                               from_start=existing.start_seconds, to_start=clip.start_seconds,
                               from_length=existing.length_seconds, to_length=clip.length_seconds)
                )
                changed_here = True
            elif not reaudioed:
                preview.untouched_clips += 1

        for orphan_id in clip_plan.orphaned_ids:
            orphan = live_clips_by_id.get(orphan_id)
            preview.clip_changes.append(
                ClipChange(track_name=track.name, kind="orphan",
                           clip_name=orphan.name if orphan else orphan_id)
            )

        if not changed_here:
            preview.untouched_tracks += 1

    # A clip whose audio isn't in the shared folder is about to become a
    # silent item on someone's timeline, so say it before the push, not
    # after. Reported for the clips the push would actually place: an
    # untouched clip that's already sitting in the DAW plays fine from
    # whatever the DAW has locally.
    _warn_about_missing_audio(preview, placing, store)

    # Two DAW objects carrying one bridge id (someone duplicated a tagged
    # track or item - the copy inherits the tag) collapse to one entry
    # here, so only one of them would ever be updated and the other
    # silently drifts.
    for warning in describe_duplicate_live_ids(live_tracks):
        preview.warnings.append(warning)

    # Only Pro Tools has these limitations. Reaper layers overlapping
    # items and loops natively, so surfacing them for a Reaper push would
    # be noise - and a warning people learn to skim is worse than none.
    if target == "protools":
        for track in canonical.tracks:
            for earlier, later in find_overlapping_clips(track.clips):
                preview.warnings.append(
                    f"clips {earlier.name!r} and {later.name!r} overlap on track {track.name!r} - "
                    f"Pro Tools has one playlist per track and will overwrite/split them"
                )
            for clip in track.clips:
                # Reaper sets "loop source" on items by default, so the
                # flag alone means almost nothing. It only matters when
                # the item is actually longer than its source audio.
                if not clip.loop_source:
                    continue
                source_seconds = _source_duration(store, clip)
                if source_seconds is None or clip.length_seconds <= source_seconds + 0.01:
                    continue
                preview.warnings.append(
                    f"clip {clip.name!r} on track {track.name!r} loops its source "
                    f"({source_seconds:.2f}s repeated to {clip.length_seconds:.2f}s) - Pro Tools has no "
                    f"loop-to-length and will receive a single {source_seconds:.2f}s iteration"
                )

    return preview


#: Beyond this many missing-audio lines, the list stops being readable
#: and starts being wallpaper - the count says the rest.
_MAX_MISSING_AUDIO_WARNINGS = 5


def _warn_about_missing_audio(preview: PushPreview, placing: list[tuple[str, Clip]], store) -> None:
    if store is None or not placing:
        return
    warnings = []
    for track_name, clip in placing:
        warning = missing_audio_warning(clip, track_name, store)
        if warning:
            warnings.append(warning)
    for warning in warnings[:_MAX_MISSING_AUDIO_WARNINGS]:
        preview.warnings.append(warning)
    if len(warnings) > _MAX_MISSING_AUDIO_WARNINGS:
        preview.warnings.append(
            f"...and {len(warnings) - _MAX_MISSING_AUDIO_WARNINGS} more clip(s) whose audio is "
            f"missing from the shared folder"
        )


def describe_duplicate_live_ids(live_tracks: list) -> list[str]:
    """Warnings for bridge ids that appear on more than one live object."""
    warnings = []
    seen_tracks: dict[str, int] = {}
    for track in live_tracks:
        if track.bridge_id:
            seen_tracks[track.bridge_id] = seen_tracks.get(track.bridge_id, 0) + 1
    for bridge_id, count in seen_tracks.items():
        if count > 1:
            name = next(t.name for t in live_tracks if t.bridge_id == bridge_id)
            warnings.append(
                f"{count} tracks in your DAW carry the same DAWBridge id (#{bridge_id}, "
                f"{name!r}) - one was duplicated from the other. Only one of them will be "
                f"kept in sync; rename or re-adopt the copy (delete the ' #{bridge_id}' from "
                f"its name) so it gets its own identity"
            )

    for track in live_tracks:
        seen_clips: dict[str, int] = {}
        for clip in track.clips:
            if clip.bridge_id:
                seen_clips[clip.bridge_id] = seen_clips.get(clip.bridge_id, 0) + 1
        for bridge_id, count in seen_clips.items():
            if count > 1:
                warnings.append(
                    f"{count} clips on track {track.name!r} carry the same DAWBridge id "
                    f"(#{bridge_id}) - one was duplicated from the other. Only one will be "
                    f"kept in sync, and a pull will publish just one of their positions"
                )
    return warnings


# Two Pro Tools tracks a musician gave the same visible name used to be
# reported here, because the backend's clip lookup merged them. It no
# longer does - the lookup keys on the full native name, bridge tag
# included (protools_backend._clip_bucket_key) - so the warning became a
# false alarm about a situation that now works, and a warning people
# learn to skim is worse than none. The remaining, genuinely
# indistinguishable case (identical FULL names, which Pro Tools does not
# allow) is reported by the backend itself.


def _audio_differs(live_clip, clip: Clip, store) -> bool:
    """Whether the DAW is playing different audio than canonical expects.

    Needs a store to know where canonical's audio lives, and a DAW that
    reports its source path; if either is missing, say "no" rather than
    claim a change we can't substantiate.
    """
    if store is None or not clip.audio_file or not getattr(live_clip, "source_path", ""):
        return False
    try:
        from pathlib import Path

        wanted = Path(store.resolve_audio_path(clip.audio_file))
        actual = Path(live_clip.source_path)
        return wanted.name.lower() != actual.name.lower()
    except Exception:
        return False


def _source_duration(store, clip: Clip) -> float | None:
    if store is None or not clip.audio_file:
        return None
    try:
        from . import audiofile

        return audiofile.duration_seconds(store.resolve_audio_path(clip.audio_file))
    except Exception:
        return None


def renumber_tracks(tracks: list[Track]) -> None:
    """Make Track.order actually mean the track's position in the DAW.

    It never did. `merge_pulled_track` sets order once, at mint time, to
    `len(session.tracks)` - and during a pull the backends build a
    separate `pulled_tracks` list, so `session.tracks` doesn't grow as
    they go. Every track adopted in one pull therefore got the SAME
    number. Confirmed against the real shared session: all six tracks in
    `Shady Grove.dawbridge/session.json` are `"order": 6`.

    Both `dawbridge status` and the GUI's status pane sort by this field,
    so they only display the right order by accident (a stable sort of
    equal keys). Any later pull that adopts one more track would have
    ranked it identically to five others.

    Call at the end of a pull, once the pulled list is in DAW order.
    """
    for position, track in enumerate(tracks):
        track.order = position


def claim_live_id(bridge_id: str | None, seen: set[str]) -> str | None:
    """Whether a bridge id read out of a DAW may be used as identity.

    Returns the id the first time it's seen and None afterwards, meaning
    "treat this object as untagged" - the backend then mints a fresh id
    and stamps it, exactly as it does for any local object the bridge has
    never met.

    Duplicating a track or an item is an ordinary DAW operation, and both
    Reaper and Pro Tools copy the name - tag and all - so the copy claims
    the original's identity. Confirmed by test: pulling with two tracks
    carrying one id put the SAME Track object into the canonical list
    twice and the second copy's clip layout overwrote the first's, so the
    original arrangement was gone from the shared session with nothing
    reported. A duplicate is a new object; give it a new identity.
    """
    if not bridge_id or bridge_id in seen:
        return None
    seen.add(bridge_id)
    return bridge_id


def missing_audio_warning(clip: Clip, track_name: str, store) -> str | None:
    """Warning for a clip whose audio can't be found in the shared folder.

    Two ways this happens in normal use, both of them quiet: the partner
    published and their audio hasn't finished uploading/downloading yet
    (session.json is a few KB and arrives in a blink; a 90MB stem does
    not), or the clip was pulled from a DAW that gave no source path at
    all, leaving `audio_file` empty.

    Pro Tools already skips and says so. Reaper did not: it handed the
    path to PCM_Source_CreateFromFile regardless, which yields an item
    with no source - a clip that looks present on the timeline and plays
    silence.

    `exists()` is a stat, not a read, so this is safe against cloud-only
    Dropbox placeholders - it does not trigger a download.
    """
    if store is None:
        return None
    if not clip.audio_file:
        return (
            f"clip {clip.name!r} on track {track_name!r} has no audio file recorded in the "
            f"shared session - it would arrive as an empty, silent item; re-pull it from the "
            f"DAW it came from"
        )
    try:
        if store.resolve_audio_path(clip.audio_file).exists():
            return None
    except OSError:
        return None
    return (
        f"clip {clip.name!r} on track {track_name!r} references {clip.audio_file!r}, which is "
        f"not in the shared folder's audio/ yet - if your partner just published, their audio "
        f"may still be uploading; wait for the folder to finish syncing rather than pushing a "
        f"silent clip"
    )


def should_reimport_audio(live_source_path: str, clip: Clip, store) -> bool:
    """Whether a pull should re-capture the audio under an already-tagged
    clip because the DAW is now playing something else.

    Pull refreshes a known clip's name, position, length and offset, but
    never its audio. So replacing the audio under a tagged clip - a
    re-record, a comp, Reaper's "apply track FX to items", anything that
    re-points the take - published as "no change at all". Worse, canonical
    still names the OLD file, and push re-points takes to what canonical
    names, so the next push into the same project silently reverted the
    user's own replacement.

    Compares paths, not content: the file canonical already knows lives in
    the shared store under a content-hash name, so a clip that came from a
    push points straight at it and costs one string compare. Only a clip
    pointing somewhere else gets re-imported (and import_audio_file dedupes
    by hash, so re-importing something already known is a no-op).
    """
    if store is None or not live_source_path:
        return False
    if not clip.audio_file:
        return True
    try:
        return Path(live_source_path).resolve() != Path(store.resolve_audio_path(clip.audio_file)).resolve()
    except OSError:
        return False


def describe_sample_rate_mismatch(session: Session, daw_rate: int | None, daw: str) -> str | None:
    """Warning when the DAW's session sample rate differs from the rate
    the shared session was captured at.

    `Session.sample_rate` was in the schema from the start and was never
    written by either backend, so it read 48000 forever - the real shared
    session says 48000 while carrying 44.1k audio. It matters because Pro
    Tools has to convert on import when the rates differ, and conversion
    is the path that was confirmed live to fail outright (an empty
    CommandError, likely a timeout) on a multi-minute file. Two people
    whose sessions are at different rates will keep hitting that with no
    idea why.
    """
    if not daw_rate or not session.sample_rate:
        return None
    if int(daw_rate) == int(session.sample_rate):
        return None
    return (
        f"your {daw} session runs at {int(daw_rate)} Hz but the shared session was captured at "
        f"{int(session.sample_rate)} Hz. Audio has to be sample-rate converted to cross between "
        f"them, which is slow and (in Pro Tools) fails outright on long files. Set both DAWs to "
        f"the same rate and re-pull"
    )


def describe_meter_mismatch(session: Session, live_numerator: int, live_denominator: int) -> str | None:
    """Warning when the DAW's time signature differs from canonical's.

    Pull captures the meter; no backend applies it. Reaper's push sets the
    tempo (SetCurrentBPM) and nothing else, and Pro Tools' scripting API
    exposes no meter command at all. So a 6/8 song published from one side
    arrives on the other with the right BPM on a 4/4 grid, and every
    bars/beats edit after that lands in the wrong place while every clip
    stays sample-accurate - which is exactly the kind of wrongness nobody
    thinks to check.

    Warning rather than writing it: applying a meter in Reaper means
    inserting a tempo/time-signature marker, which edits the project's
    tempo map, and that can't be validated without a live Reaper.
    """
    if not live_numerator or not live_denominator:
        return None
    if (int(live_numerator), int(live_denominator)) == (
        int(session.time_signature_numerator),
        int(session.time_signature_denominator),
    ):
        return None
    return (
        f"the shared session is in {session.time_signature_numerator}/"
        f"{session.time_signature_denominator} but this project is in "
        f"{int(live_numerator)}/{int(live_denominator)}. DAWBridge captures the time signature "
        f"but cannot set it in either DAW - change it by hand, or the grid you edit against "
        f"won't match your partner's"
    )


def describe_dropped_tracks(before: list[Track], after: list[Track]) -> list[str]:
    """Warnings for tracks that were in the shared session and won't be
    after this publish.

    Publishing replaces the track list wholesale (see this module's
    docstring) - that's the chosen simplification, not a bug. But it was
    also completely silent: the CLI reported "N new track(s) adopted, M
    total" and never mentioned the three tracks that stopped existing.
    The person who loses them isn't the person publishing, so nobody in
    the room sees it happen.
    """
    surviving = {t.id for t in after}
    lost = [t for t in before if t.id not in surviving]
    if not lost:
        return []
    return [
        f"track {t.name!r} ({len(t.clips)} clip(s)) was in the shared session but is not in "
        f"this DAW, so publishing removes it. Nothing is deleted from anyone's project, and "
        f"`dawbridge history` can bring the old version back"
        for t in lost
    ]


def describe_publish_sample_rate_change(previous_rate: int | None, new_rate: int | None) -> str | None:
    """Warning when a publish changes the rate the shared session claims.

    Not the same check as describe_sample_rate_mismatch, which asks "does
    this DAW match the session before I write into it". This one asks
    "am I about to redefine the session's rate out from under my
    partner", which is the moment their next push starts needing
    conversion on every file.
    """
    if not previous_rate or not new_rate or int(previous_rate) == int(new_rate):
        return None
    return (
        f"the shared session was captured at {int(previous_rate)} Hz and this publish changes "
        f"it to {int(new_rate)} Hz. Audio already in the shared folder is at the old rate, so "
        f"your partner's next push will have to convert it - agree on one rate and stick to it"
    )


def duplicate_adoption_warning(kind: str, name: str, bridge_id: str) -> str:
    """Told when a duplicated object was given a fresh identity.

    Silent re-adoption would be its own small mystery: the partner
    suddenly sees two tracks where they expected one. Saying it turns
    that into an explanation.
    """
    return (
        f"{kind} {name!r} is a copy of another {kind} that DAWBridge already tracks "
        f"(both were named #{bridge_id}). The copy has been adopted as a new {kind}, so it "
        f"will appear as an additional one for your partner"
    )


def describe_tempo_map_flattening(marker_count: int, published_tempo: float) -> str | None:
    """Warning when a project's tempo map can't fit in the schema.

    The shared session models ONE tempo. A project with tempo or meter
    changes through it publishes only the value at the start, and the map
    isn't recorded anywhere - the push side already refuses to overwrite
    a tempo map, but nothing said that the publish had already thrown the
    rest of it away.
    """
    if marker_count <= 1:
        return None
    return (
        f"this project has {marker_count} tempo/time-signature markers, and the shared session "
        f"holds a single tempo - only {published_tempo:g} BPM (the value at the start) was "
        f"published. Tempo changes later in the song do not cross the bridge"
    )


def merge_pulled_track(
    session: Session, native_name: str, native_id: str | None, kind: str = "audio", channels: int = 2
) -> Track:
    """Given a track observed live in a DAW (native_id is the bridge tag
    if one was already present, else None for a not-yet-adopted local
    track), return the canonical Track to represent it - reusing the
    existing canonical entry if tagged, otherwise minting a new one so it
    gets adopted into the bridge on this push. `channels` (1=mono,
    2=stereo) is only used when minting a new entry - an already-known
    track keeps whatever width it was created with, since DAWs generally
    won't let you change a track's width after the fact.
    """
    if native_id:
        existing = session.track_by_id(native_id)
        if existing is not None:
            # Refresh the mutable fields from what's live right now. The
            # id is the identity; everything else is just state the DAW
            # owns, and a pull's whole job is to capture the current
            # state. Returning `existing` untouched (as this used to)
            # meant renaming a track in one DAW never reached the other.
            existing.name = native_name
            return existing
        # Already tagged in the DAW (identity assigned on a previous
        # push/pull) but this canonical session doesn't know it yet -
        # preserve the existing id rather than minting a new one.
        return Track(id=native_id, name=native_name, kind=kind, order=len(session.tracks), channels=channels)
    return Track.new(name=native_name, kind=kind, order=len(session.tracks), channels=channels)
