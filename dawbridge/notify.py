"""Post a line to Discord when someone publishes or loads.

The shared folder answers "what is the session now?" but never "did
anything just happen?" - so the quickstart has to tell people to message
each other after publishing, which is exactly the kind of discipline that
lapses at 1am. This closes that loop.

Three rules shape everything here:

1. **A notification must never break a sync.** Discord being down, the
   network being out, a mistyped URL - none of it may raise, and none of
   it may leave a sync half-done. Every failure returns a sentence for
   the log and the sync carries on. The work is the point; the message
   is a courtesy.
2. **Nothing sensitive leaves the machine.** Who, which DAW, the
   revision, and counts. Never file paths (they carry usernames and
   folder structure), never audio, never the session contents.
3. **The URL is a place to send data**, and it lives in a folder the
   other person - and Dropbox - can write to. So it is validated against
   Discord's own hosts before anything is posted. Without that, editing
   one file in the shared folder would silently redirect every sync
   notification to an arbitrary server.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

CONFIG_NAME = "notify.json"

# Consent lives here, on the machine, and never in the shared folder.
# The URL is shared so one person sets the channel up once; agreeing to
# post to it is a separate decision each person makes for themselves.
# Without this split, picking the shared folder silently enrolled you in
# posting to somebody else's Discord.
_OPT_IN_PATH = Path.home() / ".dawbridge_notify.json"

# Discord's own hosts, and nothing else. See rule 3 above.
_ALLOWED_HOSTS = {"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"}

# Long enough for a normal round trip, short enough that nobody watches a
# frozen window wondering whether their publish worked.
_TIMEOUT_SECONDS = 6


def config_path(root: Path) -> Path:
    return Path(root) / CONFIG_NAME


def load_config(root: Path) -> dict:
    """The notify config, or {} when there isn't one.

    Absence is the normal case, not an error: the feature is off until
    somebody sets it up.
    """
    try:
        data = json.loads(config_path(root).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_config(root: Path, config: dict) -> None:
    """Write the notify config into the shared folder.

    Deliberately shared rather than per-machine: one person sets the
    channel up and both machines start posting to it, which is the whole
    point - a notification only one of you receives is worse than none.
    The cost is that the URL is visible to anyone with folder access,
    which is exactly the people already in the channel.
    """
    config_path(root).write_text(json.dumps(config, indent=2), encoding="utf-8")
    # Setting the webhook up here is itself agreement to use it; nobody
    # configures a channel and then wants to be asked again.
    set_machine_opt_in(root, True)


def webhook_url(root: Path) -> Optional[str]:
    url = str(load_config(root).get("discord_webhook", "")).strip()
    return url or None


def _opt_ins() -> dict:
    try:
        data = json.loads(_OPT_IN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _opt_in_key(root: Path) -> str:
    return str(Path(root).resolve())


def machine_opted_in(root: Path) -> bool:
    """Has *this machine* agreed to post to this folder's channel?

    Absent means no. A first launch posts nothing, whatever the shared
    folder already contains.
    """
    return bool(_opt_ins().get(_opt_in_key(root), False))


def set_machine_opt_in(root: Path, on: bool) -> None:
    data = _opt_ins()
    data[_opt_in_key(root)] = bool(on)
    try:
        _OPT_IN_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass  # a missing preference must never break a sync


def is_enabled_for(root: Path, event: str) -> bool:
    """Needs both a channel to post to and this machine's agreement to.

    Both events are on by default once those two hold. Knowing your
    partner *loaded* your work is worth as much as knowing they
    published - it's the difference between "they have it" and "it's
    still sitting there".
    """
    config = load_config(root)
    if not str(config.get("discord_webhook", "")).strip():
        return False
    if not machine_opted_in(root):
        return False
    events = config.get("events")
    if events is None:
        return True
    return event in events


def reject_reason(url: str) -> Optional[str]:
    """Why this URL may not be posted to, or None if it's fine."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "the webhook URL must start with https://"
    if parsed.hostname not in _ALLOWED_HOSTS:
        return (f"the webhook URL points at {parsed.hostname!r}, which is not Discord - "
                f"refusing to send session details there")
    if "/api/webhooks/" not in parsed.path:
        return "that doesn't look like a Discord webhook URL (no /api/webhooks/ in it)"
    return None


_DAW_LABELS = {"reaper": "Reaper", "protools": "Pro Tools"}


def daw_label(daw: str) -> str:
    return _DAW_LABELS.get(daw, daw)


def project_name(root) -> str:
    """The project's name, taken from the shared folder itself.

    `Session.name` would be the obvious source and is useless: neither
    backend captures it, so every session reads 'Untitled'. The folder is
    what people actually named the thing -
    ".../Shady Grove/Shady Grove.dawbridge" becomes "Shady Grove".

    Matters most when several projects post to one channel, where
    "published r36" with nothing attached to it is unreadable.
    """
    leaf = Path(root).resolve().name
    if leaf.lower().endswith(".dawbridge"):
        leaf = leaf[: -len(".dawbridge")]
    return leaf.strip() or "DAWBridge"


def _prefix(project: str) -> str:
    return f"**{project}** - " if project else ""


def describe_publish(who: str, daw: str, revision: int, tracks: int, clips: int,
                     warnings: int = 0, project: str = "") -> str:
    line = (f"{_prefix(project)}{who} published **r{revision}** from "
            f"{daw_label(daw)} - {tracks} track(s), {clips} clip(s)")
    if warnings:
        line += f" - {warnings} warning(s)"
    return line + "\nPull from Bridge to pick it up."


def describe_load(who: str, daw: str, revision: int, project: str = "") -> str:
    return (f"{_prefix(project)}{who} loaded **r{revision}** into "
            f"{daw_label(daw)}")


def post(root: Path, message: str, sender=None) -> Optional[str]:
    """Send `message`. Returns None on success, or a sentence for the log.

    `sender` exists so tests never touch the network.
    """
    url = webhook_url(root)
    if not url:
        return None  # not configured - silence is correct, not a failure

    reason = reject_reason(url)
    if reason:
        return f"not notifying Discord: {reason}"

    try:
        (sender or _send)(url, message)
    except urllib.error.HTTPError as exc:
        return f"Discord rejected the notification ({exc.code}) - the sync itself was fine"
    except Exception as exc:  # noqa: BLE001 - a courtesy must never break a sync
        return f"could not reach Discord ({type(exc).__name__}) - the sync itself was fine"
    return None


def _send(url: str, message: str) -> None:
    payload = json.dumps({
        "content": message,
        # Post under the app's name rather than whatever the webhook was
        # called when it was created. The avatar is set in Discord's own
        # webhook settings - it can't be supplied from here without
        # hosting the image somewhere public.
        "username": "DAWBridge",
        # Discord renders @everyone in content unless told not to; a sync
        # notification pinging a whole server would get this switched off
        # within a day.
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json", "User-Agent": "DAWBridge"},
    )
    with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS):
        pass
