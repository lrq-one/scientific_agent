$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$BackendOut = Join-Path $RuntimeDir "backend.out.log"
$BackendErr = Join-Path $RuntimeDir "backend.err.log"

Set-Location $Root

try {
    $Process = Start-Process -FilePath $Python -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8000") -WorkingDirectory $Root -RedirectStandardOutput $BackendOut -RedirectStandardError $BackendErr -NoNewWindow -Wait -PassThru
    exit $Process.ExitCode
}
catch {
    $_ | Out-String | Add-Content -Path $BackendErr
    exit 1
}
