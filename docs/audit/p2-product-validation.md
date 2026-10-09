# P2 product validation

The UI projects five user-facing phases—任务理解、规划、数据分析、证据校验、最终回答—while preserving the raw event list. `executionStages.js` maps
event types without deleting or coalescing audit records. Detailed Decision,
ToolCall, Observation, SQL, Evidence and error data remains behind expandable
Agent Trace. Persisted backend status remains authoritative.
