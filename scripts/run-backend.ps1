$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
Set-Location $Root
& $Python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 1>> (Join-Path $RuntimeDir "backend.out.log") 2>> (Join-Path $RuntimeDir "backend.err.log")
exit $LASTEXITCODE
