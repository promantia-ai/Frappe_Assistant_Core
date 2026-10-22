#!/bin/bash
# make_lane2_scripts.sh: makes lane 2's copies of the agent scripts in /home/madesh/sddscripts/agents/lane2/,
# pointed at lane 2's benches (FAC-L2, FAC16-L2), sites (fac-l2, fac16-l2) and Redis (11008, 11009).
# Run it again after changing a lane-1 script.
A=/home/madesh/sddscripts/agents
L=$A/lane2
mkdir -p $L/logs $L/tmp
for f in run_tests.sh full_run.sh sync_v16.sh red_check.sh affected.sh baseline.sh affected_imports.py; do
  sed -E \
    -e 's#/home/madesh/sddscripts/agents/#/home/madesh/sddscripts/agents/lane2/#g' \
    -e 's#\$A/#$A/#g' \
    -e 's#/home/madesh/FAC16\b#@@V16@@#g' \
    -e 's#/home/madesh/FAC\b#/home/madesh/FAC-L2#g' \
    -e 's#@@V16@@#/home/madesh/FAC16-L2#g' \
    -e 's#fac16\.localhost#fac16-l2.localhost#g' \
    -e 's#fac\.localhost#fac-l2.localhost#g' \
    -e 's#\b11006\b#11008#g' \
    -e 's#\b11007\b#11009#g' \
    -e 's#^A=/home/madesh/sddscripts/agents$#A=/home/madesh/sddscripts/agents/lane2#' \
    $A/$f > $L/$f
done
# The baseline logs of the start live with lane 1; lane 2's split falls back to them for timings
sed -i 's#/home/madesh/sddscripts/agents/lane2/v15-baseline#/home/madesh/sddscripts/v15-baseline#' $L/full_run.sh
chmod +x $L/*.sh
for f in $L/*.sh; do bash -n $f || echo "SYNTAX ERROR in $f"; done
grep -c 'FAC-L2\|FAC16-L2' $L/run_tests.sh $L/sync_v16.sh | tr '\n' ' '; echo
echo "lane 2 scripts in $L"
