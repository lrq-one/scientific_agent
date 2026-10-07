from __future__ import annotations

import json
import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Any, Sequence

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool

from app.config import SKILLS_ROOT
from app.services.checkpointing import CheckpointService
from app.services.mcp_client import ScientificMCPClient
from app.services.workspace import WorkspaceService
from app.services.llm_config import llm_settings


class DeterministicRuntimeModel(BaseChatModel):
    """Offline model used only to exercise the real DeepAgents loop in demo/tests."""

    @property
    def _llm_type(self) -> str:
        return "deterministic-deepagents-runtime"

    def bind_tools(self, tools: Sequence[BaseTool | dict | type | Any], **kwargs):
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        user_message = next((m.content for m in reversed(messages) if isinstance(m, HumanMessage)), "scientific task")
        if messages and isinstance(messages[-1], ToolMessage):
            molecule_match = re.search(r"\b((?:M|T)\d{3,})\b", str(user_message), flags=re.I)
            if messages[-1].name == "register_runtime_context" and molecule_match:
                response = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "get_molecule_features",
                            "args": {"molecule_id": molecule_match.group(1).upper()},
                            "id": f"mcp-{uuid.uuid4().hex[:10]}",
                            "type": "tool_call",
                        }
                    ],
                )
            else:
                response = AIMessage(content="DeepAgents runtime context registered; no explicit molecule_id requires MCP lookup.")
        else:
            response = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "register_runtime_context",
                        "args": {"goal": str(user_message)[:500]},
                        "id": f"runtime-{uuid.uuid4().hex[:10]}",
                        "type": "tool_call",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=response)])


class DeepAgentRuntime:
    def __init__(self, checkpointing: CheckpointService, workspace: WorkspaceService | None = None):
        self.checkpointing = checkpointing
        self.workspace = workspace or WorkspaceService()
        self.mcp = ScientificMCPClient()

    def _model(self):
        settings = llm_settings()
        # DeepAgents is a bounded sub-runtime for skill loading and explicit MCP
        # enrichment. LangGraph remains the task lifecycle authority. Keep this
        # deterministic by default so the sub-runtime cannot block the main plan.
        if settings.configured and os.getenv("DEEP_RUNTIME_LLM") == "1":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=settings.model,
                api_key=settings.api_key,
                base_url=settings.api_base,
                temperature=0,
            )
        return DeterministicRuntimeModel()

    async def run_bounded_subtask(
        self,
        goal: str,
        user_id: str,
        thread_id: str,
        selected_skills: list[str],
    ) -> dict[str, Any]:
        calls: list[dict[str, Any]] = []

        @tool
        def register_runtime_context(goal: str) -> str:
            """Register the bounded scientific task with the runtime before execution."""
            item = {"tool": "register_runtime_context", "goal": goal}
            calls.append(item)
            return json.dumps({"registered": True, "goal": goal}, ensure_ascii=False)

        @tool
        def get_molecule_features(molecule_id: str) -> str:
            """Call the registered scientific MCP server for synthetic molecule metadata."""
            result = asyncio.run(self.mcp.call("get_molecule_features", {"molecule_id": molecule_id}))
            calls.append({
                "tool": "mcp:get_molecule_features",
                "molecule_id": molecule_id,
                "success": result.success,
                "result": result.model_dump(mode="json"),
            })
            return result.model_dump_json()

        root = self.workspace.path_for(user_id, thread_id, create=True)
        skill_paths = []
        for name in selected_skills:
            source = SKILLS_ROOT / name / "SKILL.md"
            if not source.exists():
                continue
            target = root / ".skills" / name / "SKILL.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            skill_paths.append(f"/.skills/{name}")
        backend = FilesystemBackend(root_dir=root, virtual_mode=True)
        graph = create_deep_agent(
            model=self._model(),
            tools=[register_runtime_context, get_molecule_features],
            system_prompt=(
                "You are the bounded DeepAgents sub-agent for a scientific analysis agent. "
                "Execute only the bounded sub-task with registered tools and loaded skills. Do not fabricate evidence or expose chain-of-thought."
            ),
            skills=skill_paths,
            backend=backend,
            checkpointer=self.checkpointing.checkpointer,
            name="scientific_runtime",
        )
        result = await asyncio.to_thread(
            graph.invoke,
            {"messages": [{"role": "user", "content": goal}]},
            {"configurable": {"thread_id": f"deep:{thread_id}"}, "recursion_limit": 12},
        )
        final_message = result["messages"][-1]
        return {
            "runtime": "deepagents.create_deep_agent",
            "role": "bounded_subagent",
            "tools": [register_runtime_context.name, get_molecule_features.name],
            "skills": selected_skills,
            "skill_paths": skill_paths,
            "workspace_backend": type(backend).__name__,
            "workspace": str(root),
            "checkpointer": type(self.checkpointing.checkpointer).__name__,
            "persistent_checkpoint": self.checkpointing.persistent,
            "tool_calls": calls,
            "result": str(final_message.content),
            "model_mode": "real_llm_opt_in" if llm_settings().configured and os.getenv("DEEP_RUNTIME_LLM") == "1" else "deterministic_bounded_runtime",
        }



    async def run_scaffold(
        self,
        goal: str,
        user_id: str,
        thread_id: str,
        selected_skills: list[str],
    ) -> dict[str, Any]:
        """Backward-compatible alias; use run_bounded_subtask in product code."""
        return await self.run_bounded_subtask(goal, user_id, thread_id, selected_skills)
