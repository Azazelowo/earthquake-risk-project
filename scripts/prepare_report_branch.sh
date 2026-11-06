#!/usr/bin/env bash
# Prepares the daily-reports worktree and computes the retrieval window for one run.
#
# Why a script: these steps used to be prose in the prompt, and the agent rewrote
# them each run — sometimes skipping cleanup or misjudging whether days were missed.
# A script makes them deterministic.
#
# Usage (from any checkout of this repo):
#   git show origin/main:scripts/prepare_report_branch.sh | bash
#   source "${TMPDIR:-/tmp}/daily_report_env.sh"
#
# Writes these variables to the env file (and prints them):
#   WT                 worktree path on branch daily-reports
#   TODAY              report date, Asia/Taipei (YYYY-MM-DD)
#   WINDOW_START_UTC   earthquake retrieval window start (ISO 8601, UTC)
#   WINDOW_END_UTC     window end = now (ISO 8601, UTC)
#   COVERED_UNTIL_UTC  end of the previous run's window (from data/coverage.json)
#   CATCHUP            yes if the previous window ended more than 24 h ago
set -euo pipefail

git fetch -q origin

# Remove a leftover worktree that still holds daily-reports, or the add below fails.
git worktree prune
OLD=$(git worktree list --porcelain | awk '/^worktree /{p=$2} /^branch refs\/heads\/daily-reports$/{print p}')
if [ -n "$OLD" ]; then git worktree remove --force "$OLD"; fi

WT="$(mktemp -d)/wt"
if git ls-remote --exit-code --heads origin daily-reports >/dev/null; then
  git worktree add -q -B daily-reports "$WT" origin/daily-reports
else
  git worktree add -q -b daily-reports "$WT" origin/main
fi
cd "$WT"

# The report branch must not carry its own copy of the prompt (it would go stale).
if [ -d prompts ]; then git rm -r -q prompts; fi

PY=$(command -v python3 || command -v python)
ENV_FILE="${TMPDIR:-/tmp}/daily_report_env.sh"

PYTHONIOENCODING=utf-8 "$PY" -I - "$WT" "$ENV_FILE" <<'PY'
import datetime as dt, json, os, subprocess, sys

wt, env_file = sys.argv[1], sys.argv[2]
utc = dt.timezone.utc
now = dt.datetime.now(utc).replace(microsecond=0)
today = now.astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat()

def parse(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(utc)

# Where the previous run's window ended: data/coverage.json, else the last commit time.
covered = None
cov_path = os.path.join(wt, "data", "coverage.json")
if os.path.exists(cov_path):
    with open(cov_path, encoding="utf-8") as f:
        covered = parse(json.load(f)["last_window_end_utc"])
else:
    out = subprocess.run(["git", "-C", wt, "log", "-1", "--format=%cI"],
                         capture_output=True, text=True).stdout.strip()
    covered = parse(out) if out else now - dt.timedelta(hours=24)

default_start = now - dt.timedelta(hours=24)
earliest = now - dt.timedelta(days=7)
start = max(min(default_start, covered), earliest)
catchup = "yes" if covered < default_start else "no"

fmt = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
values = {
    "WT": wt,
    "TODAY": today,
    "WINDOW_START_UTC": fmt(start),
    "WINDOW_END_UTC": fmt(now),
    "COVERED_UNTIL_UTC": fmt(covered),
    "CATCHUP": catchup,
}
with open(env_file, "w", encoding="utf-8", newline="\n") as f:
    for k, v in values.items():
        f.write(f"export {k}='{v}'\n")
for k, v in values.items():
    print(f"{k}={v}")
print(f"ENV_FILE={env_file}")
PY
