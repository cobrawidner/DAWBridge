# DAWBridge backlog

The plan, written down so it doesn't live in one session's head. Anyone —
a new session, a subagent, Travis from a phone — should be able to read
this and know what's next and why.

**Keeping this true is the main session's job.** Agents file proposals and
update what they touch; the main session sees every landing, so it owns
whether this file still matches reality. Update it *as part of* landing
something, not when someone notices it has drifted - it has drifted twice
already, both times because it was treated as a thing to tidy later.

---

## Who owns which files

Four workers edit this repo concurrently. Collisions are the main way
work gets lost, so ownership is explicit.

| Owner | Files |
|---|---|
| **main session** | `store.py`, `cli.py`, `gui.py`, `checks.py`, `notify.py`, `docs/`, build + release |
| **dawbridge-design** | `theme.py`, `assets/`, the identity artifact, `build_exe.py` icon wiring |
| **dawbridge-collab** (auditor) | `tests/` - finds bugs, files failing tests, investigates, proposes. Does not edit source. |
| **dawbridge-build** (implementer) | `sync.py`, `model.py`, `backend.py`, both backends, `audiofile.py`, `syncstate.py`, `conflicts.py`. Does not edit tests. |

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

**Nothing.** Both long-standing items are resolved.

- *Releasing* stopped being manual: tagging runs the tests, builds the
  exe, publishes a release carrying DAWBridge.exe + the handbook +
  REAPER_SETUP.md, and posts a changelog to Discord. Verified end to end
  on v0.1.6.
- *Getting builds to people* stopped needing an invite list. The repo
  went public on 2026-09-07, so release downloads work for anyone with
  the link - no collaborator management at all. History was checked for
  secrets first: the only Discord URLs ever committed are test fixtures
  (`webhooks/1/abc`), notify.json was never tracked, no tokens.

The handbook is a live page: https://cobrawidner.github.io/DAWBridge/
Served by GitHub Pages from `docs/` on main, so it updates on push.

### Proposed - awaiting triage

Nothing here is agreed. Ordered by value-per-effort as I see it.

*Landed since this list was written: **4** (selective publish) and **8**
(extended-state identity). Distribution, the Discord heads-up, the agent
split and the audio asymmetry were all answered - see Queued and Decided.*

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

**4. Publish only the tracks you changed.** — **LANDED 2026-08-11.**
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

**8. Keep a copy of each bridge id somewhere the user can't edit it.** — **LANDED 2026-08-11.**
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

**9. Track colours.** — **LANDED 2026-08-11.** Shipped in `color.py`; see
Verified live below. Pro Tools snaps to its 69-colour palette, which the user
accepted ("we can live with colors not being exact").
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

## Verified live

**2026-08-11, Reaper 7.78, run from the main session** (the auditor was cut
off by API session limits six times running, so this was done directly).
Scratch project and scratch shared folder throughout; the per-machine sync
state was redirected to a scratch file and confirmed untouched afterwards.

- **Extended state survives close and reopen.** A project saved the previous
  day, Reaper quit and relaunched: `dawbridge_id` read straight back through
  the shipping accessor, on a track named plain `FadeProbe` with no tag.
  This is the assumption the whole identity net rested on, and it had never
  been run.
- **Identity recovery works end to end.** Canonical knew the track under its
  old name; Reaper's copy had no tag. The publish produced **one** track,
  not a duplicate, restamped Reaper's name to `FadeProbe #a1b2c3d4`, and
  warned.
- **A duplicate cannot steal the original's identity.** A second track
  carrying the same extended state and no name tag was adopted as new
  (`#40bfef98`) and reported as a copy; the original kept its id. This is
  the case that would have been worst to get wrong.
- **Selective publish preserves the partner's work.** A track added to
  canonical after this machine's baseline survived a publish from a DAW that
  had never seen it - `kept_theirs: ['Partner Overdub']`.
- **Deletion still works.** The same track, once it *was* in the baseline and
  absent from the DAW, was removed - so preserving by default did not
  quietly break deleting.

**Pro Tools, same day, session `DAWBridge E2E` at 44.1kHz:**

- `unavailable_reason()` returns `None` against a running Pro Tools, and
  the closed-DAW sentence was confirmed earlier - both live.
- `capture()` read a tagged track and its clip correctly, and the new
  warning channel fired usefully: the sample-rate change from 48000 to
  44100 was reported rather than silently accepted.
- **Selective publish preserves the partner's work from the Pro Tools
  side too** - a track added to canonical after the baseline survived,
  and an unchanged project correctly reported "no changes".

**Track colour, 2026-08-11, both DAWs:** Reaper reads and writes exactly;
Pro Tools snapped `#3F7FBF` to `#1D8DA4`, its nearest of 69, and then
**three consecutive Pro Tools publishes left canonical still at
`#3F7FBF`** - the accumulation guard holding, each cycle reporting "no
changes". A genuine recolour still crossed. Forward compatibility holds:
an old client parks `color` in `extra` and writes it back untouched.

Not exercised live: the `unavailable_reason` "running but refused"
branch, which needs a Pro Tools that accepts a connection and then
rejects the command.

**Local audio, verified live 2026-09-06, Reaper 7.78.** Scratch project and
scratch shared folder throughout; the real Google Drive folder was never touched.

- **Audio lands beside the project.** All three clips resolved to
  `<project>/Audio Files`, byte sizes matching the shared originals.
- **Projects already pointing at the shared folder migrate themselves.**
  Two clips created by an earlier pull pointed into the shared folder;
  the next pull re-pointed both to local copies with nothing written for
  the purpose.
- **Peak files follow the project.** Reaper created `Audio Files/peaks`
  beside the project. The shared folder had also collected a `peaks`
  directory earlier, from the pull made while the project was unsaved -
  the pollution this feature exists to stop, caught in the act.
- **Republishing duplicates nothing.** A push with no edits reported "no
  changes" and added no files to the shared `audio/`.
- **A re-record is still noticed.** An existing tagged clip re-pointed at
  different audio published as "1 track updated" and imported the new
  file. This is the one that mattered: `should_reimport_audio` short-
  circuits on filename, and had that been too eager it would have
  published a replaced take as no change at all.

**The save step, resolved 2026-09-06.** `Main_SaveProjectEx` was wrong
twice over, both confirmed live: it ignores the filename passed to it
(Reaper opens an EMPTY Save dialog - spotted by Travis, "its not
pre-filled"), and it returns immediately instead of blocking, so reading
the path back on the next line always found nothing and the verification
always reported failure - while the project sat saved on the Desktop.

Replaced by `prompt_save_project()`: raise Reaper's own dialog with
`Main_SaveProject(forceSaveAs)` and poll for the path against a
deadline. DAWBridge's own "where shall I save it?" box is gone - it was
collecting an answer that could never be honoured, and showing two
dialogs where one would do.

Verified live end to end: Reaper prompted, DAWBridge waited, project
saved to `Desktop/testsave.rpp`, and all three clips resolved to
`Desktop/Audio Files` with peaks alongside. local: 3, shared: 0.

Worth knowing for the quickstart: Reaper's Save dialog has a "Create
subdirectory for project" checkbox. Unticked, saving to a folder like
the Desktop drops `Audio Files` and `peaks` straight into it.

**Also open, minor:** migration only happens when a pull has other work
to do. A project that is fully in sync but still pointing at the shared
folder gets "nothing to do" and never migrates.

## Queued work

00. **Verify the bundled Reaper runtime against a live Reaper.** *Blocking
   the next release.* Shipped 2026-08-17: DAWBridge now carries python.org's
   embeddable Python with reapy installed, unpacks it to
   `%LOCALAPPDATA%\DAWBridge\reaper-python`, and writes the DLL path into
   `reaper.ini` itself, so no collaborator installs Python. Triggered by a
   collaborator getting it working and reporting that step as miserable.

   Proven locally: the embedded interpreter runs and imports reapy; the zip
   is really inside the .exe; `configure()` writes reascript/DLL path/web
   interface/kb entry against a fake Reaper install and verifies them back.

   **Not proven:** that Reaper itself loads this DLL. Reaper does
   `LoadLibrary` + `Py_Initialize` rather than running `python.exe`, and
   finds the stdlib through the `python310._pth` file sitting next to the
   DLL. That's the documented behaviour and the layout is right, but it has
   never been run. Needs: close Reaper, press **Set up Reaper...**, reopen,
   check DAWBridge reaches it. Do this on a machine that does *not* already
   have a working reapy setup, or the old one masks the result.

0. **Finish the more-than-two-people wording** *(dawbridge-build)*. The
   user-visible strings and `docs/` are done. About 30 instances of
   "your partner" / "both of you" / "two people" remain in `sync.py`,
   both backends, `conflicts.py`, `model.py` and `color.py` - mostly
   comments, but the sample-rate warning a user actually reads still says
   "your partner's next push". Prose only, no behaviour change. Started
   2026-08-11, cut off by an API session limit before any edit.

0b. **Reaper should use local copies of the audio, not the Google Drive files.**
   — **LANDED 2026-08-15**, together with a save prompt on the first pull
   into an unsaved project: the two are one feature, because an unsaved
   project has no folder to put the audio in. See `localmedia.py`.
   Not yet run against a live Reaper.

   *Approved 2026-08-11.* Pro Tools already copies audio into its own
   session folder; Reaper references the shared Google Drive path directly. So a
   Reaper project breaks if the folder moves or goes offline, Reaper writes
   peak files into the shared folder, and the DAW streams from a
   cloud-synced directory during playback.

   **The reason matters, because a wrong one invites the wrong fix.** Audio
   read from Google Drive is not degraded - the bytes are identical. The real
   problems are dropouts and latency reading from a synced folder,
   cloud-only placeholder files the DAW expects to be local, Google Drive
   re-syncing a file the DAW holds open, and peak files polluting the
   shared store. Do not "fix" this by touching bit depth or format.

   Copy to a local working folder on load, point takes there; import back
   into the shared store on publish, where content-hash dedup already
   handles the round trip. This makes Reaper behave like the backend that
   already got it right.

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
- **A Pro Tools session start time does not offset anything.** Settled
  live 2026-08-11: a clip spotted at 10.0s read back as 10.0s with the
  session starting at `00:00:00:00`, and *still* 10.0s after the start
  was moved to `01:00:00:00`. The text export's sample columns and
  `SpotClipsByID` are both relative to session start, consistently. This
  was proposal 11, and the feared 3600-second silent offset does not
  exist.
- **Reaper's `I_CUSTOMCOLOR` is a trap; use `GetTrackColor`.** Measured
  live: a brand-new track nobody has coloured still reads `16576`
  (`0x0040C0`) from `I_CUSTOMCOLOR`, because only the `0x1000000` flag
  bit distinguishes "chosen" from "default". Reading the raw integer
  would publish an orange nobody picked for every uncoloured track.
  `GetTrackColor` answers 0 for "no custom colour".
- **Pro Tools colour is read-free, write-palette-only.** The `Track`
  message from `track_list()` already carries `color` as `#AARRGGBB`, so
  reading needs no new command. `SetTrackColor` takes only a palette
  index, **1-based**, valid `[1;69]` - verified by walking the ends.
  Nearest-match is therefore necessary, and Travis accepted it.
- **py-ptsl serialises empty one-of selectors.** It sets
  `always_print_fields_with_no_presence=True`, so a command with two
  mutually exclusive selectors sends both and Pro Tools refuses. Fixed
  per-command with a `json_messup` override. Any future command of that
  shape will hit the same thing.
- **PTSL has no tempo, meter, marker or time-signature command** — re-verified
  against the installed protobufs: 276 commands, none of them.
- **The Discord config belongs in the shared folder, not per-machine.**
  It ties the channel to the project. A machine can mute itself, and
  that mute is the only thing recorded locally.
- **Distribution is GitHub Releases, built by CI.** `git tag v0.x.0 &&
  git push origin v0.x.0` builds the exe on a Windows runner, runs the
  suite, and attaches the binary and QUICKSTART to a Release. Nobody
  copies a build into Google Drive by hand. The repo is private, so the
  collaborator must be added as a GitHub collaborator to download it.
- **Discord notification config is shared, not per-machine.** It lives in
  `notify.json` in the shared folder, so both copies post to the channel
  once one person sets it up. A notification only one of you receives is
  worse than none. Travis confirmed the collaborator knows.
- **`Backend.capture` / `Backend.apply`** are the backend contract. The
  old `pull`/`push` names inverted against the user-facing commands and
  are gone.
- **The mark is `i2` in signal orange.** `.ico` and `dawbridge_mark.svg` are
  build outputs; regenerate with `python tools/build_exe.py --icon-only` after
  any colourway change — they don't update from a `theme.py` edit alone.

---

## Working notes

- **There can be more than two people.** Everyone with access to the
  shared folder can publish, so "your partner" is the wrong mental model
  and the wrong wording. Three consequences that are not just verbiage:
  the 20-revision archive fills roughly N times faster; simultaneous
  publishes (and so Google Drive conflicted copies) get likelier with every
  extra person; and `updated_by` records who published *last*, not who
  changed a given track - inferable with two people, guesswork with four.
- **The damage in this codebase is quiet.** Almost every bug found has been an
  operation that appeared to succeed: audio arriving mono while still
  reporting stereo, positions quantising and drifting, a rename dropped
  silently, an entire arrangement published as empty. Prioritise silent
  failures over loud ones. A crash is a good day.
- **Size work to survive being cut off.** Three agent runs have been killed
  mid-task by API session limits. Settle and record one question at a time
  rather than doing all the setup and leaving the answers to the end.
- **The full suite runs with no DAW open** — 284 tests at time of writing.
- **`EnumProjects` returns a NULL project as the *string*
  `'(ReaProject*)0x0000000000000000'`, which is truthy.** A naive
  `while proj:` enumeration never terminates. Nothing shipping does this,
  but it is a live landmine for the tab-safety check the safety rules
  ask for. So
  most work here is possible from a cloud checkout; only live verification
  isn't.
