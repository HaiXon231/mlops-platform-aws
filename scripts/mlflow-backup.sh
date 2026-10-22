#!/usr/bin/env bash
set -euo pipefail

mkdir -p backups
temporary=$(mktemp "backups/mlflow-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX.dump.part")
backup="${temporary%.part}"
trap 'rm -f "$temporary"' EXIT

docker compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$temporary"
docker compose exec -T postgres pg_restore --list < "$temporary" > /dev/null
mv "$temporary" "$backup"
printf 'PostgreSQL backup: %s\n' "$backup"
