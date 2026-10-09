# Text-to-SQL contract

Generate one parameterized read-only query from authorized schema and the effective QueryScope. A generated query is not trusted until scope validation succeeds. Model-run lineage must be proven through experiments to dataset_versions; training membership uses the same version identity.
