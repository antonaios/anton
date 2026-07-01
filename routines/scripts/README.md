# Routines — scripts

## `run_hinotes_watcher.sh`

One-shot launcher. Runs in the foreground, prints to terminal. Use for testing or short sessions.

```bash
bash "/mnt/x/Agentic OS/routines/scripts/run_hinotes_watcher.sh"
```

## `hinotes-watcher.service`

systemd `--user` unit for always-on operation. Auto-restarts on failure, survives WSL restarts.

### Install

```bash
mkdir -p ~/.config/systemd/user
cp "/mnt/x/Agentic OS/routines/scripts/hinotes-watcher.service" ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now hinotes-watcher
```

### Verify

```bash
systemctl --user status hinotes-watcher
journalctl --user -u hinotes-watcher -f
```

### Stop / restart

```bash
systemctl --user restart hinotes-watcher
systemctl --user stop hinotes-watcher
systemctl --user disable hinotes-watcher
```

### Prerequisites

Systemd `--user` mode requires WSL2 systemd. Verify it's enabled:

```bash
cat /etc/wsl.conf
# Should contain:
#   [boot]
#   systemd=true
```

If not, edit `/etc/wsl.conf` to add the `[boot] systemd=true` block, then `wsl --shutdown` from PowerShell and re-open WSL.
