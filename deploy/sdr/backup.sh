#!/bin/bash
set -euo pipefail
umask 077
cd /opt/odoo-sdr
exec 9>.update.lock
flock -n 9 || exit 0
dir=/opt/odoo-sdr/backups/nightly-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$dir"
docker stop sdr-odoo >/dev/null
trap 'docker start sdr-odoo >/dev/null' EXIT
docker exec sdr-db pg_dump -U odoo -Fc crm > "$dir/crm.dump"
docker run --rm --entrypoint tar -v sdr-odoo-data:/data:ro odoo-sdr:20 \
  -C /data -czf - . > "$dir/filestore.tgz"
docker exec -i sdr-db pg_restore --list < "$dir/crm.dump" >/dev/null
tar -tzf "$dir/filestore.tgz" >/dev/null
touch "$dir/complete"
# Keep fourteen verified daily generations; pre-migration backups are retained separately.
find /opt/odoo-sdr/backups -maxdepth 1 -mindepth 1 -type d -name 'nightly-20*' -mtime +14 -exec rm -rf -- {} +
printf '%s\n' "$dir"
