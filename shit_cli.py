#!/usr/bin/env python3
"""
shit-cli: the brain behind the `shit` shell command.

Given the previously typed (and presumably failed) shell command, this:
  1. Re-runs it in a subprocess to capture real stdout/stderr (skipped for
     anything that looks destructive - see DANGEROUS_PATTERNS).
  2. Asks a small local Ollama model for up to 3 corrected commands.
  3. Shows a numbered menu on stderr and reads a choice from stdin.
  4. Prints ONLY the chosen command to stdout so the calling shell function
     can eval it in the current shell (so cd, env vars, aliases etc. behave
     normally, instead of running inside this throwaway python process).

Everything interactive (menu, errors, prompts) goes to stderr on purpose -
stdout is reserved for the final chosen command line, and only that.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

OLLAMA_URL = os.environ.get("SHIT_OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("SHIT_MODEL", "qwen2.5:0.5b")
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


def ask_ollama(prompt):
    body = json.dumps({
        "model": MODEL,
        "system": SYSTEM_PROMPT,
        "prompt": prompt,
        "stream": False,
        "format": RESPONSE_SCHEMA,
        "options": {"temperature": 0.2, "num_predict": 200},
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
            eprint(f"shit: model '{MODEL}' isn't pulled yet.")
            eprint(f"       run: ollama pull {MODEL}")
        else:
            eprint(f"shit: Ollama returned an error ({exc.code}): {detail}")
        return None
    except urllib.error.URLError as exc:
        eprint("shit: can't reach Ollama at " + OLLAMA_URL)
        eprint("      start it with `ollama serve` (or open the Ollama app) and try again.")
        eprint(f"      ({exc.reason})")
        return None
    except Exception as exc:  # pragma: no cover - defensive
        eprint(f"shit: unexpected error talking to Ollama: {exc}")
        return None

    raw = payload.get("response", "")
    try:
        parsed = json.loads(raw)
        suggestions = parsed.get("suggestions", [])
    except (json.JSONDecodeError, AttributeError):
        eprint("shit: model returned something that wasn't valid JSON, giving up.")
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


def prompt_choice(suggestions):
    eprint("\nDid you mean:")
    for i, s in enumerate(suggestions, 1):
        eprint(f"  {i}) {s}")
    eprint("Type a number to run it, or anything else to cancel: ", end="")
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except KeyboardInterrupt:
        return None
    if not line:
        return None
    line = line.strip()
    if line.isdigit():
        idx = int(line)
        if 1 <= idx <= len(suggestions):
            return suggestions[idx - 1]
    return None


def main():
    cmd = get_prev_command()
    if not cmd:
        eprint("shit: couldn't figure out what your previous command was.")
        return 1

    exit_code, output, was_rerun = rerun_capture(cmd)
    if not was_rerun:
        orig_exit = os.environ.get("SHIT_PREV_EXIT", "").strip()
        if orig_exit.isdigit():
            exit_code = int(orig_exit)
    prompt = build_prompt(cmd, exit_code, output)
    suggestions = ask_ollama(prompt)

    if not suggestions:
        eprint("shit: no suggestions available.")
        return 1

    suggestions = [repair_suggestion(cmd, s) for s in suggestions]

    choice = prompt_choice(suggestions)
    if choice:
        print(choice)
        return 0

    eprint("shit: cancelled.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
