# shit

Typed the wrong command? Type `shit` right after and pick the fix.

```
$ git bush
git: 'bush' is not a git command. See 'git --help'.
$ shit

Did you mean:
  1) git push
  2) git pull
Press a number to run it, Ctrl+C to cancel: 1
```

Press `1` - no Enter needed - and `git push` runs in your actual shell,
right there. Ctrl+C cancels.

## How it works

1. `shit` (a shell function) grabs the command you ran right before it from
   shell history - no background hooks, no per-prompt overhead.
2. It re-runs that command in a throwaway subprocess to capture its real
   stdout/stderr (skipped for anything that looks destructive - `rm`, `sudo`,
   `git push --force`, `DELETE`/`POST` curls, etc. - see `DANGEROUS_PATTERNS`
   in `shit_cli.py`).
3. Three cheap, deterministic tricks run first and show up on screen
   near-instantly, before any model is involved:
   - a short table of classic transposition typos (`sl` -> `ls`, `gerp` ->
     `grep`, ...)
   - if the program name doesn't resolve to anything real, fuzzy-matching
     it (by edit distance) against every executable actually on your
     `$PATH`, plus your shell aliases/functions - the same core trick
     [`thefuck`](https://github.com/nvbn/thefuck) uses - tie-broken by your
     own command history, so among equally-close matches it picks the one
     you actually use
   - parsing "did you mean"/"most similar command is" hints straight out of
     the failing tool's own error output (git, pip, cargo, apt, ... all
     self-report this already)
4. At the same time, a small model running locally via
   [Ollama](https://ollama.com) is asked for a second opinion on a
   background thread - it never blocks the menu you already see. A spinner
   plays while it's thinking, and if it comes back with a suggestion that
   isn't already on screen, that option is appended live, right where
   you're looking.
5. Pressing a number - no Enter needed - picks that option immediately,
   whether the model has answered yet or not; it's never waited on. The
   chosen command is `eval`'d in your current shell, so `cd`, env vars, and
   aliases all behave normally - it's not run in a subshell. Ctrl+C cancels
   at any point.

For the common case (a typo'd command) steps 1-3 alone are usually enough
that you'll pick an option before the model even answers - grounded in
what's actually installed on your machine rather than a small model's
training-data guesses, and free to ignore if it's wrong.

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
- pulls a small model for the LLM fallback (`qwen2.5-coder:3b` by default,
  ~1.9GB)

Safe to re-run any time - every step is skipped if it's already done.

Restart your shell afterwards (or `source ~/.bashrc`).

## Config

All optional, set as env vars before sourcing (e.g. in `.bashrc`):

| Variable              | Default                   | What it does                          |
|------------------------|----------------------------|----------------------------------------|
| `SHIT_MODEL`           | `qwen2.5-coder:3b`         | Ollama model to use for the LLM fallback |
| `SHIT_OLLAMA_URL`      | `http://127.0.0.1:11434`   | Ollama API base URL                    |
| `SHIT_RERUN_TIMEOUT`   | `5`                        | Seconds before giving up re-running the failed command |
| `SHIT_LLM_TIMEOUT`     | `30`                       | Seconds to wait for the model's response |

The model always runs in the background, but since it never blocks the
menu (see above), it's worth spending a bigger, slower-but-smarter model
there rather than optimizing purely for speed - you'll rarely be staring
at the spinner waiting on it. On this tradeoff:
- `qwen2.5-coder:1.5b` (~1GB) - if you want something lighter/faster and
  can live with a lower ceiling on complex commands.
- `qwen2.5-coder:3b` (~1.9GB, default) - clearly outperforms smaller models
  on longer, multi-flag commands and subtle mistakes (e.g. correctly fixing
  a missing `find ... -exec ... +` terminator or a malformed
  `-H "Authorization Bear TOKEN"` curl header) while still running in a
  few seconds on a modern CPU.
- `qwen2.5-coder:7b` (~4.7GB) - best accuracy if you don't mind ~5-10s+ per
  LLM call (CPU-only) and have the RAM to spare.

To switch: `ollama pull <model>` then `export SHIT_MODEL=<model>` in your
shell rc file (after the `source .../shell/integration.sh` line).

## Caveats

- Re-running the failed command is what powers the "read the real error"
  trick, same as tools like `thefuck`. The danger-pattern check is a
  best-effort safety net, not a guarantee - don't rely on it for commands
  with side effects you can't afford to repeat.
- First request after `ollama serve` starts (or after the machine has been
  idle) is slower while the model loads into memory; after that it's fast.
- Only pulls the previous command from shell history, so it won't work as
  the very first command in a fresh shell session.
- The live spinner/menu needs a real terminal (both stdin and stderr as
  ttys). Piped/non-interactive input (scripts, tests) falls back to a
  static menu that only waits on the model if the deterministic tricks
  found nothing at all to show yet.
