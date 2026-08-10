# DAWBridge backlog

The plan, written down so it doesn't live in one session's head. Anyone —
a new session, a subagent, Travis from a phone — should be able to read
this and know what's next and why.

**Keep it current.** If you finish something, move it. If you learn
something that changes the order, say so here rather than only in a
report nobody re-reads.

---

## Who owns which files

Three workers edit this repo concurrently. Collisions are the main way
work gets lost, so ownership is explicit.

| Owner | Files |
|---|---|
| **main session** | `store.py`, `cli.py`, `gui.py`, `docs/`, build + release |
| **dawbridge-design** | `theme.py`, `assets/`, the identity artifact, `build_exe.py` icon wiring |
| **dawbridge-collab** | `sync.py`, `model.py`, `backend.py`, both backends, `audiofile.py`, `syncstate.py`, `conflicts.py`, `tests/` |

Agents don't message each other. They report to the main session, which
relays. If a change needs a file you don't own, **describe the shape you
need and let the owner wire it** — that has worked every time it's been
tried and failed the once it wasn't.

---

## Blocked on Travis

- **Push to GitHub.** Remote is configured (`cobrawidner/DAWBridge`, private,
  verified). The stored PAT is expired, so it needs a browser sign-in from the
  machine: `git push -u origin main`, and if no browser appears,
  `cmdkey /delete:LegacyGeneric:target=git:https://github.com` first. Until
  this lands, phone-based work is impossible.
- **How does the collaborator receive the `.exe`?** And do they get
  `docs/QUICKSTART.md` with it? Unanswered.

## Blocked on a live DAW

The Pro Tools half was settled by live test on 2026-08-10. What remains is
all Reaper, and all of it is blocked on **one dialog dismissal**: Reaper put
up a "New Version Notification" modal, which holds its main thread, so the
web interface on port 2307 stops serving and reapy cannot attach. One click
on *Close* unblocks every item below. The probe script is written and waiting
at `scratchpad/probe_reaper.py`.

- **Reaper `EnumProjectMarkers2` tuple layout** — both handled blind by
  `_decode_marker_row`; safe, but which one this install returns is unknown.
- **Fade read-back** on the Reaper side.
- **Reaper tempo-map write** for time signature — still refused, since it
  edits the tempo map and needs a live Reaper to validate against.

Note: reapy is already configured on this machine (`csurf_0=HTTP 0 2307`,
server script registered in `reaper-kb.ini`). `dist_api_is_enabled()` returns
False only when Reaper isn't running - that is not a setup problem.

---

## Queued work

1. **Rename `Backend.pull` / `Backend.push`** *(dawbridge-collab, after live
   E2E)*. They mean "read the DAW" and "write the DAW" and now invert against
   the user-facing commands of the same name. Suggested: `read_from_daw` /
   `write_to_daw`, or `capture` / `apply`. Four call sites in `cli.py` and
   `gui.py` belong to the main session and get wired once the names are chosen.

2. **No preview for the publish direction** *(main session)*. `preview` only
   describes what a pull would change in your DAW. Before publishing —
   the destructive direction — there is nothing that shows what you're about
   to overwrite.

3. **`Session.name` is never captured.** Every session reads `'Untitled'`
   in the status pane. Small, cosmetic, visible constantly.

4. **Fidelity gaps still open** *(dawbridge-collab)*. Time signature is
   captured and never applied anywhere. Fades aren't read on the Pro Tools
   side. See the field-fidelity table in the audit reports.

5. **Audio-reference asymmetry.** Reaper references audio directly in the
   Dropbox folder; Pro Tools copies into its own session folder. So a Reaper
   project breaks if the folder moves, and Reaper writes peak files into the
   shared folder. Needs a decision, not a fix.

---

## Decided — do not re-litigate

Recorded so nobody spends a session rediscovering the reasoning.

- **Publishing replaces wholesale. There is no merge.** Deliberate
  simplification. Don't build a merge engine.
- **Pushing into a DAW never deletes.** Clips present in the DAW but not in
  the shared session are left alone and reported as orphans.
- **Identity lives in DAW-native names** (`Lead Vocal #a1b2c3d4`). It's the
  only thing two applications sharing no identifiers can agree on.
- **Commands are named from the user's point of view** — `pull` brings the
  shared session in, `push` sends yours out. Clean break, no aliases.
- **Dark theme only.** Measured: every accent fails contrast on a light
  chassis, including the steel blue that ships. A saturated accent needs a
  dark ground — which is also why the mark carries its own tile.
- **The readout rule.** Anything reporting state sits on `#101315`, never on
  the faceplate. Unaffected by dropping the light theme: a display is dark
  because it's a display.
- **Markers were implemented, not deleted** — a format claiming fidelity it
  doesn't have is worse than a smaller honest one.
- **Pro Tools cannot move a clip that already exists, and this is not
  fixable.** Confirmed live: `CId_GetClipList` returns an empty list even
  with clips on the timeline and a selection made, so there is no read path
  to a timeline clip's identity; and re-spotting a clip's own ids
  *duplicates* rather than moves — one clip at 4s became two, at 4s and 10s.
  The obvious "fix" would silently duplicate every moved clip. Warn-don't-move
  is correct. Do not revisit without new PTSL commands.
- **Pro Tools memory-location times are samples.** Reads are always samples
  regardless of the main counter (verified against Bars|Beats, TimeCode and
  Min:Secs); writes accept several formats and resolve them correctly. The
  earlier format-learning fallback was built on a wrong assumption and is
  gone.
- **PTSL has no tempo, meter, marker or time-signature command** — re-verified
  against the installed protobufs: 276 commands, none of them.
- **The mark is `i2` in signal orange.** `.ico` and `dawbridge_mark.svg` are
  build outputs; regenerate with `python tools/build_exe.py --icon-only` after
  any colourway change — they don't update from a `theme.py` edit alone.

---

## Working notes

- **The damage in this codebase is quiet.** Almost every bug found has been an
  operation that appeared to succeed: audio arriving mono while still
  reporting stereo, positions quantising and drifting, a rename dropped
  silently, an entire arrangement published as empty. Prioritise silent
  failures over loud ones. A crash is a good day.
- **Size work to survive being cut off.** Three agent runs have been killed
  mid-task by API session limits. Settle and record one question at a time
  rather than doing all the setup and leaving the answers to the end.
- **The full suite runs with no DAW open** — 209 tests at time of writing. So
  most work here is possible from a cloud checkout; only live verification
  isn't.
