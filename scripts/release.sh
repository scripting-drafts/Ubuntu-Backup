#!/usr/bin/env bash
# Publish Ubuntu Backup to the Launchpad PPA for every supported Ubuntu series.
#
# The package identity comes from debian/changelog, not the checkout directory.
# Jammy keeps the native version (1.3.31); newer series get a series-suffixed
# version (1.3.31-0ubuntu1~noble1) so pockets cannot collide.
#
#   scripts/release.sh --check
#   scripts/release.sh
#   scripts/release.sh --no-wait
#   scripts/release.sh --series "noble resolute"
#   SERIES="noble" REVISION=2 scripts/release.sh
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

PKG="$(dpkg-parsechangelog -S Source)"
if [[ "$PKG" != ubuntu-backup ]]; then
  echo "unsupported source package: $PKG" >&2
  exit 2
fi

PPA=ubuntu-backup
PPA_BASE="https://ppa.launchpadcontent.net/toomanypatterns"
PPA_HOST="$PPA_BASE/$PPA/ubuntu"
DPUT_TARGET="${DPUT_TARGET:-ubuntu-backup}"
SIGN_KEY="${SIGN_KEY:-F104A3F1CE9A23540AEB8F24F31978E49E85DDCA}"
WAIT_MAX="${WAIT_MAX:-10800}"
WAIT_STEP="${WAIT_STEP:-60}"
REVISION_EXPLICIT=0
if [[ -n "${REVISION:-}" ]]; then REVISION_EXPLICIT=1; fi
REVISION="${REVISION:-1}"
DEFAULT_SERIES=(jammy noble resolute)
if [[ -n "${SERIES:-}" ]]; then
  read -r -a DEFAULT_SERIES <<< "$SERIES"
fi

MODE=sync
while (( $# )); do
  case "$1" in
    --check) MODE=check ;;
    --no-wait) WAIT_MAX=0 ;;
    --series)
      shift
      [[ $# -gt 0 ]] || { echo "--series requires a value" >&2; exit 2; }
      read -r -a DEFAULT_SERIES <<< "$1"
      ;;
    -h|--help)
      sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "usage: $0 [--check|--no-wait] [--series \"SERIES ...\"]" >&2
      exit 2
      ;;
  esac
  shift
done

VERSION="$(dpkg-parsechangelog -S Version)"
BASE_VERSION="${VERSION%%~*}"
BASE_VERSION="${BASE_VERSION%-0ubuntu*}"

VERSION_FILES=(debian/changelog CHANGELOG.md snap/snapcraft.yaml)

series_version() {
  local series="$1"
  if [[ "$series" == jammy ]]; then
    printf '%s\n' "$BASE_VERSION"
  elif [[ "$BASE_VERSION" == *-* ]]; then
    printf '%s+%s\n' "$BASE_VERSION" "$series"
  else
    printf '%s-0ubuntu%s~%s1\n' "$BASE_VERSION" "$REVISION" "$series"
  fi
}

version_matches() {
  local published="$1" series="$2"
  if [[ "$series" == jammy ]]; then
    [[ "$published" == "$BASE_VERSION" ]]
    return
  fi
  if [[ $REVISION_EXPLICIT -eq 1 ]]; then
    [[ "$published" == "$(series_version "$series")" ]]
    return
  fi
  [[ "$published" == "$BASE_VERSION"-0ubuntu*~"$series"1 ]]
}

published_version() {
  local series="$1" tmp attempt
  tmp="$(mktemp)"
  for attempt in 1 2 3 4; do
    if curl -fsS --max-time 30 -o "$tmp" \
        "$PPA_HOST/dists/$series/main/binary-amd64/Packages.gz" 2>/dev/null \
        && gzip -t "$tmp" 2>/dev/null; then
      gzip -dc "$tmp" 2>/dev/null \
        | awk -v p="$PKG" '$1=="Package:"{cur=$2} cur==p && $1=="Version:"{print $2; exit}'
      rm -f "$tmp"
      return
    fi
    sleep 2
  done
  rm -f "$tmp"
}

version_files_ok() {
  local missing=0
  for f in "${VERSION_FILES[@]}"; do
    if [[ -f "$f" ]] && ! grep -q -- "$BASE_VERSION" "$f"; then
      echo "outdated:   $f does not mention $BASE_VERSION" >&2
      missing=1
    fi
  done
  return "$missing"
}

dirty="$(git status --porcelain | wc -l)"
branch="$(git rev-parse --abbrev-ref HEAD)"
ahead_behind="$(git rev-list --left-right --count "origin/$branch...HEAD" 2>/dev/null || echo '? ?')"

if [[ "$MODE" == check ]]; then
  echo "package:    $PKG"
  echo "repo:       $ROOT ($branch)"
  echo "base:       $BASE_VERSION"
  echo "ppa:        ppa:toomanypatterns/$PPA"
  [[ $dirty -eq 0 ]] || echo "dirty:      $dirty file(s) not committed"
  [[ "$ahead_behind" == "0	0" ]] || echo "unpushed:   $ahead_behind (behind/ahead)"
  drift=0
  version_files_ok || drift=1
  for series in "${DEFAULT_SERIES[@]}"; do
    expected="$(series_version "$series")"
    published="$(published_version "$series")"
    if [[ -z "$published" ]]; then
      echo "  $series: no published pocket yet (expected $expected)"
      drift=1
      continue
    fi
    echo "  $series: expected=$expected published=$published"
    version_matches "$published" "$series" || drift=1
  done
  if [[ $dirty -eq 0 && "$ahead_behind" == "0	0" && $drift -eq 0 ]]; then
    echo "status:     in sync"
    exit 0
  fi
  echo "status:     drift"
  exit 1
fi

if [[ $dirty -ne 0 ]]; then
  echo "working tree is dirty; commit or stash before releasing" >&2
  git status --short >&2
  exit 1
fi
if [[ "$branch" != main ]]; then
  echo "release from main, not $branch" >&2
  exit 1
fi
if [[ "$ahead_behind" != "0	0" ]]; then
  echo "branch is not in sync with origin/$branch: $ahead_behind (behind/ahead)" >&2
  exit 1
fi
version_files_ok || exit 1

supported_series="$(ubuntu-distro-info --supported 2>/dev/null || true)"
for series in "${DEFAULT_SERIES[@]}"; do
  if [[ -n "$supported_series" ]] && ! grep -qx "$series" <<< "$supported_series"; then
    echo "series $series is not a supported Ubuntu series" >&2
    exit 2
  fi
done

echo "testing $PKG $BASE_VERSION"
python3 -m unittest discover -s tests

for series in "${DEFAULT_SERIES[@]}"; do
  expected="$(series_version "$series")"
  published="$(published_version "$series")"
  if version_matches "$published" "$series"; then
    echo "$PKG $published is already published for $series"
    continue
  fi

  tmp="$(mktemp -d "${TMPDIR:-/tmp}/ubuntu-backup-release-${series}.XXXXXX")"
  trap 'rm -rf "$tmp"' EXIT

  git archive --format=tar HEAD | tar -x -C "$tmp"

  python3 - "$tmp/debian/changelog" "$expected" "$series" <<'PY'
from pathlib import Path
import re
import sys

path, version, series = sys.argv[1:4]
text = Path(path).read_text(encoding="utf-8")
lines = text.splitlines()
if not lines:
    raise SystemExit(f"empty changelog: {path}")
line = lines[0]
line = re.sub(r"\([^)]*\)", f"({version})", line, count=1)
line = re.sub(r"\)\s+\S+;", f") {series};", line, count=1)
lines[0] = line
Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

  echo "building $PKG $expected for $series"
  (
    cd "$tmp"
    export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
    dpkg-buildpackage -S -sa "-k$SIGN_KEY"
  )

  changes="$(dirname "$tmp")/${PKG}_${expected}_source.changes"
  [[ -f "$changes" ]] || { echo "missing $changes" >&2; exit 1; }

  echo "uploading $changes to $DPUT_TARGET ($series)"
  dput "$DPUT_TARGET" "$changes"

  rm -rf "$tmp"
  trap - EXIT

  if [[ $WAIT_MAX -gt 0 ]]; then
    echo "waiting for $PKG $expected on $series (up to $((WAIT_MAX / 60)) min)"
    waited=0
    while (( waited < WAIT_MAX )); do
      sleep "$WAIT_STEP"
      waited=$((waited + WAIT_STEP))
      now="$(published_version "$series")"
      if [[ "${now:-}" == "$expected" ]]; then
        echo "published: $PKG $expected on $series"
        break
      fi
    done
    if [[ "${now:-}" != "$expected" ]]; then
      echo "not published yet: $PKG $expected on $series" >&2
      exit 1
    fi
  fi
done
