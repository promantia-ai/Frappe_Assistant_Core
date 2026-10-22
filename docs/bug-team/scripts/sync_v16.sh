#!/bin/bash
# sync_v16.sh: puts the v16 bench's FAC checkout on exactly the commit the v15 checkout is on,
# including uncommitted changes, so both sites test the same code. Migrates the v16 site when a
# DocType or patch changed. Never edits code: it copies what the v15 checkout has.

SRC=/home/madesh/FAC/apps/frappe_assistant_core
DST=/home/madesh/FAC16/apps/frappe_assistant_core
cd $DST || exit 1

git remote get-url local >/dev/null 2>&1 || git remote add local $SRC
BR=$(git -C $SRC branch --show-current)
HEAD_SRC=$(git -C $SRC rev-parse HEAD)
BEFORE=$(git rev-parse HEAD)

git fetch -q local
git reset -q --hard
git clean -qfd -e node_modules
git checkout -q -B "$BR" "$HEAD_SRC"

# Uncommitted work in the v15 checkout (the developer's change before it is committed)
if ! git -C $SRC diff --quiet HEAD || [ -n "$(git -C $SRC ls-files --others --exclude-standard)" ]; then
  git -C $SRC diff HEAD --binary | git apply --whitespace=nowarn - 2>/dev/null
  git -C $SRC ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do
    mkdir -p "$(dirname "$f")"; cp "$SRC/$f" "$f"
  done
  DIRTY=" + uncommitted changes"
fi

echo "v16 FAC synced to $BR $(git rev-parse --short HEAD)$DIRTY"

# Migrate when a DocType JSON or a patch differs from what the v16 site last had
CHANGED=$( (git diff --name-only $BEFORE HEAD; git diff --name-only; git ls-files --others --exclude-standard) | sort -u \
  | grep -E 'doctype/.+/.+\.json$|patches(\.txt|/)|hooks\.py$' )
if [ -n "$CHANGED" ]; then
  echo "migrating fac16.localhost (changed: $(echo $CHANGED | tr ' ' ','))"
  cd /home/madesh/FAC16 && bench --site fac16.localhost migrate 2>&1 | grep -E 'Error|Executing|Success' | tail -5
fi
