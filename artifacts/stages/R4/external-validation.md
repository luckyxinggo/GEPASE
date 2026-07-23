# R4 外部角色执行说明

- R4 的 Proposal、Reflection、Executor、Independent Grader 和 Comparator 均由 Codex Agent-native 隔离上下文执行；Core 只导出 typed work、校验 submission 并维护状态。
- 本阶段未调用额外 Headless Provider，也未使用外部 LLM API key。
- 29 次 candidate Executor 均读取并执行完整 Skill Package；28 次成功产生任务原生 GIF 与 E3，1 次达到冻结 timeout 后以 typed failure 入账。
- 16 次 validation Comparator 均只读取匿名 A/B 产物；C 的 timeout case 由 Core 自动判负，没有生成虚构 Grader 或 Comparator 结果。
- R3 original reference 通过完整 `ReferenceEvidenceKey` 与 429 个 artifact hash 复用；candidate 一侧全部重新执行。
- token 数均为 Agent Host 提供的估算值。Host 未暴露 enqueue timestamp，因此 queue wait 明确记为未观测，没有伪造估算。
- 冻结的 7,200 秒墙钟上限被超过；实际观测为 10,311,052 ms。候选/调用/token 上限未超过。该事实保留为 R5 的运行时改进输入，不改写配置或时间戳。
