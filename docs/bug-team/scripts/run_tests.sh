#!/bin/bash
# run_tests.sh <v15|v16|both> <module> [<module> ...] [--test <TestClass.test_name>]
#
# Runs FAC test modules on the v15 site (fac.localhost, /home/madesh/FAC) and/or the v16 site
# (fac16.localhost, /home/madesh/FAC16). Prints one summary line per module and, for each failing
# test, its name and the line that ended it. Full logs: /home/madesh/sddscripts/agents/logs/<site>/.
# Runs v16 against the code of the v15 checkout: call sync_v16.sh first (this script does it).

WHERE=$1; shift
MODULES=(); TEST=""
while [ $# -gt 0 ]; do
  case $1 in
    --test) TEST=$2; shift 2 ;;
    *) MODULES+=("$1"); shift ;;
  esac
done
[ ${#MODULES[@]} -eq 0 ] && { echo "usage: run_tests.sh <v15|v16|both> <module>... [--test Class.test]"; exit 2; }

case $WHERE in
  v15) TARGETS=("/home/madesh/FAC fac.localhost 11006") ;;
  v16) TARGETS=("/home/madesh/FAC16 fac16.localhost 11007") ;;
  both) TARGETS=("/home/madesh/FAC fac.localhost 11006" "/home/madesh/FAC16 fac16.localhost 11007") ;;
  # The copy sites, used by full_run.sh to split a long run across two sites per bench
  v15b) TARGETS=("/home/madesh/FAC fac-b.localhost 11006") ;;
  v16b) TARGETS=("/home/madesh/FAC16 fac16-b.localhost 11007") ;;
  *) echo "first argument must be v15, v16 or both"; exit 2 ;;
esac

[[ $WHERE != v15 && $WHERE != v15b && -z $NO_SYNC ]] && bash /home/madesh/sddscripts/agents/sync_v16.sh

# "both": the two sites are separate benches and databases, so run them at the same time and
# print v15's lines, then v16's.
if [ $WHERE = both ]; then
  out=/home/madesh/sddscripts/agents/tmp/both.$$
  NO_SYNC=1 bash $0 v15 "${MODULES[@]}" ${TEST:+--test "$TEST"} > $out.v15 2>&1 & p15=$!
  NO_SYNC=1 bash $0 v16 "${MODULES[@]}" ${TEST:+--test "$TEST"} > $out.v16 2>&1 & p16=$!
  wait $p15; r15=$?; wait $p16; r16=$?
  cat $out.v15 $out.v16; rm -f $out.v15 $out.v16
  [ $r15 -eq 0 ] && [ $r16 -eq 0 ]; exit $?
fi

fail=0
for t in "${TARGETS[@]}"; do
  set -- $t; BENCH=$1; SITE=$2; RQ=$3
  if [ "$(redis-cli -p $RQ ping 2>/dev/null)" != PONG ]; then
    echo "!! $SITE: Redis on $RQ is down. Start that bench (bench start) and run again."; fail=1; continue
  fi
  mkdir -p /home/madesh/sddscripts/agents/logs/$SITE
  cd $BENCH
  for m in "${MODULES[@]}"; do
    m=${m%.py}; m=${m#frappe_assistant_core.tests.}
    log=/home/madesh/sddscripts/agents/logs/$SITE/$m.log
    args=(--site $SITE run-tests --module frappe_assistant_core.tests.$m)
    [ -n "$TEST" ] && args+=(--test "$TEST")
    timeout 1800 bench "${args[@]}" >$log 2>&1
    # A module can print more than one summary (e.g. a class whose setUp errored): add them all up
    ran=$(grep -oE '^Ran [0-9]+' $log | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
    f=$(grep -oE 'failures=[0-9]+' $log | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
    e=$(grep -oE 'errors=[0-9]+' $log | grep -oE '[0-9]+' | awk '{s+=$1} END{print s+0}')
    if grep -qE '^OK' $log && ! grep -qE '^FAILED' $log; then st=PASS; else st=FAIL; fail=1; fi
    printf "%-4s %-16s %-40s ran=%-4s failures=%-4s errors=%s\n" $st $SITE $m ${ran:-0} ${f:-0} ${e:-0}
    if [ $st = FAIL ]; then
      python3 - "$log" <<'PY'
import re, sys
text = open(sys.argv[1], errors="replace").read()
for block in re.split(r"\n={20,}\n", text):
    head = re.search(r"^\s*(ERROR|FAIL):?\s+(\S+)", block, re.M)
    if not head:
        continue
    top = [l for l in block.split("\n") if l.strip() and not l[0].isspace() and not l.startswith("---")]
    print(f"     {head.group(1)} {head.group(2)}: {(top[-1] if top else '')[:220]}")
PY
    fi
  done
done
exit $fail
