#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "Usage: scripts/import_database.sh /path/to/company_app.dump" >&2
  exit 1
fi

DATABASE_URL="${DATABASE_URL:-postgresql://postgres:1234@localhost:5432/company_app}"

pg_restore \
  --clean \
  --if-exists \
  --no-owner \
  --dbname="$DATABASE_URL" \
  "$1"
