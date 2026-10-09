param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
Set-Location $Root
$Project = "scientific_agent_p1_integration"
$Compose = Join-Path $Root "docker-compose.integration.yml"
$PostgresVolume = "scientific_agent_p1_integration_postgres_data"
$MinioVolume = "scientific_agent_p1_integration_minio_data"

if (-not (Test-Path $Compose)) { throw "Missing isolated compose file: $Compose" }
if ((docker compose -p $Project -f $Compose ps -aq) -or (docker volume ls -q --filter "name=^${PostgresVolume}$") -or (docker volume ls -q --filter "name=^${MinioVolume}$")) {
    throw "Refusing to reuse existing isolated resources. Inspect or remove only this exact project/volume set first."
}
foreach ($port in @(55433, 9002, 9003)) {
    if ((Test-NetConnection -ComputerName 127.0.0.1 -Port $port -InformationLevel Quiet)) {
        throw "Refusing to start isolated integration service: port $port is already in use."
    }
}

docker compose -p $Project -f $Compose up -d --wait
if ($LASTEXITCODE -ne 0) { throw "Isolated integration services failed to start" }

$env:SCIENTIFIC_AGENT_TEST_PROFILE = "integration_isolated"
$env:APP_MODE = "test"
$env:ENABLE_DEMO_DATA = "true"
$env:ALLOW_DETERMINISTIC_LLM_FALLBACK = "false"
$env:ADMIN_DATABASE_URL = "postgresql://scientific:scientific@127.0.0.1:55433/scientific_agent"
$env:CHECKPOINT_DATABASE_URL = $env:ADMIN_DATABASE_URL
$env:DATABASE_URL = "postgresql://agent_reader:reader_demo@127.0.0.1:55433/scientific_agent"
$env:TEST_POSTGRES_URL = $env:DATABASE_URL
$env:TEST_CHECKPOINT_URL = $env:ADMIN_DATABASE_URL
$env:MINIO_ENDPOINT = "127.0.0.1:9002"
$env:MINIO_ACCESS_KEY = "minioadmin"
$env:MINIO_SECRET_KEY = "change-me"
$env:MINIO_BUCKET = "scientific-agent-p1-integration"
$env:TEST_MINIO_ENDPOINT = $env:MINIO_ENDPOINT
$env:TEST_MINIO_ACCESS_KEY = $env:MINIO_ACCESS_KEY
$env:TEST_MINIO_SECRET_KEY = $env:MINIO_SECRET_KEY
$env:TEST_MINIO_BUCKET = $env:MINIO_BUCKET
foreach ($name in @("LLM_API_KEY", "OPENAI_API_KEY", "LLM_API_BASE", "OPENAI_API_BASE", "QWEN_API_KEY", "QWEN_BASE_URL")) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
    $PytestArgs = @(
        "tests/test_postgres_runtime.py",
        "tests/test_persistent_checkpoint.py",
        "tests/test_minio_file_flow.py",
        "tests/test_p1_langgraph_integration.py",
        "-q"
    )
}
& (Join-Path $Root ".venv\Scripts\python.exe") -m pytest @PytestArgs
$code = $LASTEXITCODE
Write-Host "Isolated resources remain for inspection: project=$Project, postgres port=55433, minio port=9002"
exit $code
