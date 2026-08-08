#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXPORT_DIR="${1:-$ROOT_DIR/exports}"
DATABASE_URL="${DATABASE_URL:-postgresql://postgres:1234@localhost:5432/company_app}"
STAMP="$(date +%Y%m%d_%H%M%S)"

mkdir -p "$EXPORT_DIR"

pg_dump "$DATABASE_URL" \
  --format=custom \
  --file="$EXPORT_DIR/company_app_$STAMP.dump"

echo "Wrote $EXPORT_DIR/company_app_$STAMP.dump"
