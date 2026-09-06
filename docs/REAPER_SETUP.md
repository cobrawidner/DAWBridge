# Reaper: one-time setup

**Pro Tools users: skip this entirely. There's nothing to do.**

Reaper needs to be taught to talk to DAWBridge, once. It takes about five
minutes and you never do it again.

---

## 1. Install Python 3.10

**[Download Python 3.10.11, 64-bit](https://www.python.org/ftp/python/3.10.11/python-3.10.11-amd64.exe)** (29 MB, from python.org)

It must be **64-bit**, to match Reaper. Any 3.10.x works — 3.10.11 is just
the newest one with an installer.

In the installer, **tick "Add python.exe to PATH"** on the first screen
before clicking Install. If you miss it, the commands below won't be found
and you'll have to reinstall.

## 2. Close Reaper

Actually close it, don't just minimise it. Reaper saves its settings when
it quits, and it will undo this setup if it's running while you do it.

## 3. Run two commands

Open **Command Prompt** (press Start, type `cmd`, Enter) and run these one
at a time:

```
pip install python-reapy
```

```
python -c "import reapy; reapy.configure_reaper(detect_portable_install=False)"
```

The second one prints nothing if it worked. That's success.

*If it says `FileNotFoundError`*, you have a portable Reaper install. Open
Reaper, then run the same command without `detect_portable_install=False`,
and close Reaper again afterwards.

## 4. Open Reaper, then run DAWBridge.exe

Pick your shared folder, choose **Reaper**, and click **Refresh status**.

---

## Did it work?

Click **Preview pull**. If DAWBridge describes what it would change, you're
done — it's talking to Reaper.

If it says Reaper isn't answering:

- Did you tick "Add python.exe to PATH"? Run `python --version` in Command
  Prompt. If that fails, reinstall Python and tick the box.
- Did you restart Reaper *after* step 3? The setting is only read at
  startup.
- Was Reaper closed during step 3? If not, do steps 2–4 again.

Still stuck: send the text from DAWBridge's log window. It says which of
those it is.
