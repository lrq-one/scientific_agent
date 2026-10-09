param([ValidateSet('FULL','NO_SKILL','NO_REPLAN','STATELESS_FOLLOWUP','NO_EVIDENCE_GATE')][string]$Variant='FULL', [string]$TelemetryFile)
$ErrorActionPreference='Stop'
$Root=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root
foreach ($Name in @('LLM_API_BASE','LLM_API_KEY','LLM_MODEL')) {
    $Value=[Environment]::GetEnvironmentVariable($Name,'User')
    if ($Value) { Set-Item "Env:$Name" $Value }
}
$env:APP_MODE='evaluation'
$env:AGENT_EVALUATION_ENABLED='1'
$env:AGENT_EVALUATION_VARIANT=$Variant
$env:ENABLE_DEMO_DATA='false'
$env:ALLOW_DETERMINISTIC_LLM_FALLBACK='false'
$env:ADMIN_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/phase4_eval_v2'
$env:CHECKPOINT_DATABASE_URL=$env:ADMIN_DATABASE_URL
$env:DATABASE_URL='postgresql://agent_reader:reader_demo@127.0.0.1:55432/phase4_eval_v2'
$env:EVALUATION_TELEMETRY_FILE=$TelemetryFile
& (Join-Path $Root '.venv\Scripts\python.exe') -m uvicorn evaluation.frozen.server:app --host 127.0.0.1 --port 8002
exit $LASTEXITCODE
