# DAWBridge — getting started

DAWBridge lets two people work on the same song in **different DAWs**. One of
you is in Reaper, the other in Pro Tools, and a shared Dropbox folder passes
the arrangement back and forth.

You don't need to understand anything technical to use it. You do need to
understand one idea and two buttons.

---

## The one idea

**The shared folder is the master copy.**

Think of it as a whiteboard both of you can see. You copy the whiteboard into
your DAW, do your work, then copy your version back onto the whiteboard. The
last person to write on it wins — there is no clever merging.

That's why the order matters: **load first, work second, publish third.** If
you publish without loading first, you write over whatever your collaborator
put there.

---

## The two buttons

"The Bridge" is the shared folder. You pull work in from it and push your work
out to it — the same way those words work in any other tool.

| Button | What it does |
|---|---|
| **Pull from Bridge** | Brings the shared version **into your DAW**. This is how you get your collaborator's work. Do this first, every time. |
| **Push to Bridge** | Sends what's in **your DAW** out to the shared folder. This is how you publish. Do this when you're done. |
| **Preview pull** | Shows exactly what "Pull from Bridge" would change, and changes nothing. Free to click. |
| **Refresh status** | Re-reads the shared folder and shows what's there right now. |
| **Check folder** | Looks for problems — missing audio, conflicted copies, overlapping clips. Changes nothing. |
| **History...** | The last 20 versions of the shared session, and a way to put one back. |
| **Notifications...** | Set up a Discord channel that gets told whenever either of you publishes or loads. |

If you remember nothing else: **pull to get theirs, push to send yours.**

The buttons sit left to right in the order you use them.

---

## Setting up, once

1. **Install Dropbox** and accept the shared folder invitation. Let it finish
   syncing before you do anything else — the folder contains audio, and it can
   take a while.
2. **Open your DAW** and open the song you're working on. DAWBridge talks to
   whatever project is currently open, so make sure it's the right one.
3. **Run `DAWBridge.exe`.**
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
2. **Pull from Bridge** — brings in whatever your collaborator published.
3. Read the log. It tells you what changed.
4. **Work in your DAW as normal.** DAWBridge isn't running anything while you
   work.
5. **Save your project in your DAW.** DAWBridge reads what your DAW currently
   has; unsaved work is a grey area, so save first.
6. **Push to Bridge** — publishes your version.
7. Wait for Dropbox to finish syncing before you close the laptop.

There's no preview of a publish — the preview only shows what a *pull* would
change in your DAW. Before publishing, the thing to check is that you pulled
first.

Tell your collaborator when you've published. DAWBridge will *catch* the case
where you both publish at once, but a message is faster than a warning — and
if a Discord channel is set up (below), that message sends itself.

---

## When it warns you

DAWBridge interrupts rather than guessing. Every one of these means real work
is at risk.

**"someone else has published since your last sync"**
Your collaborator published while you were working. If you continue, your
version replaces theirs and theirs is gone from the shared folder. What you
almost always want instead: cancel, click **Pull from Bridge** to load their
work, check your own changes are still there, then publish.

**"this DAW has a different project open than your last sync"**
You have a different song open than last time. Publishing would replace the
shared session with *this* song's contents. If you meant to switch songs, fine
— otherwise open the right project first.

**"the shared session moved while your DAW was being read"**
Your collaborator published in the few seconds it took to read your DAW.
Nothing was written. Load theirs, then publish again.

**"clips reference audio missing from the shared folder"**
Usually Dropbox simply hasn't finished downloading. Check the Dropbox icon,
wait, try again. If it persists, the audio genuinely didn't get published.

**A "conflicted copy" is mentioned**
You both published at almost the same moment and Dropbox kept both files. One
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
it doesn't rewind, your collaborator's copy notices, and what it replaces is
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
— so your collaborator's copy actually notices the change, and the version it
replaced is kept too, which makes an unwanted restore undoable.

---

## Telling each other automatically (optional)

DAWBridge can post to a Discord channel every time either of you publishes or
loads, so nobody has to remember to say so.

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick
the channel, and copy the URL.

Then in DAWBridge click **Notifications…**, paste the URL, and press **Save and
send test**. If the message appears in your channel, you're done.

Same thing from the command line, if you have the developer setup:

```bash
python -m dawbridge.cli notify --folder "<the shared folder>" --webhook "<the url>" --test
```

Set it up once and **both** of you post — the URL lives in the shared folder,
so your collaborator's copy finds it and needs no setup. A channel only one of
you reaches is worse than none.

Messages say who, which DAW, the revision and how many tracks and clips. They
never include file paths, audio, or anything from inside your session. If
Discord is unreachable the sync still completes normally and the app says so
in the log.

**Turn off** removes the webhook for the project, so neither of you gets messages until it's set up again. To post only on publishes, use the same window.

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
