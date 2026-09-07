# DAWBridge — everything you need to know

How to work on the same song as everyone else, without anyone losing a take.
Read it once and you're set.

**Nicer to read on the web:** https://cobrawidner.github.io/DAWBridge/

---

## What this is

Some of us work in Reaper. Some in Pro Tools. Those two don't open each
other's projects, and never will.

DAWBridge gets around that. It keeps one copy of the song in a shared Google Drive
folder, in a format both programs understand. You take that copy into whatever
you use, do your thing, and send it back.

It carries **tracks, audio clips, where they sit on the timeline, track names,
track colours, the tempo, and markers.** That's the arrangement — the actual
song. Your plugins, your mix and your routing stay on your machine, because
those don't translate between programs anyway.

---

## The one idea

**The shared folder is the real song.**

Picture a whiteboard in the practice room that everyone can see. You copy
what's on it into your notebook, work on it, then copy your version back up
onto the board.

> **Copy the board first. Work second. Put yours back last.**

That order is the whole thing. If you put yours back *without* copying the
board first, you wipe out whatever anyone else added while you were gone.
There's no clever merging — the last person to write on the board wins.

DAWBridge will stop you and ask before that happens. But it's much easier to
just do it in the right order.

---

## The window: seven buttons, two that matter

The buttons run left to right in the order you actually use them. There's a
groove down the middle: everything **left of the groove changes something**,
everything **right of it just looks**.

| Button | What it does |
|---|---|
| **Pull from Bridge** | Brings everyone else's work **into** your program. Do this first, every single time. |
| **Push to Bridge** | Sends **your** version out to the shared folder. Do this when you're done. |
| **Preview pull** | Shows exactly what a pull would change, and changes nothing. Free to click. |
| **Refresh status** | Re-reads the shared folder and tells you what's in it right now. |
| **Check folder** | Looks for trouble — missing audio, duplicate files, overlapping clips. Changes nothing. |
| **History…** | The last 20 versions, who saved each, and a button to put an old one back. |
| **Notifications…** | Set up a Discord channel that gets a message whenever anyone pulls or pushes. |

If you remember nothing else: **pull to get theirs, push to send yours.**

---

## Setting up — once, then never again

1. **Install Google Drive and accept the folder invite.** Then leave it alone until
   it finishes syncing. There's audio in there, so it takes a while. Starting
   before it's done is the most common way to see confusing errors.

2. **Reaper only: install Python once.** Reaper needs it before it can talk to
   DAWBridge. A download and two commands, about five minutes, and you never
   touch it again — see **REAPER_SETUP.md**. *Pro Tools people: skip this.*

3. **Open your program and open the song.** DAWBridge talks to whatever project
   is open right now. Joining a song for the first time? Start a new empty
   project and pull into that.

4. **Run DAWBridge.exe.** Nothing to install — it's one file.

5. **Point it at the folder.** Click **Browse…** and pick the shared folder,
   the one ending in `.dawbridge`.

6. **Pick your program and type your name.** Optional, but please do it.
   Without it every version you save is credited to "gui@reaper" — and that's
   who someone sees at 2am trying to work out which version to go back to.

It remembers all of that next time.

---

## First pull on a new song: where your audio lives

The first time you pull into a brand new project, DAWBridge asks if you want
to save it. Say yes — **Reaper** then opens its own Save box. Put the project
anywhere except inside the shared folder.

**Tick "Create subdirectory for project"** in that Save box. Your audio goes in
a folder next to the project, so saving straight onto your Desktop without
ticking it scatters an "Audio Files" folder across your Desktop.

It asks because **your program keeps its own copy of the audio, right next to
your project.** Three reasons that's worth a click:

- Your project keeps working even if the shared folder is moved, renamed,
  unshared, or you're offline.
- Your program isn't playing files out of a folder Google Drive is actively syncing
  underneath it.
- Your waveform files stay on your machine instead of being dumped into the
  shared folder, where they'd sync to everybody.

To be clear about what this is *not*: audio doesn't get damaged or lose quality
by sitting in Google Drive. The file is identical either way. This is about it
staying put and staying available.

You can say no. Everything still works and plays — the clips just read from the
shared folder, and the log says so. Save the project later and pull again, and
the audio moves across on its own.

Pro Tools already worked this way, so you'll only see this in Reaper.

---

## A normal session

1. **Preview pull**, if you want to see what's waiting. Changes nothing.
2. **Pull from Bridge.** Read the log — it says exactly what changed.
3. **Work.** DAWBridge isn't doing anything while you play. Forget it's open.
4. **Save your project** in your program. DAWBridge reads what's been saved.
5. **Push to Bridge.** Then wait for Google Drive to finish syncing before you shut
   the laptop.

There's no preview for pushing. The only thing worth checking before you push
is that you pulled first.

---

## What the warnings mean

DAWBridge would rather stop and ask than guess. Every one of these means
somebody's work is genuinely at risk.

**"someone else has published since your last sync"**
**Stop.** Somebody pushed while you were working. Carry on and your version
replaces theirs, and theirs is gone. What you want: cancel, **Pull from
Bridge** to get their work, check yours is still there, then push.

**"this DAW has a different project open than your last sync"**
You've got a different song open than last time. Pushing would replace the
shared song with *this* one. Fine if you meant to switch — otherwise open the
right project first.

**"the shared session moved while your DAW was being read"**
Someone pushed in the few seconds it took to read your project. Nothing was
written. Pull theirs, then push again.

**"clips reference audio missing from the shared folder"**
Usually just Google Drive not finished downloading. Check the tray icon, wait, try
again. If it persists, the audio genuinely didn't make it up there.

**"unmerged publishes are sitting in this folder"**
**Ask for help before doing anything else.** Two people pushed at nearly the
same moment. Google Drive can't merge, so it kept both — and one of them is
sitting in a file nobody is reading. That file is the only copy of somebody's
work, and normal Restore can't reach it.

---

## When it goes wrong anyway

**The shared folder keeps the last 20 versions.** That includes the one you
just pushed over the top of. This is the thing to remember when a warning pops
up and you're not sure: it's recoverable, so stop and ask rather than guessing.

**Check folder** tells you whether anything's wrong. **History…** lists the
last 20 versions — who saved each, when, and how many tracks — with a Restore
button.

Restoring publishes the old version as a *new* one. It doesn't rewind. That's
deliberate: everyone else's copy notices the change, and the version you
replaced is kept too, so an unwanted restore can itself be undone. It only
touches the shared folder — use **Pull from Bridge** afterwards to get it into
your program.

Still stuck? Message Travis with what happened and roughly when. The log window
is the useful bit — copy the text out of it.

---

## Optional: let the channel know for you

DAWBridge can post to Discord every time anyone pulls or pushes, so nobody has
to remember to say so. Messages look like:

```
Dead Flowers — Conner published r37 from Pro Tools — 14 track(s), 62 clip(s)
Pull from Bridge to pick it up.
```

In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick a
channel, copy the URL. Then in DAWBridge click **Notifications…**, paste it,
and press **Save and send test**.

**One person sets this up and everybody's copy starts posting** — the setting
lives in the shared folder, so nobody else has to do anything. A channel only
some of you reach is worse than none.

Messages say who, which program, the version number, and how many tracks and
clips. They never include file paths, audio, or anything from inside your
session. If Discord's down, your sync still works and the log says so.

---

## What doesn't cross yet

So none of this surprises you halfway through a session:

- **Reaper regions don't cross.** Markers do. A region has a start and an end
  and there's nowhere to put the end, so rather than quietly flattening it to a
  point you get a warning.
- **Time signature is read but not applied.** If the song isn't in 4/4, set the
  meter yourself on the receiving side. The tempo does cross.
- **Fades come in but don't go back out.** A crossfade you create may not
  survive being pushed.
- **In Pro Tools, clips that already exist don't get moved** by a pull. New
  clips arrive correctly.
- **Plugins, automation, mixer settings and routing stay on your machine.**
  Always. This syncs the arrangement, not the mix.

**The important part:** DAWBridge never deletes anything in your project. The
worst it does to *your* session is add things. All the risk sits on the shared
folder — which is exactly why the warnings are worth reading.

---

*DAWBridge v0.1.4 · Reaper and Pro Tools · Windows*
