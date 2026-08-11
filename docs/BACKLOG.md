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

## Input needed

Everything waiting on Travis, in one place. **Blocking** stops work;
**Proposed** doesn't - it's ideas parked until he wants to triage them.

Anyone - main session, any agent - may add to Proposed. The rule is that
a proposal carries its evidence: what problem it solves, where the idea
came from, and roughly what it costs. An idea with no evidence is a
preference, and preferences belong in conversation, not on a list.

### Blocking

- **How does the collaborator receive the `.exe`?** And do they get
  `docs/QUICKSTART.md` with it? Unanswered, and it is now the last thing
  between this project and being used by two people.
- **Does the collaborator know about the Discord channel?** The notify
  config lives in the shared folder, so their copy starts posting as soon
  as it reads it - without them having opted in on their machine. That is
  the intended design, but it warrants a heads-up rather than a surprise.
- **Split the audit agent in two?** Travis proposed: `dawbridge-collab`
  finds bugs and files failing tests, a new implementer agent makes them
  pass. Agreed in principle, deferred until the live E2E landed - which it
  now has. The boundary would be `tests/` (auditor) versus source
  (implementer). Awaiting the word.
- **Audio-reference asymmetry** *(a decision, not a fix)*. Reaper
  references audio directly in the Dropbox folder; Pro Tools copies into
  its own session folder. So a Reaper project breaks if the folder moves
  or goes offline, and Reaper writes peak files into the shared folder.

### Proposed - awaiting triage

Nothing here is agreed. Ordered by value-per-effort as I see it.

**1. A note attached to each publish.**
*What.* A one-line message saved with the revision - "redid the second verse
vocal, moved the bridge back 4 bars".
*Why.* A revision records who and when but never why. This makes `History...`
legible, gives the Discord message something worth reading, and tells your
collaborator what they're about to pull before they pull it.
*Evidence.* Every recovery path we built assumes someone can identify the
right revision. Right now they identify it by track count.
*Effort.* Small - one model field, one text box, one line in the notification.
*Recommendation.* Do it first. Highest value per hour on this list.

**2. "What changed since I last looked."**
*What.* A view of the difference between the shared session now and the
revision this machine last saw.
*Why.* It's the question you have every single time, and neither front end
can answer it.
*Evidence.* The data already exists - `archive/` holds the old revisions and
`syncstate` records which one you last saw. No DAW needed, no new storage.
*Effort.* Small-to-medium. Pure computation over two files already on disk.
*Recommendation.* Do it with #1; a note makes the diff far more useful.

**3. Notice when the shared session moves, without being asked.**
*What.* Poll `session.json` every ~30s; the status lamp reports "Conner
published r37, two minutes ago."
*Why.* Turns DAWBridge from a thing you operate into a thing that tells you
something. Two people working blind at the same time is the root of the
whole overwrite problem.
*Evidence.* `session.json` is 4KB; polling it is free next to the audio.
*Effort.* Small.

**4. Publish only the tracks you changed.**
*What.* Scope a publish to selected tracks instead of replacing everything.
*Why.* Every "you overwrote my work" failure traces to wholesale replace.
This removes the class structurally rather than guarding against it.
*Evidence.* Tracks already carry bridge ids, so this is not a merge engine -
there is nothing to reconcile, only "replace these three, leave the rest".
*Effort.* Large, and it revises a decision recorded below as settled.
*Recommendation.* Discuss before building. It's the biggest idea here and
the one most likely to be right.

**5. A reference bounce.**
*What.* Publish a rough mixdown alongside the session.
*Why.* Lets the other person hear where you got to without loading anything -
and catches "your version sounds wrong on my end" before a session is opened.
*Evidence.* The one idea here that's specific to music rather than to file
sync.
*Effort.* Medium; both DAWs can render, neither API makes it trivial.

**6. "Undo my last publish."**
*What.* One button that restores the revision immediately before yours.
*Why.* `History...` already makes this possible, but it asks a panicking
person to understand revisions first.
*Effort.* Small - it's `restore_archived` with the argument chosen for them.

**7. Tell an out-of-date copy where to get the new one.**
*What.* A version check against the repo or the shared folder.
*Why.* Two people on different builds is ordinary. The format guard already
tells the older one to update; nothing tells them how.
*Effort.* Small.

---

## Blocked on a live DAW

**Nothing.** Both DAWs were exercised live on 2026-08-10 and every question
on this list was answered. What remains unproven is narrower and recorded in
the Reaper module docstring: the **clip** push/pull path has never been run
end to end against a real Reaper.

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

5. **The `.exe` has no way to check for a newer version.** Two people on
   different builds is ordinary; the format guard tells the older one to
   update, but nothing tells them how.

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
- **Reaper's tempo write drags the whole arrangement.** `SetCurrentBPM`
  scales every beat-attached item, length, fade and marker by the tempo
  ratio - measured, 5.3333s/0.3333s became 4.0000s/0.2500s on 90->120.
  Canonical is in seconds, so the tempo is written only into a project
  with no items. `SetTempoTimeSigMarker` *can* write the time signature,
  but by the same mechanism, so it stays a warning.
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
