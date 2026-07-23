# R1 权威 Core 数据流

R1 的结论不是“端到端优化器已经完成”，而是仓库只剩一组可继续接线的事实模型和组件。真实 canary、角色评分与集成搜索控制器分别属于 R2、R3、R4。

    CLI / Python API
      → MultiFidelityEvalEngine
      → EvalWorkItem
      → 隔离 Agent Host
      → ExecutionBundle
      → Core Ledger / EvaluationRecord
      → TaskScoreVector（R3 接入评分角色）
      → GEPA Adapter / ASI
      → Package Graph selector
      → typed PackagePatch
      → atomic apply / rollback
      → GateDecision
      → EvolutionPool / deployable frontier
      → same-package Merge contract
      → 回到 MultiFidelityEvalEngine 重新评测

## 当前可执行边界

- gepase eval 与 MultiFidelityEvalEngine 是唯一评测入口。
- EvalWorkItem 是唯一任务下发模型；ExecutionBundle 是唯一 Agent 执行提交模型。WorkSubmission 只是同一 Python 类的历史兼容别名。
- PackageCandidate、PackagePatch、PackageGraph、TaskScoreVector、GateDecision、EvolutionPoolEntry、MergeParentSetSnapshot 各只有一个 Pydantic 定义，并由根 gepase Python API 导出。
- gepase mutation、gepase gate、gepase merge 当前暴露可复用的 Patch 队列、Gate 审计和 merge fixture/contract 能力。
- .agents/skills/gepase-orchestrator/ 只执行 WorkItem 和提交 evidence/Patch，不保存候选池、GEPA、Gate 或 merge 状态。

## 明确未完成

- R3 尚未把 Executor、Independent Grader、Comparator、Analyzer 的强制隔离与六维评分接入运行态。
- R4 尚未建立连接 GEPA、Graph、Patch、Gate、EvolutionPool 与多父 merge 的唯一状态机，因此根 CLI 暂不提供会误导用户的 optimize/evolution 阶段控制器。
- R1 的测试只证明模型、组件与仓库回归有效，不证明任何真实 Skill 已经得到优化。
