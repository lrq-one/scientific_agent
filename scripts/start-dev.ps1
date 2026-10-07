param(
    [switch]$NoDocker,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

Set-Location $Root

# First-run convenience: keep secrets local while providing documented dev defaults.
$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "Created local .env from .env.example (git-ignored)."
}

# Import Windows User-level LLM settings when this shell did not inherit them.
foreach ($Name in @("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL")) {
    if (-not (Get-Item "Env:$Name" -ErrorAction SilentlyContinue)) {
        $Value = [Environment]::GetEnvironmentVariable($Name, "User")
        if ($Value) { Set-Item "Env:$Name" $Value }
    }
}

if (-not $NoDocker) {
    Write-Host "[1/4] Starting PostgreSQL and MinIO..."
    docker compose up -d
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python virtualenv not found: $Python. Create .venv and install the project first."
}

Write-Host "[2/4] Starting FastAPI..."
$BackendOut = Join-Path $RuntimeDir "backend.out.log"
$BackendErr = Join-Path $RuntimeDir "backend.err.log"
$Backend = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") -WorkingDirectory $Root -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr -PassThru
Set-Content -Path (Join-Path $RuntimeDir "backend.pid") -Value $Backend.Id

if (-not $NoFrontend) {
    Write-Host "[3/4] Starting Vue/Vite..."
    $WebRoot = Join-Path $Root "web"
    $FrontendOut = Join-Path $RuntimeDir "frontend.out.log"
    $FrontendErr = Join-Path $RuntimeDir "frontend.err.log"
    $Frontend = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") -WorkingDirectory $WebRoot -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr -PassThru
    Set-Content -Path (Join-Path $RuntimeDir "frontend.pid") -Value $Frontend.Id
}

Write-Host "[4/4] Waiting for backend..."
$Deadline = (Get-Date).AddSeconds(45)
$Alive = $false
while ((Get-Date) -lt $Deadline) {
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($Health.status -eq "ok") {
            $Alive = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 700
    }
}
if (-not $Alive) {
    Write-Warning "FastAPI did not become healthy within 45 seconds. See .runtime/backend.err.log"
    exit 1
}

Write-Host ""
Write-Host "FastAPI:  http://127.0.0.1:8000"
Write-Host "Docs:     http://127.0.0.1:8000/docs"
if (-not $NoFrontend) { Write-Host "Web:      http://127.0.0.1:5173" }
try {
    $Ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/ready" -TimeoutSec 5
    Write-Host "Readiness:" $Ready.status
    if (-not $Ready.ready) {
        Write-Warning "Process is alive but one or more Agent dependencies are not ready. Run .\scripts\status-dev.ps1 for details."
    }
}
catch {
    Write-Warning "Could not read /ready yet."
}
