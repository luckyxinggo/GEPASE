# R5 阶段结论

R5 已把 R2–R4 的 sealed evidence 投影为可复算、可下载、可本地交互的中文最终报告；它没有新增第二套 evaluator/search，也没有重跑 Agent 评测。

代码实现、工程机制与算法效果继续分开陈述：

- 代码实现：新增只读 `CanaryReportBuilder` Python API、`gepase report build/verify` CLI、依赖无关的中文 HTML renderer、R5 frozen config 和独立机器 Gate。报告构建拒绝覆盖已有目录，验证器会重新收集上游事实并精确比较 payload、manifest、复制资产和 deployable archive。
- 工程机制：R2/R3/R4 的 19/429/877 个 artifact seal 全部复验；正式 R5 报告包含 20 个已索引 artifact、9 个 hash-matched 任务原生 GIF、完整原始/deployable Package Graph、候选/Merge DAG、评分与 Gate 证据以及 7 文件 deployable ZIP。R5-G01–G06 6/6 通过，R5 Agent/Headless/API 调用均为 0。
- 算法效果：报告独立复算 deployable candidate `candidate-04b26dff2bc83b82334bf184` 的 frozen validation mean delta 为 `+0.12426667`、3/3 wins、0 loss，并如实呈现被 timeout 与 category floor 拒绝的候选。这仍只是一个公开 Skill、一个 EvalPlan、一个模型快照和一次搜索运行上的证据。

用户在正式本地 `index.html` 路径确认页面布局、3 组 GIF case、Package Graph 版本/细粒度控件和评分 case 下拉正常。该确认只覆盖视觉与核心交互，不代替 E2/E3、Grader、Comparator 或 Gate 的机器证据。

阶段文档明确保留两项主要边界：R4 实际墙钟 `10,311,052 ms` 超过冻结的 `7,200 s` 预算；本次 accepted edit 只修改一个 `SKILL.md` instruction node，因此不能声称跨文件修改带来正向效果，也不能声称 package-aware 普遍优于单文件优化。最终 HTML 结果页只中性展示实际运行规模，不再把预算 overrun 作为醒目结论。

R5 完成后解锁 S10。S10 只负责开源安装、发布材料、最小复现和简历表述，不得修改已封存 R2–R5 证据或扩大当前效果结论。
