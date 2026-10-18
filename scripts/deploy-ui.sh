#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-nexus-265f0}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

require_command npm
require_command firebase

cd "$ROOT_DIR"
printf 'Building frontend with the production Vite configuration.\n'
npm --prefix frontend run build

printf 'Deploying Firebase Hosting for project: %s\n' "$PROJECT_ID"
firebase deploy --only hosting --project "$PROJECT_ID"
