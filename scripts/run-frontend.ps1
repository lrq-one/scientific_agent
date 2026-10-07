$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
$WebRoot = Join-Path $Root "web"
$FrontendOut = Join-Path $RuntimeDir "frontend.out.log"
$FrontendErr = Join-Path $RuntimeDir "frontend.err.log"

Set-Location $WebRoot

try {
    & npm.cmd run dev -- --host 127.0.0.1 1>> $FrontendOut 2>> $FrontendErr
    exit $LASTEXITCODE
}
catch {
    $_ | Out-String | Add-Content -Path $FrontendErr
    exit 1
}
