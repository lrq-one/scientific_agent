$ErrorActionPreference='Stop'
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
foreach($Name in @('LLM_API_BASE','LLM_API_KEY','LLM_MODEL')){
    $Value=[Environment]::GetEnvironmentVariable($Name,'User')
    if($Value){Set-Item "Env:$Name" $Value}
}
if($env:LLM_MODEL -ne 'qwen3.7-flash' -or !$env:LLM_API_KEY){throw 'Required live Qwen configuration missing (key never printed)'}
$env:APP_MODE='evaluation'
$env:AGENT_EVALUATION_ENABLED='1'
$env:AGENT_EVALUATION_VARIANT='FULL'
$env:ENABLE_DEMO_DATA='false'
$env:ALLOW_DETERMINISTIC_LLM_FALLBACK='false'
$env:ADMIN_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:CHECKPOINT_DATABASE_URL=$env:ADMIN_DATABASE_URL
$env:DATABASE_URL='postgresql://agent_reader:reader_demo@127.0.0.1:55432/phase4a_p0_development_20261008'
$env:EVALUATION_TELEMETRY_FILE=Join-Path $Root 'reports/phase4a_p0_hardening_20261008/provider_attempts.jsonl'
& (Join-Path $Root '.venv/Scripts/python.exe') -m uvicorn evaluation.p0_server:app --host 127.0.0.1 --port 8002
exit $LASTEXITCODE
