param([string]$Variant='FULL',[switch]$Frontend,[switch]$Development,[ValidatePattern('^[A-Za-z0-9_-]+$')][string]$DevelopmentName='development')
$ErrorActionPreference='Stop'
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Name=$Variant.ToLower()
$Output=Join-Path $Root "reports\phase4_v2_20261008\runs\$Name"
if($Development){$Output=Join-Path $Root "reports\phase4_v2_20261008\$DevelopmentName"}
New-Item -ItemType Directory -Path $Output -Force | Out-Null
if(Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue){throw 'Evaluation port 8002 is occupied'}
$Script=Join-Path $Root 'scripts\run-evaluation.ps1'
$Telemetry=Join-Path $Output 'provider_attempts.jsonl'
if(Test-Path $Telemetry){throw 'Refusing to overwrite existing provider attempts'}
$Backend=Start-Process powershell.exe -ArgumentList @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',"`"$Script`"",'-Variant',$Variant,'-TelemetryFile',"`"$Telemetry`"") -WindowStyle Hidden -WorkingDirectory $Root -RedirectStandardOutput (Join-Path $Output 'backend.out.log') -RedirectStandardError (Join-Path $Output 'backend.err.log') -PassThru
if($Frontend){
    if(Get-NetTCPConnection -LocalPort 5175 -State Listen -ErrorAction SilentlyContinue){throw 'Evaluation frontend port occupied'}
    $env:VITE_API_BASE='http://127.0.0.1:8002'
    $Vite=Start-Process node.exe -ArgumentList @('node_modules/vite/bin/vite.js','--host','127.0.0.1','--port','5175','--strictPort') -WindowStyle Hidden -WorkingDirectory (Join-Path $Root 'web') -RedirectStandardOutput (Join-Path $Output 'frontend.out.log') -RedirectStandardError (Join-Path $Output 'frontend.err.log') -PassThru
    Remove-Item Env:VITE_API_BASE
    Write-Output "Evaluation frontend PID=$($Vite.Id)"
}
Write-Output "Evaluation launcher PID=$($Backend.Id), variant=$Variant"
