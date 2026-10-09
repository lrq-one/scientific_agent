# Scientific Agent 验证报告

验证日期：2026-10-09

有效验证配置：显式清空 `LLM_API_KEY`、`OPENAI_API_KEY`、`DASHSCOPE_API_KEY` 与 `DATABASE_URL`；使用固定回放、Fake Decision 和纯函数合约，不调用真实模型。

## 结果

| 层级 | 结果 | 说明 |
|---|---:|---|
| 根因定向回归 | 72 passed | 最新文件失败形态、真实 mixed 反事实、schema prerequisite、Plan completion、历史隔离 |
| 相关离线矩阵（首次） | 219 passed / 2 failed | 两个失败是同一条过时测试：仍要求 schema Plan 直接拒绝 |
| 相关离线矩阵（最终） | 221 passed | 更新后的 schema 恢复契约与全部相关回归，52.03 秒 |
| 前端 Node 合约 | 18 passed | FINAL_ANSWER/quality/coverage、历史加载、精确重试 |
| 静态/构建 | passed | Python `compileall`、`git diff --check`、Vite production build |
| 全仓库探索性 pytest | 359 passed / 20 failed | 混合了旧执行器预期、固定 checkpoint、实时依赖和旧工具数量断言；不作为本轮合格门禁 |

过时测试已更新为更严格的新契约，并在 72 项定向回归和最终 221 项相关矩阵中通过。

## 正例、负例和反事实

- 正例：文件-only 任务即使 ResourceBinding 发现 `training_db`，只要真实文件 Evidence 覆盖 `structure_type`，Goal Coverage 为 SATISFIED。
- 负例：真实 mixed Plan 缺少 SQL 执行时，仍报告 `executed_database_analysis`，状态 PARTIAL。
- 反事实：同一文件任务去掉文件 Evidence 会重新缺少 `file_analysis`；没有通过改状态映射绕过 Evidence。
- Schema 正例：Runtime 添加无依赖、受权、可调用的 schema producer。
- Schema 反事实：schema 到达前 `text_to_sql` 仍不在 callable tools。
- 历史隔离：旧 `DataCorrupted` 不进入新 rerun 的无 Evidence 最终原因。

## 最新持久化任务审计

| task_id | 真实情况 | 判定 |
|---|---|---|
| `1d2df08e-491a-40ea-9404-83f681cf03ef` | 6/6 工具成功，6 Evidence，文件目标和数值完整；被伪 DB 交付拖成 PARTIAL | 确认误判，已修根因 |
| `49395a94-9bc8-48be-9940-63ef37bfb82e` | 文件 Evidence 存在，但训练覆盖 SQL 缺 lineage/未执行 | 正确的部分完成，不应强改成功 |
| `f82ab3e8-b000-4644-99d3-71318b0bdb4a` | 只读历史证据说明，`PERSISTED_STATE_REUSE`，无新执行 | 正确的复用回答 |

## 成本与限制

- 权威离线验收的真实 LLM 调用：0；估算模型 token：0。
- 未执行新的真实 Qwen/UI 科研任务，未修改历史记录，因此不能宣称真实模型路由准确率已经验收。
- 全仓库探索性运行是在发现测试分层问题前启动的，不纳入“零模型”验收数字；正式结论仅引用显式清空凭据后的离线矩阵。
- 没有删除、重建或覆盖 PostgreSQL/MinIO；数据库操作仅为 SELECT 审计。
