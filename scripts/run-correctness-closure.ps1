$ErrorActionPreference='Stop'
$ClosureRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $ClosureRoot
foreach($ClosureName in @('LLM_API_BASE','LLM_API_KEY','LLM_MODEL')){
    $ClosureValue=[Environment]::GetEnvironmentVariable($ClosureName,'User')
    if($ClosureValue){Set-Item "Env:$ClosureName" $ClosureValue}
}
if($env:LLM_MODEL -ne 'qwen3.7-flash' -or !$env:LLM_API_KEY){throw 'Required Qwen configuration missing (key never printed)'}
$env:APP_MODE='evaluation'
$env:AGENT_EVALUATION_ENABLED='1'
$env:AGENT_EVALUATION_VARIANT='FULL'
$env:ENABLE_DEMO_DATA='false'
$env:ALLOW_DETERMINISTIC_LLM_FALLBACK='false'
$env:ADMIN_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:CHECKPOINT_DATABASE_URL=$env:ADMIN_DATABASE_URL
$env:DATABASE_URL='postgresql://agent_reader:reader_demo@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:EVALUATION_TELEMETRY_FILE=Join-Path $ClosureRoot 'reports/phase4a_correctness_closure_20261009/provider_attempts.jsonl'
& (Join-Path $ClosureRoot '.venv/Scripts/python.exe') -m uvicorn evaluation.p0_server:app --host 127.0.0.1 --port 8002
exit $LASTEXITCODE
