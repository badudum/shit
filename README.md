# shit

Typed the wrong command? Type `shit` right after and pick the fix.

```
$ git bush
git: 'bush' is not a git command. See 'git --help'.
$ shit

Did you mean:
  1) git push
  2) git pull
Type a number to run it, or anything else to cancel: 1
```

Picking `1` runs `git push` in your actual shell, right there.

## How it works

1. `shit` (a shell function) grabs the command you ran right before it from
   shell history - no background hooks, no per-prompt overhead.
2. It re-runs that command in a throwaway subprocess to capture its real
   stdout/stderr (skipped for anything that looks destructive - `rm`, `sudo`,
   `git push --force`, `DELETE`/`POST` curls, etc. - see `DANGEROUS_PATTERNS`
   in `shit_cli.py`).
3. The command text + captured output go to a small model running locally
   via [Ollama](https://ollama.com), which returns up to 3 corrected
   commands as JSON.
4. You pick one by number and it's `eval`'d in your current shell, so `cd`,
   env vars, and aliases all behave normally - it's not run in a subshell.

Everything runs locally. Nothing you type ever leaves your machine.

## Install

One command:

```
git clone https://github.com/badudum/shit.git && cd shit && ./install.sh
```

The installer is entirely user-space - no `sudo`, no root, nothing installed
system-wide. It:
- symlinks `shit_cli.py` to `~/.local/bin/shit-cli`
- adds `~/.local/bin` to `PATH` and sources `shell/integration.sh` in
  `~/.bashrc` and `~/.zshrc`
- installs [Ollama](https://ollama.com) into `~/.local/bin` if it's missing
  (no separate download/setup needed - and it won't touch an existing
  system install if you already have `ollama` on your `PATH`)
- runs it as a `systemd --user` service so it's there after reboots/logins
  without needing root (falls back to a plain background process if your
  system has no user systemd instance)
- pulls a small model (`qwen2.5:0.5b` by default, ~400MB)

Safe to re-run any time - every step is skipped if it's already done.

Restart your shell afterwards (or `source ~/.bashrc`).

## Config

All optional, set as env vars before sourcing (e.g. in `.bashrc`):

| Variable              | Default                   | What it does                          |
|------------------------|----------------------------|----------------------------------------|
| `SHIT_MODEL`           | `qwen2.5:0.5b`             | Ollama model to use                    |
| `SHIT_OLLAMA_URL`      | `http://127.0.0.1:11434`   | Ollama API base URL                    |
| `SHIT_RERUN_TIMEOUT`   | `5`                        | Seconds before giving up re-running the failed command |
| `SHIT_LLM_TIMEOUT`     | `30`                       | Seconds to wait for the model's response |

Want a bit more accuracy and don't mind ~1GB instead of ~400MB?
`SHIT_MODEL=qwen2.5:1.5b ollama pull qwen2.5:1.5b` and set the env var.

## Caveats

- Re-running the failed command is what powers the "read the real error"
  trick, same as tools like `thefuck`. The danger-pattern check is a
  best-effort safety net, not a guarantee - don't rely on it for commands
  with side effects you can't afford to repeat.
- First request after `ollama serve` starts (or after the machine has been
  idle) is slower while the model loads into memory; after that it's fast.
- Only pulls the previous command from shell history, so it won't work as
  the very first command in a fresh shell session.
