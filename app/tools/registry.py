from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from time import perf_counter
from typing import Any

from pydantic import BaseModel, Field
from jsonschema import Draft202012Validator

from app.services.skills import SkillService
from app.services.llm_config import llm_settings
from app.services.llm_telemetry import UsageCollector


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    output_contract: str
    required_capability: str
    risk_level: str = "low"
    timeout_seconds: int = 30
    side_effect: str = "none"
    allowed_roles: tuple[str, ...] = ("researcher", "admin")

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


def _spec(
    name: str, description: str, capability: str, required: list[str], output: str,
    *, properties: dict[str, Any] | None = None, **kwargs,
) -> ToolSpec:
    typed = {key: {"type": "string"} for key in required}
    typed.update(properties or {})
    return ToolSpec(
        name=name,
        description=description,
        required_capability=capability,
        input_schema={
            "type": "object",
            "properties": typed,
            "required": required,
            "additionalProperties": False,
        },
        output_contract=output,
        **kwargs,
    )


TOOL_SPECS = [
    _spec("list_workspace_files", "List authorized task files.", "file", [], "filename array"),
    _spec("inspect_table", "Inspect columns, types, and row count.", "file", ["filename"], "table schema summary"),
    _spec("read_csv", "Read an authorized CSV table.", "file", ["filename"], "bounded row records"),
    _spec("read_excel", "Read an authorized Excel table.", "file", ["filename"], "bounded row records"),
    _spec("profile_dataset", "Profile missingness, uniqueness, and numeric ranges.", "file", ["filename"], "dataset profile"),
    _spec("calculate_metrics", "Compute deterministic MAE/RMSE.", "file", ["filename"], "metric object"),
    _spec("group_metrics", "Compute deterministic metrics by group.", "file", ["filename", "group"], "group metric rows"),
    _spec("compare_models", "Compare aligned model result tables.", "file", ["filenames"], "model comparison rows", properties={"filenames": {"type": "array", "items": {"type": "string"}, "minItems": 2}}),
    _spec("find_high_error_samples", "Rank samples by absolute error.", "file", ["filename"], "bounded sample rows", properties={"limit": {"type": "integer", "minimum": 1, "maximum": 500}}),
    _spec("join_tables", "Join two authorized tables by a key.", "file", ["left", "right", "on"], "joined rows"),
    _spec("filter_samples", "Filter rows using explicit column predicates.", "file", ["filename", "filters"], "filtered rows", properties={"filters": {"type": "object"}}),
    _spec("plot_metric_comparison", "Render a metric comparison as PNG.", "artifact", ["metrics"], "PNG artifact descriptor", properties={"metrics": {"type": "object"}}, side_effect="creates_object"),
    _spec("list_datasources", "List data sources authorized for the user.", "database", [], "datasource descriptors"),
    _spec("search_schema", "BM25-search table/column metadata.", "database", ["query"], "ranked schema hits"),
    _spec("get_table_schema", "Get detailed schema for one table.", "database", ["table"], "column descriptors"),
    _spec("get_table_relationships", "Get foreign-key relationships.", "database", [], "relationship descriptors"),
    _spec("preview_table", "Preview bounded rows from an allowed table.", "database", ["table"], "bounded row records", properties={"limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _spec("execute_readonly_sql", "Execute SQL Guard-approved read-only SQL.", "database", ["sql"], "bounded query rows", properties={"params": {"type": "object"}}, risk_level="medium"),
    _spec("get_molecule_features", "Retrieve molecular features through Scientific MCP.", "mcp", ["molecule_id"], "feature object"),
    _spec("predict_rt", "Predict retention time with a registered model.", "scientific_model", ["smiles"], "prediction with provenance", risk_level="medium"),
    _spec("compare_structure_groups", "Compare deterministic error metrics across structure groups.", "file", ["rows", "group"], "group comparison", properties={"rows": {"type": "array", "items": {"type": "object"}, "minItems": 1}}),
    _spec("save_result_table", "Save result rows as CSV or XLSX.", "artifact", ["rows", "format"], "table artifact descriptor", properties={"rows": {"type": "array", "items": {"type": "object"}}, "format": {"type": "string", "enum": ["csv", "xlsx"]}}, side_effect="creates_object"),
    _spec("save_chart", "Persist a validated PNG chart in object storage.", "artifact", ["image_base64", "filename"], "chart artifact descriptor", properties={"filename": {"type": "string", "pattern": "^[^/\\\\]+\\.png$"}}, side_effect="creates_object"),
]


class ToolChoice(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None, skills: SkillService | None = None, llm: Any | None = None):
        self.specs = {spec.name: spec for spec in (specs or TOOL_SPECS)}
        self.skills = skills or SkillService()
        self.llm = llm

    def all(self) -> list[ToolSpec]:
        return list(self.specs.values())

    def validate_choice(self, choice: ToolChoice) -> tuple[bool, str | None]:
        spec = self.specs.get(choice.tool)
        if spec is None:
            return False, "unknown_tool"
        errors = list(Draft202012Validator(spec.input_schema).iter_errors(choice.arguments))
        if errors:
            return False, errors[0].message
        return True, None

    def candidates(
        self,
        *,
        available_capabilities: set[str],
        role: str,
        selected_skills: list[str],
        current_step_tools: list[str] | None = None,
    ) -> list[ToolSpec]:
        """Deterministic resource -> permission -> skill -> plan-step filtering."""
        resource_filtered = [
            spec
            for spec in self.all()
            if spec.required_capability in available_capabilities
        ]
        permission_filtered = [spec for spec in resource_filtered if role in spec.allowed_roles]
        skill_map = self.skills.by_name()
        allowed_by_skills: set[str] = set()
        for name in selected_skills:
            allowed_by_skills.update(skill_map.get(name, {}).get("allowed_tools", []))
        skill_filtered = (
            [spec for spec in permission_filtered if spec.name in allowed_by_skills]
            if allowed_by_skills
            else permission_filtered
        )
        if current_step_tools:
            # The current plan step is a hard execution boundary. Skill routing
            # may narrow the set further, but a weak/mismatched Skill selection
            # must never widen execution back to unrelated tools or make a
            # trusted mandatory step impossible.
            step_names = set(current_step_tools)
            preferred = [spec for spec in skill_filtered if spec.name in step_names]
            if preferred:
                return preferred
            return [spec for spec in permission_filtered if spec.name in step_names]
        return skill_filtered

    async def select(
        self,
        goal: str,
        current_step: str,
        candidates: list[ToolSpec],
        preferred_tool: str | None = None,
        argument_context: dict[str, Any] | None = None,
    ) -> tuple[ToolChoice | None, str, dict[str, Any]]:
        if not candidates:
            return None, "no_authorized_candidate", {"llm_called": False}
        llm = self.llm
        settings = llm_settings()
        if llm is None and settings.configured:
            from langchain_openai import ChatOpenAI

            llm = ChatOpenAI(
                model=settings.model,
                api_key=settings.api_key,
                base_url=settings.api_base,
                temperature=0,
                extra_body={"enable_thinking": False} if (settings.model or "").startswith("qwen") else None,
            )
        if llm is not None:
            collector = UsageCollector()
            prompt = (
                "Choose exactly one tool from the already authorized candidate schemas. "
                "Do not invent a tool or arguments.\n"
                f"Goal: {goal}\nCurrent step: {current_step}\n"
                f"Known argument context (trusted values; use when relevant): "
                f"{json.dumps(argument_context or {}, ensure_ascii=False)}\n"
                f"Candidates: {json.dumps([item.model_dump() for item in candidates], ensure_ascii=False)}"
            )
            started = perf_counter()
            try:
                choice = await llm.with_structured_output(ToolChoice).ainvoke(
                    prompt, config={"callbacks": [collector]}
                )
                telemetry = {
                    "llm_called": True,
                    "model_configured": settings.model,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                    "http_status": None,
                    **collector.snapshot(),
                }
                valid, _ = self.validate_choice(choice)
                if choice.tool in {item.name for item in candidates} and valid:
                    return choice, "llm_structured_selection", {**telemetry, "fallback": False}
                telemetry = {**telemetry, "fallback": True, "invalid_choice": choice.model_dump()}
            except Exception as exc:
                telemetry = {
                    "llm_called": True,
                    "fallback": True,
                    "error_type": type(exc).__name__,
                    "latency_ms": round((perf_counter() - started) * 1000, 2),
                }
        else:
            telemetry = {"llm_called": False, "fallback": True}

        names = [item.name for item in candidates]
        selected = preferred_tool if preferred_tool in names else names[0]
        required = list(self.specs[selected].input_schema["required"])
        trusted = argument_context or {}
        bound = {name: trusted[name] for name in required if name in trusted}
        optional = self.specs[selected].input_schema.get("properties", {})
        for name, value in trusted.items():
            if name in optional and name not in bound:
                bound[name] = value
        fallback_choice = ToolChoice(
            tool=selected,
            arguments=bound,
            reason="validated deterministic fallback from trusted runtime context",
        )
        valid, error = self.validate_choice(fallback_choice)
        if not valid:
            source = (
                "llm_invalid_or_unauthorized_arguments"
                if telemetry.get("invalid_choice") is not None
                else "unbound_arguments"
            )
            return None, source, {**telemetry, "binding_error": error}
        return fallback_choice, "deterministic_fallback", telemetry

    def routing_trace(
        self,
        candidates: list[ToolSpec],
        selected_tool: str | None,
        selection_source: str = "deterministic_resource_permission_skill_step_filter",
    ) -> dict[str, Any]:
        return {
            "candidate_tools": [spec.name for spec in candidates],
            "selected_tool": selected_tool,
            "selection_source": selection_source,
            "candidate_schemas": [spec.model_dump() for spec in candidates],
        }
