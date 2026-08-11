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

**4. Publish only the tracks you changed.** — **APPROVED 2026-08-10, in progress**
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

*8-13 come from reading the API manifests against the use case: 159 distinct
PTSL commands (DAWBridge calls 19) and 727 ReaScript functions reachable
through reapy. Each says what exists, what we don't call, and what it would
buy two people who mostly want to know what the other did.*

**8. Keep a copy of each bridge id somewhere the user can't edit it.**
*What.* Alongside the id in the name, write it to Reaper's per-object extended
state (`P_EXT:` on tracks and items) and fall back to it when a name has lost
its tag.
*Why.* Identity lives only in a visible name today. Someone who tidies
"Lead Vocal #a1b2c3d4" back to "Lead Vocal" silently orphans the track - the
next pull adopts it as new and their partner collects a duplicate. Nothing can
warn, because a missing tag is indistinguishable from a genuinely new track.
*Evidence.* Verified live: `GetSetMediaTrackInfo_String(track, "P_EXT:db_id", ...)`
and the item equivalent round-trip through reapy - read back with a decoy
buffer to prove it isn't an echo, an unset key returns empty, and the value
**survived renaming the track**. Reaper persists it in the .rpp.
*Effort.* Medium: write on adopt, read as fallback, and decide what to do when
name and ext state disagree.
*Recommendation.* Strongest on this list. Explicitly NOT a change to
"identity lives in DAW-native names" - Pro Tools has no equivalent, so names
stay the cross-DAW mechanism. This is a Reaper-side safety net under it.

**9. Track colours.**
*What.* Carry a track's colour across the bridge.
*Why.* Colour is how musicians say "these four are the drums". It is the
cheapest possible answer to "what did they do", and it needs no explaining to
someone non-technical.
*Evidence.* Reaper's `I_CUSTOMCOLOR` reads and writes (verified live on the
scratch project). PTSL has `SetTrackColor` and `GetColorPalette`, neither
called. `Track` has no colour field.
*Effort.* Small, plus one judgement call: Reaper stores a native RGB integer
and Pro Tools a palette index, so the mapping is approximate either way.
*Recommendation.* A good first pleasant thing after all the correctness work.
Visible, cheap, and it cannot endanger audio.

**10. Say when a Pro Tools track has alternate playlists.**
*What.* On pull, count playlists per track and warn that only the active one
crosses.
*Why.* A comped vocal can sit on ten playlists. The text export shows only the
active one, so DAWBridge publishes a single take and says nothing - the partner
never learns the alternates exist, and a later push can look like the comp was
lost.
*Evidence.* `GetTrackPlaylists` and `GetPlaylistElements` exist in PTSL and are
unused. Exactly the same family as the loop and region warnings already shipped.
*Effort.* Small - read the count, add a warning.
*Recommendation.* Do the warning. Do NOT attempt to sync playlists: Reaper's
take lanes are not equivalent, and mapping them is a merge engine wearing a hat.

**11. Settle whether session start time offsets every position.**
*What.* An investigation, not a feature. Does a Pro Tools session starting at
01:00:00:00 publish its clips an hour out?
*Why.* Pro Tools sessions conventionally start at one hour, not zero.
DAWBridge stores seconds and takes positions from the text export's sample
columns. If those columns are relative to session start, every position from
such a session is wrong by 3600s - silently, and consistently enough to look
deliberate rather than broken.
*Evidence.* `GetSessionStartTime` / `SetSessionStartTime` exist and are unused.
Every live test so far used a scratch session starting at `00:00:00:00.00`, so
this case has never once been exercised. Genuinely unknown, either way.
*Effort.* Small to settle: set a scratch session to 01:00:00:00, place a clip
at a known point, pull, compare. Unknown to fix, if it needs fixing.
*Recommendation.* Settle it before anyone else relies on it. It either becomes
a bug fix or a line in Decided.

**12. We could sync automation, and shouldn't - but the reason on file is wrong.**
*What.* `model.py` says automation is excluded because PTSL cannot write it.
That is no longer true, and a wrong reason invites someone to "fix" it.
*Why.* The honest reason is scope: curves are mix, not arrangement, and the
decided position is that each side owns their own mix. That reason survives
contact with the API; "impossible" doesn't.
*Evidence.* `SetTrackControlBreakpoints`, `GetTrackControlBreakpoints` and
`GetTrackControlInfo` are all in the protocol and unused. Reaper exposes 41
envelope functions.
*Effort.* Correcting the docstring: minutes. The feature: large, and I would
argue against it.
*Recommendation.* Correct the docstring and move automation into Decided as
out-of-scope-by-choice. Do not build it.

**13. Let Pro Tools tell us it changed, rather than asking it.**
*What.* Subscribe to PTSL events so DAWBridge knows the local session has moved
since the last publish.
*Why.* A different question from #3, which watches the shared folder for the
partner. This watches your OWN DAW and answers "you have work you haven't
published yet" - the other half of not losing work, and the half nobody is
told about today.
*Evidence.* `SubscribeToEvents`, `PollEvents` and `UnsubscribeFromEvents` exist
in PTSL and are unused.
*Effort.* Medium, and Pro Tools only - Reaper would need a timer, so the two
sides would behave differently, which is its own cost.
*Recommendation.* Only after #3. If #3 lands and feels like enough, drop this.

## Blocked on a live DAW

**Proposal 11 (session start time), attempted and not settled.** Pro Tools
launches into its Dashboard when no session is open, and that modal blocks
PTSL - `create_session` never returns. Dismissing it is a click, and the
Dashboard's default Local Storage points at
`Dropbox\Conners (1)\ProTools Working Folder\`, so a misclick creates a
session in a real working folder. Left alone.
*Recipe for next time:* have someone dismiss the Dashboard first, or open an
existing session, THEN run
`scratchpad/probe_starttime.py` - it sets the start to 01:00:00:00, spots a
clip at 10.0s and prints the export's sample column against the three
reference numbers (1 hour = 172,800,000 samples at 48kHz). The zero-start
case is already known good: a clip spotted at 4.0s read back as 4.0s.

Otherwise both DAWs were exercised live on 2026-08-10 and every other question
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
  **Being revised, deliberately (2026-08-10).** Travis approved publishing
  only the tracks you changed. This is still not a content merge - no clip
  or take is ever reconciled - it only decides *which tracks a publish is
  allowed to touch*. The wholesale rule stays true inside any track a
  publish does touch.
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
