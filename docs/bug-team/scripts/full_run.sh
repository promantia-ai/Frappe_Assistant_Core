#!/bin/bash
# full_run.sh <v15|v16|both> [<module> ...]
#
# Runs many test modules fast: splits them across the two sites of each bench (v15: fac.localhost
# + fac-b.localhost; v16: fac16.localhost + fac16-b.localhost), balanced by each module's last
# measured time, and runs every queue at the same time. With no modules: every data-migration
# module. Prints the same lines as run_tests.sh, then a TOTAL per version.

WHERE=$1; shift
case $WHERE in v15|v16|both) ;; *) echo "usage: full_run.sh <v15|v16|both> [module...]"; exit 2 ;; esac
A=/home/madesh/sddscripts/agents
MODULES="$*"
if [ -z "$MODULES" ]; then
  MODULES=$(ls /home/madesh/FAC/apps/frappe_assistant_core/frappe_assistant_core/tests \
    | grep -E '^test_.*(import|plan|preset|opening|undo|signoff|migration|tally|google|document_batch|update_document|dry_run|masters|failures).*\.py$' \
    | sed 's/\.py$//' | tr '\n' ' ')
fi

[ $WHERE != v15 ] && bash $A/sync_v16.sh

split() {  # split <main site>: prints two lines, the module queues for the main and the copy site
  python3 - "$1" $MODULES <<'PY'
import os, re, sys
site, modules = sys.argv[1], sys.argv[2:]
def seconds(m):
    for d in (f"/home/madesh/sddscripts/agents/logs/{site}", "/home/madesh/sddscripts/v15-baseline"):
        try:
            found = re.findall(r"Ran \d+ tests? in ([0-9.]+)s", open(f"{d}/{m}.log", errors="replace").read())
            if found:
                return sum(map(float, found)) + 5
        except OSError:
            pass
    return 30.0
queues, load = ([], []), [0.0, 0.0]
for m in sorted(modules, key=seconds, reverse=True):   # longest first, onto the lighter queue
    i = 0 if load[0] <= load[1] else 1
    queues[i].append(m); load[i] += seconds(m)
print(" ".join(queues[0])); print(" ".join(queues[1]))
PY
}

out=$A/tmp/full.$$; pids=()
for v in v15 v16; do
  [ $WHERE != both ] && [ $WHERE != $v ] && continue
  [ $v = v15 ] && main=fac.localhost || main=fac16.localhost
  if [ ! -d /home/madesh/$([ $v = v15 ] && echo FAC || echo FAC16)/sites/$([ $v = v15 ] && echo fac-b.localhost || echo fac16-b.localhost) ]; then
    NO_SYNC=1 bash $A/run_tests.sh $v $MODULES > $out.$v.a 2>&1 & pids+=($!)
    continue
  fi
  { read q1; read q2; } < <(split $main)
  [ -n "$q1" ] && { NO_SYNC=1 bash $A/run_tests.sh $v $q1 > $out.$v.a 2>&1 & pids+=($!); }
  [ -n "$q2" ] && { NO_SYNC=1 bash $A/run_tests.sh ${v}b $q2 > $out.$v.b 2>&1 & pids+=($!); }
done
fail=0
for p in "${pids[@]}"; do wait $p || fail=1; done

for v in v15 v16; do
  ls $out.$v.* >/dev/null 2>&1 || continue
  cat $out.$v.* | sort -k3
  cat $out.$v.* | awk -v v=$v '/ran=/{for(i=1;i<=NF;i++){split($i,a,"="); if(a[1]=="ran")r+=a[2]; if(a[1]=="failures")f+=a[2]; if(a[1]=="errors")e+=a[2]}} END{print "TOTAL "v" ran="r" failures="f" errors="e}'
done
rm -f $out.*
exit $fail
