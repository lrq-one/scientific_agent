# Scientific Research Analysis Agent

科研任务智能分析 Agent。用户只描述科研目标，系统发现真实资源后自动路由 file、database、scientific model 与 MCP capability；不存在 `qa_type` 或“表格/数据库问答”模式选择。

> Demo CSV、Demo PostgreSQL 和 Demo MCP molecule data 全部是 synthetic fixtures，仅用于验证执行链路，不代表真实科研发现。

## 已实现的运行链

- Deterministic-first router；复杂/模糊任务在配置 API 后使用 LLM structured `RequestIntent`，并再次进行资源过滤。
- DeepAgents 0.5.1 `create_deep_agent` 高层 runtime，实际加载 selected skills、FilesystemBackend、tools 和 LangGraph checkpointer。
- LangGraph planning、PostgreSQL checkpoint、`interrupt()` 和 `Command(resume=...)`。
- pandas CSV/Excel 指标工具、MinIO 原始文件、PostgreSQL metadata、workspace lazy materialization。
- PostgreSQL schema/relationship inspection、BM25 relevant-table retrieval、structured Text-to-SQL、SQLGlot guard、query checker、read-only execute。
- 独立 MCP stdio server/client：`get_molecule_features`。
- Vue Agent Trace：Intent、Plan、Tool Call、Evidence、HITL 与 Final Answer。
- PostgreSQL 持久会话：`/c/{conversation_id}`、消息恢复、任务级 trace、重命名与软删除。
- 9 个科研 Skill 契约与 23 个带 schema/权限/风险元数据的 Tool Registry。
- 数据集/模型/实验/预测关系表、可配置 BM25 schema retrieval，以及 MinIO PNG/CSV/XLSX 结果产物。
- `evaluation/` 中包含 120/150/150/100/100 条评测数据与 validator/metric functions；本阶段未运行正式 LLM benchmark。

## 启动

```powershell
cd D:\工作\scientific_agent
docker compose up -d

$env:DATABASE_URL='postgresql://agent_reader:reader_demo@127.0.0.1:55432/scientific_agent'
$env:ADMIN_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/scientific_agent'
$env:CHECKPOINT_DATABASE_URL='postgresql://scientific:scientific@127.0.0.1:55432/scientific_agent'
$env:MINIO_ENDPOINT='127.0.0.1:9000'
$env:MINIO_ACCESS_KEY='minioadmin'
$env:MINIO_SECRET_KEY='change-me'
$env:MINIO_BUCKET='scientific-files'

.\.venv\Scripts\python -m uvicorn app.main:app --port 8000
```

另开终端：

```powershell
cd D:\工作\scientific_agent\web
npm run dev
```

- Web：http://127.0.0.1:5173
- API：http://127.0.0.1:8000
- OpenAPI：http://127.0.0.1:8000/docs
- MinIO Console：http://127.0.0.1:9001

宿主机已有 PostgreSQL 使用 5432，因此本项目 PostgreSQL 映射为 `55432`。MinIO 镜像固定为 `bitnamilegacy/minio:2025.7.23-debian-12-r3`，避免依赖不可拉取的 `latest`。

## 可选真实 LLM

```powershell
$env:OPENAI_API_BASE='https://your-compatible-endpoint/v1'
$env:OPENAI_API_KEY='...'
$env:LLM_MODEL_NAME='your-model'
```

配置后 Request Router 和 Text-to-SQL 使用 `with_structured_output(...)`。未配置或调用失败时自动切换 deterministic fallback；fallback 会在 trace metadata 中明确标识，不能称为真实模型验证。

## 验证

```powershell
.\.venv\Scripts\python -m compileall app
.\.venv\Scripts\python -m pytest -q
cd web
npm run build
```

当前自动化结果以本地 `pytest` 输出为准；同时执行 PostgreSQL/MinIO 集成测试与前端 production build。

## 安全边界

- API 缺少 `X-User-Id` 返回 401；datasource 未授权产生 403 语义错误事件。
- SQL 只允许单条 `SELECT`/`WITH SELECT`，并执行 table allowlist、query checker、timeout 与 max rows。
- PostgreSQL `agent_reader` 默认事务 read-only；即使绕过应用 guard，DML 仍由数据库拒绝。
- object key 包含 owner/thread/file UUID；metadata 查询同时约束 owner 与 thread。
- DeepAgents filesystem backend 仅看到隔离 workspace，skills 被复制到 workspace 的虚拟 `/.skills/`。
- `MODEL_PATH` 缺失时 `predict_rt` 返回 `capability_unavailable`。

详细状态见 [IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md)，执行流程见 [REQUEST_FLOW.md](docs/REQUEST_FLOW.md)。

## 上游说明

设计参考只读目录 `Aix-DB-master` 的 Excel、Text2SQL、DeepAgents、SSE、Skill loader 与 HITL 结构。上游 README 声明 Apache-2.0，但本地快照缺少根 `LICENSE` 文件，因此本项目没有批量复制源码，而是重写实现，并在 [AIX_DB_MIGRATION_MAP.md](docs/AIX_DB_MIGRATION_MAP.md) 记录映射。

# scientific_agent

## Current engineering delivery

The reproducible benchmark and metric definitions live in
[`evaluation/benchmark/`](evaluation/benchmark/) and
[`docs/evaluation/optimization-benchmark.md`](docs/evaluation/optimization-benchmark.md).
The benchmark replays committed A/B/C/D06/D09/M02/D08/HITL/RERUN/CANCEL
acceptance evidence; missing token or latency data is reported as
`not_measured`. `live_llm_manual` is opt-in and this branch has made no real
Qwen/OpenAI request.

Recent safety/efficiency work includes node-specific read-only context
projection, versioned prompt contracts, Skill metadata caching, SQLCandidate
lineage preservation, and a five-stage UI projection with an expandable raw
developer trace. See [`docs/architecture/scientific-agent-architecture.md`](docs/architecture/scientific-agent-architecture.md)
and [`docs/interview/scientific-agent-interview-guide.md`](docs/interview/scientific-agent-interview-guide.md).
