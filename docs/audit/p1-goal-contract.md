# P1.1 GoalContract 审计与实现

## 范围

本阶段只建立用户目标合同，不重构 Planner、Tool 执行器或 RecoveryPolicy。合同是
`ScientificAgentState` 的持久化字段，随 checkpoint 一起保存；它不是资源发现、Skill
选择、Plan、ToolCall 或 Observation 的副本。

## 已确认的问题

- 旧运行时在第一次决策后直接把 `requested_dimensions` 和
  `required_deliverables` 写回状态。LLM 的局部 Plan 因此可以增加用户没有请求的
  数据库、文件或产物义务。
- `GoalCoverage` 以前从 Plan capability 和 artifact tool 推导必需工作，导致资源已
  发现但没有被用户要求的能力进入完成条件。
- 失败候选、资源和工具结果没有一个独立、版本化的用户目标边界。

## 变更

### `app/models/schemas.py`

新增 `GoalContract`，包含原始请求、目标、维度、显式数据源、dataset version、人口
范围、交付物、用户确认、变更来源/原因和版本号。合同默认冻结；历史版本写入
`ScientificAgentState.goal_contract_history`，不会覆盖旧版本。

### `app/agents/goal_contract.py`

- `ensure_goal_contract` 只从原始用户请求、用户明确范围和已存在的合法 Population
  建立初始合同；资源发现不会增加任务。
- `apply_decision_proposal` 要求每个 LLM 维度/交付物可在原始请求中证明。整个提案先
  原子校验，合法字段不会在非法字段被拒绝时残留。
- `accept_population_requirements` 只接受已由范围绑定验证、且文字来自用户请求或
  HITL 的人口范围；普通 REPLAN 不能改变冻结人口范围。
- `revise_from_hitl` 仅在用户确认改变数据版本/范围时创建新合同版本。

### `app/agents/runtime.py` 与 `app/agents/goal_coverage.py`

运行时在资源落地时初始化合同，在每次 LLM 决策前校验提案，并在 HITL 恢复时保留
同一状态和合同历史。GoalCoverage 在合同存在时只从合同读取必需维度、交付物和分析
能力；Plan 不能制造新的完成义务。HITL 明确的 `train_vN` 会优先于旧版本绑定。

## 反事实验证

`tests/test_p1_goal_contract.py` 覆盖：

1. 文件问题即使发现 `training_db` 或 Plan 含 SQL，也不会出现
   `executed_database_analysis`；
2. 用户明确要求文件和数据库时，缺少数据库证据仍为 `PARTIAL`；
3. LLM/REPLAN 不能增加未请求的维度或交付物；
4. 合法维度提案、人口范围和 HITL dataset version 产生可追踪合同版本；
5. 删除核心证据后覆盖率重新变为不可验证。

本阶段离线测试结果：`7 passed`。同时完成 `compileall` 与 `git diff --check`。
回归集合中另外出现的 3 个环境依赖失败（PostgreSQL follow-up 异步流、无 LLM
Skill 路由的 HITL 测试）没有触及 GoalContract 路径；不会修改这些历史测试或生产
数据。

## 安全边界

本阶段没有调用 Qwen，没有写 PostgreSQL/MinIO，没有修改历史任务、冻结基准或推送
GitHub。后续 P1.2 将在此合同边界内补充 PlanStep 协议。
