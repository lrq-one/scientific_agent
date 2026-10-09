from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    available_artifact_formats: list[str] = Field(default_factory=list)
    resource_metadata: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    metadata_errors: list[str] = Field(default_factory=list)


class ResourceBinding(BaseModel):
    datasource_id: str | None = None
    dataset_id: str | None = None
    dataset_label: str | None = None
    dataset_version: str | None = None
    dataset_version_id: str | None = None
    version_identifier_type: Literal["LABEL", "UUID", "PRIMARY_KEY"] = "LABEL"
    run_ids: list[str] = Field(default_factory=list)
    run_dataset_version_ids: list[str] = Field(default_factory=list)
    run_labels: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)
    source: str = "authorized_metadata"
    ambiguous_fields: list[str] = Field(default_factory=list)


class UserProfile(BaseModel):
    """Small, explicit long-lived preferences; task facts stay in Task State."""

    user_id: str
    locale: str = "zh-CN"
    domain: str | None = None
    default_units: dict[str, str] = Field(default_factory=dict)
    response_detail: Literal["brief", "standard", "detailed"] = "standard"
    authorized_preferences: dict[str, str | bool | int | float] = Field(default_factory=dict)
    version: int = 1
    provenance: str = "user_confirmed"
    updated_at: str | None = None


class QueryScope(BaseModel):
    dataset_id: str | None = None
    dataset_version: str | None = None
    dataset_version_id: str | None = None
    version_identifier_type: Literal["LABEL", "UUID", "PRIMARY_KEY"] = "LABEL"
    split: str | None = None
    whole_dataset: bool = False
    entity: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    grouping: list[str] = Field(default_factory=list)
    aggregation: str | None = None
    comparison_target: list[str] = Field(default_factory=list)
    user_constraints: list[str] = Field(default_factory=list)
    datasource_id: str | None = None
    all_versions: bool = False
    authorized_version_ids: list[str] = Field(default_factory=list)
    authorized_version_labels: list[str] = Field(default_factory=list)


class PopulationRequest(BaseModel):
    """Existing Decision proposes intent spans, never authorization or SQL."""
    source_text: str
    query_scope: QueryScope


class PopulationRequirement(PopulationRequest):
    population_id: str


class GoalContract(BaseModel):
    """The immutable user requirement contract for one Agent run.

    Resources, Skills and Plans are execution context; none of them may add
    requirements here.  Revisions are represented by a new version and are
    only created for validated user/HITL changes.
    """

    contract_version: int = 1
    original_request: str
    required_goals: list[str] = Field(default_factory=list)
    required_dimensions: list[str] = Field(default_factory=list)
    required_data_sources: list[str] = Field(default_factory=list)
    dataset_version: str | None = None
    population_requirements: list[PopulationRequirement] = Field(default_factory=list)
    required_deliverables: list[Literal["database_analysis", "file_analysis", "artifact"]] = Field(default_factory=list)
    user_confirmed: bool = True
    change_source: Literal["user_request", "validated_proposal", "hitl"] = "user_request"
    change_reason: str = "initial user request"
    frozen: bool = True


class CompletionCondition(BaseModel):
    kind: Literal["SCHEMA", "TOOL_RESULTS", "EXECUTED_ROWS", "ARTIFACT"] = "TOOL_RESULTS"
    mode: Literal["ALL", "ANY"] = "ALL"
    required_tools: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    required_artifacts: list[str] = Field(default_factory=list)
    require_scope_match: bool = False


class RecoveryPolicy(BaseModel):
    """Structured, bounded recovery instruction attached to a failure."""

    failure_code: str
    failed_stage: str = "tool_execution"
    recoverable: bool = False
    recovery_action: Literal[
        "repair_arguments", "retrieve_schema", "establish_sql_candidate",
        "targeted_sql_repair", "safe_reject", "bounded_retry",
        "repair_plan_dependency", "stop", "ask_user", "localized_repair",
        "no_progress_stop",
    ] = "safe_reject"
    retry_budget: int = Field(default=0, ge=0, le=3)
    context: dict[str, Any] = Field(default_factory=dict)
    previous_attempt: dict[str, Any] = Field(default_factory=dict)
    strategy_changed: bool = False


class SQLCandidate(BaseModel):
    sql: str
    params: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    query_scope: QueryScope | None = None


class ToolResult(BaseModel):
    success: bool
    data: Any = None
    source: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    outcome: Literal["SUCCESS", "EMPTY_RESULT", "INVALID_ARGUMENT", "UNSUPPORTED_OPERATION",
                     "RESOURCE_NOT_FOUND", "EXECUTION_FAILED"] | None = None
    failure_code: str | None = None
    failed_stage: str | None = None
    recoverable: bool | None = None
    exception_type: str | None = None
    reason_summary: str | None = None

    @model_validator(mode="after")
    def normalize_outcome(self):
        if self.outcome is None:
            if self.success:
                self.outcome = "EMPTY_RESULT" if self.data is None or self.data == [] else "SUCCESS"
            else:
                message = (self.error or "").lower()
                if any(token in message for token in ("not found", "does not exist", "missing file")):
                    self.outcome = "RESOURCE_NOT_FOUND"
                elif any(token in message for token in ("not available", "unavailable", "not configured", "not implemented")):
                    self.outcome = "UNSUPPORTED_OPERATION"
                elif any(token in message for token in ("invalid", "requires", "unknown ", "missing column", "exactly two")):
                    self.outcome = "INVALID_ARGUMENT"
                else:
                    self.outcome = "EXECUTION_FAILED"
        if not self.success:
            self.failure_code = self.failure_code or self.outcome
            self.reason_summary = self.reason_summary or self.error or self.exception_type or "tool execution failed"
        return self


class Evidence(BaseModel):
    evidence_id: str
    claim: str
    value: Any = None
    source_type: str
    source: str
    tool_call_id: str
    dataset_version: str | None = None
    model_version: str | None = None
    query_scope: QueryScope | None = None
    population_id: str | None = None


class GroundedClaim(BaseModel):
    text: str
    evidence_ids: list[str] = Field(default_factory=list)
    status: Literal["supported", "unsupported"] = "supported"
    category: Literal["observation", "interpretation"] = "observation"


class GoalCoverage(BaseModel):
    status: Literal["SATISFIED", "PARTIAL", "UNSATISFIED", "UNVERIFIABLE"] = "UNVERIFIABLE"
    required_dimensions: list[str] = Field(default_factory=list)
    observed_dimensions: list[str] = Field(default_factory=list)
    missing_dimensions: list[str] = Field(default_factory=list)
    missing_populations: list[str] = Field(default_factory=list)
    missing_deliverables: list[str] = Field(default_factory=list)
    reason: str = ""


class PlanStep(BaseModel):
    """One executable outcome contract in an installed plan.

    ``selected_tools``/``preferred_tools`` are retained for checkpoint and
    LLM compatibility. ``allowed_tools`` is the canonical complete scope;
    the model validator mirrors the legacy fields so old plans remain
    executable without making a second planning path.
    """

    step_id: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)
    required_capabilities: list[Capability] = Field(default_factory=list)
    preferred_tools: list[str] = Field(default_factory=list)
    selected_tools: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    optional_tools: list[str] = Field(default_factory=list)
    required_inputs: list[str] | dict[str, str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed", "blocked", "skipped"] = "pending"
    plan_id: str | None = None
    query_scope: QueryScope | None = None
    population_id: str | None = None
    completion_condition: CompletionCondition | None = None
    completion_predicate: CompletionCondition | None = None
    recovery_policy: RecoveryPolicy | None = None
    completion_evidence: list[str] = Field(default_factory=list)
    observations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    result_summary: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def normalize_execution_contract(self):
        canonical = list(dict.fromkeys(self.allowed_tools or self.selected_tools or self.preferred_tools))
        if self.allowed_tools and self.selected_tools and not set(self.selected_tools) <= set(self.allowed_tools):
            raise ValueError("selected_tools must be a subset of allowed_tools")
        self.allowed_tools = canonical
        if not self.selected_tools:
            self.selected_tools = list(canonical)
        if not self.preferred_tools:
            self.preferred_tools = list(canonical)
        self.optional_tools = list(dict.fromkeys(self.optional_tools))
        if not set(self.optional_tools) <= set(self.allowed_tools):
            raise ValueError("optional_tools must be a subset of allowed_tools")
        if self.completion_condition is not None and self.completion_predicate is not None:
            if self.completion_condition.model_dump() != self.completion_predicate.model_dump():
                raise ValueError("completion_condition and completion_predicate disagree")
        if self.completion_predicate is None and self.completion_condition is not None:
            self.completion_predicate = self.completion_condition
        if self.completion_condition is None and self.completion_predicate is not None:
            self.completion_condition = self.completion_predicate
        return self


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["ANSWER", "CALL_TOOL", "ASK_USER", "REPLAN", "FINISH", "REFUSE"]
    goal: str | None = None
    interaction_type: str = "scientific_task"
    answer_basis: Literal["RESOURCE_CAPABILITY", "GENERAL_KNOWLEDGE", "PERSISTED_STATE", "TOOL_EVIDENCE"] = "TOOL_EVIDENCE"
    tool_name: str | None = None
    tool_arguments: dict[str, Any] = Field(default_factory=dict)
    input_refs: dict[str, str] = Field(default_factory=dict, description="Argument -> observation:<call_id>:data or evidence:<id>:value; server supplies exact stored values.")
    requested_context: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    question_to_user: str | None = None
    step_id: str | None = None
    completed_step_ids: list[str] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reason_summary: str = Field(default="", max_length=600, description="Short public reason; never hidden chain-of-thought.")
    requested_dimensions: list[str] = Field(default_factory=list,
        description="Bare schema column names for original grouping dimensions (e.g. split, structure_type); never aggregate aliases, prose, metrics or export descriptions. Retain unavailable requested columns rather than substitute another metric.")
    required_deliverables: list[Literal["database_analysis", "file_analysis", "artifact"]] = Field(default_factory=list)
    population_requests: list[PopulationRequest] = Field(default_factory=list, max_length=6)
    population_id: str | None = None


class GroundedResponse(BaseModel):
    answer: str
    claims: list[GroundedClaim] = Field(default_factory=list)


class ScientificAgentState(BaseModel):
    artifacts: list[str] = Field(default_factory=list)
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
    plan_id: str | None = None
    current_step: int = 0
    observations: list[ToolResult] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_count: int = 0
    failure_count: int = 0
    replan_count: int = 0
    no_progress_replan_count: int = 0
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    quality_status: str | None = None
    quality_issues: list[str] = Field(default_factory=list)
    goal_coverage: GoalCoverage = Field(default_factory=GoalCoverage)
    goal_contract: GoalContract | None = None
    goal_contract_history: list[GoalContract] = Field(default_factory=list)
    requested_dimensions: list[str] = Field(default_factory=list)
    required_deliverables: list[str] = Field(default_factory=list)
    final_answer: str | None = None
    user_request: str = ""
    conversation_context: dict[str, Any] = Field(default_factory=dict)
    resource_summary: ResourceSummary = Field(default_factory=ResourceSummary)
    resource_hint: dict[str, Any] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)
    datasource_id: str | None = None
    dataset_version: str | None = None
    resource_binding: ResourceBinding = Field(default_factory=ResourceBinding)
    query_scope: QueryScope = Field(default_factory=QueryScope)
    populations: list[PopulationRequirement] = Field(default_factory=list)
    requires_population_binding: bool = False
    grounding_ready: bool = False
    schema_cache: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    relationships_cache: list[dict[str, Any]] = Field(default_factory=list)
    iteration_count: int = 0
    consecutive_failures: int = 0
    errors: list[str] = Field(default_factory=list)
    decision: AgentDecision | None = None
    decision_valid: bool = True
    decision_telemetry: dict[str, Any] = Field(default_factory=dict)
    control_observations: list[dict[str, Any]] = Field(default_factory=list)
    recovery_policy: RecoveryPolicy | None = None
    recovery_history: list[RecoveryPolicy] = Field(default_factory=list)
    tool_cache: dict[str, ToolResult] = Field(default_factory=dict)
    hitl_answers: list[dict[str, str]] = Field(default_factory=list)
    task_id: str | None = None
    conversation_id: str | None = None
    runtime_status: str = "running"


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
    datasource_id: str | None = None
    resource: str | None = None
    split: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)
    full_rows: bool = False
    output_format: Literal["table", "chart"] | None = None
    changed_fields: list[str] = Field(default_factory=list)


class FollowUpDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interaction_type: Literal["NEW_TASK", "RESULT_EXPLANATION", "EVIDENCE_QUERY", "PROVENANCE_QUERY",
                              "ERROR_QUESTION", "TASK_REFINEMENT", "RERUN", "CONTINUE_ANALYSIS", "CLARIFY"] = Field(
        description="Semantic information need. CLARIFY only when need or target is unresolved AFTER applying the latest-task default; never ask permission to read known history.")
    target_task_id: str | None = None
    target_reference: Literal["LATEST", "EXPLICIT", "AMBIGUOUS"] = "LATEST"
    target_selector_type: Literal["TASK_ID", "VERSION", "ORDER"] | None = None
    target_reference_text: str | None = Field(default=None, description="Exact current-user quote selecting another task, never text copied from previous messages.")
    requested_content: list[Literal["answer", "claim", "evidence", "sql", "params", "raw_rows",
                                   "tools", "artifacts", "uncertainty", "error"]] = Field(default_factory=list)
    requires_execution: bool = False  # Semantic suggestion; the state resolver owns the final value.
    refinement_patch: TaskRefinementPatch | None = None
    reason: str = Field(default="", max_length=200)
    clarification_question: str | None = None
    source: str = "llm_structured"
    llm_telemetry: dict[str, Any] = Field(default_factory=dict)

    @property
    def follow_up_type(self) -> str:
        # Preserve the existing trace wire contract; routing uses interaction_type.
        return {"EVIDENCE_QUERY": "EVIDENCE_EXPLANATION", "PROVENANCE_QUERY": "EVIDENCE_EXPLANATION",
                "TASK_REFINEMENT": "REFINE_PREVIOUS_TASK", "RERUN": "RERUN_PREVIOUS_TASK"}.get(
                    self.interaction_type, self.interaction_type)


class StateSufficiency(BaseModel):
    action: Literal["REUSE", "EXECUTE", "CLARIFY", "INSUFFICIENT"]
    requires_execution: bool = False
    available_content: list[str] = Field(default_factory=list)
    missing_content: list[str] = Field(default_factory=list)
    reason: str = ""
    scope_issues: list[str] = Field(default_factory=list)


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
    params_recorded: bool = False
    sql_raw_result: Any = None
    file_raw_results: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    claims: list[dict[str, Any]] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    final_answer: str | None = None
    uncertainties: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    recovery_history: list[dict[str, Any]] = Field(default_factory=list)
    query_scope: dict[str, Any] = Field(default_factory=dict)
    raw_rows_complete: bool = False


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
