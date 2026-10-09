# Scientific Agent 整改计划

## P0（本轮已完成/验证）

### P0-1 文件任务伪数据库交付

- 根因：ResourceBinding 的资源共存被错误提升为 Goal requirement。
- 正确行为：要求只来自原始 Goal Contract 和已安装 Plan。
- 修改范围：`goal_coverage.py` 一处规则；两条反事实测试。
- 影响：文件-only 任务不再因可见数据库失败；真实 mixed Plan 仍要求 SQL。
- 验证：最新失败形态的离线回放为 SATISFIED；移除真实 DB 结果的 mixed 反事实仍为 PARTIAL。

### P0-2 Schema 前置恢复契约

- 根因：历史 Plan 把 `text_to_sql` 错当 schema producer，形成无可调用入口并重复 PLAN_REJECTED。
- 当前行为：Runtime 编译受权 schema producer，并把 SQL 生成步骤依赖到该节点。
- 本轮工作：修正已过时测试，使其验证“真实可调用入口 + 依赖 + schema 前禁止 text_to_sql”，而不是要求回到旧的直接拒绝行为。

### P0-3 成功/失败映射

- 结论：后端与前端映射一致，无需放宽。
- 保留：`FINAL_ANSWER + PARTIAL/INSUFFICIENT_EVIDENCE => failed`。
- 修复点：Goal Coverage 根因，而不是显示层。

### P0-4 历史隔离

- NEW/RERUN 不注入旧 provenance；历史错误问答只读历史过程记录；当前失败只由当前 control observations 解释。
- 相关隔离测试通过，无生产改动。

## P1（下一批，不能用关键词补丁替代）

1. 为 PlanStep 增加显式的授权工具与完成谓词分离模型；迁移后再解决“备用 MCP 未执行导致 step blocked”。
2. 将 Goal Contract 完整持久化，覆盖普通文件、数据库、mixed、Artifact、Population 和不可验证目标。
3. 建立 failure taxonomy 到 recovery action 的数据表/枚举，并记录每类重试预算。
4. 为 D06/D09/M02/D08 保留去模型确定性回放，同时增加“改变工具结果/删除 Evidence 后结论必须变化”的反事实。
5. 将固定 thread id 的测试改为每次唯一 id 或隔离 checkpoint namespace，避免上次测试中断污染下一次全量运行。

## P2（结构清理）

1. 逐步拆分 `scientific_agent.py` 的纯辅助函数，删除确认无引用的旧执行入口。
2. 将旧 canonical planning graph 标记为 compatibility-only，禁止产品入口引用。
3. 拆分测试层级：纯单元、回放、数据库合约、无模型 E2E、真实模型人工验收。
4. 为每次发布记录 runtime protocol version、加载 commit、测试 profile 和模型调用预算。

## 人工验收清单

真实模型验收必须获得用户许可后单独运行：

- 文件比较任务应返回 0.425 / 0.725、fused_ring 0.75 / 2.40、M004/M006，状态 completed。
- 文件任务不得调用数据库工具。
- mixed 训练覆盖任务缺 SQL Evidence 时必须失败或部分完成。
- D06 必须同时交付各 split 与总数。
- D09 必须保持两个独立 Population，不能用一个全局 QueryScope 替代。
- RERUN 必须产生新 tool_call_id，不得复用旧 DataCorrupted 解释。
- 移除关键 Evidence 后，状态必须从 SATISFIED 改变。
- 记录 Plan reject、无进展 replan、模型调用数、token 和延迟。
