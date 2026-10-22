#!/bin/bash
# baseline.sh <v15|v16>: runs every data-migration test module on one site, one module at a time.
# A summary line per module and a TOTAL line. Logs: /home/madesh/sddscripts/agents/logs/<site>/.
case $1 in
  v15) W=v15 ;;
  v16) W=v16 ;;
  *) echo "usage: baseline.sh <v15|v16>"; exit 2 ;;
esac
T=/home/madesh/FAC/apps/frappe_assistant_core/frappe_assistant_core/tests
mods=$(ls $T | grep -E '^test_.*(import|plan|preset|opening|undo|signoff|migration|tally|google|document_batch|update_document|dry_run|masters|failures).*\.py$' | sed 's/\.py$//')
bash /home/madesh/sddscripts/agents/run_tests.sh $W $mods > /home/madesh/sddscripts/agents/logs/baseline-$W.txt
cat /home/madesh/sddscripts/agents/logs/baseline-$W.txt
awk '/ran=/{for(i=1;i<=NF;i++){split($i,a,"="); if(a[1]=="ran")r+=a[2]; if(a[1]=="failures")f+=a[2]; if(a[1]=="errors")e+=a[2]}} END{print "TOTAL ran="r" failures="f" errors="e}' /home/madesh/sddscripts/agents/logs/baseline-$W.txt
