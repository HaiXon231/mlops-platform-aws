#!/usr/bin/env bash
set -euo pipefail

backup=${1:?Pass a PostgreSQL backup file}
test -s "$backup" || { echo "Missing or empty backup: $backup" >&2; exit 1; }
check_db="mlflow_restore_check_$(date -u +%Y%m%d%H%M%S)_$$"
created=0
cleanup() {
  if [[ "$created" == 1 ]]; then
    docker compose exec -T postgres sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$1"' sh "$check_db"
  fi
}
trap cleanup EXIT

docker compose exec -T postgres sh -c 'createdb -U "$POSTGRES_USER" "$1"' sh "$check_db"
created=1
docker compose exec -T postgres sh -c 'pg_restore -U "$POSTGRES_USER" -d "$1" --no-owner --no-acl' sh "$check_db" < "$backup"
tables=$(docker compose exec -T postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$1" -At -c "select count(*) from information_schema.tables where table_schema = '\''public'\''"' \
  sh "$check_db")
test "$tables" -gt 0 || { echo 'Restore contained no public tables' >&2; exit 1; }
printf 'Restore verified in temporary database %s (%s tables); primary database untouched.\n' "$check_db" "$tables"
