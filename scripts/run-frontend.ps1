$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
$WebRoot = Join-Path $Root "web"
Set-Location $WebRoot
& npm.cmd run dev -- --host 127.0.0.1 1>> (Join-Path $RuntimeDir "frontend.out.log") 2>> (Join-Path $RuntimeDir "frontend.err.log")
exit $LASTEXITCODE
