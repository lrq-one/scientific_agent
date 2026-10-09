# Scientific Agent 第一批 P0：SQLCandidate / Scope Recovery 最终验收

验收日期：2026-10-09（Asia/Shanghai）
项目：`D:\工作\scientific_agent`
分支：`codex/agent-protocol-audit-20261009`
验收基线提交：`d950d9e`（本轮本地提交 SHA 在提交后由交付消息给出）

## 结论

第一批 P0 达到受控合并标准：失败 Text2SQL 候选不会再被当作可信 SQLCandidate；Query Checker 在模型决策前和 Runtime 执行前均受 lineage 前置条件保护；`UNVERIFIED_SCOPE` 走按候选/范围上下文计数的一次定向修复，重复相同上下文确定性停止；Scope 真实违规与无法证明继续保持安全拒绝。

本结论是离线、只读验收结论，不等同于重新运行真实 Qwen 或重新执行真实 Agent 任务。历史 A/B/C 记录只读核对，未被修改。

## 执行协议与代码责任

实际链路为：

`AgentDecision → eligible_call_tools → Runtime 前置检查 → SQLCandidate provenance → QueryScope → Query Checker → read-only Executor → ToolResult → Observation/Evidence → GoalCoverage`

关键实现位置：

| 责任 | 文件/行 | 验收要点 |
|---|---|---|
| 模型可见工具过滤 | `app/agents/decision_node.py:43-61, 179-186` | 无可信来源时移除 `query_checker`，并把未满足前置条件写入决策上下文 |
| Runtime 二次防线 | `app/agents/runtime.py:233-244` | 决策落地前再次检查 Query Checker 可调用性 |
| SQLCandidate 来源判断 | `app/services/sql_candidate.py:17-105` | 只接受成功 Text2SQL、合法历史 provenance 或明确用户 SQL；失败候选只作诊断 |
| 候选状态写入 | `app/services/text2sql.py:20-36, 283-326` | 成功候选为 `generated`/`scope_verified`；Scope 失败保存 `diagnostic_only` 和原始诊断 |
| Scope 分类 | `app/services/query_scope.py:10-24, 78-206, 257-280` | 可证明冲突为 `SCOPE_VIOLATION`，无法证明仍为 `UNVERIFIED_SCOPE` |
| Query Checker / Executor 状态 | `app/tools/dispatcher.py:132-151, 245-270` | 成功检查写 `checked`，只读执行写 `executed`，并保留 SQL、参数、Scope provenance |
| 定向恢复及预算 | `app/agents/runtime.py:307-430` | 以候选 SQL、参数、QueryScope、Population、失败原因形成稳定上下文键；相同上下文最多一次修复跳转 |
| 失败遥测 | `app/agents/runtime.py:687-704` | 写入 `failure_code/failed_stage/recoverable/recovery_action/retry_budget`，不删除审计事件 |

SQLCandidate 状态协议为：

`generated → scope_verified → checked → executed`

任一失败生成 `diagnostic_only`，保留候选 SQL、绑定参数、Scope、验证状态、错误类型和修复指令；修改 SQL、参数、数据源、Population 或 QueryScope 后不能复用旧验证结论。

## RecoveryPolicy 验收

- `query_checker` 不是 SQL 作者。失败 Text2SQL 结果的 `sql_candidate` 不进入 `trusted_sql_candidates`，也不会出现在 LLM 可执行工具集合中。
- Runtime `_arguments` 对 Query Checker 和 Executor 使用同一份 provenance 判定；新 SQL、错误参数或错误 dataset version 直接拒绝。
- `UNVERIFIED_SCOPE` 仅允许定向 Text2SQL 修复；同一上下文第二次失败直接 `FINISH`，不重建相同 Plan，不制造新的 LLM 决策。
- 上下文键不包含自然语言步骤文案，因此仅改写文案不重置预算；不同 Population、不同 SQL 结构或不同参数会获得独立预算。
- `SCOPE_VIOLATION` 保持拒绝，不能通过放宽范围验证转换为成功；复杂 JOIN 无法证明时仍安全拒绝。
- 失败遥测按错误类型写出修复动作和预算：`UNVERIFIED_SCOPE → targeted_sql_repair`，`SCOPE_VIOLATION → reject_scope_violation`，`INVALID_ARGUMENT → repair_arguments_or_stop`，存储损坏 → `stop_storage_retry`。

## 数据库关系与 Scope 反事实

只读 PostgreSQL 核验确认：

```text
predictions.model_run_id
  → model_runs.id
  → model_runs.experiment_id
  → experiments.dataset_version_id
  → dataset_versions.id

training_memberships.dataset_version_id → dataset_versions.id
```

真实数据摘要：

| 版本 | 训练分子 | Model Runs | Predictions |
|---|---:|---:|---:|
| `train_v2` | 3 | 0 | 0 |
| `train_v3` | 7 | 2 | 42 |

离线验证覆盖：

- 原始预测分支缺版本证明的复杂/外连接继续抛 `UNVERIFIED_SCOPE`。
- 通过 `predictions → model_runs → experiments → dataset_versions` 绑定版本的查询可验证。
- `training_memberships` 覆盖查询可以作为独立 SQL 验证；不把 train_v3 predictions 归给 train_v2。
- 空结果是“查询成功但无数据”，不是执行异常；不存在 fused_ring 行不能推出全库没有 fused_ring。

## A/B/C 历史只读回放

### A — `650ffd90-e72a-4ad0-bb7d-69a88fb80c67`

真实时间 `19:35:48–19:36:56`，持久化状态为 `failed`；最终 `GoalCoverage=PARTIAL`、`QualityStatus=INSUFFICIENT_EVIDENCE`，缺失维度为空但 `missing_deliverables=["executed_database_analysis"]`。这解释了“回答已有但侧边栏 failed”：质量门仍把数据库交付义务视为未完成，不能因为文件答案正确而强制改成 completed。

真实工具序列：

`profile_dataset ×2 → group_metrics → compare_models → find_high_error_samples → compare_structure_groups(fail) ×2 → compare_structure_groups(success, input_refs.rows)`

共 8 次 ToolCall，2 次无效调用；文件证据和最后一次成功结果都保留。P0 离线反事实验证了文件-only GoalContract 不会因发现 `training_db` 自动增加数据库义务，历史记录未改写。

### B — `32694656-4c73-4d7f-bc7c-c30874de7f83`

持久化状态 `completed`，`GoalCoverage=SATISFIED`，`QualityStatus=SUPPORTED_CONCLUSION`。真实链路严格为：

`search_schema → text_to_sql → query_checker → execute_readonly_sql`

四个工具各执行一次、无失败、无 Replan，结果为 `linear=3, cyclic=2, aromatic=1, fused_ring=1`，总计 7。P0 测试保证可信候选仍能进入 Query Checker，不增加额外 SQL 校验或重复执行。

### C — `8808d8f1-ee34-443c-a56e-1e2c7eff8bf4`

持久化状态 `failed`，`GoalCoverage=UNSATISFIED`，`QualityStatus=EXECUTION_FAILED`，保留了失败原因和无数据库 Evidence 的阻塞记录。

真实工具序列：

`search_schema(success) → text_to_sql(UNVERIFIED_SCOPE) → query_checker(INVALID_ARGUMENT: no SQLCandidate lineage) → text_to_sql(UNVERIFIED_SCOPE)`

共 4 次 ToolCall；失败/无效 3 次，其中 Query Checker 无 lineage 1 次、Text2SQL Scope 失败 2 次；历史有 2 次 `REPLAN` 行为和 1 次 `NO_PROGRESS_REPLAN`，`AGENT_DECISION=7`（历史均为真实模型决策事件）。P0 改动不重写该历史，而是让相同输入在离线回放中进入“诊断候选 → 有界定向修复 → 成功继续或安全停止”。

## 反事实测试与测试结果

新增 `tests/test_c_sql_candidate_recovery.py` 14 项，分别防止：

- 诊断候选解锁 Query Checker；
- Scope 真实违规与无法证明混淆；
- 重复同一候选无限修复；
- 不同 Population、独立 SQL 结构、先成功后独立失败被错误共用预算；
- 仅改写自然语言重置预算；
- Query Checker/Executor 丢失 `checked`/`executed` 状态；
- A/B GoalCoverage 退化及 C 的两条独立可验证 SQL。

结果：

- P0/SQL/Runtime/A/B 回归集合：`210 passed`；
- 前端任务状态测试：`18 passed`（`npm test --prefix web`）；
- Python `compileall`：通过；
- `git diff --check`：通过，仅有既有 CRLF 转换提示，无 whitespace 错误；
- 全部测试使用内存状态、fixture schema 或 fake database；未调用 Qwen/OpenAI，未启动新的真实 Agent 任务。

## 成本与审计事件

| 口径 | ToolCall | 无效/失败 | Query Checker 无 lineage | Replan | NO_PROGRESS_REPLAN | LLM 决策 | 确定性恢复 |
|---|---:|---:|---:|---:|---:|---:|---:|
| A 历史真实 | 8 | 2 | 0 | 0 | 0 | 10 个 AGENT_DECISION | 0 |
| B 历史真实 | 4 | 0 | 0 | 0 | 0 | 5 个 AGENT_DECISION | 0 |
| C 历史真实 | 4 | 3 | 1 | 2 | 1 | 7 个 AGENT_DECISION | 0 |
| C 离线定向恢复测试 | 未执行真实 ToolCall | 仅构造失败 Observation | 0 | 0 | 0 | 修复跳转不调用 LLM | 1 次/上下文，随后停止 |

历史事件完整保留；离线计数不冒充线上性能。未来真实 Qwen 运行的预期是少一次无 lineage Query Checker、少一次重复 Plan/Replan，并以确定性恢复替代该跳转，但本轮没有进行该真实运行。

## 仍需关注的风险

- P1：Scope 验证器对未实现关系证明的复杂 SQL 仍会保守拒绝，需在有正式关系约束证明后单独扩展；本轮没有放宽安全边界。
- P1：本轮未进行真实 Qwen 线上回放，因此模型输出分布、延迟和生产 Token 成本仍需后续受控观测。
- 历史 A 仍按原始持久化状态显示 failed/partial；这是只读基准，不是本轮要修写历史状态。
- 前端没有删除详细审计事件；当前 P0 只验证任务状态映射，不包含阶段化轨迹 UI 改造。

## 变更与 Git 状态

本轮 P0 文件：

```text
app/agents/decision_node.py
app/agents/runtime.py
app/services/query_scope.py
app/services/text2sql.py
app/tools/dispatcher.py
app/services/sql_candidate.py
tests/test_c_sql_candidate_recovery.py
docs/audit/p0-sql-recovery-final-validation.md
```

用户既有未跟踪的 ZIP、缓存、`evaluation/runs/`、`reports/` 和其他报告未清理、未覆盖、未暂存。未推送 GitHub，未合并 main。最终本地提交 SHA 以交付消息中的 `git rev-parse HEAD` 为准。
