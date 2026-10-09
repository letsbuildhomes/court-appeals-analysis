#!/usr/bin/env bash
# Copy saved research work to a checkout of the same commit over SSH.
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: bash scripts/sync-pipeline-data.sh [--apply --writers-stopped] [--resume] HOST /absolute/checkout

Default: preview transfers without writing remote data.
--apply           Copy in/, data/, and out/, then verify contents by checksum.
--writers-stopped  Confirm data writers are stopped on BOTH computers.
--resume          Allow an existing data destination from this same migration.

Clone/check out the same Git commit on the destination first. Use an SSH config
alias for nonstandard ports, identity files, or jump hosts. Destination paths
must contain only letters, digits, /, _, ., and - (no spaces or shell syntax).
Nothing is deleted. .env, source code, .git, and runtime caches are not copied.
EOF
}

fail() { printf '%s\n' "$*" >&2; exit 1; }
apply=0
stopped=0
resume=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) apply=1; shift ;;
    --writers-stopped) stopped=1; shift ;;
    --resume) resume=1; shift ;;
    --help|-h) usage; exit 0 ;;
    --*) usage >&2; fail "Unknown option: $1" ;;
    *) break ;;
  esac
done
[[ $# -eq 2 ]] || { usage >&2; exit 1; }
host=$1
destination=${2%/}
[[ "$host" =~ ^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$ ]] || fail 'Use an SSH hostname or config alias.'
[[ "$destination" =~ ^/[a-zA-Z0-9_./-]+$ ]] || fail 'Use a simple absolute destination path, without spaces.'
[[ "$destination" != / && "$destination" != *'/../'* && "$destination" != */.. ]] || fail 'Unsafe destination path.'
[[ "$apply" -eq 0 || "$stopped" -eq 1 ]] || fail 'Stop writers on both hosts, then supply --writers-stopped.'

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
cd -- "$root"
for command in git ssh rsync python3; do
  command -v "$command" >/dev/null || fail "Missing command: $command"
done
revision=$(git rev-parse HEAD)
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || fail 'Commit or resolve source changes before syncing data.'
for directory in in data out; do
  [[ -d "$directory" && ! -L "$directory" ]] || fail "Missing directory or symlink: $directory"
done

# Validated host/path values above are safe for both SSH and legacy macOS rsync.
remote_revision=$(ssh -o BatchMode=yes "$host" "git -C '$destination' rev-parse HEAD")
[[ "$remote_revision" == "$revision" ]] || fail "Destination commit differs: $remote_revision (expected $revision)."

printf 'Source: %s\nDestination: %s:%s\nCommit: %s\n' "$root" "$host" "$destination" "$revision"
if [[ "$apply" -eq 1 ]]; then
  ssh -o BatchMode=yes "$host" "sh -s -- '$destination' '$resume'" <<'REMOTE'
set -eu
cd "$1"
if [ -n "$(git status --porcelain --untracked-files=normal)" ]; then
  echo 'Destination source changes must be committed or resolved first.' >&2
  exit 1
fi
for directory in in data out; do
  if [ -L "$directory" ]; then
    echo "Refusing symlink destination: $directory" >&2
    exit 1
  fi
  if [ "$2" != 1 ] && [ -d "$directory" ] && [ -n "$(ls -A "$directory")" ]; then
    echo "Destination $directory is not empty. Use a fresh checkout, or --resume for this migration." >&2
    exit 1
  fi
done
REMOTE
  # Legacy macOS rsync truncates subsecond timestamps. Capture them before
  # copying and restore them with Python's standard library on the destination.
  timestamp_file=$(mktemp)
  trap 'rm -f -- "$timestamp_file"' EXIT
  python3 - >"$timestamp_file" <<'PY'
import json
import os
from pathlib import Path
for directory in ("in", "data", "out"):
    for parent, dirs, files in os.walk(directory):
        for name in files:
            path = Path(parent) / name
            if path.is_symlink() or not path.is_file():
                continue
            stat = path.stat()
            print(json.dumps([str(path), stat.st_mtime_ns]))
PY
fi

# Preserve modification times: checkpoint readers use them to choose results.
# Checksum comparison also catches preexisting files with equal size/timestamp.
options=(-a --checksum --partial --itemize-changes --stats -e 'ssh -o BatchMode=yes')
if [[ "$apply" -eq 0 ]]; then
  printf 'Preview only. To copy, repeat with --apply --writers-stopped.\n'
  options+=(--dry-run)
fi
rsync "${options[@]}" in data out "$host:$destination/"

if [[ "$apply" -eq 1 ]]; then
  ssh -o BatchMode=yes "$host" "python3 -c '
import json, os, sys
from pathlib import Path
root = Path(sys.argv[1]).resolve()
for line in sys.stdin:
    relative, mtime = json.loads(line)
    path = root / relative
    if path.is_symlink() or root not in path.resolve().parents:
        raise ValueError(\"Unsafe timestamp target\")
    os.utime(path, ns=(path.stat().st_atime_ns, mtime))
    if path.stat().st_mtime_ns != mtime:
        raise ValueError(\"Destination filesystem did not preserve timestamp precision\")
' '$destination'" <"$timestamp_file"
  differences=$(rsync -a --checksum --dry-run --itemize-changes -e 'ssh -o BatchMode=yes' in data out "$host:$destination/")
  [[ -z "$differences" ]] || { printf '%s\n' "$differences" >&2; fail 'Verification found differences. Keep writers stopped and repeat with --resume.'; }
  printf 'Verified: all source data files match by checksum and copied metadata.\n'
  printf 'Destination-only files are retained; keep the source until application checks pass.\n'
fi
