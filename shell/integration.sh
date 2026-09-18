# shit - fix your last broken command with a local LLM.
# Sourced from .bashrc / .zshrc by install.sh. Works in bash and zsh.
#
# Zero overhead when idle: this defines one function and nothing else runs
# on every prompt. `fc -ln -2 -1` is used instead of a DEBUG/preexec hook so
# there's no per-command cost.

shit() {
  # Capture the real exit status of the command run right before `shit`,
  # before anything else in this function touches $?.
  local orig_status=$?

  local raw
  raw="$(fc -ln -2 -1 2>/dev/null)"

  local prev="" line
  while IFS= read -r line; do
    # trim leading/trailing whitespace
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    case "$line" in
      shit|shit\ *) continue ;;  # skip the `shit` invocation itself
      "") continue ;;
    esac
    prev="$line"
  done <<EOF
$raw
EOF

  if [ -z "$prev" ]; then
    echo "shit: no previous command found in history" >&2
    return 1
  fi

  local fixed
  fixed="$(SHIT_PREV_CMD="$prev" SHIT_PREV_EXIT="$orig_status" command shit-cli)"
  local status=$?

  if [ -n "$fixed" ]; then
    # record the fixed command in shell history so up-arrow finds it
    if [ -n "${ZSH_VERSION:-}" ]; then
      print -s -- "$fixed"
    else
      history -s -- "$fixed"
    fi
    eval "$fixed"
    return $?
  fi

  return $status
}
