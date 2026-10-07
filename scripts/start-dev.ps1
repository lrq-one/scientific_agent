param(
    [switch]$NoDocker,
    [switch]$NoFrontend
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null

function Assert-PortFree([int]$Port, [string]$Name) {
    $Listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($Listener) {
        throw "$Name cannot start because port $Port is already in use by PID $($Listener.OwningProcess). Stop the old process first."
    }
}

function Tail-IfExists([string]$Path, [int]$Lines = 40) {
    if (Test-Path $Path) {
        Write-Host ""
        Write-Host "---- $(Split-Path $Path -Leaf) ----"
        Get-Content $Path -Tail $Lines
    }
}

Set-Location $Root

$EnvFile = Join-Path $Root ".env"
if (-not (Test-Path $EnvFile)) {
    Copy-Item (Join-Path $Root ".env.example") $EnvFile
    Write-Host "Created local .env from .env.example (git-ignored)."
}

foreach ($Name in @("LLM_API_BASE", "LLM_API_KEY", "LLM_MODEL")) {
    $UserValue = [Environment]::GetEnvironmentVariable($Name, "User")
    if ($UserValue) { Set-Item "Env:$Name" $UserValue }
}

if (-not $NoDocker) {
    Write-Host "[1/4] Starting PostgreSQL and MinIO..."
    docker compose up -d
}

$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "Python virtualenv not found: $Python. Create .venv and install the project first."
}

Assert-PortFree 8000 "FastAPI"
if (-not $NoFrontend) { Assert-PortFree 5173 "Vite" }

Remove-Item (Join-Path $RuntimeDir "backend.out.log") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $RuntimeDir "backend.err.log") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $RuntimeDir "frontend.out.log") -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $RuntimeDir "frontend.err.log") -Force -ErrorAction SilentlyContinue

Write-Host "[2/4] Starting FastAPI..."
$BackendRunner = Join-Path $Root "scripts\run-backend.ps1"
$Backend = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $BackendRunner) -WorkingDirectory $Root -WindowStyle Hidden -PassThru
Set-Content -Path (Join-Path $RuntimeDir "backend.pid") -Value $Backend.Id

if (-not $NoFrontend) {
    Write-Host "[3/4] Starting Vue/Vite..."
    $FrontendRunner = Join-Path $Root "scripts\run-frontend.ps1"
    $Frontend = Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $FrontendRunner) -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $RuntimeDir "frontend.pid") -Value $Frontend.Id
}

Write-Host "[4/4] Waiting for backend..."
$Deadline = (Get-Date).AddSeconds(45)
$Alive = $false
while ((Get-Date) -lt $Deadline) {
    if (-not (Get-Process -Id $Backend.Id -ErrorAction SilentlyContinue)) { break }
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if ($Health.status -eq "ok") {
            $Alive = $true
            break
        }
    } catch {
        Start-Sleep -Milliseconds 700
    }
}

if (-not $Alive) {
    Write-Warning "FastAPI failed to stay alive."
    Tail-IfExists (Join-Path $RuntimeDir "backend.err.log")
    exit 1
}

if (-not $NoFrontend) {
    $FrontendDeadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $FrontendDeadline) {
        if (-not (Get-Process -Id $Frontend.Id -ErrorAction SilentlyContinue)) { break }
        if (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue)) {
        Write-Warning "Vite failed to stay alive."
        Tail-IfExists (Join-Path $RuntimeDir "frontend.err.log")
        exit 1
    }
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
} catch {
    Write-Warning "Could not read /ready yet."
}
