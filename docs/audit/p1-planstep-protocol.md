# P1.2 PlanStep 协议

## 协议字段

`PlanStep` 现在持久化一个可执行结果合同：

- `allowed_tools`：该结果允许使用的完整工具范围；
- `required_inputs`：执行前必须由参数或 `input_refs` 提供的字段名；
- `completion_predicate`：由真实成功 Observation/evidence 满足的完成条件；
- `optional_tools`：可以帮助结果但不能成为完成义务的工具；
- `recovery_policy`：本阶段保留的恢复策略配置槽位，P1.3 统一解释；
- `query_scope`、`population_id`：结果的绑定范围。

旧的 `selected_tools`/`preferred_tools` 仍用于读取历史 checkpoint 和接收现有 LLM
结构化输出，但在模型校验时归一化为 `allowed_tools`。不另建 Planner，现有
`prepare_replacement` 负责安装时清除模型伪造的 status/observations，并生成运行时
完成谓词。

## 完成与工具边界

`condition_for` 只从必需工具和已定义谓词生成完成条件；optional tool 不会被计入
必需工具。`satisfied` 只读取真实成功观察、scope 校验和人口绑定结果，不接受 LLM
回传的完成标记。PlanningPolicy 拒绝：

- 工具不在全局授权范围；
- `optional_tools` 不属于 `allowed_tools`；
- completion predicate 引用范围外或 optional 工具；
- 非法依赖、能力不匹配或空的 required input 名称。

CALL_TOOL 在运行时还会检查 active step 的 `required_inputs`，并将完整
`allowed_tools` 传给 Dispatcher。PlanStep 的协议字段和真实观察会继续出现在审计
事件/开发者轨迹，不以减少事件为目的删除。

## 测试

`tests/test_p1_planstep_protocol.py` 验证旧字段兼容、optional 工具、scope 完成谓词、
required inputs、伪造观察清除和非法谓词拒绝。P1.2 新增测试 5 个；与 P0 计划边界
回归合计 108 个通过。未调用 Qwen，未写 PostgreSQL/MinIO，未改历史任务或冻结基准。
