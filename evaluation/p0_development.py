"""P0 development namespace only. Never recreate or mutate a frozen snapshot."""
from pathlib import Path
import hashlib
import json
import sys
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'reports/phase4a_p0_hardening_20261008'
DATABASE = 'phase4a_p0_development_20261008'
ADMIN = 'postgresql://scientific:scientific@127.0.0.1:55432/'
TABLES = ['molecules','training_molecules','datasets','dataset_versions','molecular_features','training_memberships',
          'experiments','model_versions','model_runs','retention_time_measurements','predictions','msms_spectra','annotations']

def snapshot(database):
    data = {}
    with psycopg.connect(ADMIN + database, row_factory=dict_row) as c:
        c.execute('SET TRANSACTION READ ONLY')
        for table in TABLES:
            rows = [{k:v for k,v in r.items() if k!='created_at'} for r in c.execute('SELECT * FROM '+table)]
            data[table] = sorted(rows,key=lambda r:json.dumps(r,sort_keys=True,default=str))
    return hashlib.sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()

def prepare():
    OUT.mkdir(parents=True,exist_ok=True)
    frozen = json.loads((ROOT/'reports/phase4_v2_20261008/freeze_manifest.json').read_text(encoding='utf-8'))
    before = snapshot('phase4_eval_v2')
    assert before == frozen['database_snapshot']['sha256']
    with psycopg.connect(ADMIN+'postgres',autocommit=True) as c:
        if c.execute('SELECT 1 FROM pg_database WHERE datname=%s',(DATABASE,)).fetchone():
            raise RuntimeError('Development database already exists; refusing overwrite')
        c.execute('CREATE DATABASE '+DATABASE+' TEMPLATE phase4_eval_v2')
    # Only the brand-new named clone's interaction records are removed. Original
    # frozen scientific rows and all original conversations remain untouched.
    with psycopg.connect(ADMIN+DATABASE) as c:
        assert c.execute('SELECT current_database()').fetchone()[0] == DATABASE
        c.execute('TRUNCATE conversations CASCADE')
        c.execute('GRANT CONNECT ON DATABASE '+DATABASE+' TO agent_reader')
    assert snapshot(DATABASE) == before
    originals = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in (ROOT/'reports/phase4_v2_20261008').rglob('*') if p.is_file()}
    report = {'label':'NOT FROZEN TEST / NOT UNSEEN EVALUATION','development_database':DATABASE,
        'frozen_scientific_snapshot_sha256':before,'development_snapshot_sha256':snapshot(DATABASE),
        'original_report_hashes':originals,'original_baseline':'29/60 permanently preserved'}
    (OUT/'development_namespace.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('Fresh development DB clone ready; scientific snapshot matches frozen; original unchanged')

if __name__=='__main__':
    prepare() if '--prepare' in sys.argv else print(snapshot(DATABASE))
