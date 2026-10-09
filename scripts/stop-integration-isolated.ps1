$ErrorActionPreference = "Stop"
$Project = "scientific_agent_p1_integration"
$Compose = Join-Path (Resolve-Path (Join-Path $PSScriptRoot "..\")).Path "docker-compose.integration.yml"
$containers = docker compose -p $Project -f $Compose ps -aq
if (-not $containers) {
    Write-Host "No isolated integration containers found. No action taken."
    exit 0
}
docker compose -p $Project -f $Compose down
Write-Host "Containers removed; isolated volumes were preserved. Use an explicit, reviewed volume removal only if the exact names are confirmed: scientific_agent_p1_integration_postgres_data and scientific_agent_p1_integration_minio_data"
