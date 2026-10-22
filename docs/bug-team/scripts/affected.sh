#!/bin/bash
# affected.sh [<base ref>]: prints the test modules a change can affect, one line, space-separated.
#
# The change is everything in the v15 checkout that differs from <base ref> (default: the commit
# before HEAD), committed or not. A test module is affected when it changed itself, or when it names
# a changed code module, or a code module that (directly or through others) uses a changed one.
# DocType, hook and patch changes affect every data-migration test module; skill docs affect the
# skill manifest test.

APP=/home/madesh/FAC/apps/frappe_assistant_core
cd $APP
BASE=${1:-HEAD~1}
PKG=frappe_assistant_core
TESTS=$PKG/tests

CHANGED=${CHANGED_FILES:-$( (git diff --name-only "$BASE"; git ls-files --others --exclude-standard) | sort -u)}
ALL_MIGRATION=$(ls $TESTS | grep -E '^test_.*(import|plan|preset|opening|undo|signoff|migration|tally|google|document_batch|update_document|dry_run|masters|failures).*\.py$' | sed 's/\.py$//')

declare -A OUT
# Changed test modules
for f in $(echo "$CHANGED" | grep -E "^$TESTS/test_.*\.py$"); do OUT[$(basename $f .py)]=1; done
# Shared test helpers: everything that uses them
if echo "$CHANGED" | grep -qE "^$TESTS/(base_test|utils|__init__)\.py$"; then for m in $ALL_MIGRATION; do OUT[$m]=1; done; fi
# DocType / hooks / patches: every data-migration module
if echo "$CHANGED" | grep -qE "doctype/.+\.(json|py)$|hooks\.py$|patches(\.txt|/)"; then for m in $ALL_MIGRATION; do OUT[$m]=1; done; fi
# Skill docs
if echo "$CHANGED" | grep -qE "^docs/skills/|data/system_skills\.json$"; then OUT[test_system_skill_manifest]=1; fi

# Code modules: the changed ones, then everything that imports them (real import statements),
# until nothing new turns up. Then every test module that names one of them.
CODE=$(echo "$CHANGED" | grep -E "^$PKG/.*\.py$" | grep -v "^$TESTS/")
if [ -n "$CODE" ]; then
  FOLLOWED=$(python3 /home/madesh/sddscripts/agents/affected_imports.py $APP $CODE)
  [ -n "$DEBUG" ] && echo "followed: $FOLLOWED" >&2
  for name in $FOLLOWED; do
    for t in $(grep -lw "$name" $TESTS/test_*.py 2>/dev/null); do OUT[$(basename $t .py)]=1; done
  done
fi

echo "${!OUT[@]}" | tr ' ' '\n' | sort | tr '\n' ' '; echo
