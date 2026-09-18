#!/usr/bin/env python3
"""
shit-cli: the brain behind the `shit` shell command.

Given the previously typed (and presumably failed) shell command, this:
  1. Re-runs it in a subprocess to capture real stdout/stderr (skipped for
     anything that looks destructive - see DANGEROUS_PATTERNS).
  2. Tries two cheap, deterministic tricks first, since they're both faster
     and more reliable than an LLM for the most common case (a typo in the
     program name or a typo'd sub-command):
       a. If the program name itself doesn't resolve to anything real, fuzzy
          match it against every executable actually on $PATH (plus shell
          builtins/aliases/functions) - the same core trick `thefuck` uses.
       b. Look for the tool's own "did you mean" / "most similar command is"
          hint in its error output (git, pip, cargo, apt, etc. all do this)
          and trust it outright.
  3. Shows those deterministic suggestions immediately, while a small local
     Ollama model runs in a background thread for a second opinion - a
     spinner plays until it answers, and any genuinely new suggestion it
     comes back with is appended live to the menu already on screen.
     Pressing a number at any point wins immediately; the model call is
     never waited on.
  4. Prints ONLY the chosen command to stdout so the calling shell function
     can eval it in the current shell (so cd, env vars, aliases etc. behave
     normally, instead of running inside this throwaway python process).

Everything interactive (menu, errors, prompts) goes to stderr on purpose -
stdout is reserved for the final chosen command line, and only that.
"""

import difflib
import json
import os
import re
import select
import shutil
import subprocess
import sys
import termios
import threading
import tty
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("SHIT_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("SHIT_MODEL", "deepseek-coder-v2:16b")
RERUN_TIMEOUT = float(os.environ.get("SHIT_RERUN_TIMEOUT", "5"))
LLM_TIMEOUT = float(os.environ.get("SHIT_LLM_TIMEOUT", "30"))
MAX_OUTPUT_CHARS = 1500

DANGEROUS_PATTERNS = [
    r"\brm\b", r"\bmv\b", r"\bdd\b", r"\bmkfs\b", r"\bshred\b", r"\bwipefs\b",
    r"\bshutdown\b", r"\breboot\b", r"\bpoweroff\b", r"\bhalt\b",
    r"\bkill\b", r"\bkillall\b", r"\bpkill\b",
    r"\bsudo\b", r"\bchmod\b", r"\bchown\b", r"\btruncate\b",
    r"\bmkfs\b", r">\s*/dev/sd", r">\s*/dev/nvme",
    r"drop\s+table", r"delete\s+from",
    r"--force\b", r"\bgit\s+reset\s+--hard\b", r"\bgit\s+clean\s+-[a-z]*f",
    r"-X\s*(POST|PUT|DELETE|PATCH)",
    r"\byarn\s+publish\b", r"\bnpm\s+publish\b", r"\bgit\s+push\b.*(--force|-f\b)",
]
DANGEROUS_RE = re.compile("|".join(DANGEROUS_PATTERNS), re.IGNORECASE)

MAX_SUGGESTIONS = 6
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPIN_INTERVAL = 0.08


def _use_color():
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("SHIT_COLOR") == "always":
        return True
    return sys.stderr.isatty() and os.environ.get("TERM") != "dumb"


COLOR = _use_color()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def bold(text):
    return _c("1", text)


def dim(text):
    return _c("2", text)


def cyan(text):
    return _c("36", text)


def green(text):
    return _c("32", text)


def yellow(text):
    return _c("33", text)


def red(text):
    return _c("31", text)

SYSTEM_PROMPT = (
    "You are a terminal assistant that fixes broken shell commands. "
    "You will be given the command the user typed and the error/output it "
    "produced. Reply with ONLY a JSON object of the form "
    '{"suggestions": ["corrected command 1", "corrected command 2", "corrected command 3"]}. '
    "List 1 to 3 corrected commands ordered from most to least likely. "
    "Each suggestion MUST be the full command line exactly as the user would "
    "type it, including the program name - never just the sub-command or "
    "arguments on their own. "
    'Example: given "Typed command: git bush" with output mentioning the '
    'command "push", respond with {"suggestions": ["git push"]}, NOT '
    '{"suggestions": ["push"]}. '
    "Do not include explanations, markdown fences, or any extra fields."
)


def eprint(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


def get_prev_command():
    cmd = os.environ.get("SHIT_PREV_CMD", "").strip()
    return cmd


def is_dangerous(cmd):
    return bool(DANGEROUS_RE.search(cmd))


def rerun_capture(cmd):
    """Re-run cmd, return (exit_code, output, was_rerun)."""
    if is_dangerous(cmd):
        return None, "(not re-run: command looked potentially destructive; " \
                      "suggestions are based on the command text only)", False
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=RERUN_TIMEOUT,
            text=True,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        return proc.returncode, output, True
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        if not output:
            output = f"(command timed out after {RERUN_TIMEOUT}s with no output)"
        return None, output, True
    except Exception as exc:  # pragma: no cover - defensive
        return None, f"(failed to re-run command: {exc})", False


SHELL_BUILTINS = {
    "cd", "pwd", "echo", "export", "alias", "unalias", "source", ".", "exit",
    "return", "break", "continue", "shift", "test", "[", "[[", "read", "set",
    "unset", "trap", "wait", "jobs", "fg", "bg", "kill", "type", "hash",
    "history", "eval", "exec", "ulimit", "umask", "times", "getopts", "local",
    "declare", "typeset", "readonly", "let", "printf", "pushd", "popd",
    "dirs", "suspend", "command", "builtin", "enable", "complete", "compgen",
    "shopt", "bind", "if", "then", "else", "elif", "fi", "for", "while",
    "until", "do", "done", "case", "esac", "function", "select", "time",
    "coproc", "in",
}


# The handful of transposition/muscle-memory typos so common they're worth
# hardcoding outright rather than leaving to edit-distance ranking - plain
# Levenshtein distance actually ranks unrelated 2-letter commands (sh, su,
# nl...) *above* "ls" for the input "sl", since a transposition costs 2
# substitutions but only 1 in true typo-space. `thefuck` special-cases these
# same handful of classics for the same reason.
COMMON_TYPOS = {
    "sl": "ls", "s": "ls",
    "gerp": "grep", "grpe": "grep", "gpr": "grep",
    "got": "git", "gi": "git", "gti": "git", "igt": "git",
    "pdw": "pwd", "pwe": "pwd",
    "claer": "clear", "clera": "clear", "cls": "clear",
    "mkdri": "mkdir", "mkidr": "mkdir",
    "vmi": "vim",
    "phtyon": "python", "pyhton": "python", "pytohn": "python",
    "amke": "make", "mkae": "make",
    "touhc": "touch",
    "hsitory": "history",
}


def common_typo_suggestion(cmd):
    tokens = cmd.split()
    if not tokens:
        return None
    fix = COMMON_TYPOS.get(tokens[0])
    if fix is None:
        return None
    return " ".join([fix] + tokens[1:])


def path_executables():
    """Every executable name found on $PATH - the ground truth for 'is this
    actually a real command', independent of what an LLM thinks exists."""
    names = set()
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.is_file(follow_symlinks=True) and os.access(entry.path, os.X_OK):
                            names.add(entry.name)
                    except OSError:
                        continue
        except OSError:
            continue
    return names


def shell_known_commands():
    """Aliases/functions the shell wrapper exported for us, if any."""
    raw = os.environ.get("SHIT_KNOWN_CMDS", "")
    return {c for c in raw.split("\n") if c.strip()}


def is_real_command(word):
    return (
        shutil.which(word) is not None
        or word in SHELL_BUILTINS
        or word in shell_known_commands()
    )


def history_rank():
    """Commands this user actually runs, most-frequent first (the shell
    wrapper exports this from `history`). Plain edit distance can't tell
    "yay" and "cat" apart for the input "yat" - they're both 1 edit away -
    but this user's own history can: whichever one they actually run wins
    the tie. Returns {command: rank}, lower rank = used more often.
    """
    raw = os.environ.get("SHIT_HISTORY_CMDS", "")
    return {c: i for i, c in enumerate(x for x in raw.split("\n") if x.strip())}


def levenshtein(a, b):
    """Classic edit distance. Ranks "yat"->"yay" (1 substitution) above
    "yat"->"yat2m" (2 insertions), which is the behavior a typo-corrector
    actually wants - difflib's block-matching ratio() gets this backwards
    for suffix insertions."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,          # deletion
                cur[j - 1] + 1,       # insertion
                prev[j - 1] + (ca != cb),  # substitution
            )
        prev = cur
    return prev[-1]


def unknown_command_suggestions(cmd, limit=3):
    """If the program name itself doesn't exist, fuzzy-match it against real
    commands on this machine (by edit distance) and swap it in, keeping the
    rest of the args. e.g. "yat -Syu" -> "yay -Syu" when `yay` is installed
    but `yat` isn't - the same core trick `thefuck` uses.
    """
    tokens = cmd.split()
    if not tokens:
        return []
    base = tokens[0]
    if is_real_command(base):
        return []
    candidates = path_executables() | SHELL_BUILTINS | shell_known_commands()
    max_dist = max(2, len(base) // 2)
    hist = history_rank()
    no_history = len(hist)  # sorts after every command actually seen in history
    scored = sorted(
        (
            (levenshtein(base, c), hist.get(c, no_history), len(c), c)
            for c in candidates
            if c != base and abs(len(c) - len(base)) <= max_dist
        ),
    )
    matches = [c for dist, _, _, c in scored if dist <= max_dist][:limit]
    return [" ".join([m] + tokens[1:]) for m in matches]


HINT_RE = re.compile(
    r"(?:did you mean|most similar command is|maybe you meant|perhaps you meant)"
    r"[:\s]*\n?\s*['\"]?([A-Za-z0-9_.:/@-]+)['\"]?",
    re.IGNORECASE,
)


def hint_suggestion(cmd, output):
    """Many CLIs (git, pip, cargo, apt...) print their own correction
    straight into the error output. That beats guessing - use it directly.
    """
    m = HINT_RE.search(output or "")
    if not m:
        return None
    hint = m.group(1).strip()
    tokens = cmd.split()
    if not tokens or not hint:
        return None
    best_idx, best_ratio = None, 0.0
    for i, t in enumerate(tokens):
        ratio = difflib.SequenceMatcher(None, t, hint).ratio()
        if ratio > best_ratio:
            best_ratio, best_idx = ratio, i
    if best_idx is None or best_ratio < 0.4:
        return None
    new_tokens = tokens[:]
    new_tokens[best_idx] = hint
    return " ".join(new_tokens)


def build_prompt(cmd, exit_code, output):
    output = output.strip()
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[-MAX_OUTPUT_CHARS:]
    code_str = "unknown" if exit_code is None else str(exit_code)
    return (
        f"Typed command: {cmd}\n"
        f"Exit code: {code_str}\n"
        f"Output:\n{output}\n\n"
        "What should the user run instead?"
    )


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 3,
        },
    },
    "required": ["suggestions"],
}


def ask_ollama(prompt, quiet=False):
    """quiet=True suppresses the error eprints - used when this runs on a
    background thread while the main thread is mid-animation, since writing
    to stderr from both at once would corrupt the live-redrawn menu."""
    def err(*lines):
        if not quiet:
            for line in lines:
                eprint(line)

    body = json.dumps({
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "options": {"temperature": 0.2, "num_predict": 320},
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "ignore")
        if exc.code == 404 or "not found" in detail.lower():
            err(red(f"shit: model '{MODEL}' isn't pulled yet."), dim(f"       run: ollama pull {MODEL}"))
        else:
            err(red(f"shit: Ollama returned an error ({exc.code}): {detail}"))
        return None
    except urllib.error.URLError as exc:
        err(
            red("shit: can't reach Ollama at " + OLLAMA_URL),
            dim("      start it with `ollama serve` (or open the Ollama app) and try again."),
            dim(f"      ({exc.reason})"),
        )
        return None
    except Exception as exc:  # pragma: no cover - defensive
        err(red(f"shit: unexpected error talking to Ollama: {exc}"))
        return None

    raw = payload.get("response", "")
    try:
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions", [])
    except (json.JSONDecodeError, AttributeError):
        err(red("shit: model returned something that wasn't valid JSON, giving up."))
        return None

    # Be defensive: a small model can still ignore the schema and hand back
    # a bare string instead of a list - iterating a str yields characters,
    # so explicitly reject anything that isn't a real list first.
    if isinstance(suggestions, str):
        suggestions = [suggestions]
    elif not isinstance(suggestions, list):
        suggestions = []

    suggestions = [s.strip() for s in suggestions if isinstance(s, str) and s.strip()]
    return suggestions[:3]


def repair_suggestion(orig_cmd, suggestion):
    """Small models sometimes drop the program name and return just the
    corrected sub-command/args (e.g. "push" instead of "git push"). If the
    suggestion's first word isn't a real, runnable command but the
    original's is, assume it got truncated and re-prepend the program name.
    """
    orig_tokens = orig_cmd.split()
    sug_tokens = suggestion.split()
    if not orig_tokens or not sug_tokens:
        return suggestion
    orig_first, sug_first = orig_tokens[0], sug_tokens[0]
    if sug_first == orig_first:
        return suggestion
    if shutil.which(sug_first) is None and shutil.which(orig_first) is not None:
        return f"{orig_first} {suggestion}"
    return suggestion


def read_key():
    """Read a single raw keypress with no Enter required. Falls back to
    line-buffered input when stdin isn't a real tty (e.g. piped input in
    tests), since raw mode needs an actual terminal device."""
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        return line[0] if line else None
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return ch if ch else None


def prompt_choice_static(suggestions):
    """Non-live fallback for when stdin/stderr isn't a real tty (piped
    input, tests): show the menu once, no spinner, no background merging."""
    eprint("\n" + bold("Did you mean:"))
    for i, s in enumerate(suggestions, 1):
        eprint(f"  {bold(cyan(str(i)))}) {green(s)}")
    eprint(dim("Press a number to run it, Ctrl+C to cancel: "), end="")
    sys.stderr.flush()

    while True:
        try:
            ch = read_key()
        except KeyboardInterrupt:
            return None
        if ch is None or ch in ("\x03", "\x04"):  # EOF / Ctrl+C / Ctrl+D
            return None
        if ch.isdigit():
            idx = int(ch)
            if 1 <= idx <= len(suggestions):
                eprint(bold(cyan(ch)))  # echo back - raw mode has echo off
                return suggestions[idx - 1]
        # anything else: ignore and keep waiting for a valid digit


def start_llm_thread(prompt, cmd, quiet):
    """Kicks off the LLM call in a daemon thread so it never blocks process
    exit - if the user picks a suggestion before it finishes, main() just
    returns and the OS reclaims the still-running request underneath it.
    quiet must be True whenever a live animated menu might be on screen,
    since the background thread's error eprints would otherwise corrupt it;
    the non-tty fallback path can afford to show them for debuggability.
    """
    state = {"suggestions": None, "done": threading.Event()}

    def worker():
        try:
            raw = ask_ollama(prompt, quiet=quiet) or []
            state["suggestions"] = [repair_suggestion(cmd, s) for s in raw]
        except Exception:  # pragma: no cover - defensive
            state["suggestions"] = []
        finally:
            state["done"].set()

    threading.Thread(target=worker, daemon=True).start()
    return state


def _render_menu(suggestions, spinner_frame):
    """Redraws the whole menu block from scratch and returns how many lines
    it took, so the next call knows how far to move the cursor back up."""
    lines = []
    if suggestions:
        lines.append(bold("Did you mean:"))
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  {bold(cyan(str(i)))}) {green(s)}")
    if spinner_frame is not None:
        label = "looking for more suggestions..." if suggestions else "thinking..."
        lines.append(dim(f"{spinner_frame} {label}"))
    if suggestions:
        lines.append(dim("Press a number to run it, Ctrl+C to cancel: "))
    for line in lines:
        eprint("\r\033[2K" + line)
    return len(lines)


def prompt_choice_live(suggestions, llm_state):
    """Interactive tty version: `suggestions` (a list, mutated in place) is
    shown immediately and a spinner animates until `llm_state['done']`
    fires, at which point any new, non-duplicate suggestions the background
    LLM call produced are inserted live into the same menu. A digit
    keypress at any moment wins immediately without waiting for the model.
    """
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    prev_lines = 0
    merged = False
    try:
        tty.setcbreak(fd)
        spin_i = 0
        while True:
            done = llm_state["done"].is_set()

            if done and not merged:
                merged = True
                for s in llm_state["suggestions"] or []:
                    if s not in suggestions and len(suggestions) < MAX_SUGGESTIONS:
                        suggestions.append(s)
                if not suggestions:
                    if prev_lines:
                        eprint(f"\033[{prev_lines}A", end="")
                        for _ in range(prev_lines):
                            eprint("\r\033[2K")
                        eprint(f"\033[{prev_lines}A", end="")
                    eprint(red("shit: no suggestions available."))
                    return None

            spinner_frame = None if done else SPINNER_FRAMES[spin_i % len(SPINNER_FRAMES)]
            if prev_lines:
                eprint(f"\033[{prev_lines}A", end="")
            prev_lines = _render_menu(suggestions, spinner_frame)
            sys.stderr.flush()

            r, _, _ = select.select([fd], [], [], SPIN_INTERVAL)
            if r:
                ch = os.read(fd, 1).decode(errors="ignore")
                if ch in ("\x03", "\x04", ""):  # Ctrl+C / Ctrl+D / EOF
                    return None
                if ch.isdigit():
                    idx = int(ch)
                    if 1 <= idx <= len(suggestions):
                        eprint(bold(cyan(ch)))
                        return suggestions[idx - 1]
                continue
            spin_i += 1
    except KeyboardInterrupt:
        return None
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def main():
    cmd = get_prev_command()
    if not cmd:
        eprint(red("shit: couldn't figure out what your previous command was."))
        return 1

    exit_code, output, was_rerun = rerun_capture(cmd)
    if not was_rerun:
        orig_exit = os.environ.get("SHIT_PREV_EXIT", "").strip()
        if orig_exit.isdigit():
            exit_code = int(orig_exit)

    suggestions = []

    def add(item):
        if item and item not in suggestions and len(suggestions) < MAX_SUGGESTIONS:
            suggestions.append(item)

    add(common_typo_suggestion(cmd))
    # highest confidence first: the tool told us directly what it meant
    add(hint_suggestion(cmd, output))
    for s in unknown_command_suggestions(cmd):
        add(s)

    is_tty = sys.stdin.isatty() and sys.stderr.isatty()

    # Always give the LLM a shot at a second opinion - even when the
    # deterministic tricks already found something, it may come back with a
    # genuinely different, better answer. It runs concurrently rather than
    # blocking, so it costs nothing but a spinner.
    if len(suggestions) < MAX_SUGGESTIONS:
        llm_state = start_llm_thread(build_prompt(cmd, exit_code, output), cmd, quiet=is_tty)
    else:
        llm_state = {"suggestions": [], "done": threading.Event()}
        llm_state["done"].set()

    if is_tty:
        choice = prompt_choice_live(suggestions, llm_state)
    else:
        # no live menu to animate here, so there's nothing to gain by
        # waiting on the model when the deterministic tricks already have
        # an answer - only block on it when there's nothing else to show
        if not suggestions:
            llm_state["done"].wait(LLM_TIMEOUT + 2)
            for s in llm_state["suggestions"] or []:
                add(s)
        if not suggestions:
            eprint(red("shit: no suggestions available."))
            return 1
        choice = prompt_choice_static(suggestions)

    if choice:
        print(choice)
        return 0

    eprint(yellow("\nshit: cancelled."))
    return 1


if __name__ == "__main__":
    sys.exit(main())
