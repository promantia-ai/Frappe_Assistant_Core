#!/bin/bash
# red_check.sh <commit> <module> [<module> ...]
#
# Proves a fix's tests catch the bug: on the v16 mirror only, puts back the code as it was before
# <commit> but keeps the commit's test files, runs the modules (they should FAIL), then syncs the
# mirror back to the v15 checkout. The v15 checkout, where the code is written, is never touched.

COMMIT=$1; shift
[ -z "$COMMIT" ] || [ $# -eq 0 ] && { echo "usage: red_check.sh <commit> <module>..."; exit 2; }
SRC=/home/madesh/FAC/apps/frappe_assistant_core
DST=/home/madesh/FAC16/apps/frappe_assistant_core

bash /home/madesh/sddscripts/agents/sync_v16.sh >/dev/null
cd $DST
git cat-file -e "$COMMIT^{commit}" 2>/dev/null || { echo "unknown commit $COMMIT"; exit 2; }
TESTS=$(git diff --name-only "$COMMIT^" "$COMMIT" | grep -E '/tests/|\.spec\.js$')
[ -z "$TESTS" ] && { echo "commit $COMMIT changes no test file: nothing to check"; exit 2; }

git checkout -q "$COMMIT^" -- .
git checkout -q "$COMMIT" -- $TESTS
echo "v16 mirror: code before $(git rev-parse --short $COMMIT), tests from it: $(echo $TESTS | tr '\n' ' ')"

NO_SYNC=1 bash /home/madesh/sddscripts/agents/run_tests.sh v16 "$@"
RESULT=$?

bash /home/madesh/sddscripts/agents/sync_v16.sh >/dev/null
echo "v16 mirror synced back to the v15 checkout"
if [ $RESULT -ne 0 ]; then echo "RED_CHECK: OK (the tests fail without the fix)"; else echo "RED_CHECK: NOT PROVEN (the tests pass without the fix)"; fi
