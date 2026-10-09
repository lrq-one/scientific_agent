# P1 最终离线验收（当前分支）

## 分阶段提交

| 阶段 | 提交 | 范围 |
| --- | --- | --- |
| P1.1 | `66909da`, `9b30078` | GoalContract、目标边界和合同覆盖率（含命名数据源修正） |
| P1.2 | `3029d6f`, `c34a6af` | PlanStep allowed/completion/input/optional 协议（含 ALL/ANY 谓词） |
| P1.3 | `f68cfa9` | RecoveryPolicy 统一错误动作和预算 |
| P1.4 | `2b917c8`, `30b61e4` | 受限确定性执行与审计 decision source |
| P1.5 | 本提交 | 离线验收矩阵、回放和交付记录 |

## 离线矩阵

`tests/test_p1_acceptance_matrix.py` 固定回放 A/B/C 约束，并加载既有 D06、D09、M02、
D08 closure trace；另有独立的 HITL 合同版本、RERUN 防历史污染、两 Population
scope 和 SQLCandidate recovery 测试。当前新增 P1 测试结果：

- GoalContract：7 passed；
- PlanStep：12 passed；
- RecoveryPolicy：12 passed；
- deterministic executor：4 passed；
- acceptance matrix：7 passed；
- P0 SQL/Scope/plan/database regression selected set：151 passed；
- 前端冻结基准：Node tests 18 passed，Vite build 成功。

全仓在清空 `ADMIN_DATABASE_URL`、`DATABASE_URL`、LLM 凭据后运行：`408 passed`，另有
18 个失败，均未被隐藏或删除。失败分类如下：

- 需要 LLM Skill/真实运行时的旧端到端测试在凭据清空后留下 pending checkpoint，或
  按既有策略报告 “workflow fallback disabled”；
- follow-up/产品历史测试依赖 PostgreSQL 配置；
- `test_product_expansion` 的固定工具数断言仍期待旧的 23 个工具，而当前注册表是
  25 个；
- 一个 MCP deep-runtime 旧测试引用当前 `ScientificAgent` 不再提供的属性。

这些是环境/旧测试契约问题，不是本轮 P1 协议测试失败；本轮没有通过放宽断言、删除
测试或调用真实模型来制造全绿。应在需要完整端到端验收时单独提供受控测试配置并
维护上述旧契约。

## A/B/C 与真实性结论

- A：文件 Evidence 仍可满足文件目标；GoalContract 阻断了 Plan/资源引入的数据库
  幻影义务。失败 ToolCall 和恢复轨迹保留。
- B：四次真实工具调用顺序和结构覆盖证据保留；确定性执行只在候选唯一且参数可信
  时减少 LLM Decision，不跳过 Checker/execute 审计事件。
- C：失败 Text2SQL 候选仍为 `diagnostic_only`；无可信候选时 Query Checker 不在
  可执行集合；`UNVERIFIED_SCOPE` 只允许有界定向修复，`SCOPE_VIOLATION` 安全拒绝；
  预测误差与训练覆盖可拆成独立 QueryScope 查询，复杂 JOIN 无法证明时不放宽验证。

## 安全与交付边界

所有阶段均只在本地分支提交；没有调用真实 Qwen/OpenAI、没有写 PostgreSQL/MinIO、
没有覆盖历史 Task/Event/Evidence、没有修改冻结基准、没有删除用户预存未跟踪文件、
没有推送或合并 `main`。详细开发者 Decision/ToolCall/Observation/Recovery 仍保留，
UI 只可在展示层分阶段折叠，不能以删除审计事件减少数量。

## 受控收尾状态

P1 代码阶段的核心离线协议已完成并分阶段提交；完整端到端生产验收仍受上述真实
Skill/PostgreSQL 配置和旧测试契约限制，不能据此宣称真实 Qwen 端到端已验收。下一步
应由维护者决定是否提供受控集成环境并单独修复旧测试契约，不应在本分支擅自扩大范围。
