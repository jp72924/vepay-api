[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ProjectId,

    [string] $Service = "vepay-api",
    [string] $Region = "northamerica-south1",
    [string] $Memory = "1Gi",
    [string] $Cpu = "1",
    [int] $Concurrency = 4,
    [string] $Timeout = "300s",
    [int] $MinInstances = 0,
    [int] $MaxInstances = 3,
    [string] $EnvFile = "deploy/cloudrun.env.yaml",
    [string] $ApiKeySecret = "vepay-api-key",
    [string] $ApiKey,
    [string] $RuntimeServiceAccount,
    [string] $BuildServiceAccount,
    [string] $BuildServiceAccountName = "vepay-api-builder",
    [switch] $RequireGoogleAuth,
    [switch] $CpuAlwaysAllocated
)

$ErrorActionPreference = "Stop"

function Invoke-Gcloud {
    & gcloud @args
    if ($LASTEXITCODE -ne 0) {
        throw "gcloud failed: $($args -join ' ')"
    }
}

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    throw "gcloud CLI is required. Run this from Google Cloud Shell or install the Google Cloud CLI."
}

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $repoRoot

Invoke-Gcloud config set project $ProjectId | Out-Null
Invoke-Gcloud services enable `
    run.googleapis.com `
    cloudbuild.googleapis.com `
    artifactregistry.googleapis.com `
    secretmanager.googleapis.com `
    --project $ProjectId

$secretExists = $true
try {
    Invoke-Gcloud secrets describe $ApiKeySecret --project $ProjectId *> $null
}
catch {
    $secretExists = $false
}

if (-not $secretExists) {
    if (-not $ApiKey) {
        throw "Secret $ApiKeySecret does not exist. Pass -ApiKey to create it, or create the secret first."
    }
    Invoke-Gcloud secrets create $ApiKeySecret --project $ProjectId --replication-policy "automatic"
}

if ($ApiKey) {
    $tmpFile = New-TemporaryFile
    try {
        Set-Content -LiteralPath $tmpFile -Value $ApiKey -NoNewline
        Invoke-Gcloud secrets versions add $ApiKeySecret --project $ProjectId --data-file $tmpFile
    }
    finally {
        Remove-Item -LiteralPath $tmpFile -Force -ErrorAction SilentlyContinue
    }
}

if (-not $RuntimeServiceAccount) {
    $projectNumber = Invoke-Gcloud projects describe $ProjectId --format "value(projectNumber)"
    $RuntimeServiceAccount = "$projectNumber-compute@developer.gserviceaccount.com"
}

if (-not $BuildServiceAccount) {
    $BuildServiceAccount = "$BuildServiceAccountName@$ProjectId.iam.gserviceaccount.com"
}

$buildServiceAccountExists = $true
try {
    Invoke-Gcloud iam service-accounts describe $BuildServiceAccount --project $ProjectId *> $null
}
catch {
    $buildServiceAccountExists = $false
}

if (-not $buildServiceAccountExists) {
    Invoke-Gcloud iam service-accounts create $BuildServiceAccountName `
        --project $ProjectId `
        --display-name "VEPay API Cloud Build"
}

Invoke-Gcloud projects add-iam-policy-binding $ProjectId `
    --member "serviceAccount:$BuildServiceAccount" `
    --role "roles/run.builder" `
    --quiet | Out-Null

Invoke-Gcloud secrets add-iam-policy-binding $ApiKeySecret `
    --project $ProjectId `
    --member "serviceAccount:$RuntimeServiceAccount" `
    --role "roles/secretmanager.secretAccessor" `
    --quiet | Out-Null

$authFlag = if ($RequireGoogleAuth) { "--no-allow-unauthenticated" } else { "--allow-unauthenticated" }
$cpuFlag = if ($CpuAlwaysAllocated) { "--no-cpu-throttling" } else { "--cpu-throttling" }

$deployArgs = @(
    "run", "deploy", $Service,
    "--project", $ProjectId,
    "--source", ".",
    "--region", $Region,
    "--port", "8080",
    "--memory", $Memory,
    "--cpu", $Cpu,
    "--concurrency", "$Concurrency",
    "--timeout", $Timeout,
    "--min-instances", "$MinInstances",
    "--max-instances", "$MaxInstances",
    "--service-account", $RuntimeServiceAccount,
    "--build-service-account", "projects/$ProjectId/serviceAccounts/$BuildServiceAccount",
    "--env-vars-file", $EnvFile,
    "--update-secrets", "VEPAY_API_KEY=$ApiKeySecret`:latest",
    $authFlag,
    $cpuFlag
)

Invoke-Gcloud @deployArgs

$serviceUrl = Invoke-Gcloud run services describe $Service `
    --project $ProjectId `
    --region $Region `
    --format "value(status.url)"

Write-Host "Cloud Run service: $serviceUrl"
Write-Host "Health check: curl $serviceUrl/health"
Write-Host "Capabilities: curl -H 'X-API-Key: <api-key>' $serviceUrl/v1/capabilities"
