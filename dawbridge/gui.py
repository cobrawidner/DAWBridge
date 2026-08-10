"""Simple desktop GUI wrapping the pull/push/status CLI, for end users who
shouldn't need a terminal: pick a shared folder, pick a DAW, click a
button. Runs the same `dawbridge.cli` logic underneath - this is just a
thinner, friendlier front end.

Pull/push happen in a background thread so the window doesn't freeze while
ReaScript/PTSL calls are in flight, which can take a few seconds.
"""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import syncstate, theme
from .backend import Backend
from .store import SharedSessionMoved, SharedStore
from .sync import preview_push

_CONFIG_PATH = Path.home() / ".dawbridge_gui.json"


def _get_backend(daw: str) -> Backend:
    if daw == "reaper":
        from .reaper_backend import ReaperBackend

        return ReaperBackend()
    from .protools_backend import ProToolsBackend

    return ProToolsBackend()


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(data: dict) -> None:
    try:
        _CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass  # remembering the last folder/DAW is a convenience, not critical


class DawBridgeGUI(ttk.Frame):
    def __init__(self, master: tk.Tk):
        theme.apply(master)  # must happen before any widget is drawn
        super().__init__(master, padding=0, style="Chassis.TFrame")
        self.master = master
        self.master.title("DAWBridge")
        # Silently keeps Tk's default icon if assets/dawbridge.ico has
        # not been generated yet - `python tools/build_exe.py --icon-only`.
        theme.apply_window_icon(self.master)
        self.master.minsize(660, 580)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        config = _load_config()
        self.folder_var = tk.StringVar(value=config.get("folder", ""))
        self.daw_var = tk.StringVar(value=config.get("daw", "reaper"))

        self._build_widgets()
        self._busy = False

        if self.folder_var.get():
            self.after_idle(self._show_folder_tail)
            self._refresh_status()

    def _build_widgets(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._build_header().grid(row=0, column=0, sticky="ew")
        theme.separator(self, background=theme.PANEL_HI).grid(row=1, column=0, sticky="ew")

        body = ttk.Frame(self, style="Chassis.TFrame", padding=(16, 14, 16, 16))
        body.grid(row=2, column=0, sticky="nsew")
        self._build_controls(body)
        self._build_readouts(body)

    def _build_header(self) -> ttk.Frame:
        """Mark, wordmark, and a recessed status lamp on the right.

        The lamp reports state, so it sits in a dark readout rather than
        on the faceplate - green on mid-grey is unreadable. The mark gets
        the same recessed treatment for a different reason: it brings its
        own dark ground, which is what keeps a saturated mark legible
        whatever the chassis is doing around it.
        """
        bar = ttk.Frame(self, style="Bar.TFrame", padding=(16, 9))
        bar.columnconfigure(1, weight=1)

        # Colourway is theme.COLOURWAY - one line, no changes here.
        badge = theme.Recess(bar)
        badge.grid(row=0, column=0, sticky="w", padx=(0, 11))
        badge.mount(theme.mark_canvas(badge.well, size=26))

        ttk.Label(bar, text="DAWBridge", style="Wordmark.TLabel").grid(row=0, column=1, sticky="w")

        capsule = theme.Recess(bar)
        capsule.grid(row=0, column=2, sticky="e")
        lamp_row = tk.Frame(capsule.well, background=theme.DISPLAY)
        capsule.mount(lamp_row)
        self.led = theme.led(lamp_row)
        self.led.pack(side="left", padx=(11, 8), pady=7)
        self.led_label = tk.Label(
            lamp_row,
            text=theme.tracked("Ready"),
            background=theme.DISPLAY,
            foreground=theme.READOUT_GOOD,
            font=theme.FONTS["legend"],
            width=13,
            anchor="w",
        )
        self.led_label.pack(side="left", padx=(0, 11))
        return bar

    def _build_controls(self, body: ttk.Frame) -> None:
        body.columnconfigure(1, weight=1)

        ttk.Label(body, text=theme.tracked("Shared folder"), style="Legend.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 14), pady=6
        )
        self.folder_entry = ttk.Entry(body, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew", padx=(0, 10), pady=6)
        ttk.Button(body, text="Browse...", command=self._browse_folder).grid(row=0, column=2, pady=6)

        ttk.Label(body, text=theme.tracked("DAW"), style="Legend.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=6
        )
        # A source-select switch rather than a pair of radios: with two
        # dark pips in two dark wells, the unselected one read as the
        # filled one. Which DAW is about to be written into is the one
        # thing here that must be unmistakable from across the room.
        switch = theme.Segmented(body)
        switch.grid(row=1, column=1, columnspan=2, sticky="w", pady=6)
        for label, value in (("Reaper", "reaper"), ("Pro Tools", "protools")):
            switch.add(ttk.Radiobutton(
                switch, text=label, value=value, variable=self.daw_var,
                style="Selector.TRadiobutton", takefocus=True,
            ))

        theme.separator(body).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))

        button_frame = ttk.Frame(body, style="Chassis.TFrame")
        button_frame.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.pull_btn = ttk.Button(button_frame, text="Pull from DAW", command=self._on_pull)
        self.pull_btn.pack(side="left")
        self.preview_btn = ttk.Button(button_frame, text="Preview push", command=self._on_preview)
        self.preview_btn.pack(side="left", padx=(8, 0))
        self.push_btn = ttk.Button(button_frame, text="Push to DAW", command=self._on_push)
        self.push_btn.pack(side="left", padx=(8, 0))
        # The groove marks the divide: everything left of it changes
        # something, everything right of it only looks.
        theme.separator(button_frame, orient="vertical").pack(side="left", fill="y", padx=14)
        self.refresh_btn = ttk.Button(button_frame, text="Refresh status", command=self._refresh_status)
        self.refresh_btn.pack(side="left")

        theme.separator(body).grid(row=4, column=0, columnspan=3, sticky="ew", pady=(16, 0))

    def _build_readouts(self, body: ttk.Frame) -> None:
        """Session status and the log, both sunk into dark readouts.

        The log starts two lines taller and the two panes then share
        height changes evenly, so it stays the larger of the two at any
        window size. Giving it a heavier grid weight instead looked
        right when the window grew and backfired when it shrank - a
        heavier row also gives up more, and at the minimum size the log
        ended up shorter than the status block above it.
        """
        self.status_text = self._readout(body, label="Session status", row=5, height=8)
        self.log_text = self._readout(body, label="Log", row=7, height=10)

        theme.placeholder(self.status_text, "no shared folder selected")
        self._log("[dawbridge] ready - choose the shared folder, then pick your DAW.")

    def _readout(self, body: ttk.Frame, label: str, row: int,
                 height: int) -> tk.Text:
        """A legend, a bezel, and the readout itself.

        Deliberately not `ScrolledText`: the scrollbar it builds is a
        classic `tk.Scrollbar`, which on Windows is a native control
        that ignores every colour you give it and painted a silver bar
        down the inside of the black readout. A `ttk.Scrollbar` is drawn
        by clam and takes the theme.
        """
        ttk.Label(body, text=theme.tracked(label), style="Legend.TLabel").grid(
            row=row, column=0, columnspan=3, sticky="w", pady=(14, 5)
        )
        bezel = theme.Recess(body)
        bezel.grid(row=row + 1, column=0, columnspan=3, sticky="nsew")

        pane = tk.Frame(bezel.well, background=theme.DISPLAY)
        bezel.mount(pane)
        pane.columnconfigure(0, weight=1)
        pane.rowconfigure(0, weight=1)

        text = tk.Text(pane, height=height, state="disabled")
        face = theme.configure_readout(text)
        text.grid(row=0, column=0, sticky="nsew")
        # Row 1 is left empty: it is the slack theme.snap_rows sizes so
        # the readout never shows half a line. The scrollbar spans both
        # rows so its trough still fills the well.
        bar = ttk.Scrollbar(pane, orient="vertical", style=theme.SCROLLBAR,
                            command=text.yview)
        bar.grid(row=0, column=1, rowspan=2, sticky="ns")
        text.configure(yscrollcommand=bar.set)

        # Ask for the pane's size in pixels and stop it tracking its
        # children. Both are snap_rows' preconditions: it needs the slack
        # not to travel back up the widget tree, and Tk's own row-to-pixel
        # arithmetic for a Text leaves out the leading, so `height` rows
        # never quite fit the space Tk reserves for them.
        pane.configure(width=text.winfo_reqwidth() + bar.winfo_reqwidth(),
                       height=theme.natural_height(height, text, face))
        pane.grid_propagate(False)
        theme.snap_rows(pane, text, face)

        body.rowconfigure(row + 1, weight=1)
        return text

    def _browse_folder(self) -> None:
        chosen = filedialog.askdirectory(title="Choose the shared DAWBridge folder")
        if chosen:
            self.folder_var.set(chosen)
            self._show_folder_tail()
            self._refresh_status()

    def _show_folder_tail(self) -> None:
        """Scroll the path field to its end.

        A shared folder lives several levels inside Dropbox, so the
        field showed `C:\\Users\\Travis\\Dropbox\\Conn...` and cut off the
        one part that says which project is about to be synced.

        Before the first layout pass the field is one pixel wide and
        scrolling it does nothing, so at startup this waits for the
        geometry manager to hand it a real width and then fires once.
        """
        if self.folder_entry.winfo_width() > 1:
            self.folder_entry.xview_moveto(1.0)
            return
        self.folder_entry.bind("<Configure>", self._folder_tail_once)

    def _folder_tail_once(self, event: tk.Event) -> None:
        event.widget.unbind("<Configure>")
        event.widget.xview_moveto(1.0)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.pull_btn.configure(state=state)
        self.preview_btn.configure(state=state)
        self.push_btn.configure(state=state)
        self.refresh_btn.configure(state=state)

        colour = theme.READOUT_WARN if busy else theme.READOUT_GOOD
        theme.set_led(self.led, colour)
        self.led_label.configure(
            text=theme.tracked("Working" if busy else "Ready"), foreground=colour
        )

    def _log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        start = self.log_text.index("end-1c")
        self.log_text.insert("end", message + "\n")
        theme.tag_log_line(self.log_text, start, message)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _require_folder(self) -> Path | None:
        folder = self.folder_var.get().strip()
        if not folder:
            messagebox.showwarning("DAWBridge", "Choose a shared folder first.")
            return None
        return Path(folder)

    def _refresh_status(self) -> None:
        folder = self._require_folder()
        if folder is None:
            return
        try:
            store = SharedStore(folder)
            store.ensure_layout()
            session = store.load()
        except Exception as exc:
            self._log(f"[status] failed to read shared folder: {exc}")
            return

        lines = [
            f"session {session.name!r} - revision {session.revision}",
            f"last updated by {session.updated_by} at {session.updated_at}",
            "",
        ]
        for track in sorted(session.tracks, key=lambda t: t.order):
            lines.append(f"  [{track.id}] {track.name} ({len(track.clips)} clip(s), {track.channels}ch)")
        if session.markers:
            lines.append("markers:")
            for m in session.markers:
                lines.append(f"  [{m.id}] {m.name} @ {m.time_seconds:.2f}s")

        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("1.0", "\n".join(lines))
        theme.tag_status(self.status_text)
        self.status_text.configure(state="disabled")

        _save_config({"folder": str(folder), "daw": self.daw_var.get()})

    def _on_pull(self) -> None:
        self._run_async(self._do_pull)

    def _on_preview(self) -> None:
        self._run_async(self._do_preview)

    def _on_push(self) -> None:
        self._run_async(self._do_push)

    def _describe_preview(self, preview, session, folder: Path, daw: str) -> list[str]:
        """The preview as log lines - same content the CLI prints."""
        lines = [
            f"[preview] shared session r{session.revision}, last updated by {session.updated_by}",
        ]
        drift = syncstate.describe_drift(folder, daw, session.revision)
        if drift:
            lines.append(f"[preview] {drift}")
        lines.append(f"[preview] pushing to {daw} would: {preview.summary_line()}")

        for tc in preview.track_changes:
            label = {"create": "CREATE", "rename": "RENAME", "mute": "MUTE", "unmute": "UNMUTE"}[tc.kind]
            lines.append(f"    {label:7s} {tc.name}" + (f" - {tc.detail}" if tc.detail else ""))
        for cc in preview.clip_changes:
            if cc.kind == "add":
                lines.append(f"    ADD     [{cc.track_name}] {cc.clip_name} @ {cc.to_start:.3f}s")
            elif cc.kind == "move":
                bits = []
                if cc.from_start is not None and abs(cc.from_start - cc.to_start) > 1e-9:
                    bits.append(f"{cc.from_start:.3f}s -> {cc.to_start:.3f}s")
                if cc.from_length is not None and abs(cc.from_length - cc.to_length) > 1e-9:
                    bits.append(f"length {cc.from_length:.3f}s -> {cc.to_length:.3f}s")
                lines.append(f"    MOVE    [{cc.track_name}] {cc.clip_name}: {', '.join(bits)}")
            elif cc.kind == "reaudio":
                lines.append(f"    AUDIO   [{cc.track_name}] {cc.clip_name}: different audio file")
            else:
                lines.append(f"    ORPHAN  [{cc.track_name}] {cc.clip_name} - left alone")
        if preview.untouched_tracks or preview.untouched_clips:
            lines.append(f"    unchanged: {preview.untouched_tracks} track(s), "
                         f"{preview.untouched_clips} clip(s)")
        for w in preview.warnings:
            lines.append(f"[preview][warning] {w}")
        return lines

    def _do_preview(self, folder: Path) -> None:
        daw = self.daw_var.get()
        store = SharedStore(folder)
        store.ensure_layout()
        backend = _get_backend(daw)

        if not backend.is_available():
            self.master.after(
                0, lambda: self._log(f"[preview] {daw} doesn't look reachable - is it open and scripting enabled?")
            )
            return

        session = store.load()
        preview = preview_push(session, backend.read_live_state(), target=daw, store=store)
        for line in self._describe_preview(preview, session, folder, daw):
            self.master.after(0, lambda line=line: self._log(line))

    def _run_async(self, fn) -> None:
        if self._busy:
            return
        folder = self._require_folder()
        if folder is None:
            return
        self._set_busy(True)
        thread = threading.Thread(target=self._worker, args=(fn, folder), daemon=True)
        thread.start()

    def _worker(self, fn, folder: Path) -> None:
        try:
            fn(folder)
        except Exception as exc:
            self.master.after(0, lambda: self._log(f"[error] {exc}"))
        finally:
            self.master.after(0, lambda: self._set_busy(False))
            self.master.after(0, self._refresh_status)

    def _do_pull(self, folder: Path) -> None:
        daw = self.daw_var.get()
        store = SharedStore(folder)
        store.ensure_layout()
        backend = _get_backend(daw)

        if not backend.is_available():
            self.master.after(
                0, lambda: self._log(f"[pull] {daw} doesn't look reachable - is it open and scripting enabled?")
            )
            return

        session = store.load()

        # Publishing replaces the shared session's tracks with this DAW's
        # state. There are two different ways that silently destroys work,
        # so there are two checks before it happens.

        # One: you're publishing from a different project than last time.
        # That isn't an edit, it's a swap - during development exactly this
        # doubled every track in the shared session with nothing in the
        # output to hint why.
        project = backend.project_identity()
        project_change = syncstate.describe_project_change(folder, daw, project)
        if project_change:
            self.master.after(0, lambda: self._log(f"[pull][warning] {project_change}"))
            if not self._ask_confirm_on_main_thread_generic(
                "DAWBridge - different project open",
                f"{project_change}.\n\nPublishing replaces the shared session with "
                f"THIS project's contents.\n\nPublish anyway?",
            ):
                self.master.after(0, lambda: self._log("[pull] cancelled - shared session untouched."))
                return

        # Two: someone else published since this machine last synced, and
        # they'd never see their work go. The `session.revision > 0` arm
        # covers the first five minutes: a machine that has never synced is
        # precisely the one most likely to publish an empty or wrong
        # project over a folder that already holds real work - a fresh
        # install on a collaborator's machine. Suppressing the guard for
        # never-synced machines protected nobody.
        drift = syncstate.describe_drift(folder, daw, session.revision)
        if drift and (syncstate.last_synced(folder, daw) is not None or session.revision > 0):
            self.master.after(0, lambda: self._log(f"[pull][warning] {drift}"))
            if not self._ask_confirm_on_main_thread_generic(
                "DAWBridge - someone else published",
                f"{drift}.\n\nPublishing now replaces their changes with what's in your "
                f"{daw} right now. There is no merge.\n\nPublish anyway?",
            ):
                self.master.after(0, lambda: self._log("[pull] cancelled - shared session untouched."))
                return

        before = len(session.tracks)
        loaded_revision = session.revision
        # Things a pull notices but can't act on - tracks this publish
        # removes from the shared session, offline media, a sample rate
        # being redefined. Previously discovered and dropped in silence.
        pull_warnings: list[str] = []
        session = backend.pull(session, store, pull_warnings)
        # Reading a DAW takes seconds to minutes. Everything checked above
        # was checked before that read, so a partner publishing during it
        # would slip past every guard - this is the last chance to notice.
        try:
            store.save(session, updated_by=f"gui@{daw}", expected_revision=loaded_revision)
        except SharedSessionMoved as exc:
            self.master.after(0, lambda: self._log(f"[pull][error] {exc}"))
            self.master.after(0, lambda: self._log(
                "[pull] nothing was written - your DAW and the shared session are both untouched."))
            self.master.after(0, lambda: messagebox.showwarning(
                "DAWBridge - someone published just now",
                f"{exc}.\n\nNothing was written. Load their changes first "
                f"(Push to DAW), then publish again."))
            return
        syncstate.record_sync(folder, daw, session.revision, "pull", project)
        added = len(session.tracks) - before
        self.master.after(
            0,
            lambda: self._log(
                f"[pull] pulled from {daw}: {added} new track(s) adopted, "
                f"{len(session.tracks)} total. Revision {session.revision}."
            ),
        )
        for w in pull_warnings:
            self.master.after(0, lambda w=w: self._log(f"[pull][warning] {w}"))

    def _do_push(self, folder: Path) -> None:
        daw = self.daw_var.get()
        store = SharedStore(folder)
        store.ensure_layout()
        backend = _get_backend(daw)

        if not backend.is_available():
            self.master.after(
                0, lambda: self._log(f"[push] {daw} doesn't look reachable - is it open and scripting enabled?")
            )
            return

        session = store.load()

        # Show the diff and get an explicit yes before writing into a real
        # project. The confirm dialog has to run on the Tk main thread, so
        # hand it over and wait for the answer rather than calling it here.
        project = backend.project_identity()
        preview = preview_push(session, backend.read_live_state(), target=daw, store=store)
        for line in self._describe_preview(preview, session, folder, daw):
            self.master.after(0, lambda line=line: self._log(line))

        if preview.is_empty:
            self.master.after(0, lambda: self._log("[push] nothing to do - DAW already matches."))
            # Record the project too: writing the breadcrumb without it used
            # to erase the project on file, and describe_project_change goes
            # quiet with nothing to compare against - so a single no-op push
            # switched the project-swap guard off for good.
            syncstate.record_sync(folder, daw, session.revision, "push", project)
            return

        if not self._ask_confirm_on_main_thread(preview, daw):
            self.master.after(0, lambda: self._log("[push] cancelled - nothing was changed."))
            return

        warnings = backend.push(session, store)
        syncstate.record_sync(folder, daw, session.revision, "push", project)
        self.master.after(0, lambda: self._log(f"[push] pushed session revision {session.revision} into {daw}."))
        for w in warnings:
            self.master.after(0, lambda w=w: self._log(f"[push][warning] {w}"))

    def _ask_confirm_on_main_thread(self, preview, daw: str) -> bool:
        """Ask for confirmation from a worker thread, safely.

        Tk dialogs must be built on the main thread, so the question is
        handed over with after() and the worker waits for the reply. The
        reply is posted in a `finally` because a dialog that raised
        (a destroyed window, a Tk error) would otherwise leave the worker
        blocked forever with every button greyed out - a hang with no
        error, which is the worst possible failure for a confirm step.
        Answering "no" on failure keeps the safe default: don't write.
        """
        return self._ask_on_main_thread(lambda: self._confirm_push(preview, daw))

    def _ask_confirm_on_main_thread_generic(self, title: str, message: str) -> bool:
        return self._ask_on_main_thread(lambda: messagebox.askyesno(title, message))

    def _ask_on_main_thread(self, question) -> bool:
        answer: queue.Queue = queue.Queue(maxsize=1)

        def ask() -> None:
            result = False
            try:
                result = question()
            except Exception as exc:  # noqa: BLE001 - surfaced to the log below
                self._log(f"[dawbridge] could not show the confirmation dialog: {exc}")
            finally:
                answer.put(result)

        self.master.after(0, ask)
        return answer.get()

    def _confirm_push(self, preview, daw: str) -> bool:
        """Runs on the Tk main thread (see _ask_confirm_on_main_thread)."""
        body = [f"Push into {daw} will:", "", preview.summary_line()]
        if preview.warnings:
            body += ["", f"{len(preview.warnings)} warning(s) - see the log for details."]
        body += ["", "Full details are in the log. Go ahead?"]
        return messagebox.askyesno("DAWBridge - confirm push", "\n".join(body))


def main() -> int:
    root = tk.Tk()
    DawBridgeGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
