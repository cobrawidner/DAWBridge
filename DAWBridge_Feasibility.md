# DAWBridge Feasibility: Reaper ↔ Pro Tools, Shared-Folder Sync

## Bottom line

**Update:** the PTSL discovery changes this assessment meaningfully for the better — see "PTSL changes the architecture" below. Short version: instead of trying to write raw `.ptx` binary files (essentially unsolved) or relying on a manual AAF import step, the Pro Tools side of the bridge can be a live agent that drives a *running* Pro Tools instance directly — creating tracks, importing audio, placing clips — via Avid's own scripting API. That removes the single biggest blocker in the original assessment.

What's still true regardless: "bidirectional" is a merge/version-control problem on top of a translation problem, and plugin/mix state doesn't cleanly cross the AAX/VST divide no matter which transport mechanism you use. Full, automatic, bidirectional sync of *everything* (arrangement + edits + automation + mix/routing) is still a real project, not a weekend script — but the Pro Tools half just got a lot more tractable.

## PTSL changes the architecture

Avid ships **PTSL (Pro Tools Scripting SDK/Library)** for free — a language-independent, gRPC-based API for scripting a *running* Pro Tools instance. It's official (from Avid's developer site) and actively maintained; there's a mature open-source Python wrapper ([py-ptsl](https://github.com/iluvcapra/py-ptsl), BSD-licensed, releases into 2026) and even an existing [MCP server built on top of it](https://github.com/skrul/protools-mcp-server) exposing session management, track control, clip management, markers, and transport control — worth studying as a reference implementation, possibly reusable as a starting point.

Confirmed capabilities relevant to this project: open/close/save a session, query full session and track state, create tracks (added 2023.9), import audio into a session (added 2023.6), create memory locations (markers), timeline selection, and cut/copy/paste actions. This means the Pro Tools side of the bridge doesn't need to write a `.ptx` file at all — a local agent, running alongside Pro Tools, can read the canonical shared-folder state and *drive Pro Tools directly* to create the equivalent tracks/clips/markers. The reverse direction works too: the agent can query Pro Tools' live state and push it back to the canonical representation for Reaper to pick up.

This is a good match on the Reaper side as well — Reaper has had **ReaScript** (Lua/Python/EEL2) as a live scripting API for years, so the architecture becomes symmetric: a local agent per machine, each driving its own DAW live via that DAW's native scripting interface, rather than either side serializing to a format the other has to parse.

**What's still unconfirmed**, and worth checking directly against Avid's current PTSL command reference (it's behind a click-through license at developer.avid.com) before committing to this path: whether automation curves are readable/writable via PTSL, and how precisely clips can be spotted to exact timeline positions via the API vs. relying on Pro Tools' Spot mode plus timeline-selection commands. If automation write isn't exposed yet, that scope item stays out of reach regardless of transport mechanism.

**What doesn't change:** PTSL requires Pro Tools to actually be open and running for a sync to happen — this fits a "sync triggers while both apps are open" workflow well, but it's not a headless batch process. And it doesn't touch the plugin/mix problem at all — AAX vs. VST plugin state still isn't portable; see below.

That said, a genuinely useful tool is buildable if you narrow scope. Details below, with a recommended phased path at the end.

## Why the two formats aren't symmetric

**Reaper `.rpp`** — plain text, documented (informally but thoroughly), stable across versions. Trivial to parse, generate, or patch programmatically. This side is not the hard part.

**Pro Tools `.ptx`** — proprietary binary, XOR-encrypted, undocumented by Avid, and it changes across versions. Reverse-engineered *readers* exist ([ptformat](https://github.com/zamaudio/ptformat), [protools-to-logic](https://github.com/jacobtodd/protools-to-logic)), tested mainly against PT 10–12 sessions. Nobody in the open-source community has a working *writer* — generating a valid `.ptx` from scratch means reproducing internal checksums and block structures that are only partially understood. Getting this wrong doesn't fail loudly; it risks silently corrupting your friend's session.

Practical implication: treat "generate a `.ptx`" as off the table for a DIY build. The two realistic substitutes are AAF (Pro Tools' native, documented interchange format — your friend imports it via "Import Session Data") or an existing commercial translator.

## The plugin/mix problem you already flagged

You're right to flag this — it's a second, independent blocker from the file-format one:

- Pro Tools plugins are AAX. Reaper plugins are VST/VST3/AU/CLAP. Even a perfectly translated session can only carry over a plugin instance if the *same plugin* exists in both formats on both machines (true for a lot of third-party plugins from vendors like Waves, FabFilter, Slate — false for any DAW-stock plugin, e.g. PT's EQ3/Dyn3 vs Reaper's ReaEQ/ReaComp, which have no equivalent).
- Even when the plugin exists on both sides, the saved parameter state ("chunk" data) is per-format and not portable — an AAX plugin's saved state doesn't load into its VST3 counterpart, even from the same vendor, with rare vendor-specific exceptions (some vendors offer cloud preset sync, but that's opt-in per plugin, not something a bridge tool can drive generically).
- Automation curves referencing those plugin parameters inherit the same problem — the target parameter may not exist on the other side.

There's no clean technical fix here. The realistic move is to *not* attempt automatic plugin-state transfer, and instead have the bridge record "what plugin was on this track with these settings" as human-readable metadata, so whoever's on the receiving end recreates it manually. If you want more plugins to survive the bridge, the practical lever is standardizing on plugins you both own in both AAX and VST/VST3 form.

## The bidirectional problem

Because you said true bidirectional: if both of you can edit the arrangement and the bridge regenerates files from a shared canonical state, that's a merge problem, not a translation problem. Nobody has built 3-way merge for DAW timelines. Even AATranslator, the mature commercial tool built specifically for Reaper↔Pro Tools conversion, describes each conversion as a one-way pass — bus routing doesn't translate, panning laws differ, "won't be a 1:1 translation" (their own docs). Doing that in a loop, in both directions, without a human checking each round trip, is a good way to lose work silently.

A workable bidirectional model needs one of:
1. **Track/region-level locking** — whoever's actively editing a track "checks it out" so the other side can't clobber it, or
2. **Last-write-wins at the file level with a diff/review step** — the bridge shows what changed before regenerating the target session, so a human catches problems, or
3. **A canonical intermediate representation** (e.g. JSON describing tracks/regions/automation) that both `.rpp` and the AAF-for-PT are generated *from*, with edits only ever applied to the canonical copy, never inferred by diffing the DAW-native files themselves.

Option 3 is the soundest architecture but the most work up front.

## Architecture options, ranked by feasibility

**D. Canonical intermediate format + live agents on both sides (PTSL + ReaScript) — new top recommendation**
The shared folder holds canonical state (JSON: tracks, regions/clips, markers, timeline positions, and audio file references) plus the audio files. On each machine, a small local agent watches that canonical state and, while its DAW is open, applies changes live: on Reaper via ReaScript, on Pro Tools via PTSL (using py-ptsl or similar). Same agent reads the DAW's current state back out and writes it to canonical state for the other side to consume. No file-format writing on either end, no manual import step, and no `.ptx` reverse-engineering. Main constraints: both DAWs need to be open for a sync to apply, and automation-curve support depends on confirming PTSL's current write capabilities.

**A. Canonical intermediate format + AAF for Pro Tools (fallback if PTSL automation support is too limited)**
The shared folder holds a canonical JSON/AAF representation of tracks, regions, automation, and audio file references — plus the audio files themselves. The bridge always *generates* `.rpp` fresh from canonical state (cheap, safe, native format). For Pro Tools, it generates an `.aaf`, and your friend runs Pro Tools' native "Import Session Data" (which supports track-level, selective import — genuinely useful for merging rather than clobbering). Downside: not push-button on the PT side, and mix/plugin state is out of scope by design (see above).

**B. Wrap AATranslator as the conversion engine**
Your bridge handles the shared folder, audio files, and change-detection; AATranslator ($249, PT-compatible tier, Windows) does the actual `.rpp ↔ .ptx` translation. Fastest path to something working, since it's mature and handles far more of the `.ptx` complexity than you'd want to reimplement. Still one-way per conversion — you'd be building the "what changed, which direction, don't clobber" logic around it yourself. Adds a paid, closed-source dependency you don't control.

**C. DIY direct `.ptx` read/write**
Not recommended. High effort, undocumented target, real risk of corrupting real sessions, and it breaks every time Avid changes the format.

## Recommendation

Don't start with "true bidirectional, everything syncs." Start with:

0. **Phase 0 (do this first, low effort)**: Confirm PTSL's actual current capabilities against Avid's live command reference — specifically automation read/write and clip-spotting precision. This one check determines whether Option D (live agents) can cover your full target scope or whether automation/mix has to be deferred regardless of architecture. Also worth a quick look at `skrul/protools-mcp-server`'s source to see how much of the PT-side agent is already solved.
1. **Phase 1**: One-way, manually-triggered push (Reaper → canonical JSON → live-driven Pro Tools via PTSL), covering timeline/regions/basic edits only. No automation, no mix/routing yet. This alone is useful and is buildable in a reasonable timeframe.
2. **Phase 2**: Add the reverse direction (Pro Tools live state → canonical → Reaper via ReaScript), and automation curve translation if Phase 0 confirms it's supported.
3. **Phase 3**: Revisit true bidirectional merge only once you've seen how you two actually work — in practice, a lot of "bidirectional" needs turn out to be "usually one direction, occasionally the other," which is far simpler than symmetric merge.
4. **Plugins/mix — scope decision (settled)**: each side owns and maintains their own effects/plugin chain locally; the bridge never touches inserts, sends, or plugin state, and never needs to translate them. The only requirement is that a sync doesn't *wipe* a track's existing chain. This actually simplifies the agent design: canonical state should key tracks by a stable ID/name, and both the PTSL agent and the ReaScript agent should update matched tracks in place (move/add/remove clips only) rather than recreating tracks — recreating a track would blow away whatever FX the local user built on it. New tracks (no local match) get created fresh with an empty chain, which the local user then fills in themselves and expects to persist untouched on every future sync.

## Open questions before Phase 1 starts

- Are you (and your friend) comfortable with both DAWs needing to be open/running for a sync to apply, given PTSL and ReaScript are both live-scripting APIs rather than file generators?
- What Pro Tools and Reaper versions are you both running (matters for PTSL version compatibility and ReaScript API availability)?
- Once Phase 0 confirms PTSL's automation support, do you want to proceed with the live-agent architecture (Option D), or do you want the AAF fallback (Option A) kept as a parallel path?
