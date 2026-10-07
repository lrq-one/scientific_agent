# Scientific Agent Phase 4.5 — Capability Audit

审计日期：2026-10-07。范围：`app/agents/`、`app/services/`、`app/tools/`、`app/api/`、`app/models/`、`app/datasources/`、`evaluation/`、`web/`、9 个 `skills/*/SKILL.md` 与现有测试。仓库没有独立的 `app/schemas/`、`app/repositories/`、`app/integrations/`；相应代码分别位于 `app/models/schemas.py`、`app/services/conversation_history.py` 与 `app/services/{mcp_client,object_storage}.py`。本审计先于 Phase 4.5 功能修改，不运行旧 Phase 4 全量评测。

状态定义：`IMPLEMENTED_AND_VERIFIED`＝运行路径与针对性测试均存在；`IMPLEMENTED_BUT_UNVERIFIED`＝可调用代码存在但缺少对应端到端证据；`PARTIAL`＝只覆盖部分业务场景或关键契约缺失；`FALLBACK_ONLY`＝只有演示/确定性替代；`NOT_IMPLEMENTED`＝没有实际运行路径。V1 指标保留在 `evaluation/final_metrics.json` 和 `docs/phase4_evaluation_report.md`；Follow-up 历史结果保留在 `evaluation/followup_metrics.json`。既有 Follow-up Test 已经被查看，后续不能作为全新独立 Test。

## 1. 核心能力矩阵

| 能力与状态 | 实际文件/函数；触发与调用路径 | LLM / 真实 Tool / 持久化 | 已有验证 | 主要缺口；优先级 |
|---|---|---|---|---|
| Intent Routing — `IMPLEMENTED_AND_VERIFIED` | `app/agents/request_router.py:28,88`；`ScientificAgent.stream` 在每个新分析任务中调用 `route_async(query, resources)` | 简单单资源走确定性分支；歧义任务可用 Qwen structured output；仅产生 Intent，不直接执行工具；Intent 由 `app/api/routes.py` 写入 tasks | `tests/test_router.py`、`test_llm_router_fallback.py`；V1 Test task 100%、all-field 82.5% | 意图字段与后续执行能力不总一致；P1 |
| Follow-up Routing — `PARTIAL` | `app/api/routes.py:141` → `conversation_history.latest_analysis_context` → `followup.ConversationContextResolver.resolve_async`；解释类直接 `provenance_answer`，修改/重跑进入原 Agent | 明确规则优先；歧义可用 Qwen structured output；解释类无新工具；新 task、events、message 持久化 | `tests/test_followup_routing.py`、真实四轮 smoke；历史 Frozen Test 80% | “异常结构”误判 ERROR；部分 REFINE→NEW；只看最近一个 meaningful task，无法可靠消解多任务指代；缺证据时未提供明确澄清协议；P0 |
| Task Refinement — `PARTIAL` | `app/services/followup.py:14` `workflow_query` 恢复上一 user goal 并替换 train 版本；`routes.py` 再调用 `agent.stream` | 可重新运行真实 SQL；新 task 与 `previous_task_id` 在 intent/event 关联；新旧 Evidence 分属不同 task | `test_refine_restores_previous_goal...`、四轮 PostgreSQL/Qwen smoke | 仅 train 版本受控替换；model、过滤、artifact 偏好无结构化参数补丁；未定义哪些旧 Evidence 可复用；P0 |
| Conversation Persistence — `IMPLEMENTED_AND_VERIFIED` | `app/services/conversation_history.py:81,178,256,295,306,331`；API 写 messages/tasks/events/evidence/artifacts，历史 detail 一次返回 | PostgreSQL；不调用 LLM；保存 SQL/原始结果在 task event JSON、Evidence 在独立表 | `test_product_expansion.py`、`test_followup_routing.py`、真实四轮 DB smoke | 没有显式 Claim→Evidence 表；大量事件 JSON 可膨胀；P0 |
| Evidence Provenance — `PARTIAL` | `app/services/followup.py:140,210` 从持久化 events/evidence 重建 SQL、参数、raw rows、来源和回答 | 无需新 Tool；可选分类 LLM；来源可追踪至 task/tool call | provenance 单测与 DB 四轮 smoke | 只有 Evidence 列表；最终回答的每条 Claim 没有明确 evidence ID 集合；不支持跨多个任务/冲突来源的严谨映射；P0 |
| Evidence Quality Gate — `PARTIAL` | `ScientificAgent._database_evidence`、`_evidence_quality_issues`、`_finalize` | 本地检查；不会额外调用 LLM/工具；结果随 final answer 持久化 | `test_followup_routing.py` 覆盖 0 rows、缺误差列、缺 fused 行 | 缺 NULL、版本不符、来源冲突、样本量、部分结果、SQL 目标语义等判定；仅输出 `INSUFFICIENT_EVIDENCE`；Finalizer 仍有固定解释模板；P0 |
| Skill Selection — `PARTIAL` | `app/services/skills.py:24,38` 加载 9 个契约并按 tags/intents 打分选前 2；`ScientificAgent.stream:222` 调用 | 产品路径无 Skill LLM 排序；skill 影响候选 Tool 过滤及 DeepAgents scaffold 加载，但不决定实际 Python 分支 | `test_product_expansion.py`、V1 独立 Skill 评测 Recall@3 100% | V1 评测的 All-Skill LLM 不是产品 `SkillService.select`；Skill 契约与实际业务执行未逐一绑定；P2 |
| Tool Registry / Selection — `PARTIAL` | `app/tools/registry.py:17-152` 23 个 ToolSpec；`ScientificAgent.stream:226-252` 生成候选并选一个 | 可用 Qwen structured selection；但 `selected_choice` 仅进入 `TOOL_CANDIDATES` Trace，实际调用由后续固定 Python 分支决定；metadata 不等于执行器 | `test_product_expansion.py`、V1 Test selection 94%、argument exact 34%（未执行工具） | 无统一参数 schema validation/dispatch；`_spec` 把所有 required 字段设为 string、允许任意额外字段；P3 |
| File Analysis — `PARTIAL` | `app/tools/file_tools.py` 具备读 CSV/XLSX、profile、metrics、group、join/filter；产品流只调用 `inspect_columns`、`calculate_metrics`、`group_metrics` | 确定性真实文件工具；部分 Evidence 写 DB | `test_agent.py`、`test_product_expansion.py`、`test_mixed_e2e.py` | 文件列缺失后产品流会索引 `result.data` 而非按 `success` 恢复；匹配样本比较未在主链实现；P1/P3 |
| Database / Text-to-SQL — `PARTIAL` | `app/services/text2sql.py:139` → `DatabaseService.schema/relationships/check_query/execute` → `PostgresQueryExecutor` | Qwen SQLCandidate 或固定 fixture fallback；SQLGlot 表白名单、EXPLAIN、只读凭据、500 行上限；SQL/raw rows 在 event | SQL Guard、Postgres runtime、Text2SQL 单测；V1 Test result 93.33%，E2E 17/20 | 三类真实语义错误（不存在列、未 JOIN 别名、错字段）；有限 repair/replan 缺失；500 行截断未返回 total/partial 标记；P1/P3 |
| Scientific MCP — `IMPLEMENTED_BUT_UNVERIFIED` | `app/services/mcp_client.py:32` 启动独立 stdio server；mixed flow 对固定 `M004` 调 `get_molecule_features` | 真实 stdio Tool，输入固定演示 ID；Evidence 在主链可持久化 | `test_mcp_tool.py`、`test_mixed_e2e.py` | 实际分子 ID 未从数据动态传入；服务不可用时缺 recovery；P1/P3 |
| Artifact Generation — `PARTIAL` | `app/services/artifacts.py` 可产生 PNG/CSV/XLSX；`ScientificAgent.stream:323-338` 仅复杂文件/混合流生成 PNG+CSV；`ObjectStorageService` 上传 MinIO，API 下载 | 真实对象存储；artifact metadata 入 DB | `test_product_expansion.py` 测 PNG/CSV 字节；V1 E2E artifact correctness 50% | 未核对必需列、数值与 Evidence 一致；产品流未生成 XLSX；空数据、跨任务隔离和下载内容缺 E2E 断言；P3 |
| Scientific Model — `NOT_IMPLEMENTED` | `app/tools/scientific_model.py:7` `predict_rt` 无论 `MODEL_PATH` 是否配置都返回失败 | 没有模型推理；`ToolSpec`/Skill 中的 `predict_rt` 不构成真实能力 | 无权重推理测试 | 缺权重、预处理、adapter、版本/资源约束；依赖外部资源；P3 设计/缺失清单 |
| Frontend Agent Experience — `PARTIAL` | `web/src/App.vue` 渲染 messages、常规 trace、Evidence 列表、artifact 下载、HITL；`conversationLoader.js` 防历史切换竞态 | HTTP/SSE；历史读取不触发 LLM | 4 个历史切换测试 | Follow-up trace 未显示类型/上一 task；Evidence 无 SQL/参数/claim 引用详情；无取消控件；P0/P4 |

## 2. Planning、Replanning、Recovery 的实际控制权

| 能力与状态 | 实际执行证据 | 缺口与优先级 |
|---|---|---|
| Planning — `PARTIAL` | `app/agents/planning_graph.py:15` 的 LangGraph 只有一个 `create_plan` 节点，`mixed_analysis` 固定生成 3 步，其余任务固定 1 步。`ScientificAgent.stream:278-290` 调 graph 后，`292-382` 仍以 `if task_type` 分支执行，并手工修改 `state.plan[0..2]`。 | Plan 是真实持久化/展示对象，但不负责选择、调度或阻止步骤执行；依赖关系没有运行期检查。P1 |
| DeepAgents — `PARTIAL` | `app/agents/deep_runtime.py:92` 构建真实 `create_deep_agent`，仅注册 `register_runtime_context` 和 `get_molecule_features` 两个 Tool，返回 scaffold trace；主链随后继续固定 Python 分支。 | 框架运行了，但不承担文件/SQL/Artifact 业务循环、Observation-driven Replanning 或 Finalizer 控制；不能以存在 DeepAgents graph 宣称核心 Agent 闭环完成。P1 |
| Replanning — `NOT_IMPLEMENTED` | 代码中有 `MAX_REPLANS`、`plan_version`、`replan_count` 字段；没有 Observation→Replan→Re-execute 的边或调度函数。 | 缺失字段/空结果/MCP 不可用不会生成 revised plan。P1 |
| Tool Failure Recovery — `PARTIAL` | `ScientificAgent._record_tool` 有 `MAX_TOOL_CALLS`、failure_count；`stream` 有 `TASK_TIMEOUT`；API 捕获异常并发 ERROR。 | 无错误类型分流、有限 retry/backoff、重复调用 guard、用户取消；多处未检查 `ToolResult.success` 即读取 data。P1 |
| HITL / Durable Execution — `PARTIAL` | `app/services/checkpointing.py:24,36,80-90` 使用 LangGraph interrupt/Command(resume) 与 PostgresSaver；`ScientificAgent.resume:468` 使用同 thread；API 校验 conversation/task/thread。 | 后端重建已独测；重复/并发/过期/取消 resume 与恢复后失败尚未验证或限制，文件缺失等待仅在内存 `pending`。P1 |

## 3. 现有 9 个 Skill 的业务完整度

九份 Skill 文档都有 Purpose、Preconditions、Procedure、Evidence、HITL、Failure、Output 等段落；这是**契约覆盖**，不是业务工作流覆盖。产品主链选中 Skill 后没有按其 Procedure 调度。

| Skill | 状态 | 实际可调用链 / 反例或缺口 | 优先级 |
|---|---|---|---|
| `training_coverage_analysis` | `PARTIAL` | DB branch 可执行 schema→SQL→Evidence；版本可经 HITL 补充；缺跨版本组成/分母校验，空/矛盾观察不重规划。 | P0/P1 |
| `model_comparison` | `PARTIAL` | 复杂 file branch 分别算两个文件 MAE/RMSE 并生成图表；没有调用 `compare_models` 对齐样本，不能证明成对改进。 | P2/P3 |
| `mass_spec_error_analysis` | `PARTIAL` | `group_metrics`、固定 M004 MCP 可执行；高误差样本排名、版本与组样本量没有完整进入主链。 | P2 |
| `structure_subgroup_analysis` | `PARTIAL` | `group_metrics` 可运行；主链固定取 fused_ring 行，其他结构与缺失组未通用化。 | P2 |
| `dataset_quality_audit` | `IMPLEMENTED_BUT_UNVERIFIED` | `profile_dataset`、`join_tables`、`filter_samples` Tool 函数存在；产品主链未按 Skill 执行质量审计。 | P2 |
| `model_regression_diagnosis` | `IMPLEMENTED_BUT_UNVERIFIED` | `compare_models`、`find_high_error_samples` 独立函数存在；产品流未执行配对样本 delta 和发布门禁。 | P2 |
| `experiment_reproducibility_check` | `PARTIAL` | DB 有 experiment/model_runs/version 表，可用通用 SQL 查询；无专用 checklist、缺失依赖分类或 artifact 交付链。 | P2 |
| `scientific_result_summary` | `PARTIAL` | `_finalize` 输出固定 Evidence/Interpretation/Uncertainty；缺 Claim→Evidence grounding 和按目标定制。 | P0 |
| `rt_prediction_review` | `FALLBACK_ONLY` | Skill 文档和 `predict_rt` ToolSpec 存在，但 `predict_rt` adapter 永远返回 unavailable。 | P3/外部依赖 |

`mass_spec_error_analysis`、`structure_subgroup_analysis` 与 `model_regression_diagnosis` 对结构误差有职责交叠；新增 Skill 前应先划清“误差发现 / 子群检验 / 成对回归”的输入与输出边界。

## 4. 新增 Scientific Skills 的候选顺序

1. `cross_dataset_comparison`：现有 version、membership、molecules 表足够支撑组成、覆盖和版本增减的只读查询；先实现带分母的结果与证据，P2 首选。
2. `model_data_consistency_check`：现有 model_runs、predictions、training_memberships 和版本关系足够做一致性审计，适合补当前 provenance 缺口。
3. `experiment_run_diagnosis`：已有 experiment/run/metric 元数据，但需要专门的失败分类和可复现 checklist；可在一致性检查后实现。
4. `distribution_shift_analysis`：可做结构/质量分布的描述统计；OOD 和“漂移导致性能变化”需要额外统计方法、样本量和独立测试数据，暂不承诺完整结论。
5. `annotation_candidate_analysis`：需先确认 annotations 与 MS/MS 记录内容、候选筛选 Tool 的可用性；若只有 synthetic fixture，则仅上线明确标识的演示/不足证据路径。
6. `spectrum_quality_analysis`：当前仅有 msms_spectra 表和少量字段，尚无真实谱峰质量处理链；标记 `BLOCKED`，不为凑数上线。

## 5. 优先级与实施批次

1. **Batch 2 / P0**：构建有界 conversation context 和任务指代解析；明确缺失/歧义时的澄清；Claim→Evidence 显式关联；质量门禁状态机；结构化 refinement 参数补丁。先新增 Dev 和针对性回归，历史 Follow-up Test 只做回归，不能作为全新独立 Test。
2. **Batch 3 / P1**：把 PlanStep 与实际 dispatch 绑定，选择至少一个可重复的失败→Observation→Replan→Alternative/Fail Safely 场景；再做错误分类、bounded retry 与 HITL 幂等/并发控制。
3. **Batch 4 / P2**：先修已有 9 Skill 的运行契约与覆盖，再新增有真实表/Tool 支撑的 Skill，分别测试正常、负样例、数据不足、失败、组合。
4. **Batch 5 / P3**：Tool 参数 schema 与执行验证、SQL AST/alias/列/版本检查及有条件 repair、Artifact 内容正确性和真实跨资源关联。真实模型仅做本地权重/依赖审查与 adapter 设计，未获得完整资源前不标称可用。
5. **Batch 6 / Phase 4 v2**：新建 scenario/template disjoint 数据和覆盖矩阵；冻结协议后再跑新 Test。V1 历史基线与原始 JSON 不覆盖。

## 6. 审计边界

本文件是代码路径、现有测试和历史运行记录的能力审计；`IMPLEMENTED_BUT_UNVERIFIED` 与 `PARTIAL` 不代表功能不可运行，而是当前证据不足以宣称达到用户描述的完整科研业务契约。审计阶段没有新增 LLM 调用、没有改动业务逻辑，也没有更改 V1/Frozen Test 数据或指标。
