# R5 外部与页面验证说明

- R5 报告生成、证据复算和 Gate 全部在 Python Core/CLI 内完成；没有分发 Executor、Grader、Comparator、Analyzer、Reflection 或 Proposer。
- 本阶段没有调用 Headless Provider、外部 LLM API 或额外 API key，也没有重跑 R3/R4、重新搜索候选或修改 frozen EvalPlan/scoring policy。
- R2/R3/R4 run seal 分别重新校验 19、429、877 个 artifact；R5 复制的 9 个任务原生 GIF 均与 sealed source hash 一致。
- HTML 为单文件 CSS/JavaScript，业务数据内嵌；没有外部脚本、样式或分析服务。inline JavaScript 已由 Node.js 独立编译检查。
- Codex 内置 Browser 因安全策略拒绝自动导航本地 `file://` 页面；没有改用 localhost、其他浏览器自动化或 raw CDP 绕过。用户随后在目标 `file://` 路径打开正式 R5 报告，并确认页面布局、三组 GIF case 切换、Package Graph 版本/细粒度控件和评分 case 下拉均正常；确认范围与时间记录在 `evidence/visual-validation.json`，不被解释为重新评分或扩大算法结论。
