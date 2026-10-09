from __future__ import annotations

import json
import os
import re
from time import perf_counter
from typing import Any, Protocol

from rank_bm25 import BM25Okapi
import sqlglot
from sqlglot import exp
from app.tools.sql_guard import SQLGuardError

from app.models.schemas import SQLCandidate
from app.config import ALLOW_DETERMINISTIC_LLM_FALLBACK
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector


class SQLScopeValidationError(ValueError):
    """Keep rejected generated SQL as diagnostics, never as executed evidence."""
    def __init__(self, error, candidate, metadata):
        super().__init__(str(error))
        self.candidate = candidate
        from app.services.query_scope import ScopeViolation
        failure_code = "SCOPE_VIOLATION" if isinstance(error, ScopeViolation) else "UNVERIFIED_SCOPE"
        self.failure_code = failure_code
        recovery_action = "safe_reject" if failure_code == "SCOPE_VIOLATION" else "targeted_sql_repair"
        retry_budget = 0 if failure_code == "SCOPE_VIOLATION" else 1
        self.metadata = {**metadata, "sql_candidate": candidate.model_dump(mode="json"),
                         "sql_candidate_status": "diagnostic_only",
                         "scope_validation": {"verified": False, "status": failure_code,
                                               "failure_code": failure_code, "reason": str(error)},
                         "recovery": {"failed_stage": "sql_scope_validation",
                                      "candidate_role": "diagnostic_only",
                                      "failure_code": failure_code,
                                      "recovery_action": recovery_action,
                                      "retry_budget": retry_budget,
                                      "instruction": "The supplied QueryScope is unchanged. Repair the SQL predicate/lineage, or retrieve missing authorized schema; renaming a Plan or dropping scope does not repair SQL."}}


def tokenize(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|[\u4e00-\u9fff]", text.lower())
    words: list[str] = []
    for token in raw:
        parts = [token, *token.split("_")] if "_" in token else [token]
        for part in parts:
            if not part:
                continue
            words.append(part[:-1] if len(part) > 3 and part.endswith("s") else part)
    return words or [text.lower()]


class SchemaRetrieverProtocol(Protocol):
    def search(
        self,
        goal: str,
        schema: dict[str, list[dict[str, Any]]],
        relationships: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]: ...


class SchemaRetriever:
    """BM25 implementation behind a stable interface for future dense/hybrid retrieval."""

    def __init__(self, top_k: int | None = None):
        self.top_k = top_k or int(os.getenv("SCHEMA_TOP_K", "5"))

    def search(
        self,
        goal: str,
        schema: dict[str, list[dict[str, Any]]],
        relationships: list[dict[str, str]] | None = None,
    ) -> list[dict[str, Any]]:
        if not schema:
            return []
        tables = list(schema)
        documents = []
        for table in tables:
            columns = schema[table]
            table_description = next((column.get("table_description", "") for column in columns), "")
            text = " ".join(
                [table, table_description]
                + [f"{column['name']} {column.get('description', '')}" for column in columns]
            )
            documents.append(tokenize(text))
        query_tokens = tokenize(goal)
        bm25_scores = BM25Okapi(documents).get_scores(query_tokens)
        # Exact lexical overlap stabilizes very small schemas where Okapi IDF can
        # be negative for a term present in half of the documents.
        scores = [
            float(score) + len(set(query_tokens) & set(document))
            for score, document in zip(bm25_scores, documents, strict=True)
        ]
        ranked = sorted(zip(tables, scores), key=lambda item: (-float(item[1]), item[0]))[: self.top_k]
        relations = relationships or []
        return [
            {
                "table": table,
                "columns": schema[table],
                "relationships": [
                    relation
                    for relation in relations
                    if relation.get("source_table") == table or relation.get("target_table") == table
                ],
                "score": round(float(score), 6),
            }
            for table, score in ranked
        ]

    def retrieve(
        self,
        goal: str,
        schema: dict[str, list[dict[str, Any]]],
        limit: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        if not schema:
            return {}
        effective_limit = limit or self.top_k
        hits = SchemaRetriever(top_k=effective_limit).search(goal, schema)
        selected = [hit["table"] for hit in hits if hit["score"] > 0]
        if not selected:
            selected = ["training_molecules"] if "training_molecules" in schema else list(schema)[:limit]
        return {table: schema[table] for table in selected}


class TextToSQLService:
    @staticmethod
    def validate_goal_projection(sql: str, goal: str) -> None:
        if not re.search(r"(?:不同|各|所有|每种|按).{0,8}结构类型|(?:different|each|all|by).{0,20}structure[_ ]types?", goal, re.I):
            return
        if re.search(r"其他|others?", goal, re.I):
            return
        parsed = sqlglot.parse_one(re.sub(r"%\([A-Za-z_][A-Za-z0-9_]*\)s", "'__bound_param__'", sql), read="postgres")
        for selection in parsed.find_all(exp.Select):
            for projection in selection.expressions:
                if projection.alias_or_name.lower() == "structure_type" and projection.find(exp.Case):
                    raise SQLGuardError("column structure_type grouping collapses original categories and does not answer the goal")

    def __init__(self, llm: Any | None = None):
        self.llm = llm
        self.retriever = SchemaRetriever()

    def _configured_llm(self):
        if self.llm is not None:
            return self.llm
        settings = llm_settings()
        if not settings.configured:
            return None
        from langchain_openai import ChatOpenAI
        from app.services.live_budget import live_max_retries

        return ChatOpenAI(
            model=settings.model,
            api_key=settings.api_key,
            base_url=settings.api_base,
            temperature=0, max_retries=live_max_retries(1),
            extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
        )

    def prompt(
        self,
        goal: str,
        current_step: str,
        datasource: str,
        schema: dict[str, list[dict[str, str]]],
        relationships: list[dict[str, str]],
        dataset_version: str | None = None,
        repair_feedback: str | None = None,
        skill_context: str | None = None,
        original_goal: str | None = None,
        query_scope=None,
        resource_binding=None,
    ) -> str:
        dataset_constraint = ""
        if dataset_version and query_scope is None:
            dataset_constraint = (
                f"Dataset version: {dataset_version}. The query must constrain results to this exact "
                "dataset version using the appropriate version table/column, the named placeholder "
                "%(dataset_version)s, and params.dataset_version.\n"
            )
        repair_constraint = (
            f"Actual validation feedback and previous SQL (do not invent a different error):\n{repair_feedback[:3000]}\n"
            "Correct this exact failure while preserving the original analytical goal. "
            "Use only columns and aliases present in the supplied schema.\n"
            if repair_feedback else ""
        )
        skill_constraint = (
            f"Selected scientific Skill guidance (follow it unless it conflicts with security/schema):\n{skill_context[:4000]}\n"
            if skill_context else ""
        )
        prompt = (
            "Generate one parameterized PostgreSQL read-only query as SQLCandidate.\n"
            f"Goal: {goal}\nCurrent step: {current_step}\nDatasource: {datasource}\n"
            f"Original User Goal (immutable): {original_goal or goal}\n"
            f"QueryScope (authoritative): {query_scope.model_dump_json() if query_scope else '{}'}\n"
            "This QueryScope is the EFFECTIVE population for this one query. The original goal may require other independent populations, fulfilled by separate executions. Do not impose a sibling population's version/split or combine them unless this scope requires it. all_versions requires exactly the authorized version IDs/labels in its scope, never an unrestricted global database scan.\n"
            f"ResourceBinding (labels and primary keys are distinct): {resource_binding.model_dump_json() if resource_binding else '{}'}\n"
            f"{skill_constraint}"
            f"{dataset_constraint}"
            f"{repair_constraint}"
            f"Relevant schema: {json.dumps(schema, ensure_ascii=False)}\n"
            f"Relationships: {json.dumps(relationships, ensure_ascii=False)}\n"
            f"Allowed tables: {list(schema)}\n"
            "The supplied schema is inspected but may be incomplete. If a required split/version column or population table is missing, retrieve authorized schema before assuming the operation is unsupported. "
            "A scope validation error describes the generated SQL, NOT a missing QueryScope input. Correct the rejected SQL using actual feedback; do not repeat it or remove required constraints. "
            "Constraints: exactly one SELECT or WITH SELECT; no DML/DDL; use named psycopg params; no comments. "
            "Every parameter must use the exact named placeholder %(parameter_name)s with the same key in params. "
            "Never use positional %s, ?, or :name with a params dictionary. For a comparison of runs, include all requested runs in the query, not only the first run. "
            "When asked for each/different structure type, group by original structure_type values; "
            "never collapse categories into fused-ring/other unless explicitly requested. "
            "Answer the original analytical outcome and observation-selected subgroup, not just a narrowed local description. "
            "When both an overall total and subgroup counts are requested, return both explicitly, preserving the actual counting unit and avoiding JOIN-induced duplicates. "
            "For coverage, absence, or membership comparisons preserve the relevant base population, including non-members and zero-count groups. "
            "Do not put a nullable LEFT JOIN right-side version/split predicate in WHERE when that eliminates the absent records the question asks about; filter the joined membership in ON, a filtered CTE, or EXISTS instead. "
            "Distinguish membership in a dataset version from the actual train split. Return enough grouped counts/denominators to answer the requested coverage question. "
            "When the goal combines prediction error with training coverage, prefer separate scope-bound populations. Prove prediction lineage through predictions→model_runs→experiments→dataset_versions and coverage lineage through training_memberships→dataset_versions; never treat a model_run label as a training version without that relationship. "
            "用户要求不同/各结构类型时，必须使用 schema 中真实的 structure_type 列分组；"
            "包括 fused-ring 不代表只分 fused/non-fused 两组。用户目标优先于 Skill 示例。"
        )
        from app.services.prompt_catalog import prompt_catalog
        return prompt_catalog.compose("text2sql", prompt)

    async def generate(
        self,
        goal: str,
        current_step: str,
        datasource: str,
        full_schema: dict[str, list[dict[str, str]]],
        relationships: list[dict[str, str]],
        dataset_version: str | None = None,
        repair_feedback: str | None = None,
        skill_context: str | None = None,
        original_goal: str | None = None,
        query_scope=None,
        resource_binding=None,
    ) -> tuple[SQLCandidate, dict[str, Any]]:
        from app.services.context_projection import text2sql_context
        from app.services.query_decomposition import analyze_training_error_coverage
        relevant = self.retriever.retrieve((original_goal or "") + "\n" + goal, full_schema)
        if query_scope and (query_scope.dataset_version or query_scope.all_versions):
            for name in ("dataset_versions", "datasets", "training_memberships"):
                if name in full_schema: relevant[name] = full_schema[name]
        related_names = set(relevant)
        for relation in relationships:
            if relation.get("source_table") in related_names:
                related_names.add(relation.get("target_table", ""))
            if relation.get("target_table") in related_names:
                related_names.add(relation.get("source_table", ""))
        relevant = {name: full_schema[name] for name in related_names if name in full_schema}
        decomposition = analyze_training_error_coverage(goal=(original_goal or goal), schema=full_schema, relationships=relationships)
        llm = self._configured_llm()
        if llm is not None:
            collector = UsageCollector()
            started = perf_counter()
            from app.services.live_budget import record_live_call, reserve_live_call
            reserve_live_call("text2sql", max(1, len(self.prompt(goal, current_step, datasource, relevant, relationships, dataset_version, repair_feedback, skill_context, original_goal, query_scope, resource_binding)) // 4 + 1200))
            try:
                candidate = await llm.with_structured_output(SQLCandidate).ainvoke(
                    self.prompt(
                        goal,
                        current_step,
                        datasource,
                        relevant,
                        relationships,
                        dataset_version,
                        repair_feedback,
                        skill_context,
                        original_goal, query_scope, resource_binding,
                    ),
                    config={"callbacks": [collector]},
                )
                from app.services.prompt_contract import PROMPT_VERSIONS
                metadata = {
                    "prompt_contract_version": PROMPT_VERSIONS["text2sql"],
                    "generator": "llm_structured_output", "relevant_tables": list(relevant),
                    "context_ledger": text2sql_context(goal=goal, query_scope=query_scope,
                                                        resource_binding=resource_binding,
                                                        schema=relevant, relationships=relationships,
                                                        repair_feedback=repair_feedback).ledger,
                    "scope_decomposition": decomposition,
                    "llm_telemetry": {
                        "llm_called": True, "model_configured": llm_settings().model,
                        "latency_ms": round((perf_counter() - started) * 1000, 2),
                        "http_status": None, "fallback": False, **collector.snapshot(),
                    },
                }
                record_live_call("text2sql", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000)
                if dataset_version and query_scope is None and "%(dataset_version)s" not in candidate.sql:
                    raise ValueError("SQLCandidate did not bind the requested dataset_version")
                if dataset_version and query_scope is None:
                    # The model chooses SQL structure; the trusted HITL/request value owns binding.
                    metadata["parameter_binding_repaired"] = candidate.params.get("dataset_version") != dataset_version
                    candidate.params["dataset_version"] = dataset_version
                if query_scope:
                    from app.services.query_scope import validate_scope
                    candidate.query_scope = query_scope.model_copy(deep=True)
                    metadata["query_scope"] = query_scope.model_dump(mode="json")
                    try:
                        metadata["scope_validation"] = validate_scope(candidate.sql, candidate.params, query_scope, full_schema)
                    except ValueError as error:
                        raise SQLScopeValidationError(error, candidate, metadata) from error
                    candidate.query_scope = query_scope.model_validate(metadata['scope_validation']['effective_query_scope']) if metadata['scope_validation'].get('effective_query_scope') else candidate.query_scope
                metadata["sql_candidate_status"] = "scope_verified" if query_scope else "generated"
                return candidate, metadata
            except Exception as exc:
                record_live_call("text2sql", collector.snapshot(), latency_ms=(perf_counter() - started) * 1000, error=type(exc).__name__)
                if isinstance(exc, SQLScopeValidationError):
                    raise
                if isinstance(exc, ValueError) and str(exc).startswith("QueryScope"):
                    raise ValueError(str(exc)) from None
                if not ALLOW_DETERMINISTIC_LLM_FALLBACK:
                    raise RuntimeError(
                        f"real Text-to-SQL LLM call failed ({type(exc).__name__}); deterministic fixture fallback is disabled"
                    ) from exc
                fallback_reason = f"LLM unavailable after {type(exc).__name__}; deterministic fixture fallback"
                failure_telemetry = {
                    "llm_called": True, "model_configured": llm_settings().model,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                    "http_status": None, "fallback": True, "error_type": type(exc).__name__,
                    **collector.snapshot(),
                }
        else:
            if not ALLOW_DETERMINISTIC_LLM_FALLBACK:
                raise RuntimeError("Text-to-SQL LLM is not configured and deterministic fixture fallback is disabled")
            fallback_reason = "LLM API not configured; deterministic fixture fallback"
            failure_telemetry = {"llm_called": False, "fallback": True}

        params: dict[str, Any] = {}
        where = ""
        if dataset_version:
            where = " WHERE tm.dataset_version = %(dataset_version)s"
            params["dataset_version"] = dataset_version
        candidate = SQLCandidate(
            sql=(
                "SELECT m.structure_type, COUNT(*) AS sample_count "
                "FROM training_molecules AS tm "
                "JOIN molecules AS m ON m.molecule_id = tm.molecule_id"
                f"{where} GROUP BY m.structure_type ORDER BY sample_count DESC"
            ),
            params=params,
            reason=fallback_reason,
        )
        from app.services.prompt_contract import PROMPT_VERSIONS
        return candidate, {"generator": "deterministic_fixture_fallback", "prompt_contract_version": PROMPT_VERSIONS["text2sql"], "relevant_tables": list(relevant),
                           "scope_decomposition": decomposition,
                           "context_ledger": text2sql_context(goal=goal, query_scope=query_scope,
                                                               resource_binding=resource_binding,
                                                               schema=relevant, relationships=relationships,
                                                               repair_feedback=repair_feedback).ledger,
                           "sql_candidate_status": "generated",
                           "llm_telemetry": failure_telemetry}

