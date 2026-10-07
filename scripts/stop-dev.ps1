param(
    [switch]$KeepDocker
)

$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"

function Stop-RecordedProcess([string]$Name) {
    $PidFile = Join-Path $RuntimeDir "$Name.pid"
    if (-not (Test-Path $PidFile)) { return }
    $RecordedPid = (Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($RecordedPid -and ($RecordedPid -match "^\d+$")) {
        Write-Host "Stopping $Name process tree (PID $RecordedPid)..."
        & taskkill.exe /PID $RecordedPid /T /F 2>$null | Out-Null
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
}

Stop-RecordedProcess "frontend"
Stop-RecordedProcess "backend"

if (-not $KeepDocker) {
    Set-Location $Root
    Write-Host "Stopping Docker services..."
    docker compose stop | Out-Host
}

Write-Host "Development runtime stopped."
