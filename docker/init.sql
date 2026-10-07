DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_reader') THEN
    CREATE ROLE agent_reader LOGIN PASSWORD 'reader_demo';
  END IF;
END $$;

ALTER ROLE agent_reader SET default_transaction_read_only = on;

CREATE TABLE IF NOT EXISTS molecules (
  molecule_id text PRIMARY KEY,
  name text NOT NULL,
  smiles text NOT NULL,
  structure_type text NOT NULL
);

CREATE TABLE IF NOT EXISTS training_molecules (
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  dataset_version text NOT NULL,
  is_cyclic boolean NOT NULL,
  PRIMARY KEY (molecule_id, dataset_version)
);

CREATE TABLE IF NOT EXISTS file_metadata (
  file_id uuid PRIMARY KEY,
  object_key text UNIQUE NOT NULL,
  owner_id text NOT NULL,
  thread_id text NOT NULL,
  filename text NOT NULL,
  content_type text NOT NULL,
  size bigint NOT NULL CHECK (size >= 0),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Product conversation/task history.  conversation_id and thread_id are
-- deliberately separate: one conversation may contain multiple agent runs.
CREATE TABLE IF NOT EXISTS conversations (
  id uuid PRIMARY KEY,
  user_id text NOT NULL,
  title text NOT NULL DEFAULT '新建科研任务',
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE INDEX IF NOT EXISTS conversations_user_updated_idx
  ON conversations (user_id, updated_at DESC) WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS tasks (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  thread_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('running', 'waiting_for_user', 'cancelling', 'cancelled', 'completed', 'failed')),
  intent_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  selected_skills_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  started_at timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz
);

CREATE INDEX IF NOT EXISTS tasks_conversation_started_idx
  ON tasks (conversation_id, started_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_conversation_thread_idx
  ON tasks (conversation_id, thread_id);
CREATE INDEX IF NOT EXISTS tasks_active_per_conversation_idx
  ON tasks (conversation_id) WHERE status IN ('running', 'waiting_for_user', 'cancelling');

CREATE TABLE IF NOT EXISTS messages (
  id uuid PRIMARY KEY,
  conversation_id uuid NOT NULL REFERENCES conversations(id),
  task_id uuid REFERENCES tasks(id),
  role text NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'error')),
  content text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS messages_conversation_created_idx
  ON messages (conversation_id, created_at, id);

CREATE TABLE IF NOT EXISTS task_events (
  id bigserial PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES tasks(id),
  event_type text NOT NULL,
  payload_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS task_events_task_created_idx
  ON task_events (task_id, created_at, id);

CREATE TABLE IF NOT EXISTS evidence (
  id uuid PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES tasks(id),
  claim text NOT NULL,
  value_json jsonb NOT NULL,
  source_type text NOT NULL,
  source text NOT NULL,
  tool_call_id text NOT NULL,
  local_evidence_id text,
  dataset_version text,
  model_version text,
  created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE evidence ADD COLUMN IF NOT EXISTS dataset_version text;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS model_version text;
ALTER TABLE evidence ADD COLUMN IF NOT EXISTS local_evidence_id text;

CREATE INDEX IF NOT EXISTS evidence_task_created_idx ON evidence (task_id, created_at);

CREATE TABLE IF NOT EXISTS claims (
  id uuid PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES tasks(id),
  claim_text text NOT NULL,
  status text NOT NULL CHECK (status IN ('supported', 'unsupported')),
  category text NOT NULL CHECK (category IN ('observation', 'interpretation')),
  evidence_ids_json jsonb NOT NULL DEFAULT '[]'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_task_created_idx ON claims (task_id, created_at);

CREATE TABLE IF NOT EXISTS artifacts (
  id uuid PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES tasks(id),
  artifact_type text NOT NULL,
  object_key text NOT NULL UNIQUE,
  filename text NOT NULL,
  metadata_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS artifacts_task_created_idx ON artifacts (task_id, created_at);

-- Scientific metadata graph used by schema retrieval and read-only analysis.
CREATE TABLE IF NOT EXISTS datasets (
  id uuid PRIMARY KEY,
  name text NOT NULL UNIQUE,
  description text NOT NULL,
  source_note text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS dataset_versions (
  id uuid PRIMARY KEY,
  dataset_id uuid NOT NULL REFERENCES datasets(id),
  version text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (dataset_id, version)
);

CREATE TABLE IF NOT EXISTS molecular_features (
  molecule_id text PRIMARY KEY REFERENCES molecules(molecule_id),
  molecular_weight double precision,
  ring_count integer NOT NULL DEFAULT 0,
  aromatic_ring_count integer NOT NULL DEFAULT 0,
  is_fused_ring boolean NOT NULL DEFAULT false,
  feature_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS training_memberships (
  dataset_version_id uuid NOT NULL REFERENCES dataset_versions(id),
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  split text NOT NULL CHECK (split IN ('train', 'validation', 'test')),
  PRIMARY KEY (dataset_version_id, molecule_id)
);

CREATE INDEX IF NOT EXISTS training_memberships_molecule_idx ON training_memberships (molecule_id);

CREATE TABLE IF NOT EXISTS experiments (
  id uuid PRIMARY KEY,
  name text NOT NULL,
  dataset_version_id uuid NOT NULL REFERENCES dataset_versions(id),
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS model_versions (
  id uuid PRIMARY KEY,
  model_name text NOT NULL,
  version text NOT NULL,
  description text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (model_name, version)
);

CREATE TABLE IF NOT EXISTS model_runs (
  id uuid PRIMARY KEY,
  experiment_id uuid NOT NULL REFERENCES experiments(id),
  model_version_id uuid NOT NULL REFERENCES model_versions(id),
  run_name text NOT NULL,
  metrics_json jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS model_runs_model_version_idx ON model_runs (model_version_id);

CREATE TABLE IF NOT EXISTS retention_time_measurements (
  id uuid PRIMARY KEY,
  dataset_version_id uuid NOT NULL REFERENCES dataset_versions(id),
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  retention_time double precision NOT NULL,
  unit text NOT NULL DEFAULT 'min',
  UNIQUE (dataset_version_id, molecule_id)
);

CREATE TABLE IF NOT EXISTS predictions (
  id uuid PRIMARY KEY,
  model_run_id uuid NOT NULL REFERENCES model_runs(id),
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  predicted_rt double precision NOT NULL,
  observed_rt double precision,
  absolute_error double precision GENERATED ALWAYS AS (abs(predicted_rt - observed_rt)) STORED,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (model_run_id, molecule_id)
);

CREATE INDEX IF NOT EXISTS predictions_molecule_idx ON predictions (molecule_id);
CREATE INDEX IF NOT EXISTS predictions_run_error_idx ON predictions (model_run_id, absolute_error DESC);

CREATE TABLE IF NOT EXISTS msms_spectra (
  id uuid PRIMARY KEY,
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  dataset_version_id uuid NOT NULL REFERENCES dataset_versions(id),
  precursor_mz double precision NOT NULL,
  peaks_json jsonb NOT NULL DEFAULT '[]'::jsonb
);

CREATE INDEX IF NOT EXISTS msms_spectra_molecule_idx ON msms_spectra (molecule_id);

CREATE TABLE IF NOT EXISTS annotations (
  id uuid PRIMARY KEY,
  molecule_id text NOT NULL REFERENCES molecules(molecule_id),
  annotation_type text NOT NULL,
  value_json jsonb NOT NULL,
  source text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS annotations_molecule_type_idx ON annotations (molecule_id, annotation_type);

INSERT INTO molecules (molecule_id, name, smiles, structure_type) VALUES
  ('T001', 'Synthetic ethanol', 'CCO', 'linear'),
  ('T002', 'Synthetic ethylamine', 'CCN', 'linear'),
  ('T003', 'Synthetic propane', 'CCC', 'linear'),
  ('T004', 'Synthetic cyclohexane', 'C1CCCCC1', 'cyclic'),
  ('T005', 'Synthetic tetrahydrofuran', 'C1CCOC1', 'cyclic'),
  ('T006', 'Synthetic benzene', 'c1ccccc1', 'aromatic'),
  ('T007', 'Synthetic naphthalene', 'c1ccc2ccccc2c1', 'fused_ring')
ON CONFLICT (molecule_id) DO UPDATE SET
  name = EXCLUDED.name,
  smiles = EXCLUDED.smiles,
  structure_type = EXCLUDED.structure_type;

INSERT INTO training_molecules (molecule_id, dataset_version, is_cyclic) VALUES
  ('T001', 'train_v3', false), ('T002', 'train_v3', false), ('T003', 'train_v3', false),
  ('T004', 'train_v3', true), ('T005', 'train_v3', true), ('T006', 'train_v3', true),
  ('T007', 'train_v3', true),
  ('T001', 'train_v2', false), ('T002', 'train_v2', false), ('T004', 'train_v2', true)
ON CONFLICT DO NOTHING;

INSERT INTO datasets (id, name, description, source_note) VALUES
  ('10000000-0000-0000-0000-000000000001', 'RT benchmark fixtures', 'Synthetic retention-time dataset for pipeline validation.', 'Synthetic fixture; not experimental evidence.')
ON CONFLICT (id) DO NOTHING;

INSERT INTO dataset_versions (id, dataset_id, version, description) VALUES
  ('11000000-0000-0000-0000-000000000002', '10000000-0000-0000-0000-000000000001', 'train_v2', 'Earlier synthetic training split.'),
  ('11000000-0000-0000-0000-000000000003', '10000000-0000-0000-0000-000000000001', 'train_v3', 'Expanded synthetic training split.')
ON CONFLICT (id) DO NOTHING;

INSERT INTO molecular_features (molecule_id, molecular_weight, ring_count, aromatic_ring_count, is_fused_ring, feature_json) VALUES
  ('T001', 46.07, 0, 0, false, '{"hetero_atoms":1}'),
  ('T002', 45.08, 0, 0, false, '{"hetero_atoms":1}'),
  ('T003', 44.10, 0, 0, false, '{"hetero_atoms":0}'),
  ('T004', 84.16, 1, 0, false, '{"hetero_atoms":0}'),
  ('T005', 72.11, 1, 0, false, '{"hetero_atoms":1}'),
  ('T006', 78.11, 1, 1, false, '{"hetero_atoms":0}'),
  ('T007', 128.17, 2, 2, true, '{"hetero_atoms":0}')
ON CONFLICT (molecule_id) DO UPDATE SET feature_json = EXCLUDED.feature_json;

INSERT INTO training_memberships (dataset_version_id, molecule_id, split) VALUES
  ('11000000-0000-0000-0000-000000000003', 'T001', 'train'),
  ('11000000-0000-0000-0000-000000000003', 'T002', 'train'),
  ('11000000-0000-0000-0000-000000000003', 'T003', 'train'),
  ('11000000-0000-0000-0000-000000000003', 'T004', 'train'),
  ('11000000-0000-0000-0000-000000000003', 'T005', 'validation'),
  ('11000000-0000-0000-0000-000000000003', 'T006', 'validation'),
  ('11000000-0000-0000-0000-000000000003', 'T007', 'test'),
  ('11000000-0000-0000-0000-000000000002', 'T001', 'train'),
  ('11000000-0000-0000-0000-000000000002', 'T002', 'train'),
  ('11000000-0000-0000-0000-000000000002', 'T004', 'test')
ON CONFLICT DO NOTHING;

INSERT INTO model_versions (id, model_name, version, description) VALUES
  ('12000000-0000-0000-0000-000000000001', 'TC-TopoRT', 'v1', 'Synthetic baseline model record.'),
  ('12000000-0000-0000-0000-000000000002', 'TC-TopoRT', 'v2', 'Synthetic comparison model record.')
ON CONFLICT (id) DO NOTHING;

INSERT INTO experiments (id, name, dataset_version_id, description) VALUES
  ('13000000-0000-0000-0000-000000000001', 'RT comparison fixture', '11000000-0000-0000-0000-000000000003', 'Synthetic reproducible comparison experiment.')
ON CONFLICT (id) DO NOTHING;

INSERT INTO model_runs (id, experiment_id, model_version_id, run_name, metrics_json) VALUES
  ('14000000-0000-0000-0000-000000000001', '13000000-0000-0000-0000-000000000001', '12000000-0000-0000-0000-000000000001', 'baseline-run', '{"mae":0.44,"rmse":0.55}'),
  ('14000000-0000-0000-0000-000000000002', '13000000-0000-0000-0000-000000000001', '12000000-0000-0000-0000-000000000002', 'candidate-run', '{"mae":0.36,"rmse":0.49}')
ON CONFLICT (id) DO NOTHING;

INSERT INTO retention_time_measurements (id, dataset_version_id, molecule_id, retention_time) VALUES
  ('15000000-0000-0000-0000-000000000001', '11000000-0000-0000-0000-000000000003', 'T001', 1.20),
  ('15000000-0000-0000-0000-000000000002', '11000000-0000-0000-0000-000000000003', 'T004', 3.10),
  ('15000000-0000-0000-0000-000000000003', '11000000-0000-0000-0000-000000000003', 'T007', 5.80)
ON CONFLICT (id) DO NOTHING;

INSERT INTO predictions (id, model_run_id, molecule_id, predicted_rt, observed_rt) VALUES
  ('16000000-0000-0000-0000-000000000001', '14000000-0000-0000-0000-000000000001', 'T001', 1.36, 1.20),
  ('16000000-0000-0000-0000-000000000002', '14000000-0000-0000-0000-000000000001', 'T004', 3.56, 3.10),
  ('16000000-0000-0000-0000-000000000003', '14000000-0000-0000-0000-000000000001', 'T007', 6.72, 5.80),
  ('16000000-0000-0000-0000-000000000004', '14000000-0000-0000-0000-000000000002', 'T001', 1.28, 1.20),
  ('16000000-0000-0000-0000-000000000005', '14000000-0000-0000-0000-000000000002', 'T004', 3.34, 3.10),
  ('16000000-0000-0000-0000-000000000006', '14000000-0000-0000-0000-000000000002', 'T007', 6.95, 5.80)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE training_molecules IS 'Synthetic demo fixtures only; not real scientific data.';
COMMENT ON TABLE molecules IS 'Synthetic demo molecule metadata only.';
COMMENT ON TABLE datasets IS 'Dataset catalog. Seed rows are synthetic fixtures and identify their source explicitly.';
COMMENT ON TABLE dataset_versions IS 'Versioned dataset snapshots used by experiments and membership records.';
COMMENT ON TABLE training_memberships IS 'Molecule membership and split for a dataset version.';
COMMENT ON TABLE predictions IS 'Per-molecule model-run predictions and observed retention times.';
COMMENT ON COLUMN predictions.absolute_error IS 'Generated absolute retention-time error.';

GRANT CONNECT ON DATABASE scientific_agent TO agent_reader;
GRANT USAGE ON SCHEMA public TO agent_reader;
GRANT SELECT ON molecules, training_molecules, datasets, dataset_versions, molecular_features,
  training_memberships, experiments, model_versions, model_runs, predictions,
  retention_time_measurements, msms_spectra, annotations TO agent_reader;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON molecules, training_molecules,
  datasets, dataset_versions, molecular_features, training_memberships, experiments,
  model_versions, model_runs, predictions, retention_time_measurements, msms_spectra,
  annotations FROM agent_reader;

