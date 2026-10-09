$ErrorActionPreference='Stop'
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
$Python=Join-Path $Root '.venv\Scripts\python.exe'
$Report=Join-Path $Root 'reports\phase4_v2_20261008'
foreach($Name in @('LLM_API_BASE','LLM_API_KEY','LLM_MODEL')){
    $Value=[Environment]::GetEnvironmentVariable($Name,'User')
    if($Value){Set-Item "Env:$Name" $Value}
}
function Stop-EvaluationBackend {
    $Listener=Get-NetTCPConnection -LocalPort 8002 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if(!$Listener){return}
    $Process=Get-CimInstance Win32_Process -Filter "ProcessId=$($Listener.OwningProcess)"
    if($Process.CommandLine -notlike '*evaluation.frozen.server*'){throw 'Port 8002 is not the evaluation server'}
    $Active= & $Python -m evaluation.frozen.inspect_records --active-count
    if($LASTEXITCODE -ne 0 -or [int]$Active -gt 0){throw 'Cannot stop an evaluation server with active tasks'}
    Stop-Process -Id $Listener.OwningProcess
}
if(!(Test-Path (Join-Path $Report 'freeze_manifest.json'))){throw 'Benchmark must be frozen first'}
$Variants=@('FULL','NO_SKILL','NO_REPLAN','STATELESS_FOLLOWUP','NO_EVIDENCE_GATE')
foreach($Variant in $Variants){
    & $Python -m evaluation.frozen.freeze --verify
    if($LASTEXITCODE -ne 0){throw 'Freeze verification failed'}
    Stop-EvaluationBackend
    & (Join-Path $Root 'scripts\start-evaluation.ps1') -Variant $Variant
    Push-Location (Join-Path $Root 'web')
    try{ & node scripts/frozenBenchmark.mjs $Variant.ToLower() }
    finally{Pop-Location}
    if($LASTEXITCODE -ne 0){throw "Formal harness failed in $Variant. Stop; preserve records and increment benchmark version before rerun."}
}
Stop-EvaluationBackend
& $Python -m evaluation.frozen.freeze --verify
if($LASTEXITCODE -ne 0){throw 'Final freeze verification failed'}
& $Python -m evaluation.frozen.inspect_records
if($LASTEXITCODE -ne 0){throw 'Inspection infrastructure failed'}
& $Python -m evaluation.frozen.evaluate
if($LASTEXITCODE -ne 0){throw 'Grading infrastructure failed; do not fake metrics'}
& $Python -m evaluation.frozen.report
if($LASTEXITCODE -ne 0){throw 'Reporting failed'}
Write-Output 'Frozen experiments, metrics and reports complete.'
