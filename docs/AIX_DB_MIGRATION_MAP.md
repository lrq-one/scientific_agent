# Aix-DB Migration Map

本地上游快照的 README 声明 Apache-2.0，但根 `LICENSE`/`NOTICE` 缺失。本项目采用“检查设计后重写”，没有原样复制整套 Agent 或 Web。

| 源文件/模块 | 新文件 | 复用的设计 | 改写内容与原因 |
|---|---|---|---|
| `agent/excel/excel_agent.py` | `app/agents/scientific_agent.py` | SSE 生命周期、表格分析阶段概念 | 删除用户级 ExcelAgent 与 `FILEDATA_QA`，文件能力成为统一 Agent 的步骤工具 |
| `agent/excel/excel_graph.py` | `app/agents/scientific_agent.py` | parsing → SQL/metrics → result 的图式分段 | 改为 simple/complex 计划步骤，可在同一任务转入 DB branch |
| `agent/excel/excel_agent_state.py` | `app/models/schemas.py` | typed state/result | 扩展为跨资源 `ScientificAgentState`、Observation、Evidence、PlanStep |
| `agent/excel/excel_duckdb_manager.py` | `app/tools/file_tools.py` | 多表文件分析与确定性聚合思路 | 第一版用 pandas 实现 CSV/Excel 工具；DuckDB 多文件 SQL 是后续优化而非伪称完成 |
| `agent/excel/excel_mapping_node.py` | `app/tools/file_tools.py` | 字段 inspection | 移除 chat 级 manager，全路径由 request workspace 显式解析 |
| `agent/excel/excel_sql_node.py` / `excel_excute_sql.py` | `app/tools/file_tools.py` | 聚合指标能力 | 科研 demo 指标使用确定性 pandas，避免让 LLM 生成不必要 SQL |
| `agent/excel/excel_summarizer.py` | `ScientificAgent._finalize` | 结果摘要 | 强制拆分 Evidence / Interpretation / Uncertainty |
| `agent/excel/excel_chart_generator.py` | 未迁移 | artifact/chart 设计 | 当前版本没有 PNG chart，避免声称未完成能力 |
| `agent/text2sql/text2_sql_agent.py` | `app/tools/database_tools.py` | schema → SQL → execute → normalize | 删除独立用户级 Text2SqlAgent，变为 request-scoped Database Branch |
| `agent/text2sql/datasource/selector.py` | `app/services/resources.py` | datasource selection | 授权资源先发现，datasource 显式传递；无模块级 `_current_datasource` |
| `agent/text2sql/database/db_service.py` | `app/tools/database_tools.py` | schema/execution interface | Demo 用 SQLite read-only URI；保留 PostgreSQL 生产升级边界 |
| `agent/text2sql/sql/generator.py` | mixed task deterministic query | SQL generation stage | Demo 仅用固定可审计 coverage query；通用 LLM Text-to-SQL 未伪称完成 |
| `agent/text2sql/permission/*` | `app/tools/sql_guard.py` | SQL AST 与授权思想 | SQLGlot single statement、SELECT-only、table allowlist、max rows |
| `agent/deepagent/tools/schema_retriever.py` | `DatabaseService.schema` | 先取 schema 再查询 | request-scoped、同步 demo 实现 |
| `agent/deepagent/tools/native_sql_tools.py` | `DatabaseService.execute` | 原生 SQL 工具 | 每次执行重新 guard；只读 URI 与归一化 dict rows |
| `agent/common/enhanced_common_agent.py` | `app/agents/scientific_agent.py` | bounded loop、SSE phases | Sanic 改 FastAPI；生产 DeepAgents 循环尚未接入并明确列为限制 |
| `agent/common/tools/ask_user_tool.py` | `ScientificAgent.pending/resume` | interrupt/resume 交互 | 当前 demo 为 in-memory same-thread resume |
| `services/skill_service.py` | `app/services/skills.py` | SKILL.md、YAML frontmatter、dynamic load | 内容改为科研，第一版 metadata filter，不伪称 hybrid retrieval |
| `web/src/views/chat/index.vue` | `web/src/App.vue` | chat、SSE progress、upload、HITL 面板 | 全部移除 `COMMON_QA`/`DATABASE_QA`/`FILEDATA_QA`/`REPORT_QA` 与 mode picker |
| `web/src/views/chat/default-page.vue` | `web/src/App.vue` | welcome prompts | 改为三个 RT/coverage 科研建议问题 |
| `web/src/store/business/index.ts` | `web/src/App.vue` local state | SSE accumulated state | 删除 Pinia 中 `qa_type`；第一版缩小状态面以保证 demo 可运行 |
| `web/src/api/index.ts` | `web/src/api.js` | streaming request | `/sanic/...` 改为 `/api/...`，payload 不再发送 `qa_type` |

## Resume Alignment Phase

| Aix-DB source | New implementation | Alignment |
|---|---|---|
| `agent/common/enhanced_common_agent.py:create_deep_agent` | `app/agents/deep_runtime.py` | Actual DeepAgents 0.5.1 graph creation with model, tools, selected skills, FilesystemBackend and checkpoint saver |
| `agent/common/tools/ask_user_tool.py` | `app/services/checkpointing.py` | Actual LangGraph `interrupt()` and `Command(resume=...)`; PostgreSQL saver when configured |
| `agent/deepagent/tools/schema_retriever.py` | `app/services/text2sql.py:SchemaRetriever` | BM25 schema/table selection over current authorized datasource |
| `agent/deepagent/tools/native_sql_tools.py` | `app/datasources/postgres.py` | Request-scoped PostgreSQL schema/relationship/query adapters with read-only execution |
| `common/minio_util.py` concept | `app/services/object_storage.py` | MinIO objects plus PostgreSQL metadata and lazy isolated materialization |
| MCP adapters in upstream dependency set | `app/mcp_server.py`, `app/services/mcp_client.py` | Real local stdio MCP protocol, list-tools and call-tool |

