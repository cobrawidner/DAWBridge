"""Backend contract: one implementation per DAW, both driving a *running*
instance of their DAW live (ReaScript for Reaper, PTSL for Pro Tools) -
neither backend reads or writes the other DAW's native project file.

    read_live_state()      what's in the DAW right now, as neutral
                     LiveTrack/LiveClip records. Both push() and the
                     preview read through this ONE method so a preview
                     can't disagree with what the push then does.
    pull(session, store)   live DAW state -> merged into canonical Session
                     (adopts any untagged local tracks/clips by tagging
                     them and adding them to the session; newly-adopted
                     clips have their audio file copied into the shared
                     store so the other side can actually resolve them)
    push(session, store)   canonical Session -> applied into the live DAW
                     (non-destructive: see sync.py)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from .model import Session


@dataclass
class LiveClip:
    """A clip observed in a running DAW. `bridge_id` is None for clips the
    bridge has never tagged (local-only content it must leave alone).
    """
    bridge_id: Optional[str]
    name: str  # base name, bridge tag stripped
    start_seconds: float
    length_seconds: float
    #: Absolute path of the audio the DAW is actually playing. Compared
    #: against canonical so replacing a clip's audio is detected - without
    #: it, a swapped file looked like "no changes" and never propagated.
    source_path: str = ""
    native: Any = None  # opaque DAW handle, for backends that need it


@dataclass
class LiveMarker:
    """A marker/memory location observed in a running DAW.

    `bridge_id` is None for markers the bridge has never tagged - local
    punch-in points and personal scribbles the user made for themselves,
    which are left strictly alone.

    `native` carries whatever handle that DAW needs to edit this marker
    again (Reaper's markrgnindexnumber, Pro Tools' memory location
    number); neither DAW can be told "move the marker called X".
    """
    bridge_id: Optional[str]
    name: str  # base name, bridge tag stripped
    time_seconds: float
    native: Any = None


@dataclass
class LiveTrack:
    bridge_id: Optional[str]
    name: str  # base name, bridge tag stripped
    channels: int = 2
    muted: bool = False
    clips: list[LiveClip] = field(default_factory=list)
    native: Any = None  # opaque DAW handle, for backends that need it

    @property
    def clip_ids(self) -> set[str]:
        return {c.bridge_id for c in self.clips if c.bridge_id}


class Backend(ABC):
    name: str = "backend"

    @abstractmethod
    def is_available(self) -> bool:
        """Whether the DAW is reachable right now (running + scripting
        enabled). Callers should check this before pull/push and give the
        user a clear error rather than a stack trace.
        """

    def project_identity(self) -> str:
        """Path of the local project/session this DAW currently has open.

        Recorded on every sync so the bridge can notice you've switched
        projects. That matters because a pull REPLACES the shared session
        with whatever's open: pulling from a different project than last
        time isn't an edit, it's a wholesale swap - and it silently
        duplicated every track when it happened during development.
        Backends that can't answer return "" and the check is skipped.
        """
        return ""

    @abstractmethod
    def read_live_state(self) -> list["LiveTrack"]:
        """Everything the bridge cares about in the running DAW right now.

        Deliberately the single place each backend inspects its DAW for
        push/preview purposes: the preview is only trustworthy if it is
        computed from exactly the same reading the push acts on.
        """

    def read_live_markers(self, warnings: Optional[list[str]] = None) -> Optional[list["LiveMarker"]]:
        """Markers in the running DAW right now, or None.

        None means "this DAW cannot tell me", which is NOT the same as []
        ("this DAW has no markers"). The difference decides whether the
        preview may claim anything about markers at all: given [], a
        preview says every canonical marker will be added; given None it
        says nothing, because a preview that promises what the push can't
        deliver is worse than a quiet one.

        Not abstract, and defaults to None, so a backend that hasn't
        implemented markers is honest rather than broken.
        """
        return None

    @abstractmethod
    def capture(self, session: Session, store, warnings: Optional[list[str]] = None) -> Session:
        """Read the live DAW and merge its state into `session`, returning
        the updated Session. Mutates session.tracks in place for tagged
        matches; appends newly-adopted tracks/clips for untagged ones.
        `store` is a SharedStore, used to copy newly-adopted clips' audio
        files into the shared audio/ dir so other machines can resolve them.

        `warnings`, when given, is a list the backend appends plain
        sentences to - the same style push() returns. It exists because
        a pull notices plenty worth saying (media that has gone offline,
        a duplicated track being re-adopted, tracks about to vanish from
        the shared session, a tempo map that doesn't fit the schema) and
        until it had somewhere to put them, every one of those was
        swallowed at the point of discovery. Optional so a caller that
        doesn't want them still works; backends must tolerate None.
        """

    @abstractmethod
    def apply(self, session: Session, store) -> list[str]:
        """Apply `session` into the live DAW. `store` is a SharedStore,
        used to resolve Clip.audio_file to a real path for import.
        Returns a list of human-readable warnings (e.g. orphaned clips)
        for the CLI to print - nothing here should be auto-deleted.
        """


class MockBackend(Backend):
    """In-memory stand-in used by tests and for exercising the CLI/sync
    logic without a real DAW attached.
    """

    name = "mock"

    def __init__(self):
        self.tracks: dict[str, dict] = {}  # bridge_id -> {"name": .., "clip_ids": set()}
        self.pushed_log: list[str] = []

    def is_available(self) -> bool:
        return True

    def read_live_state(self) -> list[LiveTrack]:
        return [
            LiveTrack(
                bridge_id=bridge_id,
                name=data["name"],
                muted=data.get("muted", False),
                clips=[
                    LiveClip(
                        bridge_id=cid,
                        name=cid,
                        start_seconds=data.get("clip_starts", {}).get(cid, 0.0),
                        length_seconds=data.get("clip_lengths", {}).get(cid, 1.0),
                    )
                    for cid in data["clip_ids"]
                ],
            )
            for bridge_id, data in self.tracks.items()
        ]

    def capture(self, session: Session, store=None, warnings: Optional[list[str]] = None) -> Session:
        from .sync import merge_pulled_track

        for bridge_id, data in self.tracks.items():
            track = merge_pulled_track(session, data["name"], bridge_id)
            if track not in session.tracks:
                session.tracks.append(track)
        return session

    def apply(self, session: Session, store) -> list[str]:
        from .sync import plan_tracks, plan_clips

        local_tag_to_name = {tid: data["name"] for tid, data in self.tracks.items()}
        track_plan = plan_tracks(session, local_tag_to_name)

        warnings: list[str] = []

        for track in track_plan.to_create:
            self.tracks[track.id] = {"name": track.name, "clip_ids": set()}
            self.pushed_log.append(f"create track {track.name!r}")

        all_tracks_to_sync = list(track_plan.to_create) + [t for t, _ in track_plan.to_update]
        for track in all_tracks_to_sync:
            local_clip_ids = self.tracks[track.id]["clip_ids"]
            clip_plan = plan_clips(track.clips, local_clip_ids)
            for clip in clip_plan.to_add:
                self.tracks[track.id]["clip_ids"].add(clip.id)
                self.pushed_log.append(f"add clip {clip.name!r} to {track.name!r}")
            for clip in clip_plan.to_update:
                self.pushed_log.append(f"update clip {clip.name!r} on {track.name!r}")
            for orphan_id in clip_plan.orphaned_ids:
                warnings.append(
                    f"clip {orphan_id} on track {track.name!r} is no longer in the "
                    f"canonical session; left in place, review manually"
                )

        return warnings
