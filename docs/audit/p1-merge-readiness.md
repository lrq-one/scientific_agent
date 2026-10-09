# P1 Merge Readiness

## 当前 Git 状态

- 分支：`codex/agent-protocol-audit-20261009`
- 审计基线 HEAD：`fa5586baeb7354004f689b71218de7a20be63032`
- 当前本地提交：`48a52ebfe008bffac9b507f84b14ace69cf2bfe1`（仅本地，未推送、未合并 `main`）。
- 原有未跟踪文件（压缩包、`__pycache__`、`evaluation/runs`、`reports` 及用户文档）未删除、未暂存、未覆盖。

本轮工作树修改仅限：`app/agents/runtime.py` 的确定性 Plan 边界、`tests/test_product_expansion.py` 的过时 23 工具数量契约、`tests/test_p1_langgraph_no_model_e2e.py` 的新隔离验收，以及四份审计文档。上述修改已记录在当前本地提交；未触碰原有未跟踪文件。

## 验收结果

- P1/P0 专项回归：`84 passed / 9 skipped`（显式隔离外部服务的当前聚焦集合）。
- LangGraph 无模型 E2E：`5 passed`（A/B/C/D06/RERUN/HITL）。
- Python `compileall`、`git diff --check`：通过。
- 受控全仓离线测试：`392 passed / 10 failed / 32 skipped`。失败逐项见 `p1-full-suite-failures.md`：9 条是未注入真实 Skill/Decision provider 的旧测试契约，1 条是直接调用 service 层却期待 HTTP SSE 的层级契约；数据库/持久化集成测试被显式隔离 profile 跳过。
- 原始 `408/18` 不能作为安全基线：它隐式恢复 `.env` checkpoint，并让部分测试连接默认 PostgreSQL；该事实已记录，没有把环境问题改写成生产成功。

## P0 安全边界

SQLCandidate lineage、`diagnostic_only`、Query Checker 前置条件、`SCOPE_VIOLATION` safe rejection、`UNVERIFIED_SCOPE` 有界 repair、scope fingerprint/no-progress budget、数据库存储损坏停止策略均保留并通过专项回归。没有放宽 SQL Scope 验证，没有硬编码历史 train_v2/train_v3 结果，没有删除失败审计事件。

## 结论

状态：**P1 已通过，带已知限制**。核心协议、真实 LangGraph 无模型链路和本轮生产 Bug 已验收；不能宣称真实 Qwen 端到端或 PostgreSQL/MinIO 集成已完成。进入真实人工验收前的必要条件是：在专用只读 PostgreSQL/MinIO profile 运行集成测试，并为旧 direct-Agent 测试注入固定 provider 或迁移到新的生产图契约；不得通过恢复 heuristic fallback、删除断言或连接 `.env` 默认服务来“通过”。
