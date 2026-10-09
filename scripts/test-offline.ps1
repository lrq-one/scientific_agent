param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $PytestArgs
)

$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path
Set-Location $Root

$env:SCIENTIFIC_AGENT_TEST_PROFILE = "offline"
foreach ($name in @(
    "DATABASE_URL", "ADMIN_DATABASE_URL", "CHECKPOINT_DATABASE_URL",
    "TEST_POSTGRES_URL", "TEST_CHECKPOINT_URL", "MINIO_ENDPOINT",
    "TEST_MINIO_ENDPOINT", "TEST_MINIO_ACCESS_KEY", "TEST_MINIO_SECRET_KEY",
    "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "TEST_MINIO_BUCKET",
    "LLM_API_KEY", "OPENAI_API_KEY",
    "LLM_API_BASE", "OPENAI_API_BASE", "QWEN_API_KEY", "QWEN_BASE_URL"
)) {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}

if (-not $PytestArgs -or $PytestArgs.Count -eq 0) {
    $PytestArgs = @("tests", "-q")
}
& (Join-Path $Root ".venv\Scripts\python.exe") -m pytest @PytestArgs
exit $LASTEXITCODE
