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

from . import color, localmedia
from .model import Clip, Marker, Session, Track


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


@dataclass
class MarkerSyncPlan:
    to_add: list[Marker] = field(default_factory=list)
    to_update: list[Marker] = field(default_factory=list)  # id matches something live
    orphaned_ids: list[str] = field(default_factory=list)  # tagged locally, gone from canonical


def plan_markers(canonical_markers: list[Marker], local_marker_ids: set[str]) -> MarkerSyncPlan:
    """Same shape, and the same guarantees, as plan_clips.

    Markers are matched by the bridge id embedded in their DAW-native
    name, exactly like tracks and clips. Matching on name or position
    instead would mean the second push adds every marker over again -
    the -01/-02/-03 duplication failure in a new place - and would break
    the moment somebody renamed a marker, which is the one thing markers
    are for.

    `local_marker_ids` holds only TAGGED markers. An untagged marker is
    local-only content the bridge has never adopted (a punch-in point,
    somebody's note to themselves); it is never touched and never
    reported as an orphan.
    """
    plan = MarkerSyncPlan()
    canonical_ids = set()
    for marker in canonical_markers:
        canonical_ids.add(marker.id)
        if marker.id in local_marker_ids:
            plan.to_update.append(marker)
        else:
            plan.to_add.append(marker)
    plan.orphaned_ids = sorted(local_marker_ids - canonical_ids)
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
class MarkerChange:
    """One marker's fate on push. Kept in its own list rather than folded
    into track_changes: the CLI and GUI index a dict by TrackChange.kind,
    so an unexpected kind there is a KeyError in the middle of a preview.
    """
    name: str
    kind: str  # "add" | "move" | "rename" | "orphan"
    from_time: float | None = None
    to_time: float | None = None
    detail: str = ""


@dataclass
class ColourChange:
    """One track being recoloured by a push.

    In its own list for the same reason MarkerChange is, and it matters
    more here: a recolour is the one change in the whole preview that
    nobody needs to think about before it happens. It is cosmetic, it
    cannot touch audio, and it must never read as destructive.

    `to_colour` is what canonical asks for, which for Pro Tools is not
    quite what will appear - it can only show the nearest of 69 palette
    entries. Recording what was asked for rather than what will be shown
    keeps this honest about where the approximation happens.
    """
    track_name: str
    from_colour: str | None
    to_colour: str | None


@dataclass
class PushPreview:
    """What a push would do, without doing any of it."""
    track_changes: list[TrackChange] = field(default_factory=list)
    clip_changes: list[ClipChange] = field(default_factory=list)
    marker_changes: list[MarkerChange] = field(default_factory=list)
    colour_changes: list[ColourChange] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    untouched_tracks: int = 0
    untouched_clips: int = 0

    @property
    def is_empty(self) -> bool:
        # Markers count. Without them, a push that only moves the chorus
        # marker reports "nothing to do" and both front ends skip the
        # push entirely - the change would be previewed and then not
        # happen. Colours count for exactly the same reason.
        return (
            not self.track_changes
            and not self.clip_changes
            and not self.marker_changes
            and not self.colour_changes
        )

    def summary_line(self) -> str:
        creates = sum(1 for t in self.track_changes if t.kind == "create")
        renames = sum(1 for t in self.track_changes if t.kind == "rename")
        mutes = sum(1 for t in self.track_changes if t.kind in ("mute", "unmute"))
        adds = sum(1 for c in self.clip_changes if c.kind == "add")
        moves = sum(1 for c in self.clip_changes if c.kind == "move")
        reaudio = sum(1 for c in self.clip_changes if c.kind == "reaudio")
        orphans = sum(1 for c in self.clip_changes if c.kind == "orphan")
        marker_adds = sum(1 for m in self.marker_changes if m.kind == "add")
        marker_moves = sum(1 for m in self.marker_changes if m.kind in ("move", "rename"))
        parts = []
        if creates: parts.append(f"{creates} new track(s)")
        if renames: parts.append(f"{renames} rename(s)")
        if mutes: parts.append(f"{mutes} mute change(s)")
        if adds: parts.append(f"{adds} clip(s) added")
        if moves: parts.append(f"{moves} clip(s) moved/resized")
        if reaudio: parts.append(f"{reaudio} clip(s) re-pointed to different audio")
        if marker_adds: parts.append(f"{marker_adds} marker(s) added")
        if marker_moves: parts.append(f"{marker_moves} marker(s) moved/renamed")
        # Named here even though the front ends don't list colours line by
        # line: is_empty counts them, so without this the summary could
        # read "no changes" on a pull that then went and did something.
        if self.colour_changes: parts.append(f"{len(self.colour_changes)} track(s) recoloured")
        if orphans: parts.append(f"{orphans} orphan(s) left alone")
        return ", ".join(parts) if parts else "no changes - the DAW already matches the shared session"


def preview_push(
    canonical: Session, live_tracks: list, target: str = "", store=None, live_markers=None
) -> PushPreview:
    """Diff canonical against what's live in a DAW, without touching it.

    Uses the same plan_tracks/plan_clips decisions the real push uses, so
    the preview and the push cannot disagree about what will happen.

    `target` is the destination DAW name; warnings are filtered to the
    ones that actually apply to it (Reaper handles loops and overlapping
    items natively, so warning about them there is pure noise). `store`,
    when given, is used to check real audio durations so a loop warning
    only fires when the clip genuinely repeats its source.

    `live_markers` is the backend's read_live_markers() result: a list,
    or None when that DAW can't report markers. None means no marker
    lines at all rather than "everything is missing" - see
    Backend.read_live_markers.
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
        if _colour_would_change(track.color, live.color, target):
            preview.colour_changes.append(
                ColourChange(track_name=track.name,
                             from_colour=color.normalise(live.color),
                             to_colour=color.normalise(track.color))
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

    _preview_markers(preview, canonical, live_markers)

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


def colour_quantiser(target: str):
    """How `target` mangles a colour on its way to the screen, or None
    when it shows what it's given.

    Pro Tools can only be set to one of 69 palette entries, so asking it
    for #3F7FBF gets you the nearest of those. Every comparison involving
    a Pro Tools colour has to go through this or it will report a
    difference that Pro Tools has no way to resolve - and then report it
    again on the next pull, forever, because applying the colour cannot
    make the difference go away.

    Uses `color.PROTOOLS_TRACK_PALETTE` rather than the running Pro
    Tools' own palette because this module never has an engine to ask.
    The backend, which does, always asks. See that constant's comment for
    why the copy is safe.
    """
    if target == "protools":
        return lambda value: color.nearest(value, color.PROTOOLS_TRACK_PALETTE)
    return None


def _colour_would_change(canonical_colour, live_colour, target: str) -> bool:
    """Whether pushing `canonical_colour` would visibly change this track.

    Says no when canonical has no colour: a push never strips a colour
    the DAW already has (there is nothing to put there instead), which
    matches every other "loading into a DAW never deletes" rule.
    """
    if color.normalise(canonical_colour) is None:
        return False
    quantise = colour_quantiser(target)
    wanted = color.normalise(canonical_colour) if quantise is None else quantise(canonical_colour)
    return not color.same(wanted, live_colour)


def _preview_markers(preview: PushPreview, canonical: Session, live_markers) -> None:
    """Marker half of the diff, using the same plan the push will use.

    Silent when live_markers is None: that means the DAW couldn't tell us
    what it has, and a preview may not claim a change it can't stand
    behind.
    """
    if live_markers is None:
        return

    live_by_id = {m.bridge_id: m for m in live_markers if m.bridge_id}
    plan = plan_markers(canonical.markers, set(live_by_id))

    for marker in plan.to_add:
        preview.marker_changes.append(
            MarkerChange(name=marker.name, kind="add", to_time=marker.time_seconds)
        )

    for marker in plan.to_update:
        live = live_by_id[marker.id]
        if abs(live.time_seconds - marker.time_seconds) > _MOVE_TOLERANCE_SECONDS:
            preview.marker_changes.append(
                MarkerChange(name=marker.name, kind="move",
                             from_time=live.time_seconds, to_time=marker.time_seconds)
            )
        elif live.name != marker.name:
            preview.marker_changes.append(
                MarkerChange(name=marker.name, kind="rename",
                             detail=f"{live.name!r} -> {marker.name!r}")
            )

    for orphan_id in plan.orphaned_ids:
        orphan = live_by_id.get(orphan_id)
        preview.marker_changes.append(
            MarkerChange(name=orphan.name if orphan else orphan_id, kind="orphan")
        )


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


@dataclass
class PublishPlan:
    """Which tracks a publish may touch, and what it did to each."""
    tracks: list[Track] = field(default_factory=list)  # the new canonical track list
    added: list[str] = field(default_factory=list)     # new here, not in the shared session
    updated: list[str] = field(default_factory=list)   # you changed them; yours published
    removed: list[str] = field(default_factory=list)   # you deleted them; taken out
    kept_theirs: list[str] = field(default_factory=list)  # your partner's, left untouched
    unchanged: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)  # both changed; yours won, loudly
    #: You recoloured them and changed nothing else. Kept apart from
    #: `updated` because a colour is cosmetic: it must be enough to make
    #: the publish happen, and never enough to make it look dangerous.
    recoloured: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def touches_anything(self) -> bool:
        # Recolours count, or a publish whose only change is a colour
        # reports "no changes" and the front ends skip it - the colour
        # would be described and then never sent.
        return bool(self.added or self.updated or self.removed or self.recoloured)

    def summary_line(self) -> str:
        """One line for a human deciding whether to publish.

        Removals and preserved tracks are named explicitly because they are
        the two things somebody needs to see BEFORE it happens: one takes
        work out of the shared session, the other is the reassurance that
        their partner's work is not being trampled.
        """
        if not self.touches_anything:
            # Say it plainly. "3 unchanged" is a true statement that reads
            # like something is about to happen.
            return "no changes - the shared session already matches this project"

        parts = []
        if self.added:
            parts.append(f"{len(self.added)} new track(s)")
        if self.updated:
            parts.append(f"{len(self.updated)} track(s) updated")
        if self.removed:
            parts.append(f"{len(self.removed)} track(s) removed ({', '.join(self.removed)})")
        if self.recoloured:
            parts.append(f"{len(self.recoloured)} track(s) recoloured")
        if self.kept_theirs:
            parts.append(f"{len(self.kept_theirs)} of your partner's track(s) left alone")
        if self.unchanged:
            parts.append(f"{len(self.unchanged)} unchanged")
        if self.conflicts:
            parts.append(f"{len(self.conflicts)} also changed by your partner")
        return ", ".join(parts) if parts else "no changes - the shared session already matches"


def _clip_fingerprint(clip: Clip) -> tuple:
    """A clip reduced to what a human would call a difference.

    Positions are rounded to the existing move tolerance rather than
    compared exactly: a round trip through the other DAW's rounding shifts
    a value by a sample or two, and without this every publish would claim
    to have changed every track.
    """
    step = _MOVE_TOLERANCE_SECONDS
    return (
        clip.id,
        clip.name,
        clip.audio_file,
        round(clip.start_seconds / step),
        round(clip.length_seconds / step),
        round(clip.source_offset_seconds / step),
        round(clip.fade_in_seconds / step),
        round(clip.fade_out_seconds / step),
        clip.loop_source,
    )


def _track_fingerprint(track: Track) -> tuple:
    return (
        track.name,
        track.muted,
        track.channels,
        tuple(_clip_fingerprint(c) for c in track.clips),
    )


def tracks_equivalent(a: Track | None, b: Track | None) -> bool:
    """Whether two versions of a track are the same arrangement.

    Colour is deliberately NOT part of this. `plan_publish` uses this
    same answer to decide what counts as a conflict, and a track you and
    your partner both recoloured must not produce "you have both changed
    this track, there is no merge, your version is being published over
    theirs" and a confirmation dialog. Colour is handled separately, and
    quietly, by `_should_publish_my_colour`.
    """
    if a is None or b is None:
        return a is b
    return _track_fingerprint(a) == _track_fingerprint(b)


def _should_publish_my_colour(mine, theirs, was, had_baseline: bool) -> bool:
    """Whether a publish should carry YOUR colour for a track whose
    arrangement both sides already agree on.

    The same three-way reasoning `plan_publish` applies to arrangement -
    a colour your partner changed and you didn't is theirs to keep - with
    one deliberate difference. When you have BOTH recoloured it, yours
    wins silently. A colour cannot destroy work, so raising it as a
    conflict would put a "there is no merge" warning and a confirmation
    in front of somebody whose crime was recolouring a track.
    """
    if color.same(mine, theirs):
        return False
    if had_baseline and color.same(mine, was) and not color.same(theirs, was):
        return False  # they recoloured it, you didn't - leave it alone
    return True


def plan_publish(
    canonical_tracks: list[Track],
    live_tracks: list[Track],
    baseline_tracks: list[Track] | None = None,
) -> PublishPlan:
    """Decide which tracks this publish is allowed to touch.

    NOT a content merge. No clip, take or position is ever reconciled
    between two versions; a track is taken whole from one side or the
    other, and inside a track that is taken, wholesale replacement is
    unchanged. What this removes is the failure where publishing a vocal
    also republishes your older copy of a bass your partner just edited.

    `baseline_tracks` is the canonical revision this machine last agreed
    with - `syncstate` records the number and `archive/` holds the content.
    It is what tells a track you DELETED apart from a track that ARRIVED
    while you weren't looking: both are simply absent from your DAW.

    Pass None when that revision can't be found (aged out of the archive,
    or this machine has never synced). Then the two cases are genuinely
    indistinguishable and have opposite correct answers, so nothing is
    removed and the plan says why. Losing the ability to delete costs one
    extra step; losing your partner's track does not undo.
    """
    plan = PublishPlan()
    canonical_by_id = {t.id: t for t in canonical_tracks}
    live_by_id = {t.id: t for t in live_tracks}
    baseline_by_id = None if baseline_tracks is None else {t.id: t for t in baseline_tracks}

    # This DAW's order leads; anything only the shared session has follows.
    for track in live_tracks:
        theirs = canonical_by_id.get(track.id)
        if theirs is None:
            plan.tracks.append(track)
            plan.added.append(track.name)
            continue

        was = None if baseline_by_id is None else baseline_by_id.get(track.id)

        if tracks_equivalent(track, theirs):
            # Same arrangement, so the only thing left that can differ is
            # the colour. Publishing yours here is the whole reason a
            # recolour crosses the bridge at all, and it lands in its own
            # list so nothing downstream mistakes it for a real edit.
            if _should_publish_my_colour(
                track.color, theirs.color,
                was.color if was is not None else None,
                was is not None,
            ):
                plan.tracks.append(track)
                plan.recoloured.append(track.name)
            else:
                plan.tracks.append(theirs)
                plan.unchanged.append(track.name)
            continue

        i_changed = was is None or not tracks_equivalent(track, was)
        they_changed = was is not None and not tracks_equivalent(theirs, was)

        if they_changed and not i_changed:
            # You never touched it and they did. Publishing your copy would
            # put their work back the way it was - the exact failure this
            # is for.
            plan.tracks.append(theirs)
            plan.kept_theirs.append(theirs.name)
            continue

        plan.tracks.append(track)
        plan.updated.append(track.name)
        if they_changed and i_changed:
            plan.conflicts.append(track.name)
            plan.warnings.append(
                f"you and your partner have both changed track {track.name!r} since you last "
                f"synced. There is no merge - your version is being published over theirs. "
                f"Their version is still in the shared session's history if you need it back"
            )

    # Tracks the shared session has and this DAW doesn't.
    missing = [t for t in canonical_tracks if t.id not in live_by_id]
    if baseline_by_id is None:
        plan.tracks.extend(missing)
        plan.kept_theirs.extend(t.name for t in missing)
        if missing:
            plan.warnings.append(
                f"{len(missing)} track(s) in the shared session are not in this project and have "
                f"been left alone, because this machine cannot tell whether you deleted them or "
                f"they arrived while you were away - the revision it last saw has aged out of the "
                f"archive, or it has never synced. To delete a track, load the shared session "
                f"first, then delete and publish"
            )
    else:
        for track in missing:
            if track.id in baseline_by_id:
                plan.removed.append(track.name)  # you had it, you deleted it
            else:
                plan.tracks.append(track)        # arrived after you last looked
                plan.kept_theirs.append(track.name)

    return plan


def snapshot_tracks(session: Session) -> list[Track]:
    """A detached copy of a session's tracks.

    Needed because `capture()` reuses canonical's Track objects and mutates
    them in place - that is how a track keeps its clip history across a
    pull. So `list(session.tracks)` taken beforehand is not a
    before-picture: every object in it changes underneath you, and a
    comparison against it would report that nothing ever changed.
    """
    return Session.from_json(session.to_json()).tracks


def baseline_tracks_for(store, canonical: Session, last_seen_revision: int | None) -> list[Track] | None:
    """The tracks of the canonical revision this machine last agreed with,
    or None when that can't be established.

    `syncstate` records the number; the content is either canonical itself
    (the common case - you published and nobody has since, so the revision
    you last saw IS session.json, not an archive file) or one of the
    archived revisions.

    None means "don't guess": either this machine has never synced, or the
    revision has aged out of the archive. plan_publish then refuses to
    remove anything.
    """
    if last_seen_revision is None:
        return None
    if last_seen_revision == canonical.revision:
        return snapshot_tracks(canonical)
    try:
        for revision, path in store.list_archive():
            if revision == last_seen_revision:
                return Session.load(path).tracks
    except Exception:
        return None
    return None


@dataclass
class Identity:
    """Where one DAW object's bridge id came from, and what to write back.

    `bridge_id` is None when nothing could be recovered and the caller
    should mint a fresh id and adopt the object.
    """
    bridge_id: str | None
    source: str  # "name" | "extended state" | "new"
    write_name_tag: bool = False
    write_extended_state: bool = False
    warning: str | None = None


def resolve_identities(
    observed: list[tuple[str | None, str | None, str]], kind: str = "track"
) -> list[Identity]:
    """Decide each object's identity from its name tag and its stored id.

    `observed` is one (name_id, extended_state_id, label) per live object,
    in the order the DAW reported them.

    Identity lives in the DAW-native name - that is the only thing two DAWs
    sharing no identifiers can agree on, and Pro Tools has no equivalent of
    extended state, so the name stays the mechanism. Extended state is a
    second place to LOOK when the name has lost its tag, which is what
    happens when somebody tidies "Lead Vocal #a1b2c3d4" back to "Lead
    Vocal". Today that publishes as a brand new track and hands the partner
    a duplicate on every sync afterwards, silently, because a missing tag
    is indistinguishable from a genuinely new track.

    Resolved in two passes, and the order matters more than it looks:

      1. every object whose NAME carries a tag claims that id;
      2. only then may an untagged object recover an id from extended state.

    Duplicating a track copies its extended state along with its name. If
    extended state could claim first, a copy whose name had been tidied
    would look exactly like a recovery case, take the original's identity,
    and republish the original as a new track - the copy inheriting the
    history. Resolving names first makes that impossible regardless of the
    order the DAW lists them in.
    """
    claimed: set[str] = set()
    decisions: list[Identity | None] = [None] * len(observed)

    # Pass 1: a tag in the name is authoritative.
    for index, (name_id, ext_id, label) in enumerate(observed):
        if not name_id or name_id in claimed:
            continue
        claimed.add(name_id)
        if ext_id == name_id:
            decisions[index] = Identity(name_id, "name")
        elif ext_id is None:
            # Pre-existing track from before this existed: put the net in
            # place underneath it without touching the name.
            decisions[index] = Identity(name_id, "name", write_extended_state=True)
        else:
            decisions[index] = Identity(
                name_id, "name", write_extended_state=True,
                warning=(
                    f"{kind} {label!r} carries DAWBridge id #{name_id} in its name but #{ext_id} "
                    f"stored inside the project - it was probably copied in from another "
                    f"session. The name is what your partner's DAW sees, so #{name_id} wins and "
                    f"the stored id has been corrected to match"
                ),
            )

    # Pass 2: an untagged object may recover an id nobody else claimed.
    for index, (name_id, ext_id, label) in enumerate(observed):
        if decisions[index] is not None:
            continue
        if name_id and name_id in claimed:
            decisions[index] = Identity(
                None, "new", write_name_tag=True, write_extended_state=True,
                warning=duplicate_adoption_warning(kind, label, name_id),
            )
            continue
        if ext_id and ext_id not in claimed:
            claimed.add(ext_id)
            decisions[index] = Identity(
                ext_id, "extended state", write_name_tag=True,
                warning=(
                    f"{kind} {label!r} had lost its DAWBridge tag from its name - recovered "
                    f"#{ext_id} from the id stored inside the project and put the tag back. "
                    f"Without that it would have been published as a new {kind} and your "
                    f"partner would have collected a duplicate"
                ),
            )
            continue
        warning = None
        if ext_id:
            warning = (
                f"{kind} {label!r} stores DAWBridge id #{ext_id} but another {kind} is already "
                f"using it, so this one is a copy - it has been adopted as a new {kind} and will "
                f"appear as an additional one for your partner"
            )
        decisions[index] = Identity(
            None, "new", write_name_tag=True, write_extended_state=True, warning=warning
        )

    return [d for d in decisions if d is not None]


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

    A *local working copy* counts as the same audio. Both backends now
    keep their media beside the project rather than in the shared folder
    (see localmedia), so the path compare alone would call every clip
    changed and re-hash every stem on every publish - correct, but it
    reads a 90MB file per clip to conclude nothing happened. The store
    names files after their own content hash, so a local file still
    carrying that name still holds that content; `is_managed_copy_of`
    checks the name really is one the store minted, which is what makes
    the shortcut a content claim rather than a guess. A file the user
    renamed, or one Pro Tools sliced to a clip's own boundaries, fails
    that test and gets re-imported exactly as before.
    """
    if store is None or not live_source_path:
        return False
    if not clip.audio_file:
        return True
    if localmedia.is_managed_copy_of(live_source_path, clip.audio_file):
        return False
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


def tempo_write_is_safe(item_count: int) -> bool:
    """Whether pushing a tempo into a Reaper project can be done without
    moving what's already in it.

    Only when the project is empty. CONFIRMED BY LIVE TEST against Reaper
    7.69: `SetCurrentBPM` does not just change a number, it drags every
    beat-attached object with it. An item at 5.333s with a 0.333s fade
    became an item at 4.000s with a 0.250s fade when the tempo went
    90 -> 120, and back again on the way down - position, length AND
    fades all scaled by the tempo ratio.

    Canonical stores positions in SECONDS, so that is silent corruption:
    the push moves everything already in the project, then places the
    pushed clips at canonical's seconds positions, and the arrangement
    is now wrong relative to itself. The next pull publishes the shifted
    positions and the damage crosses to the other DAW. Nothing reports a
    thing - the push says it succeeded.

    An empty project has nothing to drag, and that is exactly the case
    where the tempo matters most (a partner receiving a song for the
    first time), so that one is still worth doing.
    """
    return item_count == 0


def describe_tempo_write_refusal(session: Session, live_tempo: float, item_count: int) -> str | None:
    """Warning when a project's tempo disagrees with the shared session
    and DAWBridge won't touch it. See tempo_write_is_safe.
    """
    if abs(float(live_tempo) - float(session.tempo_bpm)) <= 1e-6:
        return None
    if tempo_write_is_safe(item_count):
        return None
    return (
        f"the shared session is {session.tempo_bpm:g} BPM but this project is {float(live_tempo):g} "
        f"BPM, and DAWBridge did NOT change it. Changing a Reaper project's tempo drags every "
        f"item, fade and marker already in it to a new position (confirmed: an item at 5.333s "
        f"moved to 4.000s on a 90->120 change), which would silently shift work that is already "
        f"there. Set the tempo by hand if you want it, ideally before adding anything"
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
