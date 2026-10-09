from __future__ import annotations

import json
import os
import asyncio
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import Response, StreamingResponse

from app.agents.scientific_agent import ScientificAgent
from app.models.schemas import (
    ChatRequest,
    ConversationChatRequest,
    ConversationCreate,
    ConversationPatch,
    ResumeRequest,
    SSEEvent,
    TaskRefinementPatch,
)
from app.services.conversation_history import ActiveTaskConflict, ConversationNotFound, conversation_repository
from app.services.conversation_policy import conversation_policy
from app.services.security_policy import security_policy, REFUSAL_ANSWER
from app.services.execution_context import execution_identity
from app.services.followup import (
    ConversationContextResolver,
    StateSufficiencyResolver,
    build_provenance,
    compose_followup_response,
    conversation_context_summary,
    workflow_query,
)
from app.services.resources import ResourceService
from app.services.runtime_health import runtime_readiness
from app.services.workspace import WorkspaceError, WorkspaceService
from app.services.object_storage import ObjectStorageService
from app.services.grounded_response import GroundedResponseService, GroundedResponseValidationError
from app.services.task_completion import scientific_task_status, RUNTIME_PROTOCOL_VERSION
from app.services.evaluation_variant import VARIANT


router = APIRouter()
agent = ScientificAgent()
workspace = WorkspaceService()
storage = ObjectStorageService(workspace=workspace)
resources = ResourceService(workspace, storage)
followup_resolver = ConversationContextResolver()
grounded_responses = GroundedResponseService()
running_tasks: dict[str, asyncio.Task] = {}


def execution_failure(exc, stage, *, task_id=None, conversation_id=None, thread_id=None):
    """Public failure correlation, including exceptions with an empty message."""
    reason = str(exc).strip() or type(exc).__name__
    reason = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "[REDACTED_KEY]", reason)[:600]
    return {"error": reason, "failure_code": "EXECUTION_TIMEOUT" if isinstance(exc, TimeoutError) else "EXECUTION_FAILED",
            "failed_stage": stage, "exception_type": type(exc).__name__, "reason_summary": reason,
            "recoverable": False, "task_id": task_id, "conversation_id": conversation_id, "thread_id": thread_id,
            "plan_id": None, "step_id": None, "tool_call_id": None,
            "retry_count": None, "replan_count": None, "resource_scope": None}


def current_user(x_user_id: str | None = Header(default=None)) -> str:
    if not x_user_id:
        raise HTTPException(status_code=401, detail="missing X-User-Id")
    return x_user_id


def encode_sse(item: SSEEvent) -> str:
    payload = json.dumps({"message": item.message, **item.data}, ensure_ascii=False, default=str)
    return f"event: {item.event}\ndata: {payload}\n\n"


async def replay_task_events(repository, conversation_id: str, task_id: str, after_id: int = 0):
    """Read persisted events; a disconnected browser never owns the worker lifetime."""
    cursor = after_id
    while True:
        for row in repository.events_after(task_id, cursor):
            cursor = row["id"]
            payload = row["payload_json"] or {}
            yield f"id: {cursor}\n" + encode_sse(SSEEvent(
                event=row["event_type"], message=payload.get("message", ""),
                data={key: value for key, value in payload.items() if key != "message"},
            ))
        status = repository.task_status(task_id, conversation_id)
        if status in {"completed", "failed", "cancelled", "waiting_for_user"}:
            # The worker may commit its terminal event between the first read
            # and the status read; drain it before closing the stream.
            for row in repository.events_after(task_id, cursor):
                cursor = row["id"]
                payload = row["payload_json"] or {}
                yield f"id: {cursor}\n" + encode_sse(SSEEvent(
                    event=row["event_type"], message=payload.get("message", ""),
                    data={key: value for key, value in payload.items() if key != "message"},
                ))
            return
        await asyncio.sleep(0.2)


def start_background_task(task_id: str, repository, worker):
    async def run():
        token = execution_identity.set({"task_id": task_id, "conversation_id": repository.conversation_for_task(task_id)})
        try:
            async for _ in worker:
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            repository.add_event(task_id, "ERROR", {
                "message": "后台任务异常", "task_id": task_id, "error_type": type(exc).__name__,
            })
            repository.update_task(task_id, status="failed")
        finally:
            owns_worker = running_tasks.get(task_id) is asyncio.current_task()
            if owns_worker and repository.is_cancelled(task_id):
                repository.finish_cancellation(task_id)
            elif owns_worker and repository.task_status(task_id, repository.conversation_for_task(task_id)) == "running":
                repository.add_event(task_id, "ERROR", {
                    "message": "任务未产生最终结果", "task_id": task_id, "error_type": "MissingTerminalEvent",
                })
                repository.update_task(task_id, status="failed")
            if owns_worker:
                running_tasks.pop(task_id, None)
            execution_identity.reset(token)
    running_tasks[task_id] = asyncio.create_task(run(), name=f"scientific-task:{task_id}")


def history_repository():
    repository = conversation_repository()
    if repository is None:
        raise HTTPException(status_code=503, detail="PostgreSQL conversation history is not configured")
    return repository


def require_conversation(repository, conversation_id: str, user_id: str):
    try:
        return repository.require(conversation_id, user_id)
    except (ConversationNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc


@router.get("/health")
def health():
    """Cheap liveness probe: the FastAPI process is alive."""
    return {
        "status": "ok",
        "service": "Scientific Research Analysis Agent",
        "runtime_protocol_version": RUNTIME_PROTOCOL_VERSION,
    }


@router.get("/ready")
def ready():
    """Readiness probe for the services required by real Agent execution."""
    result = runtime_readiness(storage=storage, checkpointing=agent.checkpointing)
    return result


@router.post("/api/chat/stream")
async def chat_stream(request: ChatRequest, user_id: str = Depends(current_user)):
    security = await security_policy.resolve_async(request.query)
    async def generate():
        if security.action != "ALLOW":
            yield encode_sse(SSEEvent(event="SECURITY_DECISION", message="请求未获执行许可", data={**security.model_dump(), "new_tool_calls": 0}))
            yield encode_sse(SSEEvent(event="FINAL_ANSWER", message="安全策略回答", data={"answer": REFUSAL_ANSWER, "new_tool_calls": 0}))
            return
        try:
            async for item in agent.stream(request.query, user_id, request.thread_id, request.datasource_id):
                yield encode_sse(item)
        except PermissionError as exc:
            yield encode_sse(SSEEvent(event="ERROR", message="无权访问数据源", data={"error": str(exc), "status": 403}))
        except Exception as exc:
            yield encode_sse(SSEEvent(event="ERROR", message="任务执行失败", data={"error": str(exc)}))
    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/conversations", status_code=201)
def create_conversation(request: ConversationCreate, user_id: str = Depends(current_user)):
    return history_repository().create(user_id, request.title)


@router.get("/api/conversations")
def list_conversations(user_id: str = Depends(current_user)):
    return {"items": history_repository().list(user_id)}


@router.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, user_id: str = Depends(current_user)):
    repository = history_repository()
    try:
        return repository.get(conversation_id, user_id)
    except (ConversationNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc


@router.patch("/api/conversations/{conversation_id}")
def rename_conversation(
    conversation_id: str,
    request: ConversationPatch,
    user_id: str = Depends(current_user),
):
    repository = history_repository()
    require_conversation(repository, conversation_id, user_id)
    return repository.rename(conversation_id, user_id, request.title)


@router.delete("/api/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: str, user_id: str = Depends(current_user)):
    repository = history_repository()
    require_conversation(repository, conversation_id, user_id)
    repository.archive(conversation_id, user_id)
    return Response(status_code=204)


@router.get("/api/conversations/{conversation_id}/messages")
def conversation_messages(conversation_id: str, user_id: str = Depends(current_user)):
    repository = history_repository()
    try:
        return {"items": repository.messages(conversation_id, user_id)}
    except (ConversationNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail="conversation not found") from exc


@router.post("/api/conversations/{conversation_id}/chat/stream")
async def conversation_chat_stream(
    conversation_id: str,
    request: ConversationChatRequest,
    user_id: str = Depends(current_user),
):
    repository = history_repository()
    require_conversation(repository, conversation_id, user_id)
    security = await security_policy.resolve_async(request.query)
    if security.action != "ALLOW":
        try:
            task_id = repository.start_task(conversation_id, request.thread_id)
        except ActiveTaskConflict as exc:
            raise HTTPException(status_code=409, detail="conversation already has an active task") from exc
        repository.add_message(conversation_id, "user", request.query, task_id)
        repository.update_task(task_id, intent={"interaction_type": "SECURITY_REFUSAL", "requires_scientific_execution": False})
        repository.add_event(task_id, "SECURITY_DECISION", {
            **security.model_dump(), "message": "请求未获执行许可", "task_id": task_id, "new_tool_calls": 0,
        })
        repository.finish_task_with_answer(task_id, conversation_id, REFUSAL_ANSWER, {
            "message": "安全策略回答", "answer": REFUSAL_ANSWER, "task_id": task_id, "new_tool_calls": 0,
            "completion_status": "REFUSED", "quality_status": "REFUSED", "requires_scientific_execution": False,
        })
        return StreamingResponse(replay_task_events(repository, conversation_id, task_id), media_type="text/event-stream")
    stateless_messages = None
    if VARIANT == "STATELESS_FOLLOWUP":
        stateless_messages = [{"role": item["role"], "content": item["content"][:3500]}
                              for item in repository.messages(conversation_id, user_id)[-6:]]
        recent_contexts = []
    else:
        recent_contexts = repository.recent_analysis_contexts(conversation_id, user_id)
    latest_context = recent_contexts[0] if recent_contexts else None
    active_task = repository.active_task(conversation_id)
    resource_summary = resources.discover(user_id, request.thread_id)
    interaction = await conversation_policy.resolve_async(
        request.query,
        has_context=bool(recent_contexts or stateless_messages),
        active_task=active_task is not None,
        resources=resource_summary,
    )

    if interaction.interaction_type == "CANCEL_ACTIVE":
        if active_task is None:
            async def nothing_to_cancel():
                yield encode_sse(SSEEvent(
                    event="FINAL_ANSWER",
                    message="当前没有运行中的任务",
                    data={"answer": "当前会话没有正在执行或等待补充信息的任务。", "new_tool_calls": 0},
                ))
            return StreamingResponse(nothing_to_cancel(), media_type="text/event-stream")
        repository.add_message(conversation_id, "user", request.query, active_task["id"])
        after_id = repository.latest_event_id(active_task["id"])
        if not repository.cancel_waiting_task(active_task["id"], conversation_id):
            raise HTTPException(status_code=409, detail="task is not active or was already cancelled/completed")
        worker = running_tasks.get(active_task["id"])
        if worker is not None:
            worker.cancel()
        return StreamingResponse(
            replay_task_events(repository, conversation_id, active_task["id"], after_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if interaction.interaction_type != "DELEGATE":
        repository.set_first_query_title(conversation_id, request.query)
        try:
            task_id = repository.start_task(conversation_id, request.thread_id)
        except ActiveTaskConflict as exc:
            raise HTTPException(status_code=409, detail="conversation already has an active task") from exc
        repository.add_message(conversation_id, "user", request.query, task_id)
        repository.update_task(task_id, intent={
            "interaction_type": interaction.interaction_type,
            "interaction_source": interaction.source,
            "interaction_reason": interaction.reason,
            "requires_scientific_execution": False,
        })
        answer = conversation_policy.answer_for(interaction, resource_summary)

        async def direct_worker():
            repository.add_event(task_id, "SECURITY_DECISION", {**security.model_dump(), "message": "执行边界检查通过", "task_id": task_id})
            repository.add_event(task_id, "INTERACTION_RESOLVED", {
                "message": "已判断本轮无需执行科研工具",
                "task_id": task_id,
                "interaction_type": interaction.interaction_type,
                "source": interaction.source,
                "reason": interaction.reason,
                "new_tool_calls": 0,
            })
            repository.finish_task_with_answer(
                task_id,
                conversation_id,
                answer,
                {
                    "message": "直接回答完成",
                    "answer": answer,
                    "task_id": task_id,
                    "interaction_type": interaction.interaction_type,
                    "new_tool_calls": 0,
                },
            )
            if False:
                yield None

        start_background_task(task_id, repository, direct_worker())
        return StreamingResponse(
            replay_task_events(repository, conversation_id, task_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    follow_up = await followup_resolver.resolve_async(request.query, latest_context, recent_contexts)
    previous_context = next(
        (item for item in recent_contexts if item["task"]["id"] == follow_up.target_task_id),
        None,
    )
    refinement_patch = follow_up.refinement_patch
    sufficiency = StateSufficiencyResolver().resolve(follow_up, previous_context)
    follow_up.requires_execution = sufficiency.requires_execution
    provenance = build_provenance(previous_context)
    agent_query, workflow_dataset_version = workflow_query(
        request.query, follow_up.follow_up_type, previous_context, refinement_patch or TaskRefinementPatch()
    )
    workflow_datasource = request.datasource_id
    if previous_context and follow_up.follow_up_type in {"REFINE_PREVIOUS_TASK", "RERUN_PREVIOUS_TASK"}:
        workflow_datasource = request.datasource_id or (refinement_patch.datasource_id if refinement_patch else None) or (previous_context["task"].get("intent_json") or {}).get("datasource_id")
        previous_thread = previous_context["task"]["thread_id"]
        for filename in set(re.findall(r"[\w.-]+\.(?:csv|xlsx|xls)", agent_query, re.I)):
            target = workspace.safe_file(user_id, request.thread_id, filename, create_workspace=True)
            if target.exists():
                continue
            source = workspace.safe_file(user_id, previous_thread, filename)
            if not source.exists():
                source = storage.materialize(user_id, previous_thread, filename)
            if source is not None and source.exists():
                shutil.copy2(source, target)
    repository.set_first_query_title(conversation_id, request.query)
    try:
        task_id = repository.start_task(conversation_id, request.thread_id)
    except ActiveTaskConflict as exc:
        raise HTTPException(status_code=409, detail="conversation already has an active task") from exc
    repository.add_message(conversation_id, "user", request.query, task_id)
    repository.update_task(
        task_id,
        intent={
            "follow_up_type": follow_up.follow_up_type,
            "previous_task_id": follow_up.target_task_id,
            "classification_source": follow_up.source,
            "clarification_question": follow_up.clarification_question,
            "refinement_patch": refinement_patch.model_dump(exclude_none=True) if refinement_patch else None,
            "datasource_id": workflow_datasource,
            "follow_up_decision": follow_up.model_dump(mode="json"),
            "requires_scientific_execution": sufficiency.requires_execution,
        },
    )

    async def generate():
        terminal = False
        try:
            if repository.is_cancelled(task_id):
                yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                return
            repository.add_event(task_id, "SECURITY_DECISION", {**security.model_dump(), "message": "执行边界检查通过", "task_id": task_id})
            repository.add_event(task_id, "INTERACTION_RESOLVED", {
                "interaction_type": interaction.interaction_type, "reason": interaction.reason,
                "message": "已判断交互类型", "task_id": task_id,
            })
            trace_data = {
                "follow_up_type": follow_up.follow_up_type,
                "previous_task_id": follow_up.target_task_id,
                "loaded_evidence_count": len(provenance.evidence) if provenance else 0,
                "provenance_source": "postgres" if provenance and previous_context else "none",
                "classification_source": follow_up.source,
                "classification_reason": follow_up.reason,
                "interaction_type": follow_up.interaction_type,
                "requested_content": follow_up.requested_content,
                "requires_execution": sufficiency.requires_execution,
                "state_sufficiency": sufficiency.model_dump(),
                "llm_telemetry": follow_up.llm_telemetry,
                "new_tool_calls": 0 if not sufficiency.requires_execution else None,
                "task_id": task_id,
            }
            repository.add_event(task_id, "FOLLOW_UP_TYPE", {"message": "已解析会话上下文", **trace_data})
            yield encode_sse(SSEEvent(event="FOLLOW_UP_TYPE", message="已解析会话上下文", data=trace_data))

            if sufficiency.action == "CLARIFY":
                answer = follow_up.clarification_question or "请明确你指的是哪一次任务或哪个数据版本。"
                final_data = {"answer": answer, "task_id": task_id, "new_tool_calls": 0}
                if not repository.finish_task_with_answer(
                    task_id, conversation_id, answer, {"message": "需要澄清历史任务", **final_data}
                ):
                    yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                    return
                yield encode_sse(SSEEvent(event="FINAL_ANSWER", message="需要澄清历史任务", data=final_data))
                return

            if not sufficiency.requires_execution:
                provenance_data = provenance.model_dump(mode="json")
                loaded_data = {
                    "previous_task_id": provenance.previous_task_id,
                    "loaded_evidence_count": len(provenance.evidence),
                    "provenance_source": "postgres" if previous_context else "none",
                    "new_tool_calls": 0,
                    "provenance": provenance_data,
                    "task_id": task_id,
                }
                repository.add_event(task_id, "PROVENANCE_LOADED", {"message": "已加载持久化证据", **loaded_data})
                yield encode_sse(SSEEvent(event="PROVENANCE_LOADED", message="已加载持久化证据", data=loaded_data))
                decision_data = {"task_id": task_id, "new_tool_calls": 0,
                    "decision": {"action": "ANSWER", "answer_basis": "PERSISTED_STATE",
                                 "reason_summary": "持久化状态已满足本轮信息需求，不启动执行工作流。"},
                    "decision_source": "state_sufficiency_resolver", "previous_task_id": provenance.previous_task_id}
                repository.add_event(task_id, "AGENT_DECISION", {"message": "复用已有状态回答", **decision_data})
                yield encode_sse(SSEEvent(event="AGENT_DECISION", message="复用已有状态回答", data=decision_data))
                # An error-history question needs process records, not a new
                # empirical claim or a model-authored Evidence ID.
                reply_validation_failed = False
                error_only = (follow_up.interaction_type == "ERROR_QUESTION"
                              and bool(follow_up.requested_content)
                              and set(follow_up.requested_content) <= {"error", "uncertainty", "tools"})
                if error_only:
                    answer = compose_followup_response(follow_up, provenance, sufficiency)
                    response_claims = []
                    response_telemetry = {"llm_called": False, "fallback": False,
                                          "reason_summary": "recorded process-error explanation"}
                else:
                    try:
                        response, response_telemetry = await grounded_responses.followup(
                            request.query, follow_up, provenance, sufficiency,
                        )
                        answer = response.answer
                        response_claims = response.claims
                    except GroundedResponseValidationError:
                        reply_validation_failed = True
                        # Preserve the rejected answer only in internal audit.
                        # Never expose validator internals or present its
                        # unsupported scientific assertions as a final answer.
                        answer = ("历史回答没有通过持久化证据校验，当前无法提供可靠的科研结论。"
                                  "本轮没有重新执行数据库或科研工具；请使用重新分析获取新的实际结果。")
                        response_claims = []
                        response_telemetry = {"fallback": True,
                            "error_type": "GroundedResponseValidationError",
                            "reason_summary": "historic reply did not pass evidence validation"}
                
                final_data = {
                    "answer": answer,
                    "llm_telemetry": response_telemetry,
                    "state": {"claims": [claim.model_dump() for claim in response_claims],
                              "quality_status": "INSUFFICIENT_EVIDENCE" if (sufficiency.missing_content or reply_validation_failed) else "PERSISTED_STATE_REUSE"},
                    "previous_task_id": provenance.previous_task_id,
                    "loaded_evidence_count": len(provenance.evidence),
                    "provenance_source": "postgres" if previous_context else "none",
                    "new_tool_calls": 0,
                    "task_id": task_id,
                }
                if not repository.finish_task_with_answer(
                    task_id, conversation_id, answer, {"message": "证据说明完成", **final_data}
                ):
                    yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                    return
                yield encode_sse(SSEEvent(event="FINAL_ANSWER", message="证据说明完成", data=final_data))
                return

            async for item in agent.stream(
                agent_query,
                user_id,
                request.thread_id,
                workflow_datasource,
                conversation_context={"chat_messages": stateless_messages} if stateless_messages is not None else {
                    "follow_up_decision": follow_up.model_dump(mode="json"),
                    "previous_query_scope": ((previous_context or {}).get("task", {}).get("intent_json") or {}).get("query_scope", {}),
                    "state_sufficiency": sufficiency.model_dump(mode="json"),
                    "current_user_query": request.query,
                    # A fresh/rerun execution must not mistake OLD failed
                    # tool observations or previous answers for CURRENT facts.
                    # Refinements/continuations can still see bounded history.
                    **({} if follow_up.interaction_type in {"NEW_TASK", "RERUN"} else {
                        "summary": conversation_context_summary(request.query, recent_contexts).model_dump(mode="json"),
                        "previous_provenance": provenance.model_dump(mode="json"),
                    }),
                },
                **(
                    {"dataset_version": workflow_dataset_version}
                    if follow_up.follow_up_type in {"REFINE_PREVIOUS_TASK", "RERUN_PREVIOUS_TASK"}
                    and workflow_dataset_version
                    else {}
                ),
            ):
                if repository.is_cancelled(task_id):
                    yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                    return
                payload = {"message": item.message, **item.data, "task_id": task_id}
                if item.event != "FINAL_ANSWER":
                    repository.add_event(task_id, item.event, payload)
                if item.event == "INTENT_RESOLVED":
                    intent = {
                        **item.data.get("intent", {}),
                        "follow_up_type": follow_up.follow_up_type,
                        "previous_task_id": previous_context["task"]["id"] if previous_context else None,
                        "classification_source": follow_up.source,
                        "datasource_id": workflow_datasource,
                        "follow_up_decision": follow_up.model_dump(mode="json"),
                        "requires_scientific_execution": True,
                    }
                    repository.update_task(
                        task_id,
                        intent=intent,
                        selected_skills=item.data.get("selected_skills", []),
                    )
                elif item.event == "EVIDENCE_ADDED" and item.data.get("evidence"):
                    repository.add_evidence(task_id, item.data["evidence"])
                elif item.event == "ARTIFACT_CREATED" and item.data.get("artifact"):
                    repository.add_artifact(task_id, item.data["artifact"])
                elif item.event == "WAITING_FOR_USER":
                    repository.update_task(task_id, status="waiting_for_user")
                elif item.event == "FINAL_ANSWER":
                    status = scientific_task_status(item.data)
                    if not repository.finish_task_with_answer(task_id, conversation_id, item.data["answer"], payload, status=status):
                        yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                        return
                    claims = (item.data.get("state") or {}).get("claims") or []
                    if claims:
                        repository.add_claims(task_id, claims)
                    terminal = True
                yield encode_sse(SSEEvent(event=item.event, message=item.message, data={**item.data, "task_id": task_id}))
            if not terminal:
                # A persisted HITL event is non-terminal and remains resumable.
                pass
        except PermissionError as exc:
            if repository.is_cancelled(task_id):
                yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                return
            repository.update_task(task_id, status="failed")
            repository.add_message(conversation_id, "error", str(exc), task_id)
            error = SSEEvent(event="ERROR", message="无权访问数据源", data={"error": str(exc), "status": 403, "task_id": task_id})
            repository.add_event(task_id, error.event, {"message": error.message, **error.data})
            yield encode_sse(error)
        except Exception as exc:
            if repository.is_cancelled(task_id):
                yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": task_id}))
                return
            repository.update_task(task_id, status="failed")
            failure = execution_failure(exc, "conversation_execution", task_id=task_id,
                                        conversation_id=conversation_id, thread_id=request.thread_id)
            repository.add_message(conversation_id, "error", failure["reason_summary"], task_id)
            error = SSEEvent(event="ERROR", message="任务执行失败", data=failure)
            repository.add_event(task_id, error.event, {"message": error.message, **error.data})
            yield encode_sse(error)

    start_background_task(task_id, repository, generate())
    return StreamingResponse(
        replay_task_events(repository, conversation_id, task_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/conversations/{conversation_id}/tasks/{task_id}/events")
async def reconnect_task_events(
    conversation_id: str, task_id: str, after_id: int = 0, user_id: str = Depends(current_user),
):
    repository = history_repository()
    require_conversation(repository, conversation_id, user_id)
    if repository.task_status(task_id, conversation_id) is None:
        raise HTTPException(status_code=404, detail="task not found")
    return StreamingResponse(
        replay_task_events(repository, conversation_id, task_id, after_id),
        media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/files/upload")
async def upload_file(thread_id: str, file: UploadFile = File(...), user_id: str = Depends(current_user)):
    if not file.filename or Path(file.filename).suffix.lower() not in {".csv", ".xlsx", ".xls"}:
        raise HTTPException(status_code=400, detail="only CSV/Excel files are allowed")
    try:
        workspace.safe_file(user_id, thread_id, file.filename, create_workspace=False)
    except WorkspaceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    file.file.seek(0, 2)
    size = file.file.tell()
    file.file.seek(0)
    if size > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file exceeds 25 MiB")
    if storage.configured:
        metadata = storage.upload(
            user_id,
            thread_id,
            file.filename,
            file.content_type or "application/octet-stream",
            file.file,
            size,
        )
        return {**metadata.model_dump(), "backend": "minio"}
    target = workspace.safe_file(user_id, thread_id, file.filename, create_workspace=True)
    size = 0
    with target.open("wb") as handle:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > 25 * 1024 * 1024:
                target.unlink(missing_ok=True)
                raise HTTPException(status_code=413, detail="file exceeds 25 MiB")
            handle.write(chunk)
    return {"filename": target.name, "size": size, "thread_id": thread_id}


@router.get("/api/datasources")
def list_datasources(user_id: str = Depends(current_user)):
    summary = resources.discover(user_id, "resource-list")
    return {"items": [{"id": item, "name": "科研训练数据"} for item in summary.authorized_datasources]}


@router.get("/api/resources")
def list_resources(thread_id: str, user_id: str = Depends(current_user)):
    return resources.discover(user_id, thread_id)


@router.post("/api/conversations/{conversation_id}/tasks/{task_id}/cancel")
@router.post("/api/tasks/{task_id}/cancel")
async def cancel_waiting_task(conversation_id: str | None = None, task_id: str = "", user_id: str = Depends(current_user)):
    repository = history_repository()
    if conversation_id is None:
        conversation_id = repository.owned_task_conversation(task_id, user_id)
        if conversation_id is None:
            raise HTTPException(status_code=404, detail="task not found")
    require_conversation(repository, conversation_id, user_id)
    if not repository.cancel_waiting_task(task_id, conversation_id):
        raise HTTPException(status_code=409, detail="task is not active or was already cancelled/completed")
    worker = running_tasks.get(task_id)
    if worker is not None:
        worker.cancel()
    return {"task_id": task_id, "status": repository.task_status(task_id, conversation_id), "cancelled": True}


@router.post("/api/agent/resume")
async def resume_agent(request: ResumeRequest, user_id: str = Depends(current_user)):
    repository = None
    resume_mode = "checkpoint"
    resume_query = None
    resume_datasource_id = None
    if request.conversation_id or request.task_id:
        if not (request.conversation_id and request.task_id):
            raise HTTPException(status_code=400, detail="conversation_id and task_id must be supplied together")
        repository = history_repository()
        detail = get_conversation(request.conversation_id, user_id)
        task = next((item for item in detail["tasks"] if item["id"] == request.task_id), None)
        if task is None or task["thread_id"] != request.thread_id:
            raise HTTPException(status_code=404, detail="resumable task not found")
        waiting_event = next(
            (
                item for item in reversed(detail.get("events", []))
                if item["task_id"] == request.task_id and item["event_type"] == "WAITING_FOR_USER"
            ),
            None,
        )
        waiting_payload = (waiting_event or {}).get("payload_json") or {}
        if "missing_files" in waiting_payload:
            resume_mode = "file_wait"
            original_user = next(
                (
                    item["content"] for item in detail.get("messages", [])
                    if item.get("task_id") == request.task_id and item.get("role") == "user"
                ),
                "",
            )
            if not original_user:
                raise HTTPException(status_code=409, detail="cannot restore the original file-analysis request")
            resume_query = original_user
            answer_text = request.answer.strip()
            if answer_text and answer_text not in {"已上传", "上传完成", "done", "ok"}:
                resume_query = f"{resume_query}\n用户补充：{answer_text}"
            resume_datasource_id = (task.get("intent_json") or {}).get("datasource_id")
        if not repository.claim_resume(request.task_id, request.conversation_id, request.thread_id):
            raise HTTPException(status_code=409, detail="task is not waiting, was already resumed, or has expired")
        repository.add_message(request.conversation_id, "user", request.answer, request.task_id)

    elif (repository := conversation_repository()) is not None and repository.has_thread(request.thread_id):
        raise HTTPException(status_code=400, detail="persisted tasks require conversation_id and task_id for resume")

    async def generate():
        try:
            source = agent.resume(request.thread_id, request.answer, user_id)
            if resume_mode == "file_wait":
                agent.pending.pop(request.thread_id, None)
            async for item in source:
                if repository is not None:
                    if repository.is_cancelled(request.task_id):
                        yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": request.task_id}))
                        return
                    payload = {"message": item.message, **item.data, "task_id": request.task_id}
                    if item.event != "FINAL_ANSWER":
                        repository.add_event(request.task_id, item.event, payload)
                    if item.event == "EVIDENCE_ADDED" and item.data.get("evidence"):
                        repository.add_evidence(request.task_id, item.data["evidence"])
                    elif item.event == "ARTIFACT_CREATED" and item.data.get("artifact"):
                        repository.add_artifact(request.task_id, item.data["artifact"])
                    elif item.event == "WAITING_FOR_USER":
                        repository.update_task(request.task_id, status="waiting_for_user")
                    elif item.event == "FINAL_ANSWER":
                        status = scientific_task_status(item.data)
                        if not repository.finish_task_with_answer(
                            request.task_id, request.conversation_id, item.data["answer"], payload, status=status
                        ):
                            yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": request.task_id}))
                            return
                        claims = (item.data.get("state") or {}).get("claims") or []
                        if claims:
                            repository.add_claims(request.task_id, claims)
                    elif item.event == "ERROR":
                        repository.add_message(
                            request.conversation_id,
                            "error",
                            item.data.get("error", item.message),
                            request.task_id,
                        )
                        repository.update_task(request.task_id, status="failed")
                yield encode_sse(SSEEvent(event=item.event, message=item.message, data={**item.data, "task_id": request.task_id} if request.task_id else item.data))
        except Exception as exc:
            failure = execution_failure(exc, "resume_execution", task_id=request.task_id,
                                        conversation_id=request.conversation_id, thread_id=request.thread_id)
            if repository is not None:
                if repository.is_cancelled(request.task_id):
                    yield encode_sse(SSEEvent(event="CANCELLED", message="用户已取消任务", data={"task_id": request.task_id}))
                    return
                repository.update_task(request.task_id, status="failed")
                repository.add_message(request.conversation_id, "error", failure["reason_summary"], request.task_id)
                repository.add_event(request.task_id, "ERROR", {"message": "恢复任务失败", **failure})
            yield encode_sse(SSEEvent(event="ERROR", message="恢复任务失败", data=failure))
    if repository is not None:
        # Capture the cursor before the resumed worker can persist new events;
        # otherwise a fast worker could finish before latest_event_id() is read
        # and the client would skip the resume/final events entirely.
        resume_after_id = repository.latest_event_id(request.task_id)
        start_background_task(request.task_id, repository, generate())
        return StreamingResponse(
            replay_task_events(
                repository,
                request.conversation_id,
                request.task_id,
                after_id=resume_after_id,
            ),
            media_type="text/event-stream",
        )
    return StreamingResponse(generate(), media_type="text/event-stream")


@router.get("/api/tasks/{thread_id}")
def task_status(thread_id: str, user_id: str = Depends(current_user)):
    repository = conversation_repository()
    if repository is not None:
        task = repository.task_by_thread(thread_id)
        if task is not None:
            conversation_id = repository.owned_task_conversation(task["id"], user_id)
            if conversation_id is None:
                raise HTTPException(status_code=404, detail="task not found")
            return {
                "thread_id": thread_id,
                "task_id": task["id"],
                "conversation_id": conversation_id,
                "status": task["status"],
            }
    return {"thread_id": thread_id, "status": "waiting_for_user" if thread_id in agent.pending else "not_pending"}


@router.get("/api/artifacts/{artifact_id}/download")
def download_artifact(artifact_id: str, user_id: str = Depends(current_user)):
    repository = history_repository()
    try:
        artifact = repository.artifact(artifact_id, user_id)
    except (ConversationNotFound, ValueError) as exc:
        raise HTTPException(status_code=404, detail="artifact not found") from exc
    content = storage.download_object(artifact["object_key"])
    content_type = artifact.get("metadata", {}).get("content_type", "application/octet-stream")
    return Response(
        content=content,
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{artifact["filename"]}"'},
    )

