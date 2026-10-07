$ErrorActionPreference = "Continue"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"

function Show-RecordedProcess([string]$Name) {
    $PidFile = Join-Path $RuntimeDir "$Name.pid"
    if (-not (Test-Path $PidFile)) {
        Write-Host ("{0,-10} not recorded" -f $Name)
        return
    }
    $RecordedPid = (Get-Content $PidFile | Select-Object -First 1)
    $Process = Get-Process -Id $RecordedPid -ErrorAction SilentlyContinue
    if ($Process) {
        Write-Host ("{0,-10} running (PID {1})" -f $Name, $RecordedPid)
    } else {
        Write-Host ("{0,-10} stale pid file ({1})" -f $Name, $RecordedPid)
    }
}

Write-Host "== Processes =="
Show-RecordedProcess "backend"
Show-RecordedProcess "frontend"

Write-Host ""
Write-Host "== Docker =="
Set-Location $Root
docker compose ps

Write-Host ""
Write-Host "== API =="
try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3
    Write-Host "liveness:" $Health.status
} catch {
    Write-Host "liveness: unavailable"
}

try {
    $Ready = Invoke-RestMethod -Uri "http://127.0.0.1:8000/ready" -TimeoutSec 5
    Write-Host "readiness:" $Ready.status
    $Ready.components.PSObject.Properties | ForEach-Object {
        $Value = $_.Value
        Write-Host ("  {0,-16} {1} ready={2}" -f $_.Name, $Value.status, $Value.ready)
    }
} catch {
    Write-Host "readiness: unavailable"
}
