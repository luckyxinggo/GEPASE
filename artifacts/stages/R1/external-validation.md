# R1 外部验证

R1 不需要外部 Agent、LLM、API 或人工质量判断。本阶段全部 Gate 都是仓库结构、类型、测试、schema、安全与文档的确定性验证。

因此：

- real_agent_runs = 0
- headless_provider_runs = 0
- external effect validation = not applicable
- Skill optimization effect = not evaluated

R2 才开始接入公开 canary；R3 才要求真实隔离 Agent 执行与独立评分。
