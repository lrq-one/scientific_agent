# P1.3 RecoveryPolicy 统一协议

## 目的

运行时现在为失败生成一个结构化、可持久化的 `RecoveryPolicy`，并保留已有
`SQLCandidate` lineage、`diagnostic_only`、scope 安全拒绝、定向 SQL 修复、按
Candidate/Population/Scope 的预算和 PostgreSQL 存储损坏停止策略。没有新增第二套
SQLCandidate 或 Planner。

## 失败到动作映射

| failure_code | recovery_action | 默认预算 |
| --- | --- | --- |
| `INVALID_ARGUMENT` / `MISSING_INPUT` | `repair_arguments` | 1 |
| `SCHEMA_MISMATCH` | `retrieve_schema` | 1 |
| `SQL_CANDIDATE_LINEAGE` | `establish_sql_candidate` | 1 |
| `UNVERIFIED_SCOPE` | `targeted_sql_repair` | 1 |
| `SCOPE_VIOLATION` | `safe_reject` | 0 |
| `TIMEOUT` / `RATE_LIMIT` / `TRANSIENT_IO` | `bounded_retry` | 1 |
| `PLAN_DEPENDENCY` | `repair_plan_dependency` | 1 |
| `NO_PROGRESS_REPLAN` | `no_progress_stop` | 0 |
| `DATABASE_STORAGE_CORRUPTION` | `stop` | 0 |
| `USER_INPUT_REQUIRED` / `RESOURCE_NOT_FOUND` | `ask_user` | 0 |

每个策略都记录 `failure_code`、`failed_stage`、`recoverable`、动作、预算、当前
上下文、上一次尝试和 `strategy_changed`。即使上游错误地标记 scope violation 或
storage corruption 为可恢复，策略也强制预算为 0，保留安全拒绝。

## Runtime 接入

Tool failure 在 `TOOL_FINISHED` 的结果 metadata 写入策略；Observation 阶段把同一
策略追加到 `ScientificAgentState.recovery_history`，不改写旧 ToolCall 或删除已
保存 Evidence。P0 的 `UNVERIFIED_SCOPE` 仍由 `_directed_scope_recovery` 只做一次
同候选/Population/QueryScope 的定向 Text2SQL 修复；`SCOPE_VIOLATION` 不进入该路径。

## 验证

`tests/test_p1_recovery_policy.py` 覆盖上述矩阵、不可恢复覆盖保护和状态/PlanStep
持久化。P0 的 SQLCandidate、存储损坏和闭环回归共 66 个通过。没有 Qwen 调用、没有
数据库/MinIO 写入、没有历史任务或冻结基准修改。
