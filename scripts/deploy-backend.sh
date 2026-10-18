#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT_ID="${PROJECT_ID:-nexus-265f0}"
REGION="${REGION:-asia-south1}"
ARTIFACT_REPOSITORY="${ARTIFACT_REPOSITORY:-nexus}"
API_SERVICE="${API_SERVICE:-nexus-api}"
WORKER_JOB="${WORKER_JOB:-nexus-execution-worker}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'Required command not found: %s\n' "$1" >&2
    exit 1
  fi
}

require_command git
require_command gcloud
require_command firebase

cd "$ROOT_DIR"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD)}"
if [[ -n "$(git status --porcelain)" && "$IMAGE_TAG" == "$(git rev-parse --short HEAD)" ]]; then
  printf 'Warning: working tree has changes; using a unique dirty image tag.\n' >&2
  IMAGE_TAG="${IMAGE_TAG}-dirty-$(date -u +%Y%m%d%H%M%S)"
fi

API_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/nexus-api:${IMAGE_TAG}"
WORKER_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REPOSITORY}/nexus-worker:${IMAGE_TAG}"

printf 'Deploying Firestore indexes for project: %s\n' "$PROJECT_ID"
firebase deploy --only firestore:indexes --project "$PROJECT_ID"

printf 'Building API image: %s\n' "$API_IMAGE"
gcloud builds submit backend \
  --tag "$API_IMAGE" \
  --project "$PROJECT_ID"

printf 'Building worker image: %s\n' "$WORKER_IMAGE"
gcloud builds submit backend \
  --config=backend/cloudbuild.worker.yaml \
  --substitutions="_IMAGE=${WORKER_IMAGE}" \
  --project "$PROJECT_ID"

printf 'Deploying Cloud Run API: %s\n' "$API_SERVICE"
gcloud run deploy "$API_SERVICE" \
  --image "$API_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --quiet

printf 'Updating Cloud Run worker job: %s\n' "$WORKER_JOB"
gcloud run jobs update "$WORKER_JOB" \
  --image "$WORKER_IMAGE" \
  --region "$REGION" \
  --project "$PROJECT_ID" \
  --quiet

printf 'Backend deployment complete.\nAPI image: %s\nWorker image: %s\n' \
  "$API_IMAGE" "$WORKER_IMAGE"
