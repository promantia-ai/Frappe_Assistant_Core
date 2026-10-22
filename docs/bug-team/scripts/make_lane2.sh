#!/bin/bash
# make_lane2.sh: a second pair of benches so a second developer can work at the same time.
#   v15: /home/madesh/FAC-L2   site fac-l2.localhost    (copy of fac.localhost's data)
#   v16: /home/madesh/FAC16-L2 site fac16-l2.localhost  (copy of fac16.localhost's data)
# Every app is cloned from the existing bench's checkout (no GitHub), on the same branch.
# Lane 2's FAC checkout (in FAC-L2) gets the remote "lane1" and the branch fix/data-migration-bugs-l2;
# FAC16-L2's FAC is its mirror. Test sites only (local test DB root password).
set -e
build() {
  SRC=$1; DST=$2; PY=$3; NODE=$4; SRCSITE=$5; SITE=$6
  export PATH=/home/madesh/.nvm/versions/node/$NODE/bin:$PATH
  cd /home/madesh
  if [ -d $DST ] && ! $DST/env/bin/python -c "import frappe" 2>/dev/null; then
    echo "== $(date +%T) removing half-made $DST"; rm -rf $DST
  fi
  if [ ! -d $DST ]; then
    FB=$(git -C $SRC/apps/frappe branch --show-current)
    echo "== $(date +%T) bench init $DST (frappe $FB from $SRC)"
    bench init $DST --frappe-path $SRC/apps/frappe --frappe-branch $FB --python $PY --skip-assets 2>&1 | grep -E 'SUCCESS|ERROR' | tail -2
    cd $DST
    for app in $(ls $SRC/apps | grep -v '^frappe$'); do
      BR=$(git -C $SRC/apps/$app branch --show-current)
      echo "== $(date +%T) get-app $app ($BR)"
      bench get-app --branch $BR --skip-assets $SRC/apps/$app 2>&1 | grep -E 'ERROR|Error' | tail -2 || true
    done
  fi
  cd $DST
  ./env/bin/python -c "import frappe, erpnext, frappe_assistant_core; print('apps import OK in $DST')"
  echo "== $(date +%T) $DST ports: $(grep -oE '"(webserver_port|socketio_port)": [0-9]+|redis_queue": "[^"]+' sites/common_site_config.json | tr '\n' ' ')"
  # Redis for this bench (needed to create the site and to run tests)
  redis-server config/redis_queue.conf --daemonize yes >/dev/null 2>&1 || true
  redis-server config/redis_cache.conf --daemonize yes >/dev/null 2>&1 || true
  if ! bench --site $SITE list-apps >/dev/null 2>&1; then
    [ -d sites/$SITE ] && rm -rf sites/$SITE
    SQL=$(ls -t $SRC/sites/$SRCSITE/private/backups/*-database.sql.gz | head -1)
    echo "== $(date +%T) new site $SITE from $(basename $SQL)"
    bench new-site $SITE --db-root-username root --db-root-password 1 --admin-password 1 --source-sql "$SQL" 2>&1 \
      | grep -vE 'Updating DocTypes|^\s*$|MariaDB version' | tail -3
    KEY=$(python3 -c "import json;print(json.load(open('$SRC/sites/$SRCSITE/site_config.json')).get('encryption_key',''))")
    [ -n "$KEY" ] && bench --site $SITE set-config encryption_key "$KEY" >/dev/null
    bench --site $SITE set-config allow_tests true >/dev/null
    bench use $SITE >/dev/null
  fi
  echo "== $(date +%T) $SITE apps: $(bench --site $SITE list-apps 2>/dev/null | awk 'NF{print $1}' | tr '\n' ' ')"
}
build /home/madesh/FAC /home/madesh/FAC-L2 /usr/bin/python3.12 v18.20.8 fac.localhost fac-l2.localhost
build /home/madesh/FAC16 /home/madesh/FAC16-L2 /usr/bin/python3.14 v24.16.0 fac16.localhost fac16-l2.localhost

# Lane 2's FAC checkout: remote to lane 1, its own branch
cd /home/madesh/FAC-L2/apps/frappe_assistant_core
git remote get-url lane1 >/dev/null 2>&1 || git remote add lane1 /home/madesh/FAC/apps/frappe_assistant_core
git fetch -q lane1
git checkout -q -B fix/data-migration-bugs-l2 lane1/fix/data-migration-bugs
echo "lane 2 FAC: $(git branch --show-current) $(git log -1 --format='%h %s')"
echo LANE2_DONE
