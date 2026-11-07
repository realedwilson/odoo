#!/bin/bash
set -euo pipefail
umask 077
cd /opt/odoo-sdr
exec 9>.update.lock
flock -n 9 || exit 0
branch=codex/crm-sdr-falemulti
git -C source fetch --no-tags origin "refs/heads/$branch"
new=$(git -C source rev-parse FETCH_HEAD)
old=$(cat deployed.commit)
[ "$new" != "$old" ] || exit 0
git -C source merge-base --is-ancestor "$old" "$new" || {
  echo 'Atualização recusada: branch não é avanço do commit implantado.' >&2; exit 1;
}
release=/opt/odoo-sdr/releases/$new
mkdir -p releases backups
[ -d "$release" ] || (umask 022; git -C source worktree add --detach "$release" "$new")
requirements=$(sha256sum "$release/requirements.txt" | cut -c1-16)
image=odoo-sdr:req-$requirements
if ! docker image inspect "$image" >/dev/null 2>&1; then
  cp Dockerfile build/Dockerfile
  cp "$release/requirements.txt" build/
  docker build -t "$image" build
fi
verify=sdr_check_${new:0:12}
docker exec sdr-db createdb -U odoo "$verify"
trap 'docker exec sdr-db dropdb -U odoo --if-exists "$verify" >/dev/null' EXIT
run() {
  docker run --rm --network odoo-sdr -v "$release:/opt/odoo:ro" \
    -v /opt/odoo-sdr/odoo.conf:/etc/odoo.conf:ro "$image" -c /etc/odoo.conf "$@"
}
run -d "$verify" --db-filter="^$verify$" --no-http --stop-after-init --without-demo=True \
  -i crm_sdr_falemulti --test-enable --test-tags /crm_sdr_falemulti
backup=/opt/odoo-sdr/backups/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$backup"
old_source=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/opt/odoo"}}{{.Source}}{{end}}{{end}}' sdr-odoo)
old_image=$(docker inspect -f '{{.Config.Image}}' sdr-odoo)
docker stop sdr-odoo
if ! docker exec sdr-db pg_dump -U odoo -Fc crm > "$backup/crm.dump" ||
   ! docker run --rm --entrypoint tar -v sdr-odoo-data:/data:ro "$old_image" -C /data -czf - . > "$backup/filestore.tgz"; then
  docker start sdr-odoo
  exit 1
fi
rollback() {
  echo "Falha de atualização. Restaurando a versão $old" >&2
  docker stop sdr-odoo >/dev/null 2>&1 || true
  docker exec sdr-db dropdb -U odoo --force crm
  docker exec sdr-db createdb -U odoo crm
  docker exec -i sdr-db pg_restore --exit-on-error -U odoo -d crm < "$backup/crm.dump"
  docker run --rm --user 0 --entrypoint sh -v sdr-odoo-data:/data "$old_image" \
    -c 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
  docker run --rm -i --user 0 --entrypoint tar -v sdr-odoo-data:/data "$old_image" \
    -C /data -xzf - < "$backup/filestore.tgz"
  ./start.sh "$old_source" "$old_image"
}
if ! docker run --rm --network odoo-sdr -v "$release:/opt/odoo:ro" \
  -v /opt/odoo-sdr/odoo.conf:/etc/odoo.conf:ro -v sdr-odoo-data:/var/lib/odoo \
  "$image" -c /etc/odoo.conf -d crm --no-http --stop-after-init -u crm_sdr_falemulti; then
  rollback
  exit 1
fi
if ./start.sh "$release" "$image"; then
  for i in $(seq 1 30); do
    if curl --fail --silent --max-time 4 http://127.0.0.1:8069/web/login >/dev/null; then
      printf '%s\n' "$new" > deployed.commit.new
      mv deployed.commit.new deployed.commit
      exit 0
    fi
    sleep 2
  done
fi
rollback
exit 1
