# DAWBridge — getting started

DAWBridge lets several people work on the same song in **different DAWs**.
Some of you are in Reaper, some in Pro Tools, and a shared Google Drive folder
passes the arrangement back and forth.

You don't need to understand anything technical to use it. You do need to
understand one idea and two buttons.

---

## The one idea

**The shared folder is the master copy.**

Think of it as a whiteboard everyone can see. You copy the whiteboard into
your DAW, do your work, then copy your version back onto the whiteboard. The
last person to write on it wins — there is no clever merging.

That's why the order matters: **load first, work second, publish third.** If
you publish without loading first, you write over whatever your collaborators
put there.

---

## The two buttons

"The Bridge" is the shared folder. You pull work in from it and push your work
out to it — the same way those words work in any other tool.

| Button | What it does |
|---|---|
| **Pull from Bridge** | Brings the shared version **into your DAW**. This is how you get your collaborators's work. Do this first, every time. |
| **Push to Bridge** | Sends what's in **your DAW** out to the shared folder. This is how you publish. Do this when you're done. |
| **Preview pull** | Shows exactly what "Pull from Bridge" would change, and changes nothing. Free to click. |
| **Refresh status** | Re-reads the shared folder and shows what's there right now. |
| **Check folder** | Looks for problems — missing audio, conflicted copies, overlapping clips. Changes nothing. |
| **History...** | The last 20 versions of the shared session, and a way to put one back. |
| **Notifications...** | Set up a Discord channel that gets told whenever anyone publishes or loads. |

If you remember nothing else: **pull to get theirs, push to send yours.**

The buttons sit left to right in the order you use them.

---

## Setting up, once

1. **Install Google Drive** and accept the shared folder invitation. Let it finish
   syncing before you do anything else — the folder contains audio, and it can
   take a while.
2. **Open your DAW** and open the song you're working on. DAWBridge talks to
   whatever project is currently open, so make sure it's the right one.

   Joining a project for the first time? Then there's nothing to open yet —
   start a new empty project and pull into that. DAWBridge will ask you to
   save it first; see below.
3. **Run `DAWBridge.exe`.** There's nothing to install — it's one file.

   **Reaper users, one extra step, once.** Reaper needs Python installed
   before it can talk to DAWBridge — about five minutes, and you never do
   it again. Follow **REAPER_SETUP.md**, which has the download link and
   the two commands to run.

   **Pro Tools users:** nothing to do here.
4. Click **Browse…** and select the shared folder — the one ending in
   `.dawbridge`.
5. Pick your DAW: **Reaper** or **Pro Tools**. The selected one is highlighted.
6. Type your name into **Your name**. Optional, but without it every version
   you publish is credited to "gui@reaper" — including in the history your
   collaborator reads when trying to work out which version to go back to.

It remembers all three next time.

---

## The normal working session

1. **Preview pull**, if you want to see what's waiting before you take it.
   This changes nothing.
2. **Pull from Bridge** — brings in whatever your collaborators publish.
3. Read the log. It tells you what changed.
4. **Work in your DAW as normal.** DAWBridge isn't running anything while you
   work.
5. **Save your project in your DAW.** DAWBridge reads what your DAW currently
   has; unsaved work is a grey area, so save first.
6. **Push to Bridge** — publishes your version.
7. Wait for Google Drive to finish syncing before you close the laptop.

There's no preview of a publish — the preview only shows what a *pull* would
change in your DAW. Before publishing, the thing to check is that you pulled
first.

Tell everyone when you've published. DAWBridge will *catch* the case
where two of you publish at once, but a message is faster than a warning — and
if a Discord channel is set up (below), that message sends itself.

---

## Your first pull, and where the audio lives

The first time you pull into a brand new project, DAWBridge asks whether you
want to save it. Say yes and **Reaper** opens its own Save dialog — put the
project anywhere you like except inside the shared folder.

**Tick "Create subdirectory for project"** in that dialog. Reaper puts the
audio in a folder next to the project, so saving straight onto your Desktop
without this scatters an `Audio Files` folder across it.

It asks because **your DAW keeps its own copy of the audio, beside the
project.** That matters for three reasons:

- Your project keeps working if the shared folder is moved, renamed,
  unshared, or simply offline.
- Your DAW isn't playing files out of a folder Google Drive is syncing
  underneath it.
- Your DAW's waveform and peak files stay on your machine instead of being
  written into the shared folder, where they'd sync to everyone.

To be clear about what this is *not*: audio read from Google Drive isn't damaged
or lower quality — the file is identical either way. This is about the file
staying put and staying available.

You can say no, or cancel Reaper's dialog. The pull still works and
everything plays; the clips just read from the shared folder, and the log
says so. Save the project later and pull again, and the audio moves across
on its own.

Pro Tools has always worked this way — a Pro Tools session can't exist
unsaved, and it already keeps its audio in its own `Audio Files` folder — so
you'll only ever see this in Reaper.

---

## When it warns you

DAWBridge interrupts rather than guessing. Every one of these means real work
is at risk.

**"someone else has published since your last sync"**
Your collaborators publish while you were working. If you continue, your
version replaces theirs and theirs is gone from the shared folder. What you
almost always want instead: cancel, click **Pull from Bridge** to load their
work, check your own changes are still there, then publish.

**"this DAW has a different project open than your last sync"**
You have a different song open than last time. Publishing would replace the
shared session with *this* song's contents. If you meant to switch songs, fine
— otherwise open the right project first.

**"the shared session moved while your DAW was being read"**
Your collaborators publish in the few seconds it took to read your DAW.
Nothing was written. Load theirs, then publish again.

**"clips reference audio missing from the shared folder"**
Usually Google Drive simply hasn't finished downloading. Check the Google Drive icon,
wait, try again. If it persists, the audio genuinely didn't get published.

**A "conflicted copy" is mentioned**
Two of you published at almost the same moment and Google Drive kept both files. One
version is sitting in a file nobody is reading. Don't publish again until it's
sorted — ask for help, because the extra file is the only copy of somebody's
work.

---

## If something goes wrong

**The shared folder keeps the last 20 versions.** Nothing you do is one click
from being lost forever, including publishing over someone. That's the thing
to remember when a warning appears and you're not sure: it's recoverable, so
stop and ask rather than guessing.

**Check folder** tells you whether anything is wrong, and **History...** shows
the last 20 versions — who published each, when, and how many tracks it had —
with a Restore button. Restoring publishes the old version as a *new* one, so
it doesn't rewind, your collaborators's copy notices, and what it replaces is
archived too. It changes only the shared folder; use **Pull from Bridge**
afterwards to get it into your DAW.

If that isn't enough, **contact whoever set this up** and say what happened
and roughly when. With the developer setup the same tools exist on the
command line:

```bash
python -m dawbridge.cli doctor --folder "<the shared folder>"
```

Checks the folder for problems — missing audio, conflicted copies, overlapping
clips — without touching any DAW. Safe to run at any time.

```bash
python -m dawbridge.cli history --folder "<the shared folder>"
```

Lists the previous versions: who published each, when, and how many tracks it
had — usually enough to identify the one you want.

```bash
python -m dawbridge.cli restore --folder "<the shared folder>" --revision 12
```

Puts an old version back as the current shared version. It moves *forward* —
publishing the old content as a new version rather than rewinding the counter
— so your collaborators's copy actually notices the change, and the version it
replaced is kept too, which makes an unwanted restore undoable.

---

## Telling each other automatically (optional)

DAWBridge can post to a Discord channel every time anyone publishes or
loads, so nobody has to remember to say so.

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick
the channel, and copy the URL.

Then in DAWBridge click **Notifications…**, paste the URL, and press **Save and
send test**. If the message appears in your channel, you're done.

Same thing from the command line, if you have the developer setup:

```bash
python -m dawbridge.cli notify --folder "<the shared folder>" --webhook "<the url>" --test
```

Set it up once and **everyone** posts — the URL lives in the shared folder,
so your collaborators's copy finds it and needs no setup. A channel only some of
you reach is worse than none.

Messages say who, which DAW, the revision and how many tracks and clips. They
never include file paths, audio, or anything from inside your session. If
Discord is unreachable the sync still completes normally and the app says so
in the log.

**Turn off** removes the webhook for the project, so nobody gets messages until it's set up again. To post only on publishes, use the same window.

---

## What doesn't cross yet

Being upfront so nothing surprises you mid-session:

- **Markers** cross both ways, but haven't been tested against a live DAW
  yet. In Pro Tools they need at least one memory location that Pro Tools
  itself created before DAWBridge will write any - it copies the time format
  from yours rather than guessing, because guessing would put every marker at
  a confidently wrong position. If none exists it writes nothing and tells you
  what it skipped.
- **Reaper regions** don't cross. Only markers do — a region has a start and
  an end, and the shared format has nowhere to put the end, so publishing one
  would quietly flatten it to a point. You get a warning instead.
- **Time signature** is read but not applied. If your song isn't in 4/4, set
  the meter yourself on the receiving side — the tempo does cross.
- **Fades** cross from the shared folder into your DAW, but aren't read back
  out. A crossfade you create may not survive being published.
- **In Pro Tools**, clips that already exist aren't moved when you pull. New
  clips arrive correctly; if the preview says a clip moved and it didn't,
  that's this.
- **Anything DAWBridge doesn't know about** — plugins, automation, mixer
  settings, routing — stays entirely on your machine. This syncs the
  arrangement: tracks, clips, positions and audio.

DAWBridge never deletes anything in your DAW. The worst it does to *your*
project is add things. The risk is all on the shared folder, which is why the
warnings are worth reading.
