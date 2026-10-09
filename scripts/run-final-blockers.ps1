$ErrorActionPreference='Stop'
$BlockerRoot=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $BlockerRoot
foreach($BlockerName in @('LLM_API_BASE','LLM_API_KEY','LLM_MODEL')){
    $BlockerValue=[Environment]::GetEnvironmentVariable($BlockerName,'User')
    if($BlockerValue){Set-Item "Env:$BlockerName" $BlockerValue}
}
if($env:LLM_MODEL -ne 'qwen3.7-flash' -or !$env:LLM_API_KEY){throw 'Qwen configuration missing (key never printed)'}
$env:APP_MODE='evaluation'
$env:AGENT_EVALUATION_ENABLED='1'
$env:AGENT_EVALUATION_VARIANT='FULL'
$env:ENABLE_DEMO_DATA='false'
$env:ALLOW_DETERMINISTIC_LLM_FALLBACK='false'
$env:ADMIN_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:CHECKPOINT_DATABASE_URL=$env:ADMIN_DATABASE_URL
$env:DATABASE_URL='postgresql://agent_reader:reader_demo@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:EVALUATION_TELEMETRY_FILE=Join-Path $BlockerRoot 'reports/phase4a_final_blocker_resolution_20261009/provider_attempts.jsonl'
& (Join-Path $BlockerRoot '.venv/Scripts/python.exe') -m uvicorn evaluation.p0_server:app --host 127.0.0.1 --port 8002
exit $LASTEXITCODE
