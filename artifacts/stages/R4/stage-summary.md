# R4 阶段结论

R4 已把 GEPA、真实任务反馈、Package Graph、typed PackagePatch、严格 Gate 与同 Package 多父 Merge 接入唯一 Core 主链，并在 `slack-gif-creator` 公开 canary 上完成一次真实进化。

代码实现、工程机制与算法效果需要分开解释：

- 代码实现：单一 R4 Controller/CLI、reference cache、候选 DAG、恢复分支、同 Package merge、typed failure、runtime audit 和 stage Gate 已实现。
- 工程机制：29 个 fresh candidate case、73 次隔离评测角色调用、29 个可重算 TaskScoreVector、8/8 R4 机器 Gate 和全量静态/测试回归通过。
- 算法效果：候选 A 的 train mean delta 为 `+0.04190`，held-out validation mean delta 为 `+0.12427`，3/3 validation case 均胜出并进入 deployable frontier。这是在一个公开 Skill、一个 frozen EvalPlan 和一次搜索运行上的有效结果，不外推为普遍结论。

严格 Gate 同时拒绝了两个容易被误报为成功的候选：恢复分支 C 在 train 为 `+0.07643`，但 validation 的效率任务超时，最终均值 `-0.19782`；merge child 的 validation 总均值虽为 `+0.05828`，但 `emoji_animation=-0.09144` 低于预注册 category floor，因此被拒绝。

本阶段的主要已知问题是运行时间：reference cache 和同阶段并发按设计生效，Agent 调用总数为 77，估算 token 为 1,649,370；但端到端墙钟为 10,311,052 ms，超过冻结的 7,200 秒上限。R4 如实完成并封存此 overrun，R5 不应把它描述成预算内运行。

另一个边界是：本次 accepted edit 位于 `SKILL.md` 的一个 instruction node。Package Graph、完整目录 materialization、依赖 contribution/closure 与 merge 都进入了真实主链，但该结果不证明跨 `references/scripts/assets` 修改有效，也不证明 package-aware 优于仅修改 `SKILL.md`。
