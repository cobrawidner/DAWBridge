"""Reaper-side backend, driving a *running* Reaper instance live via
python-reapy (https://python-reapy.readthedocs.io/).

Validated against a real, running Reaper 7.69 driven through reapy's
remote API. Things that had been guessed here for a long time, and what
checking them actually showed:

  - **SetCurrentBPM is destructive.** It does not just change a number,
    it drags every beat-attached item, fade and marker to a new position.
    Measured: an item at 5.333s with a 0.333s fade became 4.000s / 0.250s
    when the tempo went 90 -> 120, and back on the way down - position,
    length and fades all scale by the tempo ratio. Canonical stores
    SECONDS, so pushing a tempo silently relocated whatever was already
    in the project and then placed the pushed clips at canonical's own
    positions, leaving the arrangement wrong against itself and
    publishing the damage on the next pull. The tempo is now only
    written into a project with no items in it (see sync.tempo_write_is_safe).
  - `SetTempoTimeSigMarker` DOES work - 4/4 @120 became 6/8 @90 - so the
    time signature is writable in principle. It is still not written,
    because it moves everything for exactly the same reason.
  - **String out-parameters do not survive reapy's remote API** unless
    the call takes an explicit buffer SIZE. `GetMediaSourceFileName(src,
    "", 4096)` works; `EnumProjectMarkers2`'s name field never gets
    filled in - it echoes back whatever buffer was passed, for
    EnumProjectMarkers/2/3 alike, and inside an inside_reaper() block
    too. reapy's own Marker class has no name property either. That is
    why marker names are read from the saved .rpp - see
    _parse_rpp_markers.
  - `EnumProjectMarkers2` returns 8 elements: (retval, proj, idx, isrgn,
    pos, rgnend, name, markrgnindexnumber). `CountProjectMarkers` returns
    (retval, proj, n_markers, n_regions). Both layouts _decode_marker_row
    knows about are kept, but this is the one that's real here.
  - Fades round-trip exactly: written 0.25/0.5, read back 0.25/0.5.
  - Markers are beat-attached, so a tempo change moves them too (12.5s
    became 16.667s on a 120 -> 90 change).
  - **`I_CUSTOMCOLOR` is a trap.** A track nobody has ever coloured still
    reads a non-zero value out of it - a brand new track gave `16576`
    (0x0040C0) - because only the `0x1000000` flag bit says whether a
    custom colour was actually set. Reading the number would have given
    every uncoloured track in the project an orange nobody chose, and
    published it. `GetTrackColor` answers 0 for "no custom colour" and
    `native | 0x1000000` otherwise, and `SetTrackColor` sets the flag for
    you, so those are what this backend uses. The packing underneath is
    BGR on Windows (pure red is 0x0000FF) but is per-platform, so
    `ColorToNative`/`ColorFromNative` do the conversion rather than any
    byte shifting here. Persists to the .rpp as `PEAKCOL <flagged int>`.

Still unverified: nothing in the clip push/pull path has been exercised
end to end against Reaper - only the marker, fade and tempo paths above.
The RPR_* calls elsewhere are long-stable parts of the ReaScript API used
in their documented forms, but reapy's argument conventions differ by
version, so treat those as strong rather than proven.

One-time setup (see README):
    pip install python-reapy
    python -c "import reapy; reapy.configure_reaper()"
    (then restart Reaper)
"""
from __future__ import annotations

import re
from pathlib import Path

from . import color, localmedia
from .backend import Backend, LiveClip, LiveMarker, LiveTrack, missing_client_library_reason
from .model import Clip, Marker, Session, Track, new_id
from .sync import (
    claim_live_id,
    resolve_identities,
    plan_markers,
    describe_dropped_tracks,
    describe_meter_mismatch,
    describe_publish_sample_rate_change,
    describe_sample_rate_mismatch,
    describe_tempo_map_flattening,
    describe_tempo_write_refusal,
    duplicate_adoption_warning,
    merge_pulled_track,
    missing_audio_warning,
    plan_clips,
    plan_tracks,
    renumber_tracks,
    should_reimport_audio,
    tempo_write_is_safe,
)
from .tagging import parse_tag, strip_tag, tag


#: EnumProjectMarkers2 fills out-parameters, and reapy's wrapper returns
#: them as a tuple. Which slot holds what depends on whether that version
#: prepends the C return value - this file's module docstring already
#: warns that reapy's buffer-style conventions differ by version, and
#: guessing wrong here would read a marker's position out of the field
#: holding its region end, i.e. put every marker at 0.
#: (retval, proj, idx, isrgn, pos, rgnend, name, markrgnindexnumber)
_MARKER_ROW_LAYOUTS = ((3, 4, 6, 7), (2, 3, 5, 6))


def _decode_marker_row(row) -> tuple[bool, float, str, int] | None:
    """(is_region, position_seconds, name, marker_index) from one
    EnumProjectMarkers2 row, or None if the row makes no sense.

    Picks the layout by checking the shape of what it finds rather than
    trusting the length alone: the name field must be a string and the
    position a number. A row that matches neither layout is skipped by
    the caller with a warning, because a misread marker is worse than a
    missing one.
    """
    for isrgn_at, pos_at, name_at, index_at in _MARKER_ROW_LAYOUTS:
        if len(row) <= index_at:
            continue
        name, position = row[name_at], row[pos_at]
        if isinstance(name, str) and isinstance(position, (int, float)) and not isinstance(position, bool):
            try:
                return bool(row[isrgn_at]), float(position), name, int(row[index_at])
            except (TypeError, ValueError):
                continue
    return None


#: One MARKER line from a saved .rpp:
#:     MARKER 1 16.66666666666667 "Verse #22222222" 0 0 1 R {GUID} 0 2
#: index, position, name, then the region flag. Reaper quotes a name with
#: whichever of " ' ` doesn't appear inside it.
_RPP_MARKER_RE = re.compile(
    r"""^MARKER \s+ (?P<index>\d+) \s+ (?P<position>[-+0-9.eE]+) \s+
        (?: "(?P<dq>[^"]*)" | '(?P<sq>[^']*)' | `(?P<bq>[^`]*)` | (?P<bare>\S+) )
        (?: \s+ (?P<isrgn>\d+) )? """,
    re.VERBOSE,
)


#: Reaper's per-object extended state key holding the bridge id. Chosen
#: because a musician never sees it: it survives a rename (confirmed live -
#: the id read back unchanged after the track was renamed), and it
#: serialises into the .rpp as `<EXT> dawbridge_id ... >` on a track and
#: `<EXTI> ... >` on an item (both confirmed in a saved scratch project,
#: on a track named plain `FadeProbe` with no tag in its name at all -
#: exactly the case this exists for).
#:
#: ASSUMED, NOT VERIFIED: that reapy reads these back after the project is
#: closed and reopened. The data is provably in the file and for RPP chunk
#: data those are the same thing in practice, but the reload has never been
#: run. Worth doing the first time a live Reaper is free.
#:
#: A SECOND PLACE TO LOOK, never the authority - see sync.resolve_identities.
_EXT_ID_KEY = "P_EXT:dawbridge_id"


def _read_ext_id(getset, native_id) -> str | None:
    """The bridge id stored on a track or item, or None.

    `getset` is RPR.GetSetMediaTrackInfo_String or its item equivalent.
    Verified live: an unset key returns "" (not the buffer that was passed
    in), so this is a real read rather than the echo that defeats
    EnumProjectMarkers2.
    """
    try:
        result = getset(native_id, _EXT_ID_KEY, "", False)
    except Exception:
        return None
    value = result[3] if isinstance(result, (list, tuple)) and len(result) > 3 else ""
    return value or None


def _write_ext_id(getset, native_id, bridge_id: str) -> None:
    """Store the bridge id where a rename can't reach it. Never fatal - the
    name is still the mechanism, this is only the net under it.
    """
    try:
        getset(native_id, _EXT_ID_KEY, bridge_id, True)
    except Exception:
        pass


def _read_track_colour(RPR, native_id) -> str | None:
    """A track's colour as "#RRGGBB", or None when it has no custom one.

    `GetTrackColor`, NOT `I_CUSTOMCOLOR`. Measured live on Reaper 7.78: a
    brand new track that nobody has ever coloured still reads `16576`
    (0x0040C0) out of `I_CUSTOMCOLOR`, because only the `0x1000000` flag
    bit distinguishes "the user picked this" from leftover default. Using
    the raw number would have published an orange nobody chose for every
    uncoloured track in the project - silent, wrong, and exactly the kind
    of damage this codebase keeps finding. `GetTrackColor` answers 0 for
    "no custom colour" and `native | 0x1000000` otherwise.

    `ColorFromNative` unpacks it because the packing is per-platform -
    Windows is BGR (measured: pure red is 0x0000FF) and macOS is not.
    """
    try:
        native = int(RPR.GetTrackColor(native_id))
    except Exception:
        return None
    if not native:
        return None
    try:
        _native, r, g, b = RPR.ColorFromNative(native & 0xFFFFFF, 0, 0, 0)
    except Exception:
        return None
    return color.to_hex(r, g, b)


def _write_track_colour(RPR, native_id, value) -> None:
    """Give a track canonical's colour. Reaper can show any of them
    exactly, so nothing is approximated on this side.

    A canonical colour of None writes nothing rather than clearing what
    the track already has: pushing into a DAW never takes anything away,
    and "the shared session doesn't know this track's colour" is not the
    same statement as "this track should have no colour".
    """
    rgb = color.rgb(value)
    if rgb is None:
        return
    try:
        RPR.SetTrackColor(native_id, RPR.ColorToNative(*rgb))
    except Exception:
        pass  # cosmetic; never worth failing a push that placed real audio


def _parse_rpp_markers(text: str) -> list[dict] | None:
    """Marker index/position/name from a saved Reaper project file, or
    None if this isn't a Reaper project at all.

    reapy cannot return a marker's name - the string out-parameter of
    EnumProjectMarkers2 is never populated over its remote API (confirmed
    live, and reapy's own Marker class has no name property either). The
    name carries the bridge tag, so without it there is no identity and
    every pull would hand the partner a fresh duplicate set.

    Reading it from the serialised project is the same move
    `_pull_clips_via_text_export` already makes on the Pro Tools side,
    for the same class of reason: the live API physically cannot answer
    and the serialised form can. Deliberately narrow - markers only.
    Positions, indices and everything else still come from the live API.

    Regions are excluded here as everywhere else: they carry isrgn=1 (as
    does the second line that marks a region's end) and the schema has
    nowhere to keep a region's end.
    """
    if "<REAPER_PROJECT" not in text:
        return None

    rows = []
    for line in text.splitlines():
        match = _RPP_MARKER_RE.match(line.strip())
        if not match:
            continue
        if (match.group("isrgn") or "0") != "0":
            continue
        name = next(
            (match.group(g) for g in ("dq", "sq", "bq", "bare") if match.group(g) is not None), ""
        )
        try:
            position = float(match.group("position"))
        except ValueError:
            continue
        rows.append({"index": int(match.group("index")), "position": position, "name": name})
    return rows


def _import_audio(store, source_path: str, warnings=None, clip_name: str = "", track_name: str = "") -> str:
    """Copy a take's source into the shared store, or "" if it isn't there.

    Reaper keeps items whose media has gone offline (a renamed folder, an
    external drive that isn't plugged in), and hashing a file that doesn't
    exist raised straight out of the middle of the pull - one offline item
    aborted the whole publish with a traceback and nothing was written.
    Returning "" instead lets the rest of the session publish, and the
    warning says which clip went out without audio; preview/push then
    refuse to place it rather than dropping a silent item on the
    timeline (sync.missing_audio_warning).
    """
    if not source_path:
        return ""
    path = Path(source_path)
    try:
        if path.is_file():
            return store.import_audio_file(path)
    except OSError:
        pass
    if warnings is not None:
        warnings.append(
            f"clip {clip_name!r} on track {track_name!r} plays {path.name!r}, which Reaper "
            f"can't find on disk - it was published with no audio, so your partner will be "
            f"told it's missing rather than getting a silent clip. Relink it in Reaper and "
            f"publish again"
        )
    return ""


def _take_source_path(RPR, take) -> str:
    """Absolute path of a take's source file, or "" if unavailable."""
    try:
        source = RPR.GetMediaItemTake_Source(take.id)
        return RPR.GetMediaSourceFileName(source, "", 4096)[1]
    except Exception:
        return ""


#: reapy's own ExtState read uses this timeout against the same port, so
#: matching it keeps the probe's answer and reapy's answer in step - a
#: probe that waited longer than reapy does would claim Reaper is there
#: in exactly the cases where reapy then can't reach it.
_WEB_INTERFACE_TIMEOUT_SECONDS = 0.5


#: Always the IPv4 literal, never the name "localhost". On Windows,
#: `localhost` resolves to ::1 (IPv6) FIRST, and Reaper's web interface
#: binds 0.0.0.0 - IPv4 only. Measured live: ::1:2307 takes 2.05s to
#: refuse, 127.0.0.1:2307 connects in 0.01s. That wasted round trip is
#: paid on every fresh connection, and reapy's `perform_action` opens one
#: with NO timeout at all, so on a machine where the IPv6 SYN is dropped
#: rather than refused it never returns - the app sits with every button
#: greyed and no error, which is exactly how this was found.
_LOOPBACK = "127.0.0.1"

#: A last-resort ceiling for reapy calls that pass no timeout of their
#: own (`WebInterface.perform_action`, `ExtState.__setitem__` - both plain
#: `urlopen(url)`). Talking to the DAW over IPv4 loopback either answers
#: immediately or fails immediately, so anything approaching this is
#: already broken. Generous enough to never fire in normal use; the point
#: is only that "frozen forever with no message" stops being reachable.
_REAPY_CALL_CEILING_SECONDS = 15


class _socket_timeout_floor:
    """Impose a default socket timeout on code that sets none.

    urllib falls back to `socket.getdefaulttimeout()` when a call passes
    no timeout, which is how a third-party library's untimed `urlopen`
    gets bounded without patching it. Restores the previous value, and
    never lowers a stricter one already in force.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self.previous = None

    def __enter__(self):
        import socket

        self.previous = socket.getdefaulttimeout()
        if self.previous is None or self.previous > self.seconds:
            socket.setdefaulttimeout(self.seconds)
        return self

    def __exit__(self, *exc):
        import socket

        socket.setdefaulttimeout(self.previous)
        return False


def use_ipv4_loopback(reapy) -> str | None:
    """Point reapy at 127.0.0.1 instead of "localhost". Returns a problem, or None.

    reapy hardcodes `host="localhost"` in both `Client` and
    `WebInterface`, and resolves it fresh on every connection. See
    _LOOPBACK for what that costs on Windows.

    `reapy.connect(host)` is reapy's own supported way to choose which
    machine to talk to - 127.0.0.1 is simply a host like any other - so
    this is a configuration call, not a patch of library internals.

    Idempotent: the selected host is read back first, so repeated calls
    cost nothing. That matters because `connect` reloads
    `reapy.reascript_api`, which is not free and would otherwise happen
    on every status refresh.

    One deliberate difference from reapy's own behaviour: `connect`
    *raises* DisabledDistAPIError for any host other than "localhost",
    where for localhost it only warns. Raising is the better answer and
    it is caught here, so a failure to connect returns a sentence instead
    of leaving a half-selected client behind.
    """
    try:
        from reapy.tools.network import machines
    except Exception as exc:  # noqa: BLE001 - an old reapy layout, not a crash
        return f"could not select IPv4 loopback for Reaper ({exc})"

    try:
        if machines.get_selected_machine_host() == _LOOPBACK:
            return None
    except Exception:
        pass  # no client selected yet, which is what we are about to fix

    try:
        with _socket_timeout_floor(_REAPY_CALL_CEILING_SECONDS):
            reapy.connect(_LOOPBACK)
    except Exception as exc:  # noqa: BLE001 - reported to the user, never raised
        return (f"Reaper is not answering on {_LOOPBACK}:"
                f"{getattr(getattr(reapy, 'config', None), 'WEB_INTERFACE_PORT', 2307)} ({exc})")
    return None


#: What the loopback probe found. Deliberately three states, not two:
#: "Reaper answered but its bridge server is not running" has to be
#: distinguishable from "nothing answered", because only the first one
#: is a trap - see bridge_server_state.
NO_ANSWER, NO_SERVER, READY = "no-answer", "no-server", "ready"


def bridge_server_state(port: int = 2307) -> str:
    """Ask Reaper directly, over plain HTTP, without importing reapy.

    **This must run before `import reapy`, and that ordering is the whole
    point of the function.** reapy connects at module-import time, and
    its `WebInterface.get_reapy_server_port` does this:

        except UndefinedExtStateError:
            self.activate_reapy_server()
            port = self.get_reapy_server_port()   # <- recurses

    with no bound and no base case. When Reaper is running but its bridge
    server never comes up, that recursion never terminates: each pass
    fires the activate action inside Reaper again and waits on a
    `urlopen` that passes no timeout. `import reapy` simply never
    returns, the GUI worker thread blocks forever with every button
    greyed, and there is no error anywhere. Observed exactly that -
    a stream of fresh sockets to port 2307 and zero I/O.

    No timeout DAWBridge sets can fix that, because it happens inside
    the import. The only reliable defence is to find out first, with
    urllib, and refuse to import reapy when the answer says it would
    hang. Read-only throughout: a GET of an ext state key, never
    `perform_action`, which would run something in the user's DAW just
    for asking whether it is there.
    """
    from urllib.request import urlopen

    url = f"http://{_LOOPBACK}:{port}/_/GET/EXTSTATE/reapy/server_port"
    try:
        with urlopen(url, timeout=_WEB_INTERFACE_TIMEOUT_SECONDS) as answer:
            body = answer.read().decode("utf-8", "replace")
    except Exception:
        return NO_ANSWER
    # Reaper answers with tab-separated fields, the value last. An
    # empty value means the key exists and holds nothing, which is
    # precisely the state that sends reapy into the loop above.
    return READY if body.rsplit(chr(9), 1)[-1].strip() else NO_SERVER


#: How long to wait for Reaper to bring its bridge server up. It starts
#: in well under a second when it works at all; this is only generous
#: enough that a busy machine is not called broken.
_ACTIVATION_SECONDS = 12


def start_bridge_server(port: int = 2307) -> str:
    """Ask Reaper to start reapy's bridge server. Returns the new state.

    **An empty `server_port` is the NORMAL state after every Reaper
    restart, not a fault.** reapy's design is that the first client to
    connect triggers the `activate_reapy_server` action, which starts the
    server and writes the port into ext state. Treating "no server yet"
    as fatal - which an earlier version of this file did - breaks every
    working setup on earth the moment Reaper is restarted, and reports it
    as "Reaper isn't set up". That is precisely backwards.

    What must NOT happen is reapy's version of this, which recurses
    without a base case and hangs the app forever (see
    bridge_server_state). So the same job is done here, bounded:

      - fire the action once, never in a loop,
      - do not depend on the HTTP response. Reaper often does not answer
        that request until the script it launched settles, and on a
        failed script it never answers at all. The response is not the
        signal; the ext state is.
      - poll for the port, and give up at a deadline.
    """
    import json
    import time
    from urllib.request import urlopen

    base = f"http://{_LOOPBACK}:{port}/_/"
    try:
        with urlopen(base + "GET/EXTSTATE/reapy/activate_reapy_server",
                     timeout=_WEB_INTERFACE_TIMEOUT_SECONDS) as answer:
            raw = answer.read().decode("utf-8", "replace")
        action = json.loads(raw.rsplit(chr(9), 1)[-1].strip())
    except Exception:
        return NO_ANSWER if bridge_server_state(port) == NO_ANSWER else NO_SERVER

    try:
        urlopen(base + str(action), timeout=_WEB_INTERFACE_TIMEOUT_SECONDS)
    except Exception:
        pass  # see above - the response is not the signal

    deadline = time.monotonic() + _ACTIVATION_SECONDS
    while time.monotonic() < deadline:
        state = bridge_server_state(port)
        if state != NO_SERVER:
            return state
        time.sleep(0.4)
    return NO_SERVER


def _reaper_web_interface_answers(reapy) -> bool:
    """Whether Reaper's ReaScript web interface responds on the loopback.

    This is the one thing observable from outside that means "Reaper is
    definitely running", which is what lets unavailable_reason separate
    a closed Reaper from a running one whose bridge is off.

    Two deliberate choices:
      - urllib, the same way reapy reaches that port, rather than a raw
        socket. If a proxy or firewall rule would break reapy's request,
        it breaks this one too, and the probe stays in agreement with
        the thing it is predicting.
      - a read-only GET of an ext state key, NOT reapy's
        `activate_reapy_server` sitting next to it, which performs an
        action inside Reaper. Asking "are you there?" must not also run
        something in the user's DAW.

    Any failure to get an answer is the same answer - False - because
    this is a probe rather than an operation: there is no partial
    success to preserve and nothing downstream that a distinction would
    change.
    """
    from urllib.request import urlopen

    port = getattr(getattr(reapy, "config", None), "WEB_INTERFACE_PORT", 2307)
    # _LOOPBACK, not "localhost" - see that constant. Using the name here
    # would spend the IPv6 timeout on every status refresh, and would
    # also make the probe disagree with a reapy that had been pointed at
    # IPv4, which is the one thing this probe must never do.
    url = f"http://{_LOOPBACK}:{port}/_/GET/EXTSTATE/reapy/server_port"
    try:
        with urlopen(url, timeout=_WEB_INTERFACE_TIMEOUT_SECONDS):
            return True
    except Exception:
        return False


def _reaper_web_interface_answers_over_name(reapy) -> bool:
    """The same probe, but by name - used ONLY to explain a failure.

    Never to decide that Reaper is reachable: everything else talks to
    the numeric address, so a "yes" here would promise a connection the
    rest of the code cannot make. It exists so that "reachable by name,
    not by address" can be reported as the local-network oddity it is,
    instead of being misreported as Reaper being closed.
    """
    from urllib.request import urlopen

    port = getattr(getattr(reapy, "config", None), "WEB_INTERFACE_PORT", 2307)
    try:
        with urlopen(f"http://localhost:{port}/_/GET/EXTSTATE/reapy/server_port",
                     timeout=_WEB_INTERFACE_TIMEOUT_SECONDS):
            return True
    except Exception:
        return False


class ReaperBackend(Backend):
    name = "reaper"

    #: What a saved project is called here, for callers offering a
    #: filename. Pro Tools has no equivalent because a Pro Tools session
    #: cannot exist unsaved.
    project_extension = ".rpp"

    def is_available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Which of the three unavailable cases this actually is - see
        Backend.unavailable_reason.

        Only two of the three are distinguishable here, and the third
        sentence says so rather than guessing:

          - `import reapy` raises -> the library isn't installed. Clean.
          - Reaper's web interface answers on 2307, but reapy still
            can't reach its own server -> Reaper IS running and the
            bridge is the problem. Clean, because only a running Reaper
            can answer that port.
          - the web interface doesn't answer -> Reaper is closed, OR it
            is open and the one-time reapy setup was never done in it
            (that setup is what creates the web interface in the first
            place). Nothing reapy exposes separates those two, and this
            method will not pretend otherwise: `dist_api_is_enabled()`
            is False for both.

        Note the reconnect. `dist_api_is_enabled()` reports a decision
        reapy made once, when it was first imported: start Reaper after
        DAWBridge and it stays False forever, which would have this
        method confidently reporting a bridge failure that isn't real.
        The retry only runs when the web interface has already answered,
        so it can't hang on a Reaper that isn't there.
        """
        # BEFORE importing reapy, because importing it is the dangerous
        # part - see bridge_server_state. Reaper running with no bridge
        # server is the one state where `import reapy` never returns, and
        # nothing after the import can rescue that.
        state = bridge_server_state()
        if state == NO_SERVER:
            # Normal after a Reaper restart: the server is started on
            # demand by the first client. Do that here, bounded, rather
            # than letting reapy do it unbounded during its import.
            state = start_bridge_server()
        if state == NO_SERVER:
            return (
                "Reaper is running, but its bridge script did not start when asked. "
                "In Reaper, check Options > Preferences > Plug-ins > ReaScript - the "
                "Python it names there has to exist and have reapy installed. Nothing "
                "was changed."
            )

        try:
            import reapy
        except ImportError as exc:
            return missing_client_library_reason("Reaper", "reapy", "python-reapy", exc)

        if reapy.is_inside_reaper():
            return None

        # Before anything asks whether Reaper is reachable, make sure the
        # asking is done over IPv4. reapy resolves "localhost" fresh on
        # every connection and gets ::1 first on Windows, where Reaper
        # binds IPv4 only - see _LOOPBACK. This is the gate every
        # operation passes through, so it is the one place that
        # guarantees the client is pointed somewhere that can answer.
        ipv4_problem = use_ipv4_loopback(reapy)

        if reapy.dist_api_is_enabled():
            return None

        if not _reaper_web_interface_answers(reapy):
            if ipv4_problem and _reaper_web_interface_answers_over_name(reapy):
                # Reachable by name but not by address: something local
                # is redirecting or filtering the loopback. Say so rather
                # than report Reaper closed, because it plainly isn't.
                return (
                    f"Reaper is running, but only answers on 'localhost', not on {_LOOPBACK}. "
                    f"That usually means a proxy, VPN or firewall is intercepting local "
                    f"connections. DAWBridge uses the numeric address on purpose - the name "
                    f"resolves to IPv6 first on Windows and Reaper does not listen there."
                )
            return (
                f"Reaper isn't answering on {_LOOPBACK}:2307. Either it isn't running, or "
                f"it hasn't been set up yet - DAWBridge can't tell those apart. If Reaper "
                f"is open, close it and press \"Set up Reaper...\"; DAWBridge brings its "
                f"own Python, so there is nothing to install."
            )

        try:
            # Bounded: reconnect goes back through WebInterface, whose
            # perform_action passes no timeout of its own. Unbounded here
            # is how a status refresh froze the whole window.
            with _socket_timeout_floor(_REAPY_CALL_CEILING_SECONDS):
                reapy.reconnect()
        except Exception:
            pass  # the message below is already the right one
        if reapy.dist_api_is_enabled():
            return None

        return (
            "Reaper is running, but its scripting bridge isn't answering - the connection, "
            "not Reaper itself, is what's missing. Close Reaper, press \"Set up Reaper...\", "
            "then start Reaper again."
        )

    def project_identity(self) -> str:
        from reapy import reascript_api as RPR

        try:
            return RPR.EnumProjects(-1, "", 4096)[2] or ""
        except Exception:
            return ""

    def project_file(self) -> str:
        """Where this project is saved, or "" if it never has been.

        Same call as `project_identity`, named for the other thing that
        answer is used for. Identity asks "is this the same project as
        last time"; this asks "is there a folder to put audio in", and a
        reader shouldn't have to know those are the same question.
        """
        return self.project_identity()

    #: How long to leave Reaper's Save dialog open before giving up. This
    #: is a person choosing a folder and typing a name, not a machine, so
    #: it is generous. Cancelling is detected the same way as a timeout -
    #: neither produces a saved project, and nothing downstream cares
    #: which it was.
    save_prompt_seconds = 180

    def prompt_save_project(self, poll_seconds: float = 0.5) -> str:
        """Ask Reaper to save the open project, and wait for the answer.

        Returns where it landed, or raises with a sentence for the user.

        **Do not go back to `Main_SaveProjectEx(0, path, 0)`.** It was
        tried, and it fails in two ways at once, both confirmed live:

          1. It ignores the filename. Reaper opens an empty Save dialog,
             so the path DAWBridge carefully asked the user for is
             discarded - which made DAWBridge's own "where shall I save
             it?" box worse than redundant, since it collected an answer
             it then threw away.
          2. It returns immediately rather than blocking. Reading the
             project path back on the next line therefore always found
             nothing, and the verification always reported "the save did
             not happen" - even when the user had saved perfectly well a
             few seconds later. Observed exactly that: the warning fired,
             and the project was on the Desktop the whole time.

        So the dialog is Reaper's and the waiting is ours. `Main_SaveProject`
        with `forceSaveAs` is the documented way to raise it - preferred
        over a raw action id, which would be a number nobody could check.
        """
        import time

        from reapy import reascript_api as RPR

        before = self.project_file()
        try:
            RPR.Main_SaveProject(0, True)
        except Exception as exc:
            raise RuntimeError(
                f"Reaper would not open its Save dialog ({exc}). Save the project yourself "
                f"in Reaper (File -> Save project as...), then pull again."
            ) from exc

        deadline = time.monotonic() + self.save_prompt_seconds
        while time.monotonic() < deadline:
            landed = self.project_file()
            if landed and landed != before:
                return landed
            time.sleep(poll_seconds)

        raise RuntimeError(
            "the project still isn't saved - the Save dialog was cancelled, or it is "
            "still open. Nothing was changed by this. Save the project in Reaper and "
            "pull again to keep the audio alongside it."
        )

    # ---- read: what's in Reaper right now ------------------------------

    def read_live_state(self) -> list[LiveTrack]:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()
        live = []
        for idx in range(project.n_tracks):
            native_track = project.tracks[idx]
            base_name, bridge_id = parse_tag(native_track.name)
            clips = []
            for i in range(native_track.n_items):
                item = native_track.items[i]
                take = item.active_take
                if take is None:
                    continue
                clip_base, clip_id = parse_tag(take.name)
                clips.append(
                    LiveClip(
                        bridge_id=clip_id,
                        name=clip_base,
                        start_seconds=item.position,
                        length_seconds=item.length,
                        source_path=_take_source_path(RPR, take),
                        native=item,
                    )
                )
            live.append(
                LiveTrack(
                    bridge_id=bridge_id,
                    name=base_name,
                    channels=int(RPR.GetMediaTrackInfo_Value(native_track.id, "I_NCHAN")),
                    muted=native_track.is_muted,
                    color=_read_track_colour(RPR, native_track.id),
                    clips=clips,
                    native=native_track,
                )
            )
        return live

    # ---- markers -------------------------------------------------------

    def read_live_markers(
        self,
        warnings: list[str] | None = None,
        *,
        for_publish: bool = False,
        save_first: bool = False,
    ) -> list[LiveMarker] | None:
        """Project markers, tagged and untagged, or None when their names
        can't be read at all (see the check at the end of this method). Regions are deliberately
        not included - the schema models a marker as a point in time and
        has nowhere to put a region's end, so publishing one would round
        it to its start and silently destroy the range.

        `for_publish` gates the regions warning to the pull path, where
        it's true that they aren't crossing the bridge. Repeating it on
        every push would be noise about something that push isn't doing,
        and a warning people learn to skim is worse than none.
        """
        from reapy import reascript_api as RPR

        _retval, _proj, n_markers, n_regions = RPR.CountProjectMarkers(0, 0, 0)
        markers: list[LiveMarker] = []
        regions = 0
        unreadable = 0

        for index in range(int(n_markers) + int(n_regions)):
            decoded = _decode_marker_row(RPR.EnumProjectMarkers2(0, index, 0, 0, 0, "", 0))
            if decoded is None:
                unreadable += 1
                continue
            is_region, position, _unusable_name, marker_index = decoded
            if is_region:
                regions += 1
                continue
            # The name from this call is always empty - see below. Carry
            # the position and index, which ARE reliable, and get the name
            # from the saved project.
            markers.append(
                LiveMarker(bridge_id=None, name="",
                           time_seconds=position, native=marker_index)
            )

        # CONFIRMED BY LIVE TEST against Reaper 7.69 + reapy: the name
        # out-parameter of EnumProjectMarkers2 is never populated over
        # reapy's remote API - it echoes back whatever buffer was passed
        # ('' stays '', 4096 spaces stay 4096 spaces), for EnumProjectMarkers,
        # EnumProjectMarkers2 and EnumProjectMarkers3 alike, and inside an
        # inside_reaper() block too. Numeric out-params are fine; string
        # ones only work where the call takes an explicit buffer SIZE
        # (GetMediaSourceFileName does, these don't). reapy's own Marker
        # class has no name property either, so there is no higher-level
        # route.
        #
        # Identity lives in the name, so the names come from the saved
        # project file instead - same move _pull_clips_via_text_export
        # makes on the Pro Tools side, for the same reason.
        if markers:
            named = self._name_markers_from_project(markers, warnings, save_first)
            if named is None:
                return None
            markers = named

        if warnings is not None and for_publish and regions:
            warnings.append(
                f"this project has {regions} region(s), which DAWBridge does not sync - the "
                f"shared session models a marker as a single point and has nowhere to keep a "
                f"region's end, so publishing one would quietly flatten it to its start. "
                f"Markers cross the bridge; regions stay put"
            )
        if warnings is not None and unreadable:
            warnings.append(
                f"{unreadable} marker(s) could not be read from Reaper (unrecognised ReaScript "
                f"reply) and were left out of the publish rather than published at a guessed "
                f"position"
            )
        return markers

    #: Live position vs the position in the saved file. They come from the
    #: same numbers, so anything above float noise means the file does not
    #: describe what's on screen.
    _MARKER_POSITION_TOLERANCE = 0.001

    def _name_markers_from_project(
        self, markers: list[LiveMarker], warnings: list[str] | None, save_first: bool
    ) -> list[LiveMarker] | None:
        """Fill in marker names (and so bridge tags) from the saved .rpp.

        Returns None - "markers can't be read" - rather than guessing,
        whenever the file can't be trusted to describe the live state. A
        stale name is worse than an admitted absence: a stale bridge tag
        duplicates, and duplication is the failure this whole approach
        exists to prevent.
        """
        import reapy
        from reapy import reascript_api as RPR

        def _give_up(reason: str) -> None:
            if warnings is not None:
                warnings.append(
                    f"markers were skipped: {reason}. DAWBridge reads marker names from the saved "
                    f"project file, because Reaper's scripting bridge cannot report them, and "
                    f"without names it cannot tell markers apart. Tracks and clips are unaffected"
                )
            return None

        try:
            project_path = RPR.EnumProjects(-1, "", 4096)[2] or ""
        except Exception as exc:
            return _give_up(f"Reaper would not say which project is open ({exc})")
        if not project_path:
            return _give_up("this project has never been saved, so there is no file to read")

        if save_first:
            # pull and push both save the project anyway; doing it here
            # first is what makes the file current enough to trust.
            try:
                reapy.Project().save()
            except Exception as exc:
                return _give_up(f"the project could not be saved ({exc})")
        else:
            # A preview must not write to anything, so it can only read a
            # file that is already current.
            try:
                if RPR.IsProjectDirty(0):
                    return _give_up("the project has unsaved changes, so its file is out of date")
            except Exception:
                pass

        try:
            text = Path(project_path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return _give_up(f"the project file could not be read ({exc})")

        rows = _parse_rpp_markers(text)
        if rows is None:
            return _give_up(f"{Path(project_path).name!r} is not a readable Reaper project file")

        by_index = {row["index"]: row for row in rows}
        named: list[LiveMarker] = []
        for marker in markers:
            row = by_index.get(marker.native)
            if row is None:
                return _give_up(
                    f"marker {marker.native} is in Reaper but not in the saved file, so the file "
                    f"is out of date"
                )
            # Cross-check the one field both sources report. Disagreement
            # means the file describes a different state than the one on
            # screen, and the names in it cannot be trusted either.
            if abs(row["position"] - marker.time_seconds) > self._MARKER_POSITION_TOLERANCE:
                return _give_up(
                    f"marker {marker.native} is at {marker.time_seconds:.3f}s in Reaper but "
                    f"{row['position']:.3f}s in the saved file, so the file is out of date"
                )
            base_name, bridge_id = parse_tag(row["name"])
            named.append(
                LiveMarker(bridge_id=bridge_id, name=base_name,
                           time_seconds=marker.time_seconds, native=marker.native)
            )
        return named

    def _pull_markers(self, session: Session, warnings: list[str]) -> None:
        """Replace canonical's marker list with what's in Reaper now.

        Wholesale, like tracks and clips - see sync.py's docstring. An
        untagged marker is adopted (stamped with a new id) so the next
        pull recognises it instead of adding a second copy.
        """
        from reapy import reascript_api as RPR

        markers_before = list(session.markers)
        pulled: list[Marker] = []
        claimed: set[str] = set()

        observed = self.read_live_markers(warnings, for_publish=True, save_first=True)
        if observed is None:
            return  # names unreadable - see read_live_markers
        for live in observed:
            bridge_id = claim_live_id(live.bridge_id, claimed)
            if live.bridge_id and bridge_id is None:
                warnings.append(duplicate_adoption_warning("marker", live.name, live.bridge_id))

            existing = next((m for m in session.markers if m.id == bridge_id), None) if bridge_id else None
            if existing is not None:
                existing.name = live.name
                existing.time_seconds = live.time_seconds
                pulled.append(existing)
                continue

            marker = Marker(id=bridge_id or new_id(), name=live.name, time_seconds=live.time_seconds)
            pulled.append(marker)
            if live.bridge_id is None or bridge_id is None:
                # Adopt it: stamp the id into the name so it's recognised
                # next time. Same reasoning as tracks and clips - without
                # this every pull would mint a new id and the other DAW
                # would collect a duplicate marker per sync.
                RPR.SetProjectMarker2(
                    0, live.native, False, live.time_seconds, 0, tag(marker.name, marker.id)
                )

        session.markers = pulled

        lost = [m.name for m in markers_before if m.id not in {x.id for x in pulled}]
        if lost:
            warnings.append(
                f"{len(lost)} marker(s) in the shared session are not in this project and this "
                f"publish removes them ({', '.join(repr(n) for n in lost[:5])}"
                f"{', ...' if len(lost) > 5 else ''})"
            )

    def _push_markers(self, session: Session, warnings: list[str]) -> None:
        """Apply canonical's markers into Reaper. Never deletes."""
        from reapy import reascript_api as RPR

        live = self.read_live_markers(warnings, save_first=True)
        if live is None:
            return  # names unreadable - see read_live_markers
        live_by_id = {m.bridge_id: m for m in live if m.bridge_id}
        plan = plan_markers(session.markers, set(live_by_id))

        for marker in plan.to_add:
            RPR.AddProjectMarker2(
                0, False, marker.time_seconds, 0, tag(marker.name, marker.id), -1, 0
            )

        for marker in plan.to_update:
            existing = live_by_id[marker.id]
            RPR.SetProjectMarker2(
                0, existing.native, False, marker.time_seconds, 0, tag(marker.name, marker.id)
            )

        for orphan_id in plan.orphaned_ids:
            orphan = live_by_id.get(orphan_id)
            warnings.append(
                f"marker {orphan.name if orphan else orphan_id!r} is in your Reaper project but "
                f"no longer in the shared session; left in place, review manually"
            )

    # ---- pull: live Reaper project -> canonical Session ----------------

    def capture(self, session: Session, store, warnings: list[str] | None = None) -> Session:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()
        if warnings is None:
            warnings = []  # local sink, so every append below is unconditional
        tracks_before = list(session.tracks)
        rate_before = session.sample_rate

        # Session tempo/meter drives the grid on both sides, so capture it
        # even though Pro Tools can only be told about it in a warning
        # (PTSL has no tempo command at all).
        _p, _t, num, denom, tempo = RPR.TimeMap_GetTimeSigAtTime(0, 0.0, 0, 0, 0)
        session.tempo_bpm = float(tempo)
        session.time_signature_numerator = int(num)
        session.time_signature_denominator = int(denom)

        # Canonical holds ONE tempo. A project with tempo changes through
        # it publishes only the value at the start, and until now nothing
        # said so - the push side refuses to overwrite a tempo map, but
        # the publish had already dropped everything past bar one.
        try:
            flattened = describe_tempo_map_flattening(
                RPR.CountTempoTimeSigMarkers(0), session.tempo_bpm
            )
            if flattened:
                warnings.append(flattened)
        except Exception:
            pass

        # Session.sample_rate has been in the schema since day one and was
        # never written by anything, so it read 48000 forever - including
        # in the real shared session, which carries 44.1k audio. Capture
        # it so the two sides can at least be told they disagree (see
        # describe_sample_rate_mismatch). PROJECT_SRATE only applies when
        # PROJECT_SRATE_USE is on; when it's off Reaper follows the audio
        # device, and claiming a rate we didn't really read would be worse
        # than leaving the previous one alone.
        try:
            if RPR.GetSetProjectInfo(0, "PROJECT_SRATE_USE", 0, False):
                rate = int(RPR.GetSetProjectInfo(0, "PROJECT_SRATE", 0, False))
                if rate > 0:
                    session.sample_rate = rate
        except Exception:
            pass

        # Replace canonical's track list wholesale with what's live right
        # now - see sync.py's module docstring for why pull doesn't merge
        # across different source projects. A track already known by id
        # keeps its existing clip history via merge_pulled_track; a track
        # not present in this pull is simply not carried forward.
        pulled_tracks = []

        # Identity is resolved for ALL tracks before any of them is used,
        # because a tag in a name has to beat an id stored in a copy of
        # that track no matter which order Reaper lists them in - see
        # sync.resolve_identities.
        natives = [project.tracks[idx] for idx in range(project.n_tracks)]
        observed = []
        for native_track in natives:
            base_name, parsed_id = parse_tag(native_track.name)
            observed.append(
                (parsed_id, _read_ext_id(RPR.GetSetMediaTrackInfo_String, native_track.id), base_name)
            )
        decisions = resolve_identities(observed, kind="track")

        for native_track, (_nid, _eid, base_name), decision in zip(natives, observed, decisions):
            if decision.warning:
                warnings.append(decision.warning)
            n_channels = int(RPR.GetMediaTrackInfo_Value(native_track.id, "I_NCHAN"))
            track = merge_pulled_track(
                session, base_name, decision.bridge_id, kind="audio", channels=n_channels
            )
            pulled_tracks.append(track)
            # Unlike channels, mute always reflects the live DAW - captured
            # fresh on every pull, not just when the track is first adopted.
            track.muted = native_track.is_muted
            # No quantiser: Reaper can display any colour it is given, so
            # "what canonical says" and "what Reaper shows" are directly
            # comparable. Pro Tools is the side that needs the guard.
            track.color = color.resolve_captured(
                track.color, _read_track_colour(RPR, native_track.id)
            )
            if decision.write_name_tag:
                # Either a brand new adoption, or a name whose tag was
                # tidied away and has just been recovered from the id
                # stored inside the project.
                RPR.GetSetMediaTrackInfo_String(
                    native_track.id, "P_NAME", tag(base_name, track.id), True
                )
            if decision.write_extended_state:
                _write_ext_id(RPR.GetSetMediaTrackInfo_String, native_track.id, track.id)

            self._pull_clips(native_track, track, session, store, warnings)

        session.tracks = pulled_tracks
        # Track.order is what `dawbridge status` and the GUI sort by; it
        # only becomes true here, once the list is in Reaper's order.
        renumber_tracks(session.tracks)

        try:
            self._pull_markers(session, warnings)
        except Exception as exc:
            # Markers are worth having but not worth losing a publish
            # over - the tracks and clips in hand are the valuable part.
            warnings.append(f"markers could not be read from Reaper ({exc}); none were published")

        # Publishing replaces the shared session's track list. Say which
        # tracks that removes, before the person who owns them finds out
        # by not finding them.
        warnings.extend(describe_dropped_tracks(tracks_before, session.tracks))
        rate_change = describe_publish_sample_rate_change(rate_before, session.sample_rate)
        if rate_change:
            warnings.append(rate_change)

        # Tags stamped above only live in the running Reaper instance
        # until the project is saved - confirmed live, closing Reaper
        # without saving loses them, and the next pull then treats every
        # track as brand new and duplicates the whole session. Save
        # immediately so identity actually persists.
        project.save()

        return session

    def _stamp_clip_identity(self, RPR, item, take, base_name: str, clip_id: str, decision) -> None:
        """Write identity back onto a live item: the tag into the take name
        when it's missing, and the id into extended state where a rename
        can't reach it. See sync.resolve_identities for which is which.
        """
        if decision.write_name_tag:
            RPR.GetSetMediaItemTakeInfo_String(take.id, "P_NAME", tag(base_name, clip_id), True)
        if decision.write_extended_state:
            _write_ext_id(RPR.GetSetMediaItemInfo_String, item.id, clip_id)

    def _pull_clips(self, native_track, track: Track, session: Session, store, warnings: list[str]) -> None:
        from reapy import reascript_api as RPR

        # Replace track.clips wholesale with what's live now - same
        # reasoning as pull()'s track-list replacement (see sync.py).
        pulled_clips = []

        # Same two-pass resolution as tracks: "duplicate items" copies the
        # take name AND the stored id, so a tag in a name has to win before
        # any stored id is consulted (sync.resolve_identities).
        takes = []
        for i in range(native_track.n_items):
            item = native_track.items[i]
            take = item.active_take
            if take is not None:
                takes.append((item, take))
        observed = []
        for item, take in takes:
            base, parsed = parse_tag(take.name)
            observed.append(
                (parsed, _read_ext_id(RPR.GetSetMediaItemInfo_String, item.id), base)
            )
        clip_decisions = resolve_identities(observed, kind="clip")

        for (item, take), (_nid, _eid, base_name), decision in zip(takes, observed, clip_decisions):
            clip_id = decision.bridge_id
            if decision.warning:
                warnings.append(decision.warning)

            source_offset = RPR.GetMediaItemTakeInfo_Value(take.id, "D_STARTOFFS")
            loop_source = bool(RPR.GetMediaItemInfo_Value(item.id, "B_LOOPSRC"))
            # Fades were in the schema and written on push, but nothing
            # ever read them, so every clip published from Reaper claimed
            # 0.0 and the crossfades between butt-joined clips arrived on
            # the other side as clicks.
            fade_in = float(RPR.GetMediaItemInfo_Value(item.id, "D_FADEINLEN") or 0.0)
            fade_out = float(RPR.GetMediaItemInfo_Value(item.id, "D_FADEOUTLEN") or 0.0)
            live_source_path = _take_source_path(RPR, take)

            existing_clip = track.clip_by_id(clip_id) if clip_id else None
            if existing_clip is not None:
                # Refresh position/length/name from the live DAW. This
                # used to `continue` here on the theory that the push
                # side would compare positions - it doesn't and can't,
                # because canonical was never updated to differ. The
                # effect was that moving or resizing an already-tagged
                # clip was silently invisible to the other DAW forever.
                existing_clip.name = base_name
                existing_clip.start_seconds = item.position
                existing_clip.length_seconds = item.length
                existing_clip.source_offset_seconds = source_offset
                existing_clip.loop_source = loop_source
                existing_clip.fade_in_seconds = fade_in
                existing_clip.fade_out_seconds = fade_out
                # Replacing the audio under an already-tagged clip - a
                # re-record, a comp, "apply track FX to items" - used to
                # publish as no change at all, and the next push then
                # re-pointed the take back to canonical's older file,
                # silently undoing the user's own edit. See
                # sync.should_reimport_audio.
                if should_reimport_audio(live_source_path, existing_clip, store):
                    existing_clip.audio_file = (
                        _import_audio(store, live_source_path, warnings, base_name, track.name)
                        or existing_clip.audio_file
                    )
                self._stamp_clip_identity(RPR, item, take, base_name, existing_clip.id, decision)
                pulled_clips.append(existing_clip)
                continue

            audio_file = _import_audio(store, live_source_path, warnings, base_name, track.name)

            new_clip = Clip(
                id=clip_id or new_id(),
                name=base_name,
                audio_file=audio_file,
                start_seconds=item.position,
                length_seconds=item.length,
                source_offset_seconds=source_offset,
                fade_in_seconds=fade_in,
                fade_out_seconds=fade_out,
                loop_source=loop_source,
            )
            pulled_clips.append(new_clip)
            self._stamp_clip_identity(RPR, item, take, base_name, new_clip.id, decision)

        track.clips = pulled_clips

    # ---- push: canonical Session -> live Reaper project -----------------

    def apply(self, session: Session, store) -> list[str]:
        import reapy
        from reapy import reascript_api as RPR

        project = reapy.Project()

        warnings: list[str] = []

        # Empty means this project has never been saved, so there is no
        # folder to keep audio beside and the clips have to play from the
        # shared folder. The GUI and CLI both offer to save first, so
        # reaching here unsaved means the user declined - which is their
        # call, but they should be told what they get.
        project_file = self.project_file()
        if not project_file:
            warnings.append(
                "this Reaper project has never been saved, so its audio plays straight from the "
                "shared folder: the project breaks if that folder moves or goes offline, and "
                "Reaper writes its peak files into it. Save the project and pull again to move "
                "the audio alongside it"
            )

        # Apply session tempo, but never silently flatten a tempo MAP:
        # canonical only models one tempo, so a project with multiple
        # tempo/time-sig markers would lose them. Leave those alone and
        # say so instead.
        n_tempo_markers = RPR.CountTempoTimeSigMarkers(0)
        _p, _t, cur_num, cur_denom, cur_tempo = RPR.TimeMap_GetTimeSigAtTime(0, 0.0, 0, 0, 0)
        if n_tempo_markers > 1:
            warnings.append(
                f"this Reaper project has {n_tempo_markers} tempo/time-signature markers; DAWBridge "
                f"models a single session tempo, so the tempo map was left untouched "
                f"(canonical tempo is {session.tempo_bpm:g} BPM)"
            )
        else:
            # SetCurrentBPM is not a cosmetic change: it drags every
            # beat-attached item, fade and marker in the project to a new
            # position. Confirmed live on Reaper 7.69 - an item at 5.333s
            # with a 0.333s fade became 4.000s / 0.250s on a 90->120
            # change, and back on the way down. Canonical is in SECONDS,
            # so doing that mid-push silently relocates whatever was
            # already in the project and then places the pushed clips at
            # canonical's own positions, leaving the arrangement wrong
            # against itself - and the next pull publishes the damage.
            # Only safe when there is nothing to drag.
            item_count = int(RPR.CountMediaItems(0))
            if tempo_write_is_safe(item_count):
                RPR.SetCurrentBPM(0, session.tempo_bpm, True)
            else:
                refusal = describe_tempo_write_refusal(session, cur_tempo, item_count)
                if refusal:
                    warnings.append(refusal)

        # The tempo above is applied; the METER never is - SetCurrentBPM
        # doesn't touch it and there's no meter write anywhere in this
        # backend. Pull captures it, so a 6/8 song crossing the bridge
        # arrives at the right BPM on a 4/4 grid, silently. Say so rather
        # than let the two people edit against different rulers.
        meter = describe_meter_mismatch(session, cur_num, cur_denom)
        if meter:
            warnings.append(meter)

        try:
            if RPR.GetSetProjectInfo(0, "PROJECT_SRATE_USE", 0, False):
                rate = describe_sample_rate_mismatch(
                    session, int(RPR.GetSetProjectInfo(0, "PROJECT_SRATE", 0, False)), "Reaper"
                )
                if rate:
                    warnings.append(rate)
        except Exception:
            pass

        # Same reading the preview uses, so a previewed change and the
        # change actually applied here can't disagree.
        local_tag_to_track = {t.bridge_id: t.native for t in self.read_live_state() if t.bridge_id}

        track_plan = plan_tracks(session, {k: v.name for k, v in local_tag_to_track.items()})

        for track in track_plan.to_create:
            new_index = project.n_tracks
            project.add_track(new_index, name=tag(track.name, track.id))
            native_track = project.tracks[new_index]
            RPR.SetMediaTrackInfo_Value(native_track.id, "I_NCHAN", max(2, track.channels))
            native_track.is_muted = track.muted
            _write_track_colour(RPR, native_track.id, track.color)
            local_tag_to_track[track.id] = native_track
            self._push_clips(native_track, track, store, warnings, project_file)

        for track, _native_name in track_plan.to_update:
            native_track = local_tag_to_track[track.id]
            if strip_tag(native_track.name) != track.name:
                RPR.GetSetMediaTrackInfo_String(
                    native_track.id, "P_NAME", tag(track.name, track.id), True
                )
            native_track.is_muted = track.muted
            # Only when it would actually change something - every RPR call
            # is a round trip over reapy's remote API, and rewriting the
            # colour a track already has is a cost with no effect.
            if not color.same(track.color, _read_track_colour(RPR, native_track.id)):
                _write_track_colour(RPR, native_track.id, track.color)
            self._push_clips(native_track, track, store, warnings, project_file)

        try:
            self._push_markers(session, warnings)
        except Exception as exc:
            warnings.append(
                f"markers could not be written into Reaper ({exc}); the tracks and clips in this "
                f"push were applied, the markers were not"
            )

        # Items created through the API don't get peak files built the way
        # a manual import/drag does, so pushed clips render as empty
        # outlines with no waveform until something forces a build
        # (confirmed live - the audio was fine and played, it just looked
        # blank). 40047 = "Peaks: Build any missing peaks".
        reapy.perform_action(40047)

        # Same reasoning as pull(): tags stamped on newly-created tracks
        # only live in-memory until saved.
        project.save()

        return warnings

    def _local_audio_path(self, project_file: str, store, clip: Clip, track_name: str,
                          warnings: list[str]) -> Path | None:
        """Where Reaper should read this clip's audio from.

        A copy beside the project, so the DAW never streams from - or
        writes peak files into - the shared cloud folder. See localmedia
        for why that matters and, just as importantly, why it is *not*
        about audio quality.

        Falls back to the shared path rather than skipping the clip when
        the copy can't be made. An arrangement playing from Dropbox is
        the old behaviour and it works; an arrangement missing a clip is
        a hole the user has to notice and repair by hand. The warning is
        what makes the fallback honest.
        """
        shared = store.resolve_audio_path(clip.audio_file)
        if not project_file:
            return shared
        try:
            return localmedia.local_copy(project_file, store, clip.audio_file)
        except OSError as exc:
            warnings.append(
                f"clip {clip.name!r} on track {track_name!r} could not be copied to this "
                f"project's {localmedia.MEDIA_DIR_NAME} folder ({exc}); it plays from the shared "
                f"folder instead, so it will stop working if that folder goes offline"
            )
            return shared

    def _push_clips(self, native_track, track: Track, store, warnings: list[str],
                    project_file: str = "") -> None:
        from reapy import reascript_api as RPR

        local_clip_ids = set()
        native_items_by_id = {}
        for i in range(native_track.n_items):
            item = native_track.items[i]
            take = item.active_take
            if take is None:
                continue
            _base, clip_id = parse_tag(take.name)
            if clip_id:
                local_clip_ids.add(clip_id)
                native_items_by_id[clip_id] = item

        clip_plan = plan_clips(track.clips, local_clip_ids)

        for clip in clip_plan.to_add:
            # Pro Tools already refused to place a clip whose audio isn't
            # in the shared folder. Reaper handed the path to
            # PCM_Source_CreateFromFile regardless, which produces an item
            # with no source: it looks like a clip, sits at the right
            # place, and plays silence. That is exactly what a partner's
            # still-uploading 90MB stem looks like from this side.
            missing = missing_audio_warning(clip, track.name, store)
            if missing:
                warnings.append(missing + " (not created in Reaper)")
                continue

            audio_path = self._local_audio_path(project_file, store, clip, track.name, warnings)
            item_id = RPR.AddMediaItemToTrack(native_track.id)
            take_id = RPR.AddTakeToMediaItem(item_id)
            source = RPR.PCM_Source_CreateFromFile(str(audio_path))
            RPR.SetMediaItemTake_Source(take_id, source)
            RPR.SetMediaItemInfo_Value(item_id, "D_POSITION", clip.start_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_LENGTH", clip.length_seconds)
            RPR.SetMediaItemTakeInfo_Value(take_id, "D_STARTOFFS", clip.source_offset_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_FADEINLEN", clip.fade_in_seconds)
            RPR.SetMediaItemInfo_Value(item_id, "D_FADEOUTLEN", clip.fade_out_seconds)
            # Set the loop flag before length: a non-looped item can't be
            # longer than its source, so length would otherwise clamp.
            RPR.SetMediaItemInfo_Value(item_id, "B_LOOPSRC", 1 if clip.loop_source else 0)
            RPR.SetMediaItemInfo_Value(item_id, "D_LENGTH", clip.length_seconds)
            RPR.GetSetMediaItemTakeInfo_String(take_id, "P_NAME", tag(clip.name, clip.id), True)

        for clip in clip_plan.to_update:
            item = native_items_by_id[clip.id]
            take = item.active_take

            # Re-point the source if canonical now references different
            # audio for this clip. Without this, replacing a clip's audio
            # on one side never reached the other - confirmed live, a
            # stereo repair changed audio_file in the shared session and
            # Reaper happily kept playing the old mono file, reporting
            # "no changes" the whole time.
            if take is not None and clip.audio_file:
                # Resolving to the local copy here is also what migrates a
                # project made before local copies existed: its takes
                # point into the shared folder, which differs from
                # `wanted`, so they get re-pointed on the next pull with
                # nothing special written for the purpose. `local_copy`
                # stats before it copies, so a clip already local costs
                # one `exists()`.
                wanted = self._local_audio_path(project_file, store, clip, track.name, warnings)
                if _take_source_path(RPR, take) != str(wanted) and wanted.exists():
                    source = RPR.PCM_Source_CreateFromFile(str(wanted))
                    RPR.SetMediaItemTake_Source(take.id, source)

            RPR.SetMediaItemInfo_Value(item.id, "D_POSITION", clip.start_seconds)
            RPR.SetMediaItemInfo_Value(item.id, "B_LOOPSRC", 1 if clip.loop_source else 0)
            RPR.SetMediaItemInfo_Value(item.id, "D_LENGTH", clip.length_seconds)
            if take is not None:
                RPR.SetMediaItemTakeInfo_Value(take.id, "D_STARTOFFS", clip.source_offset_seconds)

        for orphan_id in clip_plan.orphaned_ids:
            warnings.append(
                f"clip {orphan_id} on Reaper track {track.name!r} is no longer in the "
                f"canonical session; left in place, review manually"
            )
