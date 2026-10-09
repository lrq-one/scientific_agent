# Safe, backup-first recovery for two PostgreSQL 16 primary-key indexes.
# No volumes are removed or replaced, and no app/LLM test is started.
# Usage:
#   .\scripts\repair-corrupt-pkey-indexes.ps1           # inspect only
#   .\scripts\stop-dev.ps1 -KeepDocker
#   .\scripts\repair-corrupt-pkey-indexes.ps1 -Apply    # cold snapshot first, REINDEX only with consent
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Invoke-Docker {
    $DockerArgs = @($args)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0) {
        throw ("Docker command exited with code {0}. Review the preceding Docker/psql error." -f $LASTEXITCODE)
    }
}

$InspectSQL = @'
SELECT x.filenode, c.oid::regclass::text AS relation_name, c.relkind,
       pg_relation_filepath(c.oid) AS path
FROM (VALUES (16391), (16398), (16412), (16414), (16424)) x(filenode)
LEFT JOIN pg_class c ON c.oid = pg_filenode_relation(0, x.filenode);
'@

if (-not $Apply) {
    Write-Host 'Inspection only: no data, image or index changes.' -ForegroundColor Cyan
    Invoke-Docker @('compose','exec','-T','postgres','psql',
        '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
        '-c',$InspectSQL)
    return
}

# The application's writes must stop before an offline copy and REINDEX.
foreach ($Port in @(8000,5173)) {
    $Listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($Listening) {
        throw "Application port $Port is still listening. First run .\scripts\stop-dev.ps1 -KeepDocker"
    }
}

# Verify Docker/Compose visibility before changing any service. A missing
# container is NOT permission to create a new PostgreSQL cluster or volume.
Write-Host '[Preflight] Resolve existing Compose PostgreSQL container (read-only)' -ForegroundColor Cyan
$ComposeResult = @(& docker compose ps --all --quiet postgres)
$ComposeExit = $LASTEXITCODE
$ContainerIDs = @($ComposeResult | Where-Object {
    $_ -is [string] -and $_.Trim() -match '^[0-9a-fA-F]{12,64}
$MountsJSON = docker inspect --format '{{json .Mounts}}' $ContainerID
if ($LASTEXITCODE -ne 0 -or -not $MountsJSON) { throw 'Cannot inspect PostgreSQL volume.' }
$DBMounts = @($MountsJSON | ConvertFrom-Json | Where-Object {
    $_.Type -eq 'volume' -and $_.Destination -eq '/var/lib/postgresql/data'
})
if ($DBMounts.Count -ne 1 -or -not $DBMounts[0].Name) {
    throw 'Expected one PostgreSQL named volume at /var/lib/postgresql/data; aborting.'
}
$VolumeName = [string]$DBMounts[0].Name
$Image = [string](docker inspect --format '{{.Config.Image}}' $ContainerID)
if ($LASTEXITCODE -ne 0 -or -not $Image -or $Image -notmatch '^postgres:16') {
    throw 'Expected existing PostgreSQL 16 image; aborting.'
}

$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupDir = Join-Path (Split-Path $Root -Parent) 'scientific_agent_db_backups'
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$ArchiveName = "pgdata_cold_$Stamp.tar"
$ArchivePath = Join-Path $BackupDir $ArchiveName
if (Test-Path $ArchivePath) { throw 'Backup archive name collision; aborting.' }

Write-Host "[1/5] Stop ONLY PostgreSQL for a consistent offline volume snapshot: $VolumeName" -ForegroundColor Cyan
Invoke-Docker @('compose','stop','postgres')
$Running = [string](docker inspect --format '{{.State.Running}}' $ContainerID)
if ($LASTEXITCODE -ne 0 -or $Running.Trim() -ne 'false') {
    throw 'PostgreSQL did not stop cleanly; no backup or index repair attempted.'
}

Write-Host "[2/5] Copy existing PostgreSQL volume to host backup: $ArchivePath" -ForegroundColor Cyan
try {
    # Reuse an already-present Postgres image; do not pull or reset images.
    # Source mount is explicitly read-only; snapshot lives OUTSIDE this Git repo.
    Invoke-Docker @('run','--rm','--network','none','--pull=never',
        '--mount',"type=volume,source=$VolumeName,target=/source,readonly",
        '--mount',"type=bind,source=$BackupDir,target=/backup",
        $Image,'sh','-ec',"tar -C /source -cf /backup/$ArchiveName .; tar -tf /backup/$ArchiveName > /dev/null")
} catch {
    Write-Warning "Cold snapshot failed. PostgreSQL is STOPPED; no REINDEX performed. Error: $_"
    throw
}
if (-not (Test-Path $ArchivePath) -or (Get-Item $ArchivePath).Length -lt 1024) {
    throw 'Cold snapshot missing or suspiciously small. PostgreSQL remains stopped; no REINDEX performed.'
}
$Hash = (Get-FileHash -Path $ArchivePath -Algorithm SHA256).Hash
Write-Host "Cold backup: $ArchivePath"
Write-Host "SHA256: $Hash"
Write-Warning 'The archive is verified as a readable TAR, NOT as a healthy PostgreSQL database. Keep it unchanged.'

Write-Host '[3/5] Restart same existing PostgreSQL container; verify index identity' -ForegroundColor Cyan
Invoke-Docker @('compose','up','-d','--no-recreate','postgres')
$DBReady = $false
for ($attempt = 1; $attempt -le 20; $attempt++) {
    & docker compose exec -T postgres pg_isready -U scientific -d scientific_agent | Out-Null
    if ($LASTEXITCODE -eq 0) { $DBReady = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $DBReady) { throw 'PostgreSQL did not become ready; no REINDEX performed.' }

$IdentitySQL = @'
SELECT c.relname, i.indrelid::regclass::text AS indexed_table, c.relkind
FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.oid IN ('public.molecules_pkey'::regclass,
                'public.training_molecules_pkey'::regclass)
ORDER BY c.relname;
'@
$IndexArguments = @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-At',
    '-c',$IdentitySQL)
$IndexRows = @(& docker @IndexArguments)
if ($LASTEXITCODE -ne 0 -or $IndexRows.Count -ne 2 -or
    $IndexRows -notcontains 'molecules_pkey|molecules|i' -or
    $IndexRows -notcontains 'training_molecules_pkey|training_molecules|i') {
    throw 'Unexpected index identity. No REINDEX attempted.'
}
$IndexRows | ForEach-Object { Write-Host $_ }

# The former COUNT(*) "heap check" has intentionally been removed:
# it itself hit the broken index and prevented any backup from being made.
Write-Host '[4/5] Optional logical dump (physical cold backup is already secured)' -ForegroundColor Cyan
$DumpName = "scientific_agent_$Stamp.dump"
$DumpInContainer = "/tmp/$DumpName"
$DumpOnHost = Join-Path $BackupDir $DumpName
$LogicalBackupOK = $false
try {
    Invoke-Docker @('compose','exec','-T','postgres','pg_dump',
        '-U','scientific','-d','scientific_agent','-Fc','-f',$DumpInContainer)
    Invoke-Docker @('compose','cp',"postgres:$DumpInContainer",$DumpOnHost)
    if ((Get-Item $DumpOnHost).Length -lt 1024) { throw 'Logical dump unexpectedly small' }
    Invoke-Docker @('compose','exec','-T','postgres','pg_restore',
        '--file=/dev/null',$DumpInContainer)
    $LogicalBackupOK = $true
    Write-Host "Logical backup: $DumpOnHost"
} catch {
    Write-Warning "Logical dump could not be verified; possibly due to the damaged index. Error: $_"
    Write-Warning 'Proceed ONLY if you accept the risk; the cold volume snapshot exists, but may contain corruption.'
}
Write-Host "Physical snapshot saved: $ArchivePath"
Write-Host "Logical backup verified: $LogicalBackupOK"

$Confirm = Read-Host 'Type exactly REINDEX to rebuild ONLY public.molecules_pkey and public.training_molecules_pkey (anything else stops)'
if ($Confirm -cne 'REINDEX') {
    Write-Host 'No indexes rebuilt. Snapshot preserved.'
    return
}

Write-Host '[5/5] Rebuild only confirmed corrupt indexes; do not delete any data' -ForegroundColor Yellow
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.molecules_pkey;')
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.training_molecules_pkey;')

# Query checks after repair can still fail if any other object is corrupt.
$SmokeSQL = @'
SELECT COUNT(*) AS molecules_rows FROM public.molecules;
SELECT COUNT(*) AS training_molecules_rows FROM public.training_molecules;
SELECT COUNT(*) AS joined_rows
FROM public.training_molecules tm JOIN public.molecules m
    ON m.molecule_id = tm.molecule_id;
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$SmokeSQL)
Write-Host 'Index rebuild and targeted query smoke checks passed.' -ForegroundColor Green
Write-Host "Preserve snapshot for later recovery: $ArchivePath"

} | ForEach-Object { $_.Trim() })
if ($ComposeExit -ne 0 -or $ContainerIDs.Count -ne 1) {
    Write-Warning "Could not uniquely resolve PostgreSQL from this Compose project (exit=$ComposeExit, count=$($ContainerIDs.Count))."
    Write-Host '--- docker compose ps --all ---'
    & docker compose ps --all
    Write-Host '--- docker ps --all: Compose-labelled PostgreSQL containers ---'
    & docker ps --all --filter 'label=com.docker.compose.service=postgres' --format 'table {{.ID}}\t{{.Names}}\t{{.Status}}\t{{.Label "com.docker.compose.project"}}'
    Write-Host '--- existing PostgreSQL-labelled volumes (read-only) ---'
    & docker volume ls --filter 'label=com.docker.compose.volume=postgres_data'
    throw 'No unique PostgreSQL container in the current Compose project. No database or volume was changed. Check Docker context, Compose project name, and existing volumes before retrying.'
}
$ContainerID = [string]$ContainerIDs[0]
$MountsJSON = docker inspect --format '{{json .Mounts}}' $ContainerID
if ($LASTEXITCODE -ne 0 -or -not $MountsJSON) { throw 'Cannot inspect PostgreSQL volume.' }
$DBMounts = @($MountsJSON | ConvertFrom-Json | Where-Object {
    $_.Type -eq 'volume' -and $_.Destination -eq '/var/lib/postgresql/data'
})
if ($DBMounts.Count -ne 1 -or -not $DBMounts[0].Name) {
    throw 'Expected one PostgreSQL named volume at /var/lib/postgresql/data; aborting.'
}
$VolumeName = [string]$DBMounts[0].Name
$Image = [string](docker inspect --format '{{.Config.Image}}' $ContainerID)
if ($LASTEXITCODE -ne 0 -or -not $Image -or $Image -notmatch '^postgres:16') {
    throw 'Expected existing PostgreSQL 16 image; aborting.'
}

$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupDir = Join-Path (Split-Path $Root -Parent) 'scientific_agent_db_backups'
New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$ArchiveName = "pgdata_cold_$Stamp.tar"
$ArchivePath = Join-Path $BackupDir $ArchiveName
if (Test-Path $ArchivePath) { throw 'Backup archive name collision; aborting.' }

Write-Host "[1/5] Stop ONLY PostgreSQL for a consistent offline volume snapshot: $VolumeName" -ForegroundColor Cyan
Invoke-Docker @('compose','stop','postgres')
$Running = [string](docker inspect --format '{{.State.Running}}' $ContainerID)
if ($LASTEXITCODE -ne 0 -or $Running.Trim() -ne 'false') {
    throw 'PostgreSQL did not stop cleanly; no backup or index repair attempted.'
}

Write-Host "[2/5] Copy existing PostgreSQL volume to host backup: $ArchivePath" -ForegroundColor Cyan
try {
    # Reuse an already-present Postgres image; do not pull or reset images.
    # Source mount is explicitly read-only; snapshot lives OUTSIDE this Git repo.
    Invoke-Docker @('run','--rm','--network','none','--pull=never',
        '--mount',"type=volume,source=$VolumeName,target=/source,readonly",
        '--mount',"type=bind,source=$BackupDir,target=/backup",
        $Image,'sh','-ec',"tar -C /source -cf /backup/$ArchiveName .; tar -tf /backup/$ArchiveName > /dev/null")
} catch {
    Write-Warning "Cold snapshot failed. PostgreSQL is STOPPED; no REINDEX performed. Error: $_"
    throw
}
if (-not (Test-Path $ArchivePath) -or (Get-Item $ArchivePath).Length -lt 1024) {
    throw 'Cold snapshot missing or suspiciously small. PostgreSQL remains stopped; no REINDEX performed.'
}
$Hash = (Get-FileHash -Path $ArchivePath -Algorithm SHA256).Hash
Write-Host "Cold backup: $ArchivePath"
Write-Host "SHA256: $Hash"
Write-Warning 'The archive is verified as a readable TAR, NOT as a healthy PostgreSQL database. Keep it unchanged.'

Write-Host '[3/5] Restart same existing PostgreSQL container; verify index identity' -ForegroundColor Cyan
Invoke-Docker @('compose','up','-d','--no-recreate','postgres')
$DBReady = $false
for ($attempt = 1; $attempt -le 20; $attempt++) {
    & docker compose exec -T postgres pg_isready -U scientific -d scientific_agent | Out-Null
    if ($LASTEXITCODE -eq 0) { $DBReady = $true; break }
    Start-Sleep -Seconds 1
}
if (-not $DBReady) { throw 'PostgreSQL did not become ready; no REINDEX performed.' }

$IdentitySQL = @'
SELECT c.relname, i.indrelid::regclass::text AS indexed_table, c.relkind
FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.oid IN ('public.molecules_pkey'::regclass,
                'public.training_molecules_pkey'::regclass)
ORDER BY c.relname;
'@
$IndexArguments = @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-At',
    '-c',$IdentitySQL)
$IndexRows = @(& docker @IndexArguments)
if ($LASTEXITCODE -ne 0 -or $IndexRows.Count -ne 2 -or
    $IndexRows -notcontains 'molecules_pkey|molecules|i' -or
    $IndexRows -notcontains 'training_molecules_pkey|training_molecules|i') {
    throw 'Unexpected index identity. No REINDEX attempted.'
}
$IndexRows | ForEach-Object { Write-Host $_ }

# The former COUNT(*) "heap check" has intentionally been removed:
# it itself hit the broken index and prevented any backup from being made.
Write-Host '[4/5] Optional logical dump (physical cold backup is already secured)' -ForegroundColor Cyan
$DumpName = "scientific_agent_$Stamp.dump"
$DumpInContainer = "/tmp/$DumpName"
$DumpOnHost = Join-Path $BackupDir $DumpName
$LogicalBackupOK = $false
try {
    Invoke-Docker @('compose','exec','-T','postgres','pg_dump',
        '-U','scientific','-d','scientific_agent','-Fc','-f',$DumpInContainer)
    Invoke-Docker @('compose','cp',"postgres:$DumpInContainer",$DumpOnHost)
    if ((Get-Item $DumpOnHost).Length -lt 1024) { throw 'Logical dump unexpectedly small' }
    Invoke-Docker @('compose','exec','-T','postgres','pg_restore',
        '--file=/dev/null',$DumpInContainer)
    $LogicalBackupOK = $true
    Write-Host "Logical backup: $DumpOnHost"
} catch {
    Write-Warning "Logical dump could not be verified; possibly due to the damaged index. Error: $_"
    Write-Warning 'Proceed ONLY if you accept the risk; the cold volume snapshot exists, but may contain corruption.'
}
Write-Host "Physical snapshot saved: $ArchivePath"
Write-Host "Logical backup verified: $LogicalBackupOK"

$Confirm = Read-Host 'Type exactly REINDEX to rebuild ONLY public.molecules_pkey and public.training_molecules_pkey (anything else stops)'
if ($Confirm -cne 'REINDEX') {
    Write-Host 'No indexes rebuilt. Snapshot preserved.'
    return
}

Write-Host '[5/5] Rebuild only confirmed corrupt indexes; do not delete any data' -ForegroundColor Yellow
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.molecules_pkey;')
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.training_molecules_pkey;')

# Query checks after repair can still fail if any other object is corrupt.
$SmokeSQL = @'
SELECT COUNT(*) AS molecules_rows FROM public.molecules;
SELECT COUNT(*) AS training_molecules_rows FROM public.training_molecules;
SELECT COUNT(*) AS joined_rows
FROM public.training_molecules tm JOIN public.molecules m
    ON m.molecule_id = tm.molecule_id;
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$SmokeSQL)
Write-Host 'Index rebuild and targeted query smoke checks passed.' -ForegroundColor Green
Write-Host "Preserve snapshot for later recovery: $ArchivePath"
