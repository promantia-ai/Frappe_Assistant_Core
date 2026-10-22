#!/bin/bash
# make_shard_sites.sh: makes a second test site on each bench, an exact copy of the first, so full
# test runs can be split across two sites. v15: fac-b.localhost (copy of fac.localhost);
# v16: fac16-b.localhost (copy of fac16.localhost). Test sites only (local test DB root password).
set -e
make_copy() {
  BENCH=$1; SRC=$2; DST=$3
  cd $BENCH
  if [ -d sites/$DST ] && bench --site $DST list-apps >/dev/null 2>&1; then echo "$DST already exists"; return; fi
  if [ -d sites/$DST ]; then   # left half-made by an earlier failed attempt
    echo "== $(date +%T) removing half-made $DST"
    bench drop-site $DST --db-root-username root --db-root-password 1 --force --no-backup 2>&1 | tail -1
    rm -rf sites/$DST
  fi
  echo "== $(date +%T) backup $SRC"
  bench --site $SRC backup --ignore-backup-conf 2>&1 | tail -1
  SQL=$(ls -t $BENCH/sites/$SRC/private/backups/*-database.sql.gz | head -1)
  echo "== $(date +%T) new site $DST from $(basename $SQL)"
  bench new-site $DST --db-root-username root --db-root-password 1 --admin-password 1 --source-sql "$SQL" 2>&1 | grep -vE 'Updating DocTypes|^\s*$' | tail -3
  # The copy needs the source's encryption key to read the passwords stored in its data
  KEY=$(python3 -c "import json;print(json.load(open('sites/$SRC/site_config.json')).get('encryption_key',''))")
  [ -n "$KEY" ] && bench --site $DST set-config encryption_key "$KEY" >/dev/null
  bench --site $DST set-config allow_tests true >/dev/null
  bench --site $DST migrate 2>&1 | grep -E 'Error|Success' | tail -2
  echo "== $(date +%T) $DST apps: $(bench --site $DST list-apps 2>/dev/null | awk 'NF{print $1}' | tr '\n' ' ')"
}
make_copy /home/madesh/FAC fac.localhost fac-b.localhost
make_copy /home/madesh/FAC16 fac16.localhost fac16-b.localhost
echo SHARDS_DONE
