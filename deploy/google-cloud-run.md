# Deploy VEPay API to Google Cloud Run

This deployment keeps OCR inside the VEPay API container with Tesseract. It does
not enable Cloud Vision or Document AI by default.

## Defaults

- Service: `vepay-api`
- Region: `northamerica-south1` (Mexico)
- Container port: `8080`
- Runtime: 1 vCPU, 1 GiB RAM
- Autoscaling: min `0`, max `3`
- Cloud Run concurrency: `4`
- VEPay OCR concurrency: `2`
- Auth: public Cloud Run URL plus application API key in `X-API-Key`
- Audit UI: enabled at `/ui`, with `/` kept as API metadata

Use `us-east1` if you prefer a long-established US region, or if your Google
Cloud account cannot deploy in `northamerica-south1`.

## Deploy From Google Cloud Shell

```bash
git clone https://github.com/jp72924/vepay-api.git
cd vepay-api

export PROJECT_ID="your-gcp-project-id"
export API_KEY="$(openssl rand -base64 32)"

bash scripts/deploy_cloud_run.sh
```

The script enables the required APIs, creates or updates the Secret Manager
secret named `vepay-api-key`, grants the Cloud Run runtime service account read
access to that secret, creates a dedicated Cloud Build service account with the
Cloud Run Builder role, and deploys the existing Dockerfile with
`gcloud run deploy --source .`.

## Deploy From PowerShell

Install the Google Cloud CLI, authenticate, then run:

```powershell
.\scripts\deploy_cloud_run.ps1 `
  -ProjectId "your-gcp-project-id" `
  -ApiKey "replace-with-a-long-random-api-key"
```

To require Google IAM authentication at Cloud Run in addition to the app API key,
add `-RequireGoogleAuth`.

## Verify

After deployment, the scripts print the service URL.

```bash
SERVICE_URL="https://your-service-url"
API_KEY="the-api-key-you-used"

curl "$SERVICE_URL/health"
curl -H "X-API-Key: $API_KEY" "$SERVICE_URL/v1/capabilities"
```

Open `https://your-service-url/docs` for the FastAPI docs UI. Requests to parse
receipts must include `X-API-Key`.

Open `https://your-service-url/ui/` for the integrated audit UI. The UI asks for
the API key when application auth is enabled and does not persist it.

## Tune For Production

- Increase `MAX_INSTANCES` if OCR traffic rises above a few simultaneous users.
- Increase `MEMORY` to `2Gi` if large images trigger memory pressure.
- Keep `VEPAY_API_MAX_CONCURRENCY` near the CPU count. Tesseract is CPU-heavy.
- Prefer `/v1/receipts/parse` for Cloud Run. The `/v1/jobs` endpoint stores
  jobs in process memory, so jobs are not durable across scale-downs or multiple
  instances.
- If you must use `/v1/jobs` before adding Redis or Cloud Tasks, deploy with
  `CPU_ALWAYS_ALLOCATED=true` in bash or `-CpuAlwaysAllocated` in PowerShell.

## Common Changes

Deploy to `us-east1`:

```bash
PROJECT_ID="your-gcp-project-id" REGION="us-east1" bash scripts/deploy_cloud_run.sh
```

Use an existing secret:

```bash
PROJECT_ID="your-gcp-project-id" API_KEY_SECRET="existing-secret-name" bash scripts/deploy_cloud_run.sh
```

Rotate the API key:

```bash
PROJECT_ID="your-gcp-project-id" API_KEY="$(openssl rand -base64 32)" bash scripts/deploy_cloud_run.sh
```

Disable public Cloud Run invocation and require Google IAM:

```bash
PROJECT_ID="your-gcp-project-id" ALLOW_UNAUTHENTICATED=false bash scripts/deploy_cloud_run.sh
```

## Manual Command

If you do not want to use the helper scripts, create `vepay-api-key` in Secret
Manager and run:

```bash
gcloud run deploy vepay-api \
  --project "your-gcp-project-id" \
  --source . \
  --region "northamerica-south1" \
  --port "8080" \
  --memory "1Gi" \
  --cpu "1" \
  --concurrency "4" \
  --timeout "300s" \
  --min-instances "0" \
  --max-instances "3" \
  --allow-unauthenticated \
  --cpu-throttling \
  --build-service-account "projects/your-gcp-project-id/serviceAccounts/vepay-api-builder@your-gcp-project-id.iam.gserviceaccount.com" \
  --env-vars-file "deploy/cloudrun.env.yaml" \
  --update-secrets "VEPAY_API_KEY=vepay-api-key:latest"
```
