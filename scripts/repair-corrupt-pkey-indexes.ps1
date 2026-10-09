# PostgreSQL 16 / scientific_agent: guarded repair for two confirmed corrupt PK indexes.
# This script never deletes volumes, restores over a database, or calls an LLM.
# Usage:
#   .\scripts\repair-corrupt-pkey-indexes.ps1           # read-only inspect
#   .\scripts\stop-dev.ps1 -KeepDocker
#   .\scripts\repair-corrupt-pkey-indexes.ps1 -Apply    # dump + verify + explicit consent + REINDEX
param([switch]$Apply)

$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Root

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker command failed (exit=$LASTEXITCODE): docker $($Arguments -join ' ')"
    }
}

Write-Host '[1/5] Inspect known relation files (read-only)' -ForegroundColor Cyan
$Inspect = @'
SELECT x.filenode,
       c.oid::regclass::text AS relation_name,
       c.relkind,
       pg_relation_filepath(c.oid) AS path
FROM (VALUES (16391), (16398), (16412), (16414), (16424)) x(filenode)
LEFT JOIN pg_class c ON c.oid = pg_filenode_relation(0, x.filenode);
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$Inspect)

if (-not $Apply) {
    Write-Host 'Inspection only. Nothing repaired. To apply, stop app services with -KeepDocker, then rerun with -Apply.'
    return
}

# A live backend may continue querying damaged indexes; stop it before repair.
$Listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($Listener) {
    throw 'FastAPI is still listening on port 8000. Stop app services first: .\scripts\stop-dev.ps1 -KeepDocker'
}

Write-Host '[2/5] Verify index names and sequentially scan underlying table heaps' -ForegroundColor Cyan
$IndexCheck = @'
SELECT c.oid::regclass::text AS index_name, c.relkind,
       c.relfilenode, i.indrelid::regclass::text AS indexed_table
FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid
WHERE c.oid IN ('public.molecules_pkey'::regclass,
                'public.training_molecules_pkey'::regclass);
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$IndexCheck)
$HeapCheck = @'
BEGIN READ ONLY;
SET LOCAL enable_indexscan = off;
SET LOCAL enable_indexonlyscan = off;
SET LOCAL enable_bitmapscan = off;
SELECT COUNT(*) AS molecules_rows FROM public.molecules;
SELECT COUNT(*) AS training_molecules_rows FROM public.training_molecules;
ROLLBACK;
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$HeapCheck)

Write-Host '[3/5] Back up the entire database OUTSIDE the Git repository' -ForegroundColor Cyan
$Stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$BackupDir = Join-Path (Split-Path $Root -Parent) 'scientific_agent_db_backups'
New-Item -Path $BackupDir -ItemType Directory -Force | Out-Null
$ContainerDump = "/tmp/scientific_agent_$Stamp.dump"
$HostDump = Join-Path $BackupDir "scientific_agent_$Stamp.dump"
Invoke-Docker @('compose','exec','-T','postgres','pg_dump',
    '-U','scientific','-d','scientific_agent','-Fc','-f',$ContainerDump)
Invoke-Docker @('compose','cp',"postgres:$ContainerDump",$HostDump)
if (-not (Test-Path $HostDump) -or (Get-Item $HostDump).Length -lt 1024) {
    throw 'Backup file missing or unexpectedly small. No REINDEX has been performed.'
}
# pg_restore --file=/dev/null reads/decompresses the archive without writing to the database.
Invoke-Docker @('compose','exec','-T','postgres','pg_restore',
    '--file=/dev/null',$ContainerDump)
$Hash = (Get-FileHash -Path $HostDump -Algorithm SHA256).Hash
Write-Host "Backup: $HostDump"
Write-Host "Backup SHA256: $Hash"
Write-Warning 'A successful logical dump is NOT proof that every PostgreSQL page is intact. Preserve the original Docker volume.'

Write-Host '[4/5] Require explicit index-rebuild consent' -ForegroundColor Yellow
$Confirm = Read-Host 'Type exactly REINDEX to rebuild public.molecules_pkey and public.training_molecules_pkey'
if ($Confirm -cne 'REINDEX') {
    Write-Host 'No indexes rebuilt; backup preserved.'
    return
}
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.molecules_pkey;')
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1',
    '-c','REINDEX INDEX public.training_molecules_pkey;')

Write-Host '[5/5] Read-only smoke checks' -ForegroundColor Cyan
$Smoke = @'
SELECT COUNT(*) AS molecules_rows FROM public.molecules;
SELECT COUNT(*) AS training_molecules_rows FROM public.training_molecules;
SELECT COUNT(*) AS joined_rows
FROM public.training_molecules tm JOIN public.molecules m
  ON m.molecule_id = tm.molecule_id;
'@
Invoke-Docker @('compose','exec','-T','postgres','psql',
    '-U','scientific','-d','scientific_agent','-X','-v','ON_ERROR_STOP=1','-c',$Smoke)
Write-Host 'Index rebuild checks passed. This does not prove other relations or the whole Agent are healthy.' -ForegroundColor Green
Write-Host "Keep backup: $HostDump"
