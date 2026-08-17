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

from . import checks, localmedia, notify, reapersetup, syncstate, theme
from .backend import Backend
from .model import Session
from .store import SharedSessionMoved, SharedStore
from . import sync
from .sync import preview_push

_CONFIG_PATH = Path.home() / ".dawbridge_gui.json"


def _unavailable(backend, daw: str) -> str | None:
    """Why this DAW can't be reached, or None if it can.

    Probes once: `is_available()` on a closed Pro Tools costs seconds, and
    asking twice would double it. `getattr` because the test doubles are
    duck-typed rather than Backend subclasses and define only
    `is_available` - a bare call would AttributeError in the suite while
    working perfectly against a real backend.
    """
    probe = getattr(backend, "unavailable_reason", None)
    if probe is not None:
        return probe()
    return None if backend.is_available() else (
        f"{daw} doesn't look reachable right now - is it open and is "
        f"scripting enabled?"
    )


def _get_backend(daw: str) -> Backend:
    if daw == "reaper":
        from .reaper_backend import ReaperBackend

        return ReaperBackend()
    from .protools_backend import ProToolsBackend

    return ProToolsBackend()


def display_name(name: str, daw: str) -> str:
    """Who to credit for a sync.

    Falls back to the old `gui@reaper` form when nobody has entered a
    name, so an existing install keeps working and the field is genuinely
    optional. Kept module-level and pure because this string ends up in
    the session, in every history listing and in the Discord message -
    three places where getting it wrong is visible for a long time.
    """
    clean = " ".join(name.split())[:40]
    return f"{clean}@{daw}" if clean else f"gui@{daw}"


def short_name(name: str) -> str:
    """Just the person, for a message that already says which DAW."""
    return " ".join(name.split())[:40] or "Someone"


def _worth_restoring(path: Path) -> bool:
    """False for the empty session a new shared folder starts life with.

    An unreadable revision counts as worth showing: better a row saying
    it can't be read than a version silently missing from the history.
    """
    try:
        session = Session.load(path)
    except Exception:
        return True
    return bool(session.tracks or session.updated_by)


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
        # Wide enough that the button row never clips. Measured after
        # every change, not guessed: the row needs 875px and the body pads
        # 16 each side. Anything narrower silently hides whichever button
        # is last, and the last ones are the recovery tools - reached for
        # exactly when someone is least able to go hunting.
        self.master.minsize(907, 580)
        self.grid(sticky="nsew")
        self.master.columnconfigure(0, weight=1)
        self.master.rowconfigure(0, weight=1)

        config = _load_config()
        self.folder_var = tk.StringVar(value=config.get("folder", ""))
        self.daw_var = tk.StringVar(value=config.get("daw", "reaper"))
        self.name_var = tk.StringVar(value=config.get("name", ""))

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
        # Switch and setup button share one cell, packed left. Given a
        # column each, the button's width would set column 2 and drag
        # "Browse..." away from the folder field above it - the same trap
        # the name hint below falls into.
        daw_row = ttk.Frame(body, style="Chassis.TFrame")
        daw_row.grid(row=1, column=1, columnspan=2, sticky="w", pady=6)
        switch = theme.Segmented(daw_row)
        switch.pack(side="left")
        for label, value in (("Reaper", "reaper"), ("Pro Tools", "protools")):
            switch.add(ttk.Radiobutton(
                switch, text=label, value=value, variable=self.daw_var,
                style="Selector.TRadiobutton", takefocus=True,
            ))
        # Next to the DAW switch rather than in the button row below,
        # because it is about reaching the DAW, not about the bridge -
        # and because that row is already at the width the window is
        # sized for.
        ttk.Button(daw_row, text="Set up Reaper...",
                   command=self._on_setup_reaper).pack(side="left", padx=(12, 0))

        ttk.Label(body, text=theme.tracked("Your name"), style="Legend.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14), pady=6
        )
        # Optional, but it replaces "gui@reaper" everywhere someone is
        # credited: the status pane, every history entry, the drift
        # warning, and the Discord message.
        # Entry and hint share one cell. Given a column of their own, the
        # hint's width sets column 2 and drags "Browse..." away from the
        # folder field above it.
        name_row = ttk.Frame(body, style="Chassis.TFrame")
        name_row.grid(row=2, column=1, columnspan=2, sticky="w", pady=6)
        ttk.Entry(name_row, textvariable=self.name_var, width=26).pack(side="left")
        tk.Label(name_row, text="shown to everyone on this project", background=theme.CHASSIS,
                 foreground=theme.INK_3, font=theme.FONTS["button"]).pack(
            side="left", padx=(12, 0))

        theme.separator(body).grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))

        button_frame = ttk.Frame(body, style="Chassis.TFrame")
        button_frame.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        # Left to right is the order you actually work in: look at what's
        # waiting, take it, then send yours back.
        self.preview_btn = ttk.Button(button_frame, text="Preview pull", command=self._on_preview)
        self.preview_btn.pack(side="left")
        self.load_btn = ttk.Button(button_frame, text="Pull from Bridge", command=self._on_load)
        self.load_btn.pack(side="left", padx=(8, 0))
        self.publish_btn = ttk.Button(button_frame, text="Push to Bridge", command=self._on_publish)
        self.publish_btn.pack(side="left", padx=(8, 0))
        # The groove marks the divide: everything left of it changes
        # something, everything right of it only looks.
        theme.separator(button_frame, orient="vertical").pack(side="left", fill="y", padx=14)
        self.refresh_btn = ttk.Button(button_frame, text="Refresh status", command=self._refresh_status)
        self.refresh_btn.pack(side="left")
        # Both sit right of the groove: they only look. Restoring is a
        # change, so it lives behind the History window and asks first.
        self.check_btn = ttk.Button(button_frame, text="Check folder", command=self._on_check)
        self.check_btn.pack(side="left", padx=(8, 0))
        self.history_btn = ttk.Button(button_frame, text="History...", command=self._on_history)
        self.history_btn.pack(side="left", padx=(8, 0))
        self.notify_btn = ttk.Button(button_frame, text="Notifications...",
                                     command=self._on_notifications)
        self.notify_btn.pack(side="left", padx=(8, 0))

        theme.separator(body).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(16, 0))

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
        self.preview_btn.configure(state=state)
        self.load_btn.configure(state=state)
        self.publish_btn.configure(state=state)
        self.refresh_btn.configure(state=state)
        self.check_btn.configure(state=state)
        self.history_btn.configure(state=state)
        self.notify_btn.configure(state=state)

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

        _save_config({"folder": str(folder), "daw": self.daw_var.get(),
                      "name": self.name_var.get().strip()})

    def _on_publish(self) -> None:
        self._run_async(self._do_publish)

    def _on_preview(self) -> None:
        self._run_async(self._do_preview)

    def _on_load(self) -> None:
        self._run_async(self._do_load)

    def _on_notifications(self) -> None:
        """Set up the Discord webhook without a terminal.

        The CLI could already do this, which meant the collaborator
        running the .exe couldn't - the same gap that made the 20-revision
        archive useless to the person most likely to need it.
        """
        folder = self._require_folder()
        if folder is None:
            return

        win = tk.Toplevel(self.master)
        win.title("DAWBridge - notifications")
        win.configure(background=theme.CHASSIS)
        win.transient(self.master)
        win.resizable(False, False)

        body = ttk.Frame(win, style="Chassis.TFrame", padding=(16, 14, 16, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, wraplength=520, justify="left", text=(
            "Post a message to a Discord channel whenever anyone publishes "
            "or loads, so nobody has to remember to say so.\n\n"
            "In Discord: Server Settings > Integrations > Webhooks > New Webhook. "
            "Pick a channel, copy the URL, paste it here."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(body, text=theme.tracked("Webhook URL"), style="Legend.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=6)
        url_var = tk.StringVar(value=notify.webhook_url(folder) or "")
        entry = ttk.Entry(body, textvariable=url_var, width=52)
        entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=6)

        ttk.Label(body, text=theme.tracked("Post on"), style="Legend.TLabel").grid(
            row=2, column=0, sticky="w", padx=(0, 14), pady=6)
        events_var = tk.StringVar(
            value="publish" if (notify.is_enabled_for(folder, "publish")
                                and not notify.is_enabled_for(folder, "load")) else "both")
        switch = theme.Segmented(body)
        switch.grid(row=2, column=1, columnspan=2, sticky="w", pady=6)
        for label, value in (("Both", "both"), ("Publishes only", "publish")):
            switch.add(ttk.Radiobutton(switch, text=label, value=value, variable=events_var,
                                       style="Selector.TRadiobutton", takefocus=True))

        # Feedback reports state, so it belongs on a readout, not the faceplate.
        bezel = theme.Recess(body)
        bezel.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        status = tk.Label(bezel.well, background=theme.DISPLAY, foreground=theme.READOUT_INK,
                          font=theme.FONTS["mono_small"], anchor="w", justify="left",
                          wraplength=520, padx=10, pady=8)
        bezel.mount(status)

        def say(text: str, colour: str = theme.READOUT_INK) -> None:
            status.configure(text=text, foreground=colour)

        say("On. Everyone on this project posts here." if notify.webhook_url(folder)
            else "Not set up yet.",
            theme.READOUT_GOOD if notify.webhook_url(folder) else theme.READOUT_INK)

        def save(and_test: bool = False) -> None:
            url = url_var.get().strip()
            if not url:
                say("Paste a webhook URL first.", theme.READOUT_WARN)
                return
            reason = notify.reject_reason(url)
            if reason:
                say(reason, theme.READOUT_CRIT)
                return
            config = notify.load_config(folder)
            config["discord_webhook"] = url
            config["events"] = ["publish", "load"] if events_var.get() == "both" else ["publish"]
            try:
                notify.save_config(folder, config)
            except Exception as exc:  # noqa: BLE001 - surfaced, never raised at the user
                say(f"Could not save: {exc}", theme.READOUT_CRIT)
                return
            self._log(f"[notify] saved to {notify.config_path(folder)}")
            if not and_test:
                say("Saved. This lives in the shared folder, so your collaborator's "
                    "copy will post here too.", theme.READOUT_GOOD)
                return
            problem = notify.post(folder, "DAWBridge test message - notifications are working.")
            if problem:
                say(problem, theme.READOUT_CRIT)
            else:
                say("Test message sent - check the channel.", theme.READOUT_GOOD)

        def turn_off() -> None:
            """Off for the project, not just this machine.

            The webhook belongs to the shared folder, so there is one
            switch and it is honest about its reach - the confirm says so
            rather than leaving someone to discover it.
            """
            if not messagebox.askyesno(
                "DAWBridge - turn off notifications",
                "Turn notifications off for this project?\n\n"
                "The webhook is shared, so nobody on this project will get messages "
                "until it's set up again.",
                parent=win,
            ):
                return
            try:
                notify.config_path(folder).unlink(missing_ok=True)
            except Exception as exc:  # noqa: BLE001
                say(f"Could not turn off: {exc}", theme.READOUT_CRIT)
                return
            url_var.set("")
            say("Turned off for this project.", theme.READOUT_INK)
            self._log("[notify] notifications turned off for this project.")

        row = ttk.Frame(body, style="Chassis.TFrame")
        row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(row, text="Save", command=save).pack(side="left")
        ttk.Button(row, text="Save and send test",
                   command=lambda: save(and_test=True)).pack(side="left", padx=(8, 0))
        theme.separator(row, orient="vertical").pack(side="left", fill="y", padx=14)
        ttk.Button(row, text="Turn off", command=turn_off).pack(side="left")
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=(8, 0))
        entry.focus_set()

    def _on_setup_reaper(self) -> None:
        """Teach Reaper to talk to DAWBridge, without anyone installing Python.

        This replaces the single worst step in the whole product: a
        collaborator got DAWBridge working and reported that installing
        the right Python to get there was miserable. See
        reapersetup for what was actually required and why bundling
        reapy into the .exe never fixed it.

        Deliberately its own window rather than a chain of message boxes.
        Setup can only run with Reaper closed, and that condition needs
        somewhere to be stated, checked, and re-checked after the person
        goes and closes it - three message boxes in a row would just be
        clicked through.
        """
        win = tk.Toplevel(self.master)
        win.title("DAWBridge - set up Reaper")
        win.configure(background=theme.CHASSIS)
        win.transient(self.master)
        win.resizable(False, False)

        body = ttk.Frame(win, style="Chassis.TFrame", padding=(16, 14, 16, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)

        ttk.Label(body, wraplength=560, justify="left", text=(
            "Reaper needs a one-time setup before DAWBridge can talk to it. "
            "Pro Tools doesn't - if that's your DAW, you can close this.\n\n"
            "DAWBridge carries its own copy of Python for Reaper to use, so "
            "there is nothing for you to download or install. Nothing is added "
            "to your system and no Python already on this machine is touched.\n\n"
            "Reaper must be CLOSED. It rewrites its settings when it quits, "
            "which would undo this."
        )).grid(row=0, column=0, sticky="w", pady=(0, 12))

        bezel = theme.Recess(body)
        bezel.grid(row=1, column=0, sticky="ew")
        status = tk.Label(bezel.well, background=theme.DISPLAY, foreground=theme.READOUT_INK,
                          font=theme.FONTS["mono_small"], anchor="w", justify="left",
                          wraplength=560, padx=10, pady=8)
        bezel.mount(status)

        def say(text: str, colour: str = theme.READOUT_INK) -> None:
            status.configure(text=text, foreground=colour)

        def check() -> None:
            resource = reapersetup.resource_dir()
            lines = reapersetup.describe_state(resource, reapersetup.runtime_root())
            running = reapersetup.reaper_is_running()
            if running:
                lines.append("Reaper is OPEN - close it before setting up.")
            elif running is None:
                lines.append("Couldn't tell whether Reaper is open; make sure it's closed.")
            else:
                lines.append("Reaper is closed. Ready.")
            say("\n".join(lines),
                theme.READOUT_WARN if running else theme.READOUT_INK)

        def run_setup() -> None:
            # Re-checked here, not just displayed above: the person has
            # had the dialog open while going to close Reaper, so the
            # reading from a moment ago is exactly the stale one.
            if reapersetup.reaper_is_running():
                say("Reaper is still open. Close it, then press Set up Reaper again.",
                    theme.READOUT_WARN)
                return

            resource = reapersetup.resource_dir()
            if resource is None:
                say("Couldn't find Reaper's settings folder. Is Reaper installed for this "
                    "user account? A portable Reaper install has to be set up by hand.",
                    theme.READOUT_CRIT)
                return

            archive = theme.asset_path(reapersetup.RUNTIME_ASSET)
            if archive is None:
                say("This build of DAWBridge doesn't include the Reaper runtime, so it "
                    "can't set Reaper up on its own. Please report this.", theme.READOUT_CRIT)
                return

            say("Working...")
            win.update_idletasks()
            try:
                runtime = reapersetup.runtime_root()
                dll = reapersetup.unpack_runtime(Path(archive), runtime)
                steps = reapersetup.configure(resource, dll,
                                              reapersetup.server_script(runtime))
            except Exception as exc:  # noqa: BLE001 - surfaced, never raised at the user
                say(f"Setup failed: {exc}", theme.READOUT_CRIT)
                self._log(f"[setup] failed: {exc}")
                return

            for step in steps:
                self._log(f"[setup] {step}")
            say("Done. Start Reaper now - it will pick this up when it opens. "
                "Then come back and press Refresh status.", theme.READOUT_GOOD)

        row = ttk.Frame(body, style="Chassis.TFrame")
        row.grid(row=2, column=0, sticky="ew", pady=(14, 0))
        ttk.Button(row, text="Set up Reaper", command=run_setup).pack(side="left")
        ttk.Button(row, text="Check again", command=check).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=(8, 0))

        check()

    def _describe_plan(self, plan) -> list[str]:
        """What this publish is about to do to the shared session."""
        lines = [f"[push] publishing will: {plan.summary_line()}"]
        for name in plan.removed:
            lines.append(f"    REMOVE  {name} - you deleted it")
        for name in plan.kept_theirs:
            lines.append(f"    KEEP    {name} - someone else's, left untouched")
        for name in plan.recoloured:
            lines.append(f"    COLOUR  {name} - recoloured")
        for name in plan.conflicts:
            lines.append(f"[push][warning] you and someone else both changed {name!r}; "
                         f"yours is being published over theirs")
        for w in plan.warnings:
            lines.append(f"[push][warning] {w}")
        return lines

    def _confirm_publish(self, plan, daw: str) -> bool:
        body = [f"Publishing from {daw} will:", "", plan.summary_line(), ""]
        if plan.removed:
            body.append("Removing (you deleted these): " + ", ".join(plan.removed))
        if plan.conflicts:
            body.append("Someone else also changed these, and yours will replace theirs: "
                        + ", ".join(plan.conflicts))
        body += ["", "The shared folder keeps the last 20 versions, so this is "
                 "recoverable from History...", "", "Go ahead?"]
        return self._ask_on_main_thread(
            lambda: messagebox.askyesno("DAWBridge - confirm publish", "\n".join(body)))

    def _notify(self, folder: Path, event: str, message: str) -> None:
        """Tell Discord, if it's set up. Runs on the worker thread.

        Always logs the outcome. A notification that fails silently is
        worse than none at all, because the other person is then relying
        on a message that never arrived.
        """
        if not notify.is_enabled_for(folder, event):
            return
        problem = notify.post(folder, message)
        line = f"[notify] {problem}" if problem else "[notify] posted to Discord"
        self.master.after(0, lambda: self._log(line))

    def _on_check(self) -> None:
        self._run_async(self._do_check)

    def _do_check(self, folder: Path) -> None:
        """"Check folder" - the GUI half of `dawbridge doctor`.

        Runs on the worker thread: it stats every referenced audio file,
        and on a Dropbox folder that is not instant.
        """
        store = SharedStore(folder)
        if not store.session_path.exists():
            self.master.after(0, lambda: self._log(
                "[check] nothing has been published to this folder yet."))
            return

        problems, orphans = checks.check_folder(store)
        lines = [f"[check] {len(problems)} problem(s) found."
                 if problems else "[check] no problems found."]
        for problem in problems:
            lines += [f"    {line}" for line in str(problem).splitlines()]
        if orphans:
            mb = sum(size for _n, size in orphans) / 1e6
            lines.append(f"[check] {len(orphans)} audio file(s) referenced by nothing "
                         f"({mb:.0f} MB) - housekeeping, not a fault.")
        for line in lines:
            self.master.after(0, lambda line=line: self._log(line))

    def _on_history(self) -> None:
        """Past revisions, and a way back to one.

        The archive keeps 20 revisions, but until now reaching them meant
        a terminal and a Python install - which the person most likely to
        need them, the collaborator running the .exe, does not have. A
        safety net nobody can reach is not a safety net.
        """
        folder = self._require_folder()
        if folder is None:
            return
        store = SharedStore(folder)
        try:
            current = store.load()
            archived = store.list_archive()
        except Exception as exc:
            self._log(f"[history] could not read the shared folder: {exc}")
            return
        # Drop the placeholder `ensure_layout` writes when a folder is
        # first created: no tracks, no author, nothing anyone published.
        # Offering it as a restore target gives a worried person a way to
        # empty the shared session with two clicks, for no benefit.
        archived = [(rev, path) for rev, path in archived if _worth_restoring(path)]
        if not archived:
            self._log("[history] nothing archived yet - the archive fills up as people publish.")
            return

        win = tk.Toplevel(self.master)
        win.title("DAWBridge - history")
        win.configure(background=theme.CHASSIS)
        win.transient(self.master)
        win.minsize(560, 320)

        ttk.Label(win, text=theme.tracked("Past revisions"),
                  style="Legend.TLabel").pack(anchor="w", padx=14, pady=(14, 6))

        bezel = theme.Recess(win)
        bezel.pack(fill="both", expand=True, padx=14)
        listbox = tk.Listbox(
            bezel.well, background=theme.DISPLAY, foreground=theme.READOUT_INK,
            selectbackground=theme.READOUT_SELECT, selectforeground=theme.READOUT_BRIGHT,
            font=theme.FONTS["mono_small"], borderwidth=0, highlightthickness=0,
            activestyle="none",
        )
        listbox.pack(fill="both", expand=True, padx=6, pady=6)
        for revision, path in archived:
            listbox.insert("end", checks.describe_revision(revision, path))
        listbox.selection_set(0)

        note = ttk.Label(
            win, wraplength=520, justify="left",
            text=(f"The shared session is at r{current.revision}. Restoring publishes an "
                  f"older version as a new revision - it does not rewind, so your "
                  f"everyone else sees the change, and what it replaces is archived too."),
        )
        note.pack(anchor="w", padx=14, pady=(10, 0))

        row = ttk.Frame(win, style="Chassis.TFrame")
        row.pack(fill="x", padx=14, pady=14)
        ttk.Button(row, text="Restore selected",
                   command=lambda: self._restore_selected(win, store, archived, listbox)
                   ).pack(side="left")
        ttk.Button(row, text="Close", command=win.destroy).pack(side="left", padx=(8, 0))

    def _restore_selected(self, win: tk.Toplevel, store, archived, listbox) -> None:
        selection = listbox.curselection()
        if not selection:
            return
        revision, path = archived[selection[0]]
        try:
            candidate = Session.load(path)
        except Exception as exc:
            messagebox.showerror("DAWBridge", f"Could not read r{revision}: {exc}")
            return

        if not messagebox.askyesno(
            "DAWBridge - restore",
            f"Restore r{revision}?\n\n"
            f"{len(candidate.tracks)} track(s), published by {candidate.updated_by or '?'}.\n\n"
            f"This replaces what's currently in the shared folder. It becomes a new "
            f"revision, and the version it replaces is archived, so this is undoable.\n\n"
            f"It does not change your DAW - use Pull from Bridge afterwards.",
            parent=win,
        ):
            return

        try:
            restored = store.restore_archived(
                revision, updated_by=display_name(self.name_var.get(), "restore"))
        except Exception as exc:
            messagebox.showerror("DAWBridge", f"Restore failed: {exc}", parent=win)
            return
        self._log(f"[history] restored r{revision} as r{restored.revision} "
                  f"({len(restored.tracks)} track(s)). Use Pull from Bridge to get it "
                  f"into your DAW.")
        win.destroy()
        self._refresh_status()

    def _describe_preview(self, preview, session, folder: Path, daw: str) -> list[str]:
        """The preview as log lines - same content the CLI prints."""
        lines = [
            f"[preview] shared session r{session.revision}, last updated by {session.updated_by}",
        ]
        drift = syncstate.describe_drift(folder, daw, session.revision)
        if drift:
            lines.append(f"[preview] {drift}")
        lines.append(f"[preview] pulling into {daw} would: {preview.summary_line()}")

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
        for mc in preview.marker_changes:
            at = lambda s: "?" if s is None else f"{s:.3f}s"  # noqa: E731 - see cli._at
            if mc.kind == "add":
                lines.append(f"    ADD     marker {mc.name} @ {at(mc.to_time)}")
            elif mc.kind == "move":
                lines.append(f"    MOVE    marker {mc.name}: {at(mc.from_time)} -> {at(mc.to_time)}")
            elif mc.kind == "rename":
                lines.append(f"    RENAME  marker {mc.name}"
                             + (f" - {mc.detail}" if mc.detail else ""))
            else:
                lines.append(f"    ORPHAN  marker {mc.name} @ {at(mc.from_time)} - left alone")

        for cc in preview.colour_changes:
            lines.append(f"    COLOUR  {cc.track_name}: "
                         f"{cc.from_colour or 'none'} -> {cc.to_colour}")

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

        reason = _unavailable(backend, daw)
        if reason is not None:
            self.master.after(0, lambda: self._log(f"[preview] {reason}"))
            return

        session = store.load()
        preview = preview_push(session, backend.read_live_state(), target=daw, store=store,
                                live_markers=backend.read_live_markers())
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

    def _do_publish(self, folder: Path) -> None:
        """The "Push to Bridge" button: this DAW becomes the shared session."""
        daw = self.daw_var.get()
        store = SharedStore(folder)
        store.ensure_layout()
        backend = _get_backend(daw)

        reason = _unavailable(backend, daw)
        if reason is not None:
            self.master.after(0, lambda: self._log(f"[pull] {reason}"))
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
            self.master.after(0, lambda: self._log(f"[push][warning] {project_change}"))
            if not self._ask_confirm_on_main_thread_generic(
                "DAWBridge - different project open",
                f"{project_change}.\n\nPublishing replaces the shared session with "
                f"THIS project's contents.\n\nPublish anyway?",
            ):
                self.master.after(0, lambda: self._log("[push] cancelled - shared session untouched."))
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
            self.master.after(0, lambda: self._log(f"[push][warning] {drift}"))
            if not self._ask_confirm_on_main_thread_generic(
                "DAWBridge - someone else published",
                f"{drift}.\n\nPublishing now replaces their changes with what's in your "
                f"{daw} right now. There is no merge.\n\nPublish anyway?",
            ):
                self.master.after(0, lambda: self._log("[push] cancelled - shared session untouched."))
                return

        loaded_revision = session.revision

        # Both snapshots must be taken BEFORE capture(): it reuses
        # canonical's Track objects and mutates them in place - that is how
        # a track keeps its clip history - so a list taken afterwards is
        # not a before-picture, and the comparison would conclude that
        # nothing ever changed.
        canonical_before = sync.snapshot_tracks(session)
        seen = syncstate.last_synced(folder, daw)
        baseline = sync.baseline_tracks_for(
            store, session, seen.get("revision") if seen else None)

        # Things a pull notices but can't act on - tracks this publish
        # removes from the shared session, offline media, a sample rate
        # being redefined. Previously discovered and dropped in silence.
        pull_warnings: list[str] = []
        session = backend.capture(session, store, pull_warnings)

        # A publish touches only the tracks you actually changed, so one
        # your partner edited while you merely had it open survives.
        plan = sync.plan_publish(canonical_before, session.tracks, baseline)
        session.tracks = plan.tracks
        for line in self._describe_plan(plan):
            self.master.after(0, lambda line=line: self._log(line))

        # Ask only when somebody's work is genuinely leaving or being
        # replaced. Confirming an ordinary publish trains people to click
        # through the dialog that matters.
        if (plan.removed or plan.conflicts) and not self._confirm_publish(plan, daw):
            self.master.after(0, lambda: self._log(
                "[push] cancelled - the shared session is untouched."))
            return
        # Reading a DAW takes seconds to minutes. Everything checked above
        # was checked before that read, so a partner publishing during it
        # would slip past every guard - this is the last chance to notice.
        try:
            store.save(session, updated_by=display_name(self.name_var.get(), daw),
                       expected_revision=loaded_revision)
        except SharedSessionMoved as exc:
            self.master.after(0, lambda: self._log(f"[push][error] {exc}"))
            self.master.after(0, lambda: self._log(
                "[push] nothing was written - your DAW and the shared session are both untouched."))
            self.master.after(0, lambda: messagebox.showwarning(
                "DAWBridge - someone published just now",
                f"{exc}.\n\nNothing was written. Load their changes first "
                f"(Pull from Bridge), then publish again."))
            return
        syncstate.record_sync(folder, daw, session.revision, "publish", project)
        self.master.after(
            0,
            lambda: self._log(
                f"[push] published {daw} -> shared session: {plan.summary_line()}. "
                f"{len(session.tracks)} track(s) total, revision {session.revision}."
            ),
        )
        for w in pull_warnings:
            self.master.after(0, lambda w=w: self._log(f"[push][warning] {w}"))

        self._notify(folder, "publish", notify.describe_publish(
            who=short_name(self.name_var.get()), daw=daw, revision=session.revision,
            tracks=len(session.tracks),
            clips=sum(len(t.clips) for t in session.tracks),
            warnings=len(pull_warnings),
            project=notify.project_name(folder)))

    def _do_load(self, folder: Path) -> None:
        """The "Pull from Bridge" button: the shared session lands in this DAW."""
        daw = self.daw_var.get()
        store = SharedStore(folder)
        store.ensure_layout()
        backend = _get_backend(daw)

        reason = _unavailable(backend, daw)
        if reason is not None:
            self.master.after(0, lambda: self._log(f"[pull] {reason}"))
            return

        session = store.load()

        # Show the diff and get an explicit yes before writing into a real
        # project. The confirm dialog has to run on the Tk main thread, so
        # hand it over and wait for the answer rather than calling it here.
        project = backend.project_identity()
        preview = preview_push(session, backend.read_live_state(), target=daw, store=store,
                                live_markers=backend.read_live_markers())
        for line in self._describe_preview(preview, session, folder, daw):
            self.master.after(0, lambda line=line: self._log(line))

        if preview.is_empty:
            self.master.after(0, lambda: self._log("[pull] nothing to do - DAW already matches."))
            # Record the project too: writing the breadcrumb without it used
            # to erase the project on file, and describe_project_change goes
            # quiet with nothing to compare against - so a single no-op push
            # switched the project-swap guard off for good.
            syncstate.record_sync(folder, daw, session.revision, "load", project)
            return

        if not self._ask_confirm_on_main_thread(preview, daw):
            self.master.after(0, lambda: self._log("[pull] cancelled - nothing was changed."))
            return

        self._offer_to_save_project(backend, folder)

        warnings = backend.apply(session, store)
        syncstate.record_sync(folder, daw, session.revision, "load", project)
        self.master.after(0, lambda: self._log(f"[pull] loaded session revision {session.revision} into {daw}."))
        for w in warnings:
            self.master.after(0, lambda w=w: self._log(f"[pull][warning] {w}"))

        self._notify(folder, "load", notify.describe_load(
            who=short_name(self.name_var.get()), daw=daw, revision=session.revision,
            project=notify.project_name(folder)))

    def _offer_to_save_project(self, backend, folder: Path) -> None:
        """Get the DAW's project onto disk before its audio arrives.

        The first pull of a project is usually into an empty, never-saved
        one - that is what "start collaborating" looks like. An unsaved
        project has no folder, and with no folder there is nowhere local
        to put the audio, so every clip would play from Dropbox forever
        (see localmedia for why that's worth avoiding). Asking here, once,
        is the difference between a project that owns its media and one
        that quietly depends on a cloud folder staying put.

        Only backends that can be unsaved are asked. Pro Tools sessions
        always have a path - you cannot create one without choosing where
        it goes - so this is a capability probe rather than a check for
        which DAW is selected.

        Every failure is a log line, never an exception: declining, a
        refused save, a read-only disk. `apply()` warns about the
        consequence, the pull still happens, and nothing is lost.
        """
        probe = getattr(backend, "project_file", None)
        saver = getattr(backend, "save_project_as", None)
        if probe is None or saver is None:
            return
        try:
            if probe():
                return  # already saved; its folder is the media home
        except Exception as exc:  # noqa: BLE001 - never block a pull over this
            self.master.after(0, lambda exc=exc: self._log(
                f"[pull] could not tell whether the project is saved ({exc}); continuing."))
            return

        suggested = localmedia.default_project_file(
            notify.project_name(folder),
            extension=getattr(backend, "project_extension", ".rpp"),
        )
        chosen = self._ask_on_main_thread(
            lambda: self._prompt_save_project(suggested), default=None)
        if not chosen:
            self.master.after(0, lambda: self._log(
                "[pull] project not saved - its audio will play from the shared folder."))
            return

        try:
            landed = saver(chosen)
        except Exception as exc:  # noqa: BLE001 - reported, never raised at the user
            self.master.after(0, lambda exc=exc: self._log(f"[pull][warning] {exc}"))
            return
        self.master.after(0, lambda: self._log(f"[pull] project saved to {landed}"))
        self.master.after(0, lambda: self._log(
            f"[pull] audio will be copied to {localmedia.describe_media_home(landed)}"))

    def _prompt_save_project(self, suggested: Path):
        """Ask where to save the project. Returns a path, or None to skip.

        Runs on the Tk main thread (see _ask_on_main_thread). Offers a
        filled-in default rather than only a file browser, because the
        person seeing this is mid-pull and does not want to make a
        filing decision - they want the thing to work. The browser is
        there for the one who does care.
        """
        result: dict = {}

        win = tk.Toplevel(self.master)
        win.title("DAWBridge - save this project first")
        win.configure(background=theme.CHASSIS)
        win.transient(self.master)
        win.resizable(False, False)
        win.grab_set()

        body = ttk.Frame(win, style="Chassis.TFrame", padding=(16, 14, 16, 16))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        ttk.Label(body, wraplength=600, justify="left", text=(
            "This project hasn't been saved yet.\n\n"
            "Save it now and DAWBridge keeps its own copy of the audio right "
            "beside it, so the project keeps working even if the shared folder "
            "moves or goes offline - and your DAW stops playing files out of a "
            "folder Dropbox is syncing underneath it.\n\n"
            "Skip this and the clips play straight from the shared folder."
        )).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(body, text=theme.tracked("Save to"), style="Legend.TLabel").grid(
            row=1, column=0, sticky="w", padx=(0, 14), pady=6)
        path_var = tk.StringVar(value=str(suggested))
        # Wide enough to show a default path whole. A clipped path is a
        # decision made blind - the point of offering one is that the
        # person can see where their project is about to go.
        entry = ttk.Entry(body, textvariable=path_var, width=62)
        entry.grid(row=1, column=1, sticky="ew", pady=6)

        def browse() -> None:
            picked = filedialog.asksaveasfilename(
                parent=win,
                title="Save the project as",
                initialfile=suggested.name,
                initialdir=str(suggested.parent),
                defaultextension=suggested.suffix,
                filetypes=[("Reaper project", f"*{suggested.suffix}"), ("All files", "*.*")],
            )
            if picked:
                path_var.set(picked)

        ttk.Button(body, text="Browse...", command=browse).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=6)

        def save() -> None:
            chosen = path_var.get().strip()
            if chosen:
                result["path"] = chosen
            win.destroy()

        def skip() -> None:
            win.destroy()

        row = ttk.Frame(body, style="Chassis.TFrame")
        row.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Button(row, text="Save project", command=save).pack(side="left")
        ttk.Button(row, text="Not now", command=skip).pack(side="left", padx=(8, 0))
        entry.focus_set()

        win.protocol("WM_DELETE_WINDOW", skip)
        self.master.wait_window(win)
        return result.get("path")

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

    def _ask_on_main_thread(self, question, default=False):
        answer: queue.Queue = queue.Queue(maxsize=1)

        def ask() -> None:
            result = default
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
        body = [f"Pulling into {daw} will:", "", preview.summary_line()]
        if preview.warnings:
            body += ["", f"{len(preview.warnings)} warning(s) - see the log for details."]
        body += ["", "Full details are in the log. Go ahead?"]
        return messagebox.askyesno("DAWBridge - confirm pull", "\n".join(body))


def main() -> int:
    root = tk.Tk()
    DawBridgeGUI(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
