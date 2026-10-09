"""Typed resource identity resolution, never task/tool routing or name-pattern guessing."""
from __future__ import annotations
import re
import hashlib
import json
from app.models.schemas import ResourceBinding, ResourceSummary, QueryScope


def independent_population_intent(state):
    query = state.user_request or state.goal
    return bool(re.search(r"全部版本|所有版本|全版本|all\s+versions", query, re.I) and any(
        any(mentioned(v.get(k), query) for k in ("id", "version"))
        for v in state.resource_summary.resource_metadata.get("dataset_versions", [])))


def bind_populations(state, requests):
    """Compile quoted original intent against authorized metadata, not plan prose.

    Explicit all-version requests require an exact allowed-version SQL bound.
    Unsupported/ambiguous constraints remain rejected, not guessed or broadened.
    """
    from app.models.schemas import PopulationRequirement
    original = state.user_request or state.goal
    compiled = []
    for request in requests:
        text = request.source_text.strip()
        if not text or text not in original or original.count(text) != 1:
            raise ValueError("Population source_text must uniquely quote the original user request")
        proposed = request.query_scope
        all_versions = bool(re.search(r"全部版本|所有版本|全版本|all\s+versions", text, re.I))
        if proposed.all_versions != all_versions:
            raise ValueError("Population all-version scope is not supported by original intent span")
        binding = resolve_binding(text, state.resource_summary, datasource=state.datasource_id)
        if proposed.datasource_id and proposed.datasource_id != binding.datasource_id:
            raise ValueError("Population datasource is not authorized by the resource binding")
        if binding.datasource_id not in state.resource_summary.authorized_datasources:
            raise ValueError("Population requires an authorized datasource")
        if not all_versions and "dataset_version" in binding.ambiguous_fields:
            raise ValueError("Population version is ambiguous; quote the requested version or clarify")
        scope = resolve_scope(text, binding)
        # Explicit field=value clauses are deterministic constraints; the
        # model cannot omit/change them in its local population proposal.
        for field, value in re.findall(r"\b([A-Za-z_]\w*)\s*=\s*['\"]([^'\"]+)['\"]", text):
            if field not in {"dataset_version", "dataset_version_id", "split"}:
                if proposed.filters.get(field) != value:
                    raise ValueError("Population omitted an explicit original field=value constraint")
        identities = {str(v.get(k)) for v in state.resource_summary.resource_metadata.get("dataset_versions", [])
                      for k in ("id", "version")} | set(binding.run_ids + binding.run_labels)
        literal_categories = set(re.findall(r"\b[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)+\b", text)) - identities
        if literal_categories - {str(v) for v in proposed.filters.values()}:
            raise ValueError("Population omitted an explicit categorical literal from its original intent span")
        if all_versions:
            scope.dataset_version = scope.dataset_version_id = None
            # "all versions" is a version quantifier, not permission to drop
            # an explicitly requested train/validation/test population.
            requested_splits = [s for s in ("train", "validation", "test") if mentioned(s, text)]
            if not requested_splits and re.search(r"训练集|训练子集", text):
                requested_splits = ["train"]
            all_splits = bool(re.search(r"所有\s*split|all\s+splits|整个数据集|whole\s+dataset", text, re.I))
            scope.split = requested_splits[0] if len(requested_splits) == 1 and not all_splits else None
            scope.whole_dataset = scope.split is None
            scope.comparison_target = requested_splits if len(requested_splits) > 1 or all_splits else []
            scope.all_versions = True
            versions = [v for v in state.resource_summary.resource_metadata.get("dataset_versions", [])
                        if v.get("datasource_id") == binding.datasource_id and
                        (not binding.dataset_id or str(v.get("dataset_id")) == binding.dataset_id)]
            if not versions or any(not v.get("id") or not v.get("version") for v in versions):
                raise ValueError("All-version population lacks complete authorized version identities")
            scope.authorized_version_ids = sorted({str(v["id"]) for v in versions})
            scope.authorized_version_labels = sorted({str(v["version"]) for v in versions})
        for key in ("dataset_id", "dataset_version", "dataset_version_id", "split"):
            value = getattr(proposed, key)
            if value is not None and value != getattr(scope, key):
                raise ValueError(f"Population {key} conflicts with original intent/authorized metadata")
        # These are user filters, not model-controlled version/run grants.
        for key, value in proposed.filters.items():
            if not isinstance(value, (str, int, float, bool)) or not str(value) or str(value) not in text:
                raise ValueError("Population filter must be an explicit scalar in the intent span")
            if key in {"version_bound_run_ids", "version_bound_run_labels"}:
                raise ValueError("Population cannot propose trusted run identity grants")
            scope.filters[key] = value
        if proposed.grouping and proposed.grouping != scope.grouping:
            raise ValueError("Population grouping must be grounded in the original intent span")
        fingerprint = hashlib.sha256(json.dumps(scope.model_dump(mode="json"), sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()[:16]
        compiled.append(PopulationRequirement(population_id="population-" + fingerprint,
            source_text=text, query_scope=scope))
    if len({p.population_id for p in compiled}) != len(compiled):
        raise ValueError("Duplicate population requirements")
    if independent_population_intent(state):
        named_versions = {str(v["version"]) for v in state.resource_summary.resource_metadata.get("dataset_versions", [])
            if any(mentioned(v.get(k), original) for k in ("id", "version"))}
        if not any(p.query_scope.all_versions for p in compiled) or not named_versions <= {
                p.query_scope.dataset_version for p in compiled if not p.query_scope.all_versions}:
            raise ValueError("Original independent version/all-version populations must all be bound")
    if state.populations and [p.model_dump() for p in state.populations] != [p.model_dump() for p in compiled]:
        raise ValueError("Original population requirements are immutable within this task")
    return compiled


def effective_scope(state, population_id=None):
    if not state.populations:
        if state.requires_population_binding:
            raise ValueError("Original independent populations are unbound; provide population_requests before SQL")
        if population_id:
            raise ValueError("Unknown population_id")
        return state.query_scope.model_copy(deep=True)
    population = next((p for p in state.populations if p.population_id == population_id), None)
    if population is None:
        raise ValueError("SQL requires a trusted population_id from the original task requirements")
    return population.query_scope.model_copy(deep=True)


def mentioned(value, text):
    value = str(value or "")
    return bool(value and re.search(r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text, re.I))


def resolve_binding(query, resources: ResourceSummary, previous=None, explicit_version=None, datasource=None):
    previous = previous or {}
    metadata = resources.resource_metadata
    binding = ResourceBinding(datasource_id=datasource)
    sources = [s for s in resources.authorized_datasources if mentioned(s, query)]
    if not binding.datasource_id:
        binding.datasource_id = sources[0] if len(sources) == 1 else (
            resources.authorized_datasources[0] if len(resources.authorized_datasources) == 1 else None)
    binding.files = [f for f in resources.available_files if mentioned(f, query)]
    versions = [v for v in metadata.get("dataset_versions", [])
                if not binding.datasource_id or v.get("datasource_id") == binding.datasource_id]
    named_datasets = [d for d in metadata.get("datasets", []) if
                      any(mentioned(d.get(k), query) for k in ("id", "name", "dataset_name"))]
    if len(named_datasets) == 1:
        binding.dataset_id = str(named_datasets[0].get("id", named_datasets[0].get("dataset_id")))
        binding.dataset_label = named_datasets[0].get("name")
        versions = [v for v in versions if str(v.get("dataset_id")) == binding.dataset_id]
    runs = [r for r in metadata.get("model_runs", []) if
            (not binding.datasource_id or r.get("datasource_id") == binding.datasource_id) and
            any(mentioned(r.get(key), query) for key in ("id", "run_name"))]
    binding.run_ids = [str(r["id"]) for r in runs]
    binding.run_labels = [str(r.get("run_name", r["id"])) for r in runs]
    experiments = {str(e["id"]): e for e in metadata.get("experiments", [])
                   if not binding.datasource_id or e.get("datasource_id", binding.datasource_id) == binding.datasource_id}
    binding.run_dataset_version_ids = [str(r.get("dataset_version_id") or
        experiments.get(str(r.get("experiment_id")), {}).get("dataset_version_id") or "") for r in runs]
    explicit = [v for v in versions if any(mentioned(v.get(k), query) for k in ("id", "version", "version_name", "label"))]
    requested = explicit_version or (None if runs else previous.get("dataset_version"))
    if not explicit and requested:
        explicit = [v for v in versions if requested in {str(v.get(k)) for k in ("id", "version", "version_name", "label")}]
    if not explicit and runs:
        ids = set(binding.run_dataset_version_ids)
        explicit = [v for v in versions if str(v.get("id")) in ids]
    if not explicit and binding.files:
        file_versions = {v for f in metadata.get("files", []) if f["filename"] in binding.files for v in f.get("dataset_version", [])}
        if len(file_versions) == 1:
            explicit = [v for v in versions if v.get("version") in file_versions]
    candidates = explicit if requested and not explicit else explicit or versions
    if len(candidates) == 1:
        v = candidates[0]
        binding.dataset_version = str(v.get("version", v.get("version_name", v.get("label", v.get("id")))))
        binding.dataset_version_id = str(v["id"]) if v.get("id") else None
        binding.dataset_id = str(v["dataset_id"]) if v.get("dataset_id") else None
        binding.version_identifier_type = "UUID" if mentioned(v.get("id"), query) else "LABEL"
    elif len(candidates) > 1:
        binding.ambiguous_fields.append("dataset_version")
    # An explicitly supplied identity is retained when metadata is unavailable,
    # but is marked unverified; it can never masquerade as a discovered UUID.
    if not binding.dataset_version and explicit_version and not versions:
        binding.dataset_version = explicit_version
        binding.source = "explicit_unverified"
    datasets = [d for d in metadata.get("datasets", []) if str(d.get("id")) == binding.dataset_id]
    if datasets:
        binding.dataset_label = datasets[0].get("name")
    return binding


def resolve_scope(query, binding, previous=None):
    scope = QueryScope.model_validate(previous or {})
    for key in ("dataset_id", "dataset_version", "dataset_version_id", "version_identifier_type", "datasource_id"):
        setattr(scope, key, getattr(binding, key))
    scope.user_constraints = [query]
    whole = bool(re.search(r"所有\s*split|全版本|整个数据集|whole dataset|all splits", query, re.I))
    splits = [s for s in ("train", "validation", "test") if mentioned(s, query)]
    if not splits and re.search(r"训练集|训练子集", query):
        splits = ["train"]
    if whole:
        scope.whole_dataset = True
        scope.split = None
        scope.comparison_target = splits
    elif len(splits) == 1:
        scope.split = splits[0]
        scope.whole_dataset = False
    elif len(splits) > 1:
        scope.split = None
        scope.comparison_target = splits
    scope.filters.pop("version_bound_run_ids", None)
    scope.filters.pop("version_bound_run_labels", None)
    if binding.run_ids:
        scope.comparison_target = binding.run_ids
        if binding.dataset_version_id and set(binding.run_dataset_version_ids) == {binding.dataset_version_id}:
            scope.filters["version_bound_run_ids"] = binding.run_ids
            scope.filters["version_bound_run_labels"] = binding.run_labels
        else:
            scope.filters.pop("version_bound_run_ids", None)
            scope.filters.pop("version_bound_run_labels", None)
    if re.search(r"不同结构|各结构|按结构|structure_type", query, re.I):
        scope.grouping = ["structure_type"]
    scope.entity = scope.entity or "molecule"
    return scope


def ask_sufficiency(decision, state):
    """Reject only provably redundant/discoverable requests; never pick a tool."""
    fields = decision.missing_information
    if not fields:
        return {"necessary": True, "reason": "missing_information not structured; no guessed interpretation"}
    resolved, missing = {}, []
    b = state.resource_binding
    aliases = {"version": "dataset_version", "dataset": "dataset_version", "dataset_versions": "dataset_version",
               "files": "file", "filename": "file", "comparison_objects": "file", "run": "run_id", "run_name": "run_id"}
    for raw in fields:
        field = aliases.get(raw, raw)
        value = None
        if field == "dataset_version": value = b.dataset_version
        elif field == "dataset_id": value = b.dataset_id
        elif field == "datasource_id": value = b.datasource_id
        elif field == "split": value = state.query_scope.split
        elif field == "run_id": value = b.run_ids
        elif field == "file": value = b.files
        elif any(word in field for word in ("column", "schema", "structure_field", "prediction_field", "target_field")):
            value = [f for f in state.resource_summary.resource_metadata.get("files", []) if f["filename"] in b.files]
            if not value and b.datasource_id:
                value = {"discoverable": "authorized schema tools"}
        if value:
            resolved[raw] = value
        else:
            missing.append(raw)
    return {"necessary": bool(missing), "resolved": resolved, "missing": missing,
            "reason": "critical selection remains ambiguous" if missing else "requested information is already bound or discoverable"}
