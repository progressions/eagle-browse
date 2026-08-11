# Shared auto-upgrade for Eagle Browse launchers.
# Source from a launcher after ROOT is set:
#   # shellcheck source=self_update.sh
#   source "$ROOT/self_update.sh"
#   eagle_browse_self_update "$0" "$@"
#
# Behaviour:
#   - git fetch origin (short timeout; offline = skip, no hang)
#   - if origin is ahead and the tracked tree is clean → ff-only pull, re-exec launcher
#   - dirty tracked files → print a notice, keep running the local checkout
#   - skip if EAGLE_BROWSE_NO_UPDATE=1, or --no-update is on the command line
#   - skip a second fetch after a successful re-exec (EAGLE_BROWSE_JUST_UPDATED=1)
#
# Safe defaults: never force-push-pull, never stash, never touch untracked files.

eagle_browse_self_update() {
  local launcher="$1"
  shift || true

  # Already updated in this start chain.
  if [[ -n "${EAGLE_BROWSE_JUST_UPDATED:-}" ]]; then
    return 0
  fi

  # Explicit opt-out.
  if [[ -n "${EAGLE_BROWSE_NO_UPDATE:-}" ]]; then
    return 0
  fi
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "--no-update" ]]; then
      return 0
    fi
  done

  # Need a git checkout with a remote.
  if [[ ! -d "$ROOT/.git" ]] || ! command -v git >/dev/null 2>&1; then
    return 0
  fi

  # Serialize concurrent starts (phone-browse + inbox-watch at login).
  # Release the lock on every function return so the long-lived python process
  # does not hold it for hours.
  local lock="$ROOT/.update.lock"
  exec 9>"$lock" || return 0
  if ! flock -n 9; then
    exec 9>&- 2>/dev/null || true
    return 0
  fi
  # shellcheck disable=SC2064
  trap 'exec 9>&- 2>/dev/null || true' RETURN

  # Fetch with a hard timeout so offline boot does not hang systemd/GUI start.
  # GIT_TERMINAL_PROMPT=0 avoids credential prompts on private repos without a helper.
  local fetch_err fetch_ec=0
  fetch_err="$(
    cd "$ROOT" &&
      GIT_TERMINAL_PROMPT=0 \
        timeout 8 git fetch --quiet origin 2>&1
  )" || fetch_ec=$?
  if [[ $fetch_ec -ne 0 ]]; then
    # 124 = timeout(1); anything else = network/auth/offline. Stay quiet unless verbose.
    if [[ -n "${EAGLE_BROWSE_UPDATE_VERBOSE:-}" ]]; then
      if [[ $fetch_ec -eq 124 ]]; then
        echo "eagle-browse: update check timed out (offline?)" >&2
      elif [[ -n "$fetch_err" ]]; then
        echo "eagle-browse: update check skipped ($fetch_err)" >&2
      fi
    fi
    return 0
  fi

  local branch upstream local_sha remote_sha base
  branch="$(cd "$ROOT" && git rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 0
  # Prefer configured upstream; fall back to origin/<branch>.
  if ! upstream="$(cd "$ROOT" && git rev-parse --abbrev-ref '@{u}' 2>/dev/null)"; then
    upstream="origin/${branch}"
  fi
  if ! remote_sha="$(cd "$ROOT" && git rev-parse --verify "${upstream}" 2>/dev/null)"; then
    return 0
  fi
  local_sha="$(cd "$ROOT" && git rev-parse HEAD)"
  if [[ "$local_sha" == "$remote_sha" ]]; then
    return 0
  fi

  base="$(cd "$ROOT" && git merge-base "$local_sha" "$remote_sha" 2>/dev/null)" || return 0

  if [[ "$local_sha" == "$base" && "$remote_sha" != "$base" ]]; then
    # Remote is strictly ahead.
    # Only pull when tracked files are clean — never clobber local work.
    if ! (cd "$ROOT" && git diff --quiet && git diff --cached --quiet); then
      echo "eagle-browse: remote has updates, but this checkout has local changes — not pulling." >&2
      echo "eagle-browse: commit/stash, or run: git -C $ROOT pull --ff-only" >&2
      return 0
    fi

    local count msg
    count="$(cd "$ROOT" && git rev-list --count "${local_sha}..${remote_sha}" 2>/dev/null || echo "?")"
    msg="$(cd "$ROOT" && git log --oneline -1 "$remote_sha" 2>/dev/null || true)"
    echo "eagle-browse: updating (${count} commit(s) behind ${upstream})…" >&2
    if [[ -n "$msg" ]]; then
      echo "eagle-browse: latest: $msg" >&2
    fi

    if ! (cd "$ROOT" && git pull --ff-only --quiet origin "$branch"); then
      echo "eagle-browse: pull failed — continuing with current code." >&2
      return 0
    fi

    echo "eagle-browse: updated to $(cd "$ROOT" && git rev-parse --short HEAD). Restarting…" >&2
    # Drop lock + trap before replacing the process.
    trap - RETURN
    exec 9>&- 2>/dev/null || true
    export EAGLE_BROWSE_JUST_UPDATED=1
    # Re-exec the original launcher so the new tree is what runs.
    exec "$launcher" "$@"
  elif [[ "$remote_sha" == "$base" && "$local_sha" != "$base" ]]; then
    # Local is ahead of origin — fine for a dev machine.
    if [[ -n "${EAGLE_BROWSE_UPDATE_VERBOSE:-}" ]]; then
      echo "eagle-browse: local is ahead of ${upstream} (not pulling)." >&2
    fi
    return 0
  else
    # Diverged.
    echo "eagle-browse: local and ${upstream} have diverged — not auto-updating." >&2
    echo "eagle-browse: resolve with git -C $ROOT pull (or push), then restart." >&2
    return 0
  fi
}

# Strip --no-update from a copy of argv for the python process.
# Usage: eagle_browse_filter_args "$@"; then use "${EAGLE_BROWSE_ARGS[@]}"
eagle_browse_filter_args() {
  EAGLE_BROWSE_ARGS=()
  local arg
  for arg in "$@"; do
    if [[ "$arg" != "--no-update" ]]; then
      EAGLE_BROWSE_ARGS+=("$arg")
    fi
  done
}
