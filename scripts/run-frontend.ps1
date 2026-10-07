$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$RuntimeDir = Join-Path $Root ".runtime"
$WebRoot = Join-Path $Root "web"
$FrontendOut = Join-Path $RuntimeDir "frontend.out.log"
$FrontendErr = Join-Path $RuntimeDir "frontend.err.log"

Set-Location $WebRoot

try {
    $Process = Start-Process -FilePath "npm.cmd" -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1") -WorkingDirectory $WebRoot -RedirectStandardOutput $FrontendOut -RedirectStandardError $FrontendErr -NoNewWindow -Wait -PassThru
    exit $Process.ExitCode
}
catch {
    $_ | Out-String | Add-Content -Path $FrontendErr
    exit 1
}
