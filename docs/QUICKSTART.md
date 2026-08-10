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

The names describe what DAWBridge does *to your DAW*. If you've used GitHub,
ignore what "pull" and "push" mean there — it's the opposite.

| Button | What it actually does |
|---|---|
| **Push to DAW** | Takes the shared version and **puts it into your DAW**. This is how you get your collaborator's work. Do this first, every time. |
| **Pull from DAW** | Takes what's in **your DAW** and makes it the shared version. This is how you publish. Do this when you're done. |
| **Preview push** | Shows you exactly what "Push to DAW" would change, and changes nothing. Free to click. |
| **Refresh status** | Re-reads the shared folder. Shows what's there right now. |

If you remember nothing else: **Push to DAW = get theirs. Pull from DAW = send yours.**

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

It remembers both next time.

---

## The normal working session

1. **Push to DAW** — pulls in whatever your collaborator published.
2. Read the log. It tells you what changed.
3. **Work in your DAW as normal.** DAWBridge isn't running anything while you
   work.
4. **Save your project in your DAW.** DAWBridge reads what your DAW currently
   has; unsaved work is a grey area, so save first.
5. **Preview push**, if you want to see what will be published.
6. **Pull from DAW** — publishes your version.
7. Wait for Dropbox to finish syncing before you close the laptop.

Tell your collaborator when you've published. DAWBridge will *catch* the case
where you both publish at once, but a message is faster than a warning.

---

## When it warns you

DAWBridge interrupts rather than guessing. Every one of these means real work
is at risk.

**"someone else has published since your last sync"**
Your collaborator published while you were working. If you continue, your
version replaces theirs and theirs is gone from the shared folder. What you
almost always want instead: cancel, click **Push to DAW** to load their work,
check your own changes are still there, then publish.

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

Recovery isn't in the app yet. If you're running `DAWBridge.exe`, the buttons
in the window are all you have — **contact whoever set this up** and say what
happened and roughly when. That's enough to get the right version back.

If you have the developer setup (Python and the source), the tools are:

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

## What doesn't cross yet

Being upfront so nothing surprises you mid-session:

- **Markers** are being implemented. Don't rely on them crossing yet.
- **Time signature** is read but not applied. If your song isn't in 4/4, set
  the meter yourself on the receiving side — the tempo does cross.
- **Fades** cross from the shared folder into your DAW, but aren't read back
  out. A crossfade you create may not survive being published.
- **In Pro Tools**, clips that already exist aren't moved by a push. New clips
  arrive correctly; if the preview says a clip moved and it didn't, that's this.
- **Anything DAWBridge doesn't know about** — plugins, automation, mixer
  settings, routing — stays entirely on your machine. This syncs the
  arrangement: tracks, clips, positions and audio.

DAWBridge never deletes anything in your DAW. The worst it does to *your*
project is add things. The risk is all on the shared folder, which is why the
warnings are worth reading.
