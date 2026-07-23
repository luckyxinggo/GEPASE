# R3 external validation

R3 使用 Codex Agent-native 隔离角色执行真实任务、盲评、匿名比较和失败分析。所有角色输出均通过 typed artifact 回收到 Core，并由 Core 校验 schema、证据边界、上下文唯一性、图节点与分数复算。

本阶段不需要额外用户 UI 验收，也没有调用外部 Headless Provider 或模型 API。用户此前对 R2 离线审核页面交互的确认不被重复解释为 R3 结果审核。

Agent Host 没有提供精确 token telemetry；各角色 usage 中的 token 数均明确标记为 `estimated`。本阶段不记录费用。
