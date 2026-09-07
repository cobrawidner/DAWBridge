# DAWBridge (Phase 1)

**[Read the handbook](https://cobrawidner.github.io/DAWBridge/)** — everything you need to use this, in about ten minutes. Reaper users also need the [one-time Python setup](docs/REAPER_SETUP.md).

**[Download the latest build](https://github.com/cobrawidner/DAWBridge/releases/latest)** — one file, nothing to install.

Shared-folder session bridge between Reaper and Pro Tools, for two people in
different locations who each prefer a different DAW. See the
`DAWBridge_Feasibility.md` write-up shared alongside this project for the
full background and the reasoning behind these choices.

## How it works

A shared network folder holds the source of truth: a `session.json`
describing tracks, clips, and markers (positions, lengths, which audio file
each clip uses - not automation, not plugin/mix state, see "Out of scope"
below), plus an `audio/` folder of the actual audio files.

Neither DAW's native project file is touched by the other side. Instead,
each machine runs `dawbridge` against whichever DAW is open there, and it
drives that DAW *live*:

- **Reaper**: via [ReaScript](https://www.reaper.fm/sdk/reascript/reascripthelp.html),
  through the [python-reapy](https://python-reapy.readthedocs.io/) wrapper.
- **Pro Tools**: via Avid's [PTSL (Pro Tools Scripting SDK)](https://developer.avid.com/scripting/),
  through the [py-ptsl](https://github.com/iluvcapra/py-ptsl) wrapper.

Both DAWs need to actually be open for a sync to do anything - this isn't a
headless batch process, it's closer to "run this command while your DAW is
open and it'll bring things up to date."

Every track and clip DAWBridge manages gets a short id stamped into its
name (e.g. `Lead Vocal #a1b2c3d4`) so it can be recognized again on a later
sync even if you rename it. Untagged tracks/clips are local content the
bridge has never seen - it leaves them alone.

## Commands (manual trigger, Phase 1)

```bash
# Pull whatever's currently in your open DAW into the shared session
dawbridge pull --daw reaper    --folder /path/to/shared/folder
dawbridge pull --daw protools  --folder /path/to/shared/folder

# Apply the shared session into your open DAW
dawbridge push --daw reaper    --folder /path/to/shared/folder
dawbridge push --daw protools  --folder /path/to/shared/folder

# See what's in the shared session without touching either DAW
dawbridge status --folder /path/to/shared/folder
```

A typical round trip: you arrange in Reaper, run `pull` to capture it into
the shared session, your friend runs `push` in Pro Tools to bring it in.
There's no automatic merge of simultaneous edits yet (see Roadmap) - treat
it as "whoever's turn it is runs pull, then push."

## Non-destructive by design

- Pushing a track that already exists (matched by its tag) only adds/moves
  clips on it - it never deletes or recreates the track, so whatever
  effects chain you built on it locally is untouched.
- A clip that disappears from the canonical session (e.g. deleted on the
  other side) is never auto-deleted locally - it's reported as a warning
  so you can review and remove it yourself.
- Each side owns and maintains their own plugin/mix setup entirely. The
  bridge never reads or writes insert/send/plugin state.

## Out of scope (for now)

- **Automation curves.** PTSL doesn't currently expose a way to read or
  write automation, so this isn't attempted. If that changes in a future
  PTSL version, this is the first thing worth revisiting.
- **Plugin/mix state.** Deliberately excluded - AAX and VST plugin state
  aren't portable between DAWs anyway, so each side maintains their own.
- **Automatic bidirectional merge.** If you both edit before syncing, last
  push wins for anything that conflicts. See Roadmap.

## Setup

### If you're using the .exe (what collaborators should do)

Download `DAWBridge.exe` from the Releases page and run it. Nothing to
install, Python included.

**Reaper:** close Reaper, press **Set up Reaper...**, then start Reaper
again. That's it.

DAWBridge carries its own copy of Python for Reaper to load, because reapy
is a client/server pair: the client ships in the .exe, but the server half
is a ReaScript that runs *inside* Reaper on an interpreter Reaper loads
itself. That used to mean every collaborator had to install a Python of a
version Reaper would accept before anything worked at all. It now unpacks
to `%LOCALAPPDATA%\DAWBridge\reaper-python`, nothing goes on PATH, no
Python already on the machine is touched, and deleting that folder undoes
it.

Reaper must be closed during setup — it rewrites `reaper.ini` from memory
when it quits, which would silently undo the whole thing.

**Pro Tools:** nothing to set up. PTSL is gRPC over a socket, so no code
runs inside Pro Tools. You do need to accept Avid's Scripting SDK licence
(below) and have scripting enabled in Pro Tools.

### If you're working from source

```bash
# Reaper machine
pip install python-reapy
python -c "import reapy; reapy.configure_reaper()"
# restart Reaper

# Pro Tools machine
pip install py-ptsl
```
You'll also need to accept Avid's Pro Tools Scripting SDK license at
https://developer.avid.com/scripting/ and make sure Pro Tools has scripting
access enabled (check current Pro Tools docs for the exact preference, this
has moved around between versions).

### Either machine, for the bridge itself
```bash
cd dawbridge
pip install -e .
```

### Building the .exe

```bash
python tools/build_reaper_runtime.py   # once; downloads ~10MB from python.org
python tools/build_exe.py
```

The first step produces `assets/reaper_runtime.zip`, the interpreter Reaper
loads. It isn't in git — it's third-party binaries that rebuild from a
pinned URL in seconds. `build_exe.py` warns rather than fails without it, so
a build that skipped it still runs and still syncs Pro Tools; it just can't
set Reaper up on its own. CI checks the finished exe's size to catch exactly
that.

## Status of this code

The parts that don't depend on a real DAW are written, tested, and passing:
`model.py`, `tagging.py`, `store.py`, `sync.py`, `backend.py` (including a
`MockBackend` used by the test suite). Run them with:

```bash
pip install pytest
pytest
```

`reaper_backend.py` and `protools_backend.py` are real implementations
against each API's documented commands, but **neither has been run against
a live Reaper or Pro Tools instance** - this was built in a sandbox with
neither application available. Specifically:

- `protools_backend.py` calls a few PTSL commands (`CreateNewTracks`,
  `Spot`, `SetTimelineSelection`, `CreateMemoryLocation`) that didn't have
  a documented `Engine` wrapper method as of this writing. Those calls go
  through a `_call()` helper that fails with clear guidance rather than
  guessing - see the comments at the top of that file for how to find the
  right call once py-ptsl is actually installed.
- Pulling clip-level detail from Pro Tools uses Avid's "Session Info as
  Text" export, parsed with a regex that hasn't been validated against a
  real export. Export a session to text once and compare before trusting
  it - see `_parse_session_text_export()`.
- `reaper_backend.py`'s ReaScript calls use long-stable function names,
  but reapy's exact argument conventions for a couple of buffer-style
  get/set calls should be double-checked against your installed version.

None of this is unusual for the DAW side of a project like this - it's the
part that can only really be finished with both apps in front of you. The
architecture and the non-destructive sync logic underneath it is the part
that matters most to get right early, and that's the part that's tested.

## Roadmap

1. **Now**: one-way, manually-triggered pull/push, timeline + clips only.
2. **Next**: validate the two backends against real Reaper/Pro Tools,
   fix up whatever guesses were wrong.
3. **Later**: revisit automation if/when PTSL exposes it; consider a
   watch-and-auto-sync mode once manual pull/push proves the model works;
   only build real bidirectional conflict handling if simultaneous edits
   turn out to actually happen often in practice.
