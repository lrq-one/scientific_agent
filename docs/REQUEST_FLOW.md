# Request Flow

1. FastAPI 校验 `X-User-Id`。
2. ResourceService 获取 workspace/MinIO files、authorized datasource、configured model 和 MCP tools。
3. Deterministic router 判断简单确定任务；复杂或模糊任务尝试 LLM structured RequestIntent。
4. 对 LLM 结果再次按真实资源过滤 capability。
5. complex task 创建 LangGraph Plan，并执行真实 DeepAgents runtime scaffold，加载 selected skills、workspace backend、tools 和 checkpointer。
6. 每个 PlanStep 动态选择 file/database/MCP scope，并通过 SSE 发出结构化 trace。
7. ToolResult 进入 Observation；确定性数据构造 Evidence。
8. Finalizer 分开输出 Observed Evidence、Interpretation 与 Uncertainty。

## Text-to-SQL

`current goal/step → PostgreSQL schema → relationships → BM25 relevant tables → structured SQLCandidate(sql, params, reason) → SQLGlot guard → EXPLAIN checker → read-only execute → normalized rows → evidence`

真实 LLM 未配置时使用明确标记的 synthetic fixture fallback SQL；它走相同 guard/checker/executor，不能称为真实 LLM Text-to-SQL。

## File flow

`Browser → FastAPI → MinIO object + PostgreSQL file_metadata → Resource discovery → lazy download → workspace/{user}/{thread} → File tool`

## HITL

数据库 coverage-only 请求缺少 `dataset_version` 时，LangGraph node 调用 `interrupt()`，SSE 返回 `WAITING_FOR_USER`。`POST /api/agent/resume` 使用同一 thread ID 和 `Command(resume=...)` 从 PostgreSQL checkpoint 恢复；中断发生在数据库 tool execution 前，不重复已完成的数据库步骤。

## Mixed flow

`intent → selected skills → LangGraph plan → DeepAgents scaffold → file metrics → subgroup → MCP → evidence → schema/relationship → BM25 → Text-to-SQL → SQL guard/checker → PostgreSQL → database evidence → final answer`

