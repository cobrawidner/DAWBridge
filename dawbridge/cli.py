"""Phase 1 CLI: manually-triggered pull/push, one shared folder, one DAW
per invocation (run it on whichever machine, pointed at whichever DAW is
open there).

Usage:
    dawbridge pull    --daw reaper   --folder /path/to/shared
    dawbridge push    --daw reaper   --folder /path/to/shared
    dawbridge preview --daw protools --folder /path/to/shared
    dawbridge pull    --daw protools --folder /path/to/shared --dry-run
    dawbridge status  --folder /path/to/shared
    dawbridge history --folder /path/to/shared
    dawbridge restore --folder /path/to/shared --revision 7
    dawbridge doctor  --folder /path/to/shared
    dawbridge notify  --folder /path/to/shared --webhook <discord url>

pull:    shared session.json -> applied into the live DAW (non-destructive)
push:    live DAW state -> becomes the shared session.json
preview: what a pull WOULD change, touching nothing (same as pull --dry-run)
status:  show what's in the shared session without touching any DAW
history: past revisions of the shared session still kept in archive/
restore: republish an archived revision as the current shared session
doctor:  check the shared folder for conflicted copies, missing audio and
         overlaps - read-only, touches no DAW
notify:  set up a Discord webhook so the channel is told on every
         publish and load

The names are from your point of view, not the app's: you pull the shared
session in and push your work out, the way those words work everywhere
else. Internally a publish still *reads* from the DAW, so `cmd_publish`
backs the `push` command and `cmd_load` backs `pull` - named for the
action so the mapping is stated once rather than inferred.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from . import checks, localmedia, notify, syncstate
from .backend import Backend
from .model import Session, UnsupportedSchemaVersion
from .store import SharedSessionMoved, SharedStore
from . import sync
from .sync import preview_push


def _at(seconds: float | None) -> str:
    """A marker position, or "?" when the DAW couldn't report one.

    Markers legitimately carry a missing time - a marker in a Pro Tools
    session whose unit couldn't be resolved is reported rather than
    guessed at - and a format string blowing up mid-preview would take
    the whole listing with it.
    """
    return "?" if seconds is None else f"{seconds:.3f}s"


def _print_preview(preview, session, folder: Path, daw: str) -> None:
    """Render a PushPreview for a human deciding whether to go ahead."""
    print(f"[dawbridge] shared session r{session.revision}, last updated by "
          f"{session.updated_by} at {session.updated_at}")

    drift = syncstate.describe_drift(folder, daw, session.revision)
    if drift:
        print(f"[dawbridge] {drift}")

    print(f"[dawbridge] pulling into {daw} would: {preview.summary_line()}")

    if preview.track_changes:
        print("\n  tracks:")
        for tc in preview.track_changes:
            label = {"create": "CREATE", "rename": "RENAME", "mute": "MUTE", "unmute": "UNMUTE"}[tc.kind]
            detail = f" - {tc.detail}" if tc.detail else ""
            print(f"    {label:7s} {tc.name}{detail}")

    if preview.clip_changes:
        print("\n  clips:")
        for cc in preview.clip_changes:
            if cc.kind == "add":
                print(f"    ADD     [{cc.track_name}] {cc.clip_name} "
                      f"@ {cc.to_start:.3f}s ({cc.to_length:.3f}s long)")
            elif cc.kind == "move":
                bits = []
                if cc.from_start is not None and abs(cc.from_start - cc.to_start) > 1e-9:
                    bits.append(f"{cc.from_start:.3f}s -> {cc.to_start:.3f}s")
                if cc.from_length is not None and abs(cc.from_length - cc.to_length) > 1e-9:
                    bits.append(f"length {cc.from_length:.3f}s -> {cc.to_length:.3f}s")
                print(f"    MOVE    [{cc.track_name}] {cc.clip_name}: {', '.join(bits)}")
            elif cc.kind == "reaudio":
                print(f"    AUDIO   [{cc.track_name}] {cc.clip_name}: now points to different audio")
            else:
                print(f"    ORPHAN  [{cc.track_name}] {cc.clip_name} "
                      f"- in your DAW but not in the shared session; left alone")

    if preview.marker_changes:
        print("\n  markers:")
        for mc in preview.marker_changes:
            if mc.kind == "add":
                print(f"    ADD     {mc.name} @ {_at(mc.to_time)}")
            elif mc.kind == "move":
                print(f"    MOVE    {mc.name}: {_at(mc.from_time)} -> {_at(mc.to_time)}")
            elif mc.kind == "rename":
                detail = f" - {mc.detail}" if mc.detail else ""
                print(f"    RENAME  {mc.name}{detail}")
            else:
                print(f"    ORPHAN  {mc.name} @ {_at(mc.from_time)} "
                      f"- in your DAW but not in the shared session; left alone")

    if preview.colour_changes:
        print("\n  colours:")
        for cc in preview.colour_changes:
            print(f"    COLOUR  {cc.track_name}: {cc.from_colour or 'none'} -> {cc.to_colour}")

    if preview.untouched_tracks or preview.untouched_clips:
        print(f"\n  unchanged: {preview.untouched_tracks} track(s), {preview.untouched_clips} clip(s)")

    for w in preview.warnings:
        print(f"\n[dawbridge][warning] {w}")


def _unavailable(backend, daw: str) -> str | None:
    """Why this DAW can't be reached, or None if it can.

    Probes once: `is_available()` on a closed Pro Tools costs seconds, and
    asking twice would double it. `getattr` because the test doubles are
    duck-typed rather than Backend subclasses and define only
    `is_available` - a bare call would AttributeError in the suite while
    working perfectly against a real backend.
    """
    probe = getattr(backend, "unavailable_reason", None)
    if probe is not None:
        return probe()
    return None if backend.is_available() else (
        f"{daw} doesn't look reachable right now - is it open and is "
        f"scripting enabled?"
    )


def _get_backend(daw: str) -> Backend:
    if daw == "reaper":
        from .reaper_backend import ReaperBackend

        return ReaperBackend()
    if daw == "protools":
        from .protools_backend import ProToolsBackend

        return ProToolsBackend()
    raise SystemExit(f"Unknown --daw {daw!r}, expected 'reaper' or 'protools'")


def cmd_publish(args: argparse.Namespace) -> int:
    """The `push` command: your DAW's state becomes the shared session.

    Named for what it does rather than for the direction the data moves
    inside the app - the command reads *from* the DAW, which is why this
    used to be called `pull` and confused everyone who had ever used git.
    """
    store = SharedStore(Path(args.folder))
    store.ensure_layout()
    backend = _get_backend(args.daw)

    reason = _unavailable(backend, args.daw)
    if reason is not None:
        print(f"[dawbridge] {reason}", file=sys.stderr)
        return 1

    session = store.load()

    # Publishing REPLACES the shared session's tracks with this DAW's
    # state (see sync.py - pull doesn't merge). If the shared session has
    # moved since this machine last synced, someone else published in the
    # meantime and this would silently discard their work. Refuse by
    # default: an accidental publish is invisible to the person losing
    # the work, and there is no merge to fall back on.
    project = backend.project_identity()
    project_change = syncstate.describe_project_change(Path(args.folder), args.daw, project)
    if project_change and not args.force:
        print(f"[dawbridge] refusing to publish: {project_change}", file=sys.stderr)
        print(f"[dawbridge] publishing replaces the shared session with THIS project's contents. "
              f"If that's what you want, re-run with --force.", file=sys.stderr)
        return 1

    drift = syncstate.describe_drift(Path(args.folder), args.daw, session.revision)
    # `or session.revision > 0` covers a machine that has never synced with
    # this folder - a collaborator's fresh install. That's the case most
    # likely to publish an empty or wrong project over a folder that already
    # holds real work, and it was the one case the guard let through.
    first_time_onto_real_work = syncstate.last_synced(Path(args.folder), args.daw) is None
    if drift and not args.force and (not first_time_onto_real_work or session.revision > 0):
        print(f"[dawbridge] refusing to publish: {drift}", file=sys.stderr)
        print(f"[dawbridge] publishing now would replace those changes with what's in your "
              f"{args.daw} right now.", file=sys.stderr)
        print(f"[dawbridge] there is no merge - either load theirs first "
              f"(dawbridge pull --daw {args.daw} --folder ...), or re-run with --force to "
              f"publish yours over theirs on purpose.", file=sys.stderr)
        return 1

    loaded_revision = session.revision

    # Both snapshots must be taken BEFORE capture(). capture() reuses
    # canonical's Track objects and mutates them in place - that is how a
    # track keeps its clip history - so a list taken afterwards is not a
    # before-picture and the comparison would conclude nothing ever
    # changed. See sync.snapshot_tracks.
    canonical_before = sync.snapshot_tracks(session)
    seen = syncstate.last_synced(Path(args.folder), args.daw)
    baseline = sync.baseline_tracks_for(
        store, session, seen.get("revision") if seen else None)

    # A pull notices things it cannot act on - tracks this publish removes,
    # offline media, a sample rate being redefined. Those used to be
    # discovered and dropped on the floor at the point of discovery.
    pull_warnings: list[str] = []
    session = backend.capture(session, store, pull_warnings)

    # A publish no longer replaces the whole track list. It touches only
    # the tracks you actually changed, so a track your partner edited
    # while you merely had it open survives.
    plan = sync.plan_publish(canonical_before, session.tracks, baseline)
    session.tracks = plan.tracks
    print(f"[dawbridge] publishing will: {plan.summary_line()}")
    for name in plan.removed:
        print(f"    REMOVE  {name} - you deleted it")
    for name in plan.kept_theirs:
        print(f"    KEEP    {name} - someone else's, left untouched")
    for name in plan.recoloured:
        print(f"    COLOUR  {name} - recoloured")
    for name in plan.conflicts:
        print(f"[dawbridge][warning] you and someone else both changed {name!r}; yours is being published "
              f"over theirs")
    for w in plan.warnings:
        print(f"[dawbridge][warning] {w}")

    try:
        # --force means "publish over theirs on purpose", so it also waives
        # the check for anything that landed while we were reading.
        store.save(session, updated_by=f"{getpass.getuser()}@{args.daw}",
                   expected_revision=None if args.force else loaded_revision)
    except SharedSessionMoved as exc:
        print(f"[dawbridge] refusing to publish: {exc}", file=sys.stderr)
        print(f"[dawbridge] nothing was written - your {args.daw} is untouched and so is "
              f"the shared session.", file=sys.stderr)
        print(f"[dawbridge] load their work first (dawbridge pull --daw {args.daw} "
              f"--folder ...), or re-run with --force to publish over it.", file=sys.stderr)
        return 1
    syncstate.record_sync(Path(args.folder), args.daw, session.revision, "publish", project)
    if drift and args.force:
        print(f"[dawbridge] --force: published over a newer shared session ({drift})")
    print(f"[dawbridge] published {args.daw} -> shared session: {plan.summary_line()}. "
          f"{len(session.tracks)} track(s) total, now at revision {session.revision}.")
    for w in pull_warnings:
        print(f"[dawbridge][warning] {w}")

    if notify.is_enabled_for(Path(args.folder), "publish"):
        problem = notify.post(Path(args.folder), notify.describe_publish(
            who=getpass.getuser(), daw=args.daw, revision=session.revision,
            tracks=len(session.tracks),
            clips=sum(len(t.clips) for t in session.tracks),
            warnings=len(pull_warnings),
            project=notify.project_name(Path(args.folder))))
        print(f"[dawbridge][notify] {problem}" if problem
              else "[dawbridge][notify] told Discord")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    store = SharedStore(Path(args.folder))
    store.ensure_layout()
    backend = _get_backend(args.daw)

    reason = _unavailable(backend, args.daw)
    if reason is not None:
        print(f"[dawbridge] {reason}", file=sys.stderr)
        return 1

    session = store.load()
    preview = preview_push(session, backend.read_live_state(), target=args.daw, store=store,
                            live_markers=backend.read_live_markers())
    _print_preview(preview, session, Path(args.folder), args.daw)
    return 0


def _save_project_if_asked(backend, folder: Path, requested) -> None:
    """Save the DAW's project before its audio is copied beside it.

    The GUI can offer a dialog; a command line cannot interrupt a
    scripted run to ask, so this only acts when `--save-project-as` said
    where to put it. Without the flag it prints what an unsaved project
    costs and carries on - `apply()` warns too, but by then the audio is
    already pointing at the shared folder and the advice arrives too late
    to act on.
    """
    probe = getattr(backend, "project_file", None)
    saver = getattr(backend, "save_project_as", None)
    if probe is None or saver is None:
        return  # a Pro Tools session always has a path
    try:
        if probe():
            return
    except Exception as exc:  # noqa: BLE001 - never block a pull over this
        print(f"[dawbridge] could not tell whether the project is saved ({exc}); continuing.")
        return

    if not requested:
        suggested = localmedia.default_project_file(
            notify.project_name(folder),
            extension=getattr(backend, "project_extension", ".rpp"),
        )
        print(f"[dawbridge] this project has never been saved, so its audio will play from "
              f"the shared folder. To keep a local copy instead, re-run with:")
        print(f"             --save-project-as \"{suggested}\"")
        return

    try:
        landed = saver(requested)
    except Exception as exc:  # noqa: BLE001 - reported, never raised at the user
        print(f"[dawbridge][warning] {exc}")
        return
    print(f"[dawbridge] project saved to {landed}")
    print(f"[dawbridge] audio will be copied to {localmedia.describe_media_home(landed)}")


def cmd_load(args: argparse.Namespace) -> int:
    """The `pull` command: the shared session is applied into your DAW."""
    store = SharedStore(Path(args.folder))
    store.ensure_layout()
    backend = _get_backend(args.daw)

    reason = _unavailable(backend, args.daw)
    if reason is not None:
        print(f"[dawbridge] {reason}", file=sys.stderr)
        return 1

    session = store.load()

    # Always show what's about to happen. A push writes into a real
    # project someone may have spent hours on; "here's the diff" costs one
    # read and is the difference between a surprise and a decision.
    preview = preview_push(session, backend.read_live_state(), target=args.daw, store=store,
                            live_markers=backend.read_live_markers())
    _print_preview(preview, session, Path(args.folder), args.daw)
    project = backend.project_identity()
    project_change = syncstate.describe_project_change(Path(args.folder), args.daw, project)
    if project_change:
        print(f"[dawbridge][warning] {project_change}")

    if args.dry_run:
        print("\n[dawbridge] dry run - nothing was changed.")
        return 0

    if preview.is_empty:
        print("\n[dawbridge] nothing to do.")
        # Record the project too. Writing the breadcrumb without it erases
        # the project already on file, and describe_project_change goes
        # quiet when there's nothing to compare against - so a single
        # no-op push would silently switch the project-swap guard off.
        syncstate.record_sync(Path(args.folder), args.daw, session.revision, "load", project)
        return 0

    print()
    _save_project_if_asked(backend, Path(args.folder), getattr(args, "save_project_as", None))
    warnings = backend.apply(session, store)
    syncstate.record_sync(Path(args.folder), args.daw, session.revision, "load", project)
    print(f"[dawbridge] loaded session revision {session.revision} into {args.daw}.")
    for w in warnings:
        print(f"[dawbridge][warning] {w}")

    if notify.is_enabled_for(Path(args.folder), "load"):
        problem = notify.post(Path(args.folder), notify.describe_load(
            who=getpass.getuser(), daw=args.daw, revision=session.revision,
            project=notify.project_name(Path(args.folder))))
        print(f"[dawbridge][notify] {problem}" if problem
              else "[dawbridge][notify] told Discord")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    store = SharedStore(Path(args.folder))
    store.ensure_layout()
    session = store.load()
    print(f"session {session.name!r} - revision {session.revision}, "
          f"last updated by {session.updated_by} at {session.updated_at}")
    for track in sorted(session.tracks, key=lambda t: t.order):
        print(f"  [{track.id}] {track.name} ({len(track.clips)} clip(s))")
    if session.markers:
        print("markers:")
        for m in session.markers:
            print(f"  [{m.id}] {m.name} @ {m.time_seconds:.2f}s")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    store = SharedStore(Path(args.folder))
    current = store.load()
    print(f"[dawbridge] shared session is at r{current.revision}, "
          f"last updated by {current.updated_by} at {current.updated_at}")

    archived = store.list_archive()
    if not archived:
        print("[dawbridge] nothing archived yet - the archive fills up as people publish.")
        return 0

    print(f"\n  archived revisions ({len(archived)} kept):")
    for revision, path in archived:
        try:
            session = Session.load(path)
        except Exception:
            print(f"    r{revision:<5d} (unreadable: {path.name})")
            continue
        print(f"    r{revision:<5d} {session.updated_at or '?':<26s} "
              f"{session.updated_by or '?':<20s} {len(session.tracks)} track(s)")
    print(f"\n  restore one with: dawbridge restore --folder {args.folder} --revision N")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    store = SharedStore(Path(args.folder))
    current = store.load()
    try:
        restored = store.restore_archived(args.revision, updated_by=f"{getpass.getuser()}@restore")
    except FileNotFoundError as exc:
        print(f"[dawbridge] {exc}", file=sys.stderr)
        print("[dawbridge] see what's available with: "
              f"dawbridge history --folder {args.folder}", file=sys.stderr)
        return 1

    print(f"[dawbridge] restored r{args.revision} ({len(restored.tracks)} track(s)) over "
          f"r{current.revision}, published as r{restored.revision}.")
    print(f"[dawbridge] the version this replaced is archived as r{current.revision}, "
          f"so this is undoable.")
    print("[dawbridge] this only changes the shared session - run "
          f"`dawbridge pull --daw <daw> --folder {args.folder}` to get it into a DAW.")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Answer "is this shared folder OK?" without touching a DAW.

    The checking lives in `checks.py` so this and the GUI's "Check folder"
    button cannot drift into disagreeing about whether a folder is healthy.
    """
    root = Path(args.folder)
    store = SharedStore(root)
    if not store.session_path.exists():
        print(f"[doctor] no session.json in {root} - nothing has been published here yet.")
        return 0

    session = store.load()
    print(f"[doctor] session {session.name!r} r{session.revision}, last updated by "
          f"{session.updated_by} at {session.updated_at}")
    print(f"[doctor] {len(session.tracks)} track(s), "
          f"{sum(len(t.clips) for t in session.tracks)} clip(s)")

    problems, orphans = checks.check_folder(store)

    if orphans:
        mb = sum(size for _name, size in orphans) / 1e6
        print(f"\n[doctor] {len(orphans)} audio file(s) referenced by nothing "
              f"({mb:.0f} MB reclaimable) - not a fault, just housekeeping:")
        for name, size in orphans[:8]:
            print(f"    {size/1e6:8.1f} MB  {name}")

    if not problems:
        print("\n[doctor] no problems found.")
        return 0

    print(f"\n[doctor] {len(problems)} problem(s):")
    for problem in problems:
        for i, line in enumerate(str(problem).splitlines()):
            print(("    " if i == 0 else "      ") + line)
    return 1


def cmd_notify(args: argparse.Namespace) -> int:
    """Set up, inspect or test the Discord notification for this folder."""
    root = Path(args.folder)
    if not root.is_dir():
        print(f"[notify] no such folder: {root}", file=sys.stderr)
        return 1

    if args.off:
        notify.config_path(root).unlink(missing_ok=True)
        print("[notify] notifications turned off for this project - neither of you "
              "will get messages until a webhook is set up again.")
        return 0

    if args.webhook:
        reason = notify.reject_reason(args.webhook)
        if reason:
            print(f"[notify] refusing: {reason}", file=sys.stderr)
            return 1
        config = notify.load_config(root)
        config["discord_webhook"] = args.webhook
        if args.events:
            config["events"] = args.events
        notify.save_config(root, config)
        print(f"[notify] saved to {notify.config_path(root)}")
        print("[notify] this lives in the SHARED folder, so both machines will post "
              "to the channel - that's deliberate.")

    url = notify.webhook_url(root)
    if not url:
        print("[notify] not configured. Set one up with:")
        print(f"    dawbridge notify --folder {args.folder} --webhook <discord webhook url>")
        return 0

    events = [e for e in ("publish", "load") if notify.is_enabled_for(root, e)]
    print(f"[notify] configured, posting on: {', '.join(events) or 'nothing'}")

    if args.test:
        problem = notify.post(root, "DAWBridge test message - notifications are working.")
        if problem:
            print(f"[notify] {problem}", file=sys.stderr)
            return 1
        print("[notify] test message sent - check the channel.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dawbridge")
    sub = parser.add_subparsers(dest="command", required=True)

    # pull = bring the shared session in, push = send yours out. The words
    # mean what they mean everywhere else; the functions are named for the
    # action so the mapping is visible in one place rather than inferred.
    for name, fn in (("pull", cmd_load), ("push", cmd_publish), ("preview", cmd_preview)):
        p = sub.add_parser(name)
        p.add_argument("--daw", required=True, choices=["reaper", "protools"])
        p.add_argument("--folder", required=True, help="Path to the shared network folder")
        if name == "pull":
            p.add_argument("--dry-run", action="store_true",
                           help="show what would change, then stop without touching the DAW")
            p.add_argument("--save-project-as", metavar="PATH",
                           help="if the DAW's project has never been saved, save it here first, "
                                "so the audio can be copied beside it instead of played from "
                                "the shared folder (Reaper only; ignored if already saved)")
        if name == "push":
            p.add_argument("--force", action="store_true",
                           help="publish even though someone else published since you last synced, "
                                "replacing their changes")
        p.set_defaults(func=fn, dry_run=False, force=False, save_project_as=None)

    for name, fn in (("status", cmd_status), ("history", cmd_history), ("doctor", cmd_doctor)):
        p = sub.add_parser(name)
        p.add_argument("--folder", required=True)
        p.set_defaults(func=fn)

    p = sub.add_parser("notify")
    p.add_argument("--folder", required=True)
    p.add_argument("--webhook", help="Discord webhook URL to post to")
    p.add_argument("--events", nargs="+", choices=["publish", "load"],
                   help="which events to post (default: both)")
    p.add_argument("--test", action="store_true", help="send a test message now")
    p.add_argument("--off", action="store_true",
                   help="turn notifications off for this project")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("restore")
    p.add_argument("--folder", required=True)
    p.add_argument("--revision", required=True, type=int,
                   help="which archived revision to republish (see `dawbridge history`)")
    p.set_defaults(func=cmd_restore)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except UnsupportedSchemaVersion as exc:
        # Caught here rather than per-command: the two people sharing a
        # folder will end up on different versions of the app, and the one
        # who hasn't updated should get a sentence telling them so from
        # every command, not a traceback from whichever one they tried.
        print(f"[dawbridge] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
