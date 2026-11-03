#!/usr/bin/env bash
# Run from the deployed project directory. Restore instructions: docs/deployment.md.
set -euo pipefail
umask 077
mkdir -p backups
backup="backups/sivay-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres pg_dump -U sivay -d sivay -Fc > "$backup"
echo "Created $backup"
