#!/usr/bin/env bash
# Keep the git tree and the Launchpad PPA in sync.
#
# The default action publishes the version in debian/changelog when the PPA
# does not serve it yet. Nothing is uploaded from a dirty tree or from a branch
# that is not pushed.
#
#   scripts/release.sh              # publish the current version if needed
#   scripts/release.sh --check      # report drift, change nothing (exit 1 on drift)
#   scripts/release.sh --no-wait    # upload and return without waiting
#
# Bump first with: dch -i "what changed", then run this script.
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"
REPO="$(basename "$ROOT")"

PPA_HOST="https://ppa.launchpadcontent.net/toomanypatterns/ubuntu-backup/ubuntu"
DIST="jammy"
DPUT_TARGET="${DPUT_TARGET:-ubuntu-backup}"
SIGN_KEY="${SIGN_KEY:-F104A3F1CE9A23540AEB8F24F31978E49E85DDCA}"
WAIT_MAX="${WAIT_MAX:-5400}"
WAIT_STEP="${WAIT_STEP:-60}"

case "$REPO" in
  QA-Sentinels)
    PKG=qa-sentinels
    VERSION_FILES=(debian/changelog)
    TEST_CMD=(python3 -m unittest discover -s tests)
    ;;
  QA-Dashboard)
    PKG=qa-dashboard
    VERSION_FILES=(debian/changelog package.json package-lock.json README.md)
    TEST_CMD=(node tests/backend.test.js)
    ;;
  Ubuntu-Backup)
    PKG=ubuntu-backup
    VERSION_FILES=(debian/changelog CHANGELOG.md snap/snapcraft.yaml)
    TEST_CMD=(python3 -m unittest discover -s tests)
    ;;
  *)
    echo "unsupported repository: $REPO" >&2
    exit 2
    ;;
esac

MODE=sync
case "${1:-}" in
  --check) MODE=check ;;
  --no-wait) MODE=sync; WAIT_MAX=0 ;;
  "") ;;
  *) echo "usage: $0 [--check|--no-wait]" >&2; exit 2 ;;
esac

version="$(dpkg-parsechangelog -SVersion)"
published="$(curl -fsS --max-time 30 \
  "$PPA_HOST/dists/$DIST/main/binary-amd64/Packages.gz" 2>/dev/null \
  | gunzip -c 2>/dev/null \
  | awk -v p="$PKG" '$1=="Package:"{cur=$2} cur==p && $1=="Version:"{print $2; exit}')"
published="${published:-unknown}"

dirty="$(git status --porcelain | wc -l)"
branch="$(git rev-parse --abbrev-ref HEAD)"
ahead_behind="$(git rev-list --left-right --count "origin/$branch...HEAD" 2>/dev/null || echo '? ?')"

if [[ $MODE == check ]]; then
  echo "repo:       $REPO ($branch)"
  echo "git:        $version"
  echo "ppa:        $published"
  [[ $dirty -eq 0 ]] || echo "dirty:      $dirty file(s) not committed"
  [[ $ahead_behind == "0	0" ]] || echo "unpushed:   $ahead_behind (behind/ahead)"
  missing=0
  for f in "${VERSION_FILES[@]}"; do
    [[ -f $f ]] || continue
    grep -q -- "$version" "$f" || { echo "outdated:   $f does not mention $version"; missing=1; }
  done
  if [[ $version == "$published" && $dirty -eq 0 && $ahead_behind == "0	0" && $missing -eq 0 ]]; then
    echo "status:     in sync"
    exit 0
  fi
  echo "status:     drift"
  exit 1
fi

if [[ $version == "$published" ]]; then
  echo "$PKG $version is already published; nothing to do"
  exit 0
fi
if [[ $dirty -ne 0 ]]; then
  echo "working tree is dirty; commit or stash before releasing" >&2
  git status --short >&2
  exit 1
fi
if [[ $branch != main ]]; then
  echo "release from main, not $branch" >&2
  exit 1
fi
if [[ $ahead_behind != "0	0" ]]; then
  echo "branch is not in sync with origin/$branch: $ahead_behind (behind/ahead)" >&2
  exit 1
fi

echo "testing $PKG $version"
"${TEST_CMD[@]}"

echo "building the source package"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
dpkg-buildpackage -S -sa "-k$SIGN_KEY"

changes="../${PKG}_${version}_source.changes"
[[ -f $changes ]] || { echo "missing $changes" >&2; exit 1; }

echo "uploading $changes to $DPUT_TARGET"
dput "$DPUT_TARGET" "$changes"

if [[ $WAIT_MAX -eq 0 ]]; then
  echo "uploaded; publication not awaited"
  exit 0
fi

echo "waiting for $PKG $version on the PPA (up to $((WAIT_MAX / 60)) min)"
waited=0
while (( waited < WAIT_MAX )); do
  sleep "$WAIT_STEP"
  waited=$((waited + WAIT_STEP))
  now="$(curl -fsS --max-time 30 \
    "$PPA_HOST/dists/$DIST/main/binary-amd64/Packages.gz" 2>/dev/null \
    | gunzip -c 2>/dev/null \
    | awk -v p="$PKG" '$1=="Package:"{cur=$2} cur==p && $1=="Version:"{print $2; exit}')"
  if [[ ${now:-} == "$version" ]]; then
    echo "published: $PKG $version"
    exit 0
  fi
  echo "  ${waited}s: still ${now:-unknown}"
done

echo "upload succeeded but publication did not finish within $((WAIT_MAX / 60)) min" >&2
echo "check: https://launchpad.net/~toomanypatterns/+archive/ubuntu/ubuntu-backup/+packages" >&2
exit 1
