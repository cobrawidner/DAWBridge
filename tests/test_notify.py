"""Discord notifications.

Two properties matter more than the feature working: a notification must
never break a sync, and the URL must never be a way to send session
details somewhere that isn't Discord - it lives in a folder the other
person and Dropbox can both write to.

Nothing here touches the network. `post()` takes a sender for exactly
that reason.
"""
import json

import pytest

from dawbridge import notify


@pytest.fixture(autouse=True)
def _isolated_opt_in(tmp_path, monkeypatch):
    """Never let a test touch the real machine's consent file."""
    monkeypatch.setattr(notify, "_OPT_IN_PATH", tmp_path / "opt_in.json")


def _configure(tmp_path, opted_in=True, **config):
    """A folder with a webhook. `opted_in` is this machine's consent,
    which is deliberately separate - see test_a_fresh_machine_* below."""
    (tmp_path / notify.CONFIG_NAME).write_text(json.dumps(config), encoding="utf-8")
    if opted_in:
        notify.set_machine_opt_in(tmp_path, True)
    return tmp_path


def test_no_config_means_silence_not_failure(tmp_path):
    assert notify.is_enabled_for(tmp_path, "publish") is False
    assert notify.post(tmp_path, "hello") is None


def test_a_configured_webhook_sends(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/abc")
    sent = []

    assert notify.post(root, "hello", sender=lambda url, msg: sent.append((url, msg))) is None
    assert sent == [("https://discord.com/api/webhooks/1/abc", "hello")]


def test_a_network_failure_never_breaks_the_sync(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/abc")

    def explode(url, msg):
        raise OSError("network is down")

    problem = notify.post(root, "hello", sender=explode)

    assert problem is not None, "the caller must get something to log"
    assert "the sync itself was fine" in problem


def test_a_non_discord_url_is_refused_without_sending(tmp_path):
    # The config lives in a shared, synced folder. Anyone who can write
    # there could otherwise redirect every session notification.
    root = _configure(tmp_path, discord_webhook="https://evil.example.com/api/webhooks/1/abc")
    sent = []

    problem = notify.post(root, "secret", sender=lambda u, m: sent.append(u))

    assert sent == [], "nothing may be sent to a non-Discord host"
    assert "not Discord" in problem


@pytest.mark.parametrize("url,expected", [
    ("http://discord.com/api/webhooks/1/a", "https"),
    ("https://discord.com/webhooks/1/a", "webhook URL"),
    ("https://discord.com.evil.net/api/webhooks/1/a", "not Discord"),
    ("https://discord.com/api/webhooks/1/a", None),
    ("https://discordapp.com/api/webhooks/1/a", None),
])
def test_url_validation(url, expected):
    reason = notify.reject_reason(url)
    if expected is None:
        assert reason is None
    else:
        assert reason and expected in reason


def test_events_can_be_narrowed_to_publishes_only(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/a",
                      events=["publish"])
    assert notify.is_enabled_for(root, "publish") is True
    assert notify.is_enabled_for(root, "load") is False


def test_both_events_are_on_by_default(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/a")
    assert notify.is_enabled_for(root, "publish")
    assert notify.is_enabled_for(root, "load")


def test_a_second_machine_posts_without_being_configured(tmp_path):
    """Travis's call: handing the URL to DAWBridge is the consent.

    So the collaborator sets nothing up - their copy finds the webhook in
    the shared folder and posts. A channel only one of you reaches is
    worse than none.
    """
    root = _configure(tmp_path, opted_in=False,
                      discord_webhook="https://discord.com/api/webhooks/1/a")

    assert notify.is_enabled_for(root, "publish") is True
    assert notify.is_enabled_for(root, "load") is True


def test_muting_this_machine_leaves_the_shared_folder_alone(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/a")
    before = (root / notify.CONFIG_NAME).read_text(encoding="utf-8")

    notify.set_machine_opt_in(root, False)

    assert notify.is_enabled_for(root, "publish") is False, "an explicit mute sticks"
    assert (root / notify.CONFIG_NAME).read_text(encoding="utf-8") == before, (
        "muting yourself must not dismantle the channel for your partner"
    )


def test_setting_the_webhook_up_is_itself_consent(tmp_path):
    # Nobody configures a channel and then wants to be asked whether they
    # meant it.
    notify.save_config(tmp_path, {"discord_webhook": "https://discord.com/api/webhooks/1/a"})
    assert notify.is_enabled_for(tmp_path, "publish") is True


def test_opting_out_leaves_the_channel_configured_for_everyone_else(tmp_path):
    root = _configure(tmp_path, discord_webhook="https://discord.com/api/webhooks/1/a")
    notify.set_machine_opt_in(root, False)

    assert notify.is_enabled_for(root, "publish") is False
    assert notify.webhook_url(root), "your partner's channel is still set up"


def test_a_corrupt_config_is_treated_as_absent(tmp_path):
    (tmp_path / notify.CONFIG_NAME).write_text("{not json", encoding="utf-8")
    assert notify.is_enabled_for(tmp_path, "publish") is False


def test_messages_say_what_happened_without_leaking_paths(tmp_path):
    publish = notify.describe_publish("travis", "reaper", 12, tracks=7, clips=23, warnings=2)
    assert "r12" in publish and "7 track" in publish and "2 warning" in publish
    assert "Pull from Bridge" in publish, "tell them what to do about it"

    load = notify.describe_load("conner", "protools", 12)
    assert "r12" in load and "Pro Tools" in load, "written the way people write it"

    for message in (publish, load):
        assert ":\\" not in message and "/Users/" not in message, "no filesystem paths"


def test_mentions_are_disabled_in_the_payload(monkeypatch, tmp_path):
    # A sync notification that pings @everyone gets the feature switched
    # off within a day.
    captured = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(notify.urllib.request, "urlopen", fake_urlopen)
    notify._send("https://discord.com/api/webhooks/1/a", "@everyone hi")

    assert captured["body"]["allowed_mentions"] == {"parse": []}


def test_project_name_comes_from_the_shared_folder(tmp_path):
    # Session.name is useless for this - neither backend captures it, so
    # every session reads 'Untitled'. The folder is what people named it.
    folder = tmp_path / "Shady Grove" / "Shady Grove.dawbridge"
    folder.mkdir(parents=True)
    assert notify.project_name(folder) == "Shady Grove"


def test_project_name_without_the_suffix(tmp_path):
    folder = tmp_path / "Valley Bend"
    folder.mkdir()
    assert notify.project_name(folder) == "Valley Bend"


def test_project_name_never_comes_back_empty(tmp_path):
    # A trailing separator used to resolve to '' and produce "**** - ".
    folder = tmp_path / "Song"
    folder.mkdir()
    assert notify.project_name(str(folder) + "\\") == "Song"


def test_the_project_leads_the_message():
    # Several projects can post to one channel; "published r36" with no
    # project attached is unreadable there.
    line = notify.describe_publish("Travis", "reaper", 36, tracks=7, clips=23,
                                   project="Shady Grove")
    assert line.startswith("**Shady Grove** - Travis published **r36** from Reaper")

    load = notify.describe_load("Conner", "protools", 36, project="Shady Grove")
    assert load.startswith("**Shady Grove** - Conner loaded **r36** into Pro Tools")


def test_daw_names_are_written_the_way_people_write_them():
    assert notify.daw_label("protools") == "Pro Tools"
    assert notify.daw_label("reaper") == "Reaper"
    assert notify.daw_label("something else") == "something else"


def test_a_message_without_a_project_still_reads():
    assert notify.describe_publish("Travis", "reaper", 36, tracks=1, clips=1).startswith(
        "Travis published")
