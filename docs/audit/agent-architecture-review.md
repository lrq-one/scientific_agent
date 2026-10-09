# Scientific Agent 架构评审

## 当前评价

技术方向成立：LangGraph 适合管理可中断的多步 Agent，Tool Registry、Pydantic state、PostgreSQL 事件与 Evidence 持久化也具备正确基础。主要问题不是技术栈，而是“资源、计划、步骤、目标、回答和任务状态”曾被局部规则交叉推导，导致一个层级的事实被误当成另一个层级的义务。

当前生产链已经基本收敛到 `DecisionRuntime`，但仓库结构尚未跟上：旧执行器仍留在 `scientific_agent.py`，旧 canonical planner 仍留在 `planning_graph.py`，测试也同时覆盖新旧契约。

## 状态语义

以下状态必须独立：

| 层级 | 权威事实 | 不代表 |
|---|---|---|
| ToolResult | 一次工具调用是否成功、返回什么 | PlanStep 或用户目标已完成 |
| PlanStep | 已安装步骤的完成谓词是否由真实 Observation 满足 | 整个科研目标已完成 |
| GoalCoverage | 原始目标的维度、population、交付物是否有证据覆盖 | 最终回答已经生成 |
| QualityStatus | Evidence 是否足以支持结论 | 工作流是否还有节点 |
| FINAL_ANSWER | 本轮执行流终止并产生用户可见答复 | 科研任务必然成功 |
| tasks.status | 产品层 completed/failed/waiting | 单独的模型主观判断 |

本轮失败正是把 ResourceBinding（有哪些资源）越级当成 Goal Contract（必须做哪些分析）。

## 建议目标架构

```text
OriginalRequest
  -> GoalContract(required dimensions, populations, deliverables)
  -> ResourceBinding(only identities/authorisation)
  -> SkillGuidance(method constraints only)
  -> Runtime-owned PlanContract
       allowed_tools != completion_predicate
  -> ToolResult / Observation ledger
  -> Evidence ledger (strict tool_call lineage)
  -> GoalCoverage(only GoalContract + verified ledgers)
  -> QualityStatus
  -> GroundedResponse
  -> Product task status
```

### Runtime

Runtime 应继续作为唯一 Plan 安装、状态变更、预算和恢复权威。LLM 只提出 Decision/Plan，不直接写入完成状态。已存在的原子 Plan replacement、schema prerequisite 编译、no-progress signature 和 Evidence gate 应保留。

### Plan 协议

下一步应把以下字段分开：

- `allowed_tools`：该步骤授权调用的工具集合。
- `completion_predicate`：ALL、ANY、工具序列、Artifact 或 scope-verified rows 等确定性条件。
- `recovery_policy`：参数修正、补 schema、瞬时重试、replan、终止。

默认把所有授权工具当成必需工具不适合带备用路径的步骤。完成谓词不能由 Plan prose 或关键词反推，应是受验证的结构化协议。

### Goal Contract

Goal Contract 应在第一次 Decision 后冻结，并持久化到 task intent/state。后续局部目标、Skill、Plan 或资源发现只能补充证据，不能删除或新增用户未要求的交付项。当前 `requested_dimensions`、`required_deliverables` 是初版实现；Population 和一般交付物还应统一进入同一模型。

### Skill

Skill 只提供方法、工具约束和领域质量标准，不控制 LangGraph 跳转，不定义产品任务状态。当前 Skill 选择结果进入 Tool Registry 过滤是合理的；不得再引入并行 Planner。

### Legacy 退出

1. 先用静态引用和覆盖报告列出 `scientific_agent.py` 中仍被 Runtime/测试调用的辅助函数。
2. 将纯函数迁到明确模块（evidence quality、recording、legacy adapters）。
3. 给旧执行入口加弃用标记并禁止新引用。
4. 在回放矩阵覆盖等价后分批删除，而不是本轮一次性重写。

## 恢复决策

| 失败类别 | 处理 |
|---|---|
| 参数/lineage/scope 可修正 | 保留 Plan 与 Evidence，修正当前工具调用 |
| 缺少 schema | Runtime 编译受权 schema 前置，不消耗整图 replan |
| 瞬时 I/O | 同工具有界重试 |
| Plan 依赖/能力冲突 | 原子拒绝 replacement，保留旧 Plan |
| 同执行路径无进展 | action signature 去重并有界终止 |
| PostgreSQL storage corruption | 不重试、不 replan，过程性失败终止 |
| 目标证据不足 | 保留部分 Evidence，`PARTIAL/INSUFFICIENT_EVIDENCE` |

该设计保留现有 LangGraph、Schema、Tool Registry 与数据库设施，不增加第二套 Planner。
