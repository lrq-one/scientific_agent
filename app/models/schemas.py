from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class Capability(str, Enum):
    FILE = "file"
    DATABASE = "database"
    SCIENTIFIC_MODEL = "scientific_model"
    MCP = "mcp"
    ARTIFACT = "artifact"


class RequestIntent(BaseModel):
    goal: str = ""
    domain: str = "general"
    task_type: Literal["general", "file_analysis", "database_analysis", "mixed_analysis", "scientific_model"] = "general"
    complexity: Literal["simple", "complex"] = "simple"
    required_capabilities: list[Capability] = Field(default_factory=list)
    need_planning: bool = False
    reason: str = ""


class ResourceSummary(BaseModel):
    available_files: list[str] = Field(default_factory=list)
    authorized_datasources: list[str] = Field(default_factory=list)
    available_scientific_models: list[str] = Field(default_factory=list)
    available_mcp_tools: list[str] = Field(default_factory=list)


class SQLCandidate(BaseModel):
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class Evidence(BaseModel):
    evidence_id: str
    claim: str
    value: Any = None
    source_type: str
    source: str
    tool_call_id: str
    dataset_version: str | None = None
    model_version: str | None = None


class GroundedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "unsupported"] = "supported"
    category: Literal["observation", "interpretation"] = "observation"


class PlanStep(BaseModel):
    step_id: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    required_capabilities: list[Capability] = Field(default_factory=list)
    preferred_tools: list[str] = Field(default_factory=list)
    selected_tools: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed", "skipped"] = "pending"
    observations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    result_summary: str | None = None
    error: str | None = None


class ScientificAgentState(BaseModel):
    user_id: str
    thread_id: str
    goal: str
    domain: str = "general"
    task_type: str = "general"
    complexity: str = "simple"
    available_files: list[str] = Field(default_factory=list)
    available_datasources: list[str] = Field(default_factory=list)
    available_models: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    selected_skills: list[str] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    plan_version: int = 0
    current_step: int = 0
    observations: list[ToolResult] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_count: int = 0
    failure_count: int = 0
    replan_count: int = 0
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    quality_status: str | None = None
    quality_issues: list[str] = Field(default_factory=list)
    final_answer: str | None = None


class SSEEvent(BaseModel):
    event: str
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


class FileMetadata(BaseModel):
    file_id: str
    object_key: str
    owner_id: str
    thread_id: str
    filename: str
    content_type: str
    size: int


FollowUpType = Literal[
    "NEW_TASK",
    "EVIDENCE_EXPLANATION",
    "RESULT_EXPLANATION",
    "CONTINUE_ANALYSIS",
    "REFINE_PREVIOUS_TASK",
    "RERUN_PREVIOUS_TASK",
    "ERROR_QUESTION",
]


class FollowUpClassification(BaseModel):
    follow_up_type: FollowUpType
    reason: str = ""
    target_task_id: str | None = None
    clarification_question: str | None = None
    source: str = "deterministic"


class TaskRefinementPatch(BaseModel):
    dataset_version: str | None = None
    model_version: str | None = None
    subgroup: str | None = None
    output_format: Literal["table", "chart"] | None = None
    changed_fields: list[str] = Field(default_factory=list)


class ConversationContextSummary(BaseModel):
    conversation_id: str | None = None
    current_user_query: str
    previous_user_query: str | None = None
    previous_assistant_answer: str | None = None
    previous_task_id: str | None = None
    previous_task_status: str | None = None
    previous_task_goal: str | None = None
    available_evidence_summary: list[dict[str, Any]] = Field(default_factory=list)
    dataset_versions: list[str] = Field(default_factory=list)
    model_versions: list[str] = Field(default_factory=list)
    recent_tasks: list[dict[str, Any]] = Field(default_factory=list)


class ProvenanceRecord(BaseModel):
    previous_task_id: str | None = None
    conversation_id: str | None = None
    thread_id: str | None = None
    datasource: str | None = None
    dataset_version: str | None = None
    model_version: str | None = None
    selected_skills: list[str] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    sql_candidate: dict[str, Any] | None = None
    sql_params: dict[str, Any] = Field(default_factory=dict)
    sql_raw_result: Any = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    final_answer: str | None = None
    uncertainties: list[str] = Field(default_factory=list)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    datasource_id: str | None = None


class ConversationChatRequest(ChatRequest):
    pass


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    thread_id: str = Field(min_length=1)
    answer: str
    conversation_id: str | None = None
    task_id: str | None = None


class ConversationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(default="新建科研任务", min_length=1, max_length=200)


class ConversationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
