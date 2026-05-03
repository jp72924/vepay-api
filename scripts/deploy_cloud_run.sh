#!/usr/bin/env bash
set -euo pipefail

SERVICE="${SERVICE:-vepay-api}"
REGION="${REGION:-northamerica-south1}"
MEMORY="${MEMORY:-1Gi}"
CPU="${CPU:-1}"
CONCURRENCY="${CONCURRENCY:-4}"
TIMEOUT="${TIMEOUT:-300s}"
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-3}"
ENV_FILE="${ENV_FILE:-deploy/cloudrun.env.yaml}"
API_KEY_SECRET="${API_KEY_SECRET:-vepay-api-key}"
ALLOW_UNAUTHENTICATED="${ALLOW_UNAUTHENTICATED:-true}"
CPU_ALWAYS_ALLOCATED="${CPU_ALWAYS_ALLOCATED:-false}"
BUILD_SERVICE_ACCOUNT_NAME="${BUILD_SERVICE_ACCOUNT_NAME:-vepay-api-builder}"

if [[ -z "${PROJECT_ID:-}" ]]; then
  echo "Set PROJECT_ID to the Google Cloud project id." >&2
  exit 2
fi

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI is required. Run this from Google Cloud Shell or install the Google Cloud CLI." >&2
  exit 2
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${REPO_ROOT}"

gcloud config set project "${PROJECT_ID}" >/dev/null
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  --project "${PROJECT_ID}"

if ! gcloud secrets describe "${API_KEY_SECRET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  if [[ -z "${API_KEY:-}" ]]; then
    echo "Secret ${API_KEY_SECRET} does not exist. Set API_KEY to create it, or create the secret first." >&2
    exit 2
  fi
  gcloud secrets create "${API_KEY_SECRET}" \
    --project "${PROJECT_ID}" \
    --replication-policy="automatic"
fi

if [[ -n "${API_KEY:-}" ]]; then
  tmp_file="$(mktemp)"
  trap 'rm -f "${tmp_file}"' EXIT
  printf "%s" "${API_KEY}" > "${tmp_file}"
  gcloud secrets versions add "${API_KEY_SECRET}" \
    --project "${PROJECT_ID}" \
    --data-file="${tmp_file}"
fi

PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")"
RUNTIME_SERVICE_ACCOUNT="${RUNTIME_SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
BUILD_SERVICE_ACCOUNT="${BUILD_SERVICE_ACCOUNT:-${BUILD_SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com}"

if ! gcloud iam service-accounts describe "${BUILD_SERVICE_ACCOUNT}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${BUILD_SERVICE_ACCOUNT_NAME}" \
    --project "${PROJECT_ID}" \
    --display-name "VEPay API Cloud Build"
fi

gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member "serviceAccount:${BUILD_SERVICE_ACCOUNT}" \
  --role "roles/run.builder" \
  --quiet >/dev/null

gcloud secrets add-iam-policy-binding "${API_KEY_SECRET}" \
  --project "${PROJECT_ID}" \
  --member "serviceAccount:${RUNTIME_SERVICE_ACCOUNT}" \
  --role "roles/secretmanager.secretAccessor" \
  --quiet >/dev/null

deploy_args=(
  run deploy "${SERVICE}"
  --project "${PROJECT_ID}"
  --source "."
  --region "${REGION}"
  --port "8080"
  --memory "${MEMORY}"
  --cpu "${CPU}"
  --concurrency "${CONCURRENCY}"
  --timeout "${TIMEOUT}"
  --min-instances "${MIN_INSTANCES}"
  --max-instances "${MAX_INSTANCES}"
  --service-account "${RUNTIME_SERVICE_ACCOUNT}"
  --build-service-account "projects/${PROJECT_ID}/serviceAccounts/${BUILD_SERVICE_ACCOUNT}"
  --env-vars-file "${ENV_FILE}"
  --update-secrets "VEPAY_API_KEY=${API_KEY_SECRET}:latest"
)

if [[ "${ALLOW_UNAUTHENTICATED}" == "true" ]]; then
  deploy_args+=(--allow-unauthenticated)
else
  deploy_args+=(--no-allow-unauthenticated)
fi

if [[ "${CPU_ALWAYS_ALLOCATED}" == "true" ]]; then
  deploy_args+=(--no-cpu-throttling)
else
  deploy_args+=(--cpu-throttling)
fi

gcloud "${deploy_args[@]}"

SERVICE_URL="$(gcloud run services describe "${SERVICE}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format="value(status.url)")"

echo "Cloud Run service: ${SERVICE_URL}"
echo "Health check: curl ${SERVICE_URL}/health"
echo "Capabilities: curl -H 'X-API-Key: <api-key>' ${SERVICE_URL}/v1/capabilities"
