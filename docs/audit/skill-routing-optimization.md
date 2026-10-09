# Skill routing audit

`SkillService` still owns semantic selection; no case-specific workflow or
second Planner was introduced. Metadata is cached by the `SKILL.md` file
fingerprint, and telemetry records candidate count, catalog/prompt size,
fallback and usage. Capability and two-dataset-version guards remain before
selection. Selection semantics are unchanged until a paired benchmark proves a
safe recall-preserving filter.
