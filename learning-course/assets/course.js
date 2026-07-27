"use strict";

const COURSE_PAGES = [
  ["overview", "index.html", "总览：先看懂全局", "为什么做、整体怎么转"],
  ["glossary", "00-glossary.html", "术语地图", "把英文词翻成项目语言"],
  ["foundations", "01-foundations.html", "思想来源", "GEPA、SkillOpt 与启发式学习"],
  ["graph", "02-package-graph.html", "Package 与图", "把文件夹变成可推理结构"],
  ["eval-plan", "03-eval-plan.html", "设计评测", "Trigger、Functional 与人工审核"],
  ["agent-eval", "04-agent-evaluation.html", "真实 Agent 评测", "隔离执行、证据与角色"],
  ["score-gate", "05-scoring-gates.html", "评分与 Gate", "向量分数、配对差值与严格验证"],
  ["gepa-deep", "06-gepa-deep.html", "GEPA 深入", "从官方对象到完整优化循环"],
  ["pareto", "06-pareto-lab.html", "Pareto 推导实验室", "支配、前沿与父代抽样"],
  ["search", "06-gepa-search.html", "GEPASE 搜索适配", "把 GEPA 接到 Package 主链"],
  ["patch", "07-patch-evolution.html", "图引导 Patch", "定位、修改、回滚与合并"],
  ["canary", "08-canary.html", "真实 Canary 复盘", "slack-gif-creator 的完整闭环"],
  ["usage", "09-code-usage.html", "代码与使用", "Core、CLI、API 和扩展点"],
  ["interview", "10-interview.html", "面试总复习", "追问、边界与项目表达"]
];

const JOURNEY = {
  overview: ["一份刚写完但不知好坏的 Skill", "建立整条进化路线", "一张从 Package 到 deployable candidate 的地图"],
  glossary: ["阅读中不断出现的英文概念", "建立随用随查的术语工具", "可回到任意流程章节继续学习"],
  foundations: ["模型冻结，但 Skill 仍需从反馈中变好", "确定什么是可训练状态，并拆解方法来源", "把完整 Package 定义为待优化策略 θ"],
  graph: ["一个由多类文件组成的原始 Package", "解析 IR、依赖图和静态诊断", "稳定节点、关系边和可用于定位的 Package Graph"],
  "eval-plan": ["Package 能力、依赖与风险画像", "设计 Trigger/Functional case 并人工冻结", "带 hash、split、rubric 和可见性边界的 EvalPlan"],
  "agent-eval": ["冻结 EvalPlan 与 no-skill/original/candidate 变体", "隔离 Executor 真正完成任务并收集证据", "ExecutionBundle、原生产物、trace、usage 与 typed failure"],
  "score-gate": ["产物、断言、独立评分和盲比较", "构造六维 TaskScoreVector 与 paired delta", "可供优化器使用的任务反馈和严格保护线"],
  "gepa-deep": ["任务级分数、轨迹和自然语言反馈", "理解官方 GEPA 如何反思、提案、评测和更新状态", "可映射到 GEPASE 的搜索引擎心智模型"],
  pareto: ["多个候选在不同 case/目标上各有所长", "推导支配、前沿、per-key champion 与抽样", "不会过早丢失局部优势的父代集合"],
  search: ["GEPA 心智模型、Pareto 父代和 GEPASE 失败证据", "适配 TaskScoreVector、Graph、预算、谱系与 merge", "一个有证据的 mutation 方向和父代/目标节点"],
  patch: ["反思结论、父候选和 graph-guided scope", "生成 typed Patch 并原子应用到隔离副本", "可重现 child snapshot、graph diff 和 affected closure"],
  canary: ["候选、train 结果和冻结 validation", "逐层 Gate、拒绝回归、封存报告", "一个可部署候选与诚实的量化结论边界"],
  usage: ["已经理解的端到端算法流程", "沿类型、CLI、API 与 artifact 进入源码", "能够复现、调试和扩展的新使用者"],
  interview: ["完整项目认知与真实 canary 证据", "压缩成不同长度的解释并接受追问", "能区分实现、机制测试和效果验证的项目表达"]
};

const TERM_BOOK = {
  "LLM": ["大语言模型", "Large Language Model。根据上下文生成文本、代码或结构化输出的模型；在本项目里模型权重冻结，Skill Package 才是被优化的外部状态。"],
  "Agent": ["智能体", "能读取上下文、规划步骤、调用工具并交付产物的执行者。LLM 是它的推理核心，但 Agent 还包括工具、运行环境和状态。"],
  "Skill": ["技能", "交给 Agent 的一组可复用说明与资源，告诉它在某类任务中何时介入、如何工作、使用哪些脚本和参考资料。"],
  "Skill Package": ["技能包", "完整技能目录：SKILL.md、references、scripts、assets、metadata/runtime 配置，以及文件之间的依赖与调用关系。"],
  "Prompt": ["提示文本", "发给模型的自然语言上下文。SKILL.md 含有提示性质，但完整 Skill Package 不只是一段 Prompt。"],
  "Policy": ["策略", "在给定状态下选择下一步行动的规则。GEPASE 把 Prompt、代码、规则与外部文件共同视为可更新策略。"],
  "Runtime": ["运行时", "真正承载 Agent、工具和任务执行的环境。GEPASE 不自研通用 Runtime，而由 Codex、Claude Code 等 Host 提供。"],
  "Agent Host": ["Agent 宿主", "能够启动隔离 Agent、提供工具并保存执行过程的系统，例如 Codex 或 Claude Code。"],
  "CLI": ["命令行接口", "Command-Line Interface。用户在终端执行的 gepase 命令，是 Core 的主要入口之一。"],
  "API": ["编程接口", "Application Programming Interface。Python 程序可直接调用的类型和函数；可选的 LLM Provider API 只是角色后端，不是项目主体。"],
  "IR": ["中间表示", "Intermediate Representation。把 Markdown、Python、Shell、二进制资产解析成统一、稳定、可分析的数据结构。"],
  "Snapshot": ["快照", "某一时刻整个 Package 的不可变视图，包含文件、哈希和解析结果，便于复现与比较。"],
  "Hash": ["哈希", "文件或配置内容的短指纹。内容相同通常产生相同哈希，用于缓存、复现和防止偷换条件。"],
  "Node": ["节点", "图中的实体，例如文件、Markdown 标题、Python 函数或资产。"],
  "Edge": ["边", "图中节点之间的关系，例如包含、引用、导入、调用或运行时访问。"],
  "Graph": ["图", "由节点和边组成的结构。这里用它表达 Skill Package 内部依赖，而不是仅画一张漂亮示意图。"],
  "Reverse Slice": ["反向切片", "从失败或输出节点逆着依赖边追溯，找出可能影响它的上游文件和组件。"],
  "Dependency Closure": ["依赖闭包", "从一组修改节点出发，把必须一起检查或携带的直接、间接依赖全部纳入。"],
  "Blast Radius": ["影响半径", "一个修改可能波及多少文件、组件和任务；越大通常需要更强验证。"],
  "Eval": ["评测", "用预先定义的任务、证据和规则判断 Agent 是否完成任务，以及 Skill 是否带来增益。"],
  "EvalPlan": ["评测计划", "冻结后的任务集合与评分契约，包括 prompt、fixture、期望、rubric、split、证据等级和人工审核记录。"],
  "Fixture": ["测试输入材料", "Task 执行时可以看到并使用的输入文件或模拟环境，例如图片、数据表和项目目录。"],
  "Rubric": ["评分量表", "把正确性、完整性、专业度等主观要求拆成可核查的分项标准。Executor 不可见，Grader 可见。"],
  "Oracle": ["标准答案信息", "断言、rubric、expected answer 等会泄露正确方向的信息；必须对 Executor 隐藏。"],
  "Baseline": ["对照组", "同一个任务在没有候选 Skill 或使用原始 Skill 时的执行结果，用于回答“Skill 是否真的有帮助”。"],
  "Variant": ["执行变体", "同一 case 的 no-skill、original、candidate 等相互隔离版本。"],
  "Trigger Eval": ["触发评测", "只判断 Skill 是否应被召回：该用时能不能触发，不该用时会不会误触发。与功能质量分开。"],
  "Functional Eval": ["功能评测", "让 Agent 真正完成任务并检查原生产物，回答“触发之后到底做得好不好”。"],
  "E0": ["静态证据", "不运行任务，只检查 Package 结构、语法、安全与依赖。快，但不能证明任务完成。"],
  "E1": ["计划证据", "Agent 只说明准备如何做。适合便宜诊断，不可用于证明功能成功或接纳部署候选。"],
  "E2": ["真实执行证据", "Agent 读取完整 Package、调用工具并产生任务原生输出，同时保存 transcript、trace、usage、timing 和哈希。"],
  "E3": ["产物断言证据", "在 E2 真实产物上运行确定性检查；E3 依赖 E2，不是框架凭空生成业务结果。"],
  "Executor": ["执行者", "看见任务和输入，实际完成工作；不能看断言、rubric、兄弟输出、候选身份或预期胜者。"],
  "Grader": ["评分者", "独立读取任务产物与证据，评价正确性、完整性、专业度和可用性，并核验 Agent 声明。"],
  "Comparator": ["盲比者", "不知道 A/B 身份地比较两个关键输出，降低“因为知道它是候选所以偏爱它”的风险。"],
  "Analyzer": ["失败分析者", "结合任务反馈、trace 和图切片解释为什么失败，并把原因映射到可修改的 Package 节点。"],
  "ASI": ["聚合式技能洞察", "Aggregated Skill Insight。把多任务失败模式合并成结构化反思，帮助选择下一次 mutation 的范围。"],
  "WorkItem": ["工作单", "Core 发给 Agent Host 的最小、带权限边界的任务描述。每个角色拿到的字段不同。"],
  "Submission": ["提交单", "Agent Host 把产物、证据、哈希与结构化结论交还 Core 的载体。"],
  "Artifact": ["产物", "任务真正生成的文件或报告。只有“文件存在”不代表内容正确。"],
  "Trace": ["执行轨迹", "Agent 实际读了什么、调用了哪些工具、发生了哪些错误的可核验证据。"],
  "Provenance": ["来源链", "记录一个分数和候选来自哪个 Package、任务、环境、角色、模型、输入与哈希，保证可以追溯。"],
  "TaskScoreVector": ["任务分数向量", "六维分数：task_correctness、output_quality、skill_gain、reliability、efficiency、package_quality。保留多目标信息，不偷压成单一 reward。"],
  "Paired Delta": ["配对差值", "同一 case、同一轮、同一环境下，候选分数减对照分数；比比较两批不相关运行更能抵消任务难度与环境波动。"],
  "Train Split": ["搜索任务集", "优化器可以反复看到反馈、用于提案与筛选的任务集合。"],
  "Held-out Validation": ["留出验证集", "优化时不可见、最终才使用的冻结任务集合，用来检查修改是否泛化而不是记住训练题。"],
  "Regression": ["回归", "修改让原本正常的任务、类别或安全属性变差。总平均上升也可能掩盖严重局部回归。"],
  "Gate": ["门禁", "候选必须逐层通过的验证条件。失败就拒绝并记录原因，不能因为“看起来不错”跳过。"],
  "Candidate": ["候选版本", "由某个或多个同 Package 父代产生、尚未被证明可部署的 Package 版本。"],
  "Lineage": ["谱系", "父代、子代、Patch、评测和接纳/拒绝形成的可追踪关系。"],
  "DAG": ["有向无环图", "Directed Acyclic Graph。候选谱系按父到子连接且不能回到祖先，便于复现搜索历史。"],
  "Mutation": ["变异", "基于任务反馈对 Package 做一次有界修改并生成新候选的过程。"],
  "PackagePatch": ["技能包补丁", "带类型、目标节点、前置条件和操作列表的结构化修改；可原子应用、失败回滚。"],
  "Precondition": ["前置条件", "Patch 应用前必须满足的旧内容、哈希或节点条件，防止把补丁误打到已经变化的文件。"],
  "Budget": ["预算", "搜索可以消耗的执行次数、候选数、角色调用或时间等上限；约束比较公平性和工程成本。"],
  "GEPA": ["反思式进化框架", "通过完整轨迹和自然语言反馈提出文本组件修改，并用 Pareto 候选池保留不同任务/目标上的局部优势。"],
  "GEPAEngine": ["GEPA 搜索引擎", "官方 GEPA 中驱动每轮父代抽样、minibatch 评测、反思提案、接纳、全量评测、状态更新与可选 merge 的主控制器。"],
  "GEPAState": ["GEPA 搜索状态", "保存候选文本组件、父子关系、逐 validation item 分数、目标聚合、frontier champion mapping、缓存、预算与执行轨迹的可恢复状态。"],
  "Adapter": ["适配器", "把具体领域的候选和评测过程转换成 GEPA 可调用契约的边界层；GEPASE Adapter 把 PackageCandidate 与真实 Agent EvaluationRecord 映射进去，但不替代 Agent Runtime。"],
  "EvaluationBatch": ["批量评测结果", "GEPA Adapter 对一批数据点评测候选后返回的结构，既含数值分数，也可保留轨迹和自然语言反馈，供后续反思使用。"],
  "Trajectory": ["完整轨迹", "一次任务从输入、读取、推理、工具调用到产物和错误的过程记录。它比单一 reward 带有更高带宽的失败信息。"],
  "Minibatch": ["小批任务", "每轮只从搜索任务集中抽取的一部分任务，用较低成本快速判断一个提案是否值得继续做全量评测。"],
  "Acceptance Criterion": ["接纳判据", "规定新提案相对父代满足什么条件才算改进。官方引擎默认采用严格改善；GEPASE 还叠加保护目标和后续 held-out Gate。"],
  "Selection Strategy": ["选择策略", "当一轮可能产生多个改善提案时，决定哪些提案进入后续完整评测；它和接纳判据是两个不同职责。"],
  "Reflection": ["反思", "不是泛泛地说“做得更好”，而是从失败任务、证据和约束中提炼可执行的修改理由。"],
  "Pareto": ["帕累托思想", "如果候选 A 在所有保护目标上都不差于 B，且至少一个目标更好，A 支配 B；否则两者可能各有所长而共同保留。"],
  "Dominance": ["支配关系", "对最大化目标，A 在所有维度都不低于 B，且至少一维严格高于 B，才称 A 支配 B；相等不构成严格支配。"],
  "Frontier Type": ["前沿类型", "GEPA 用 instance、objective、hybrid、cartesian 定义冠军 key：按题、按目标、二者并集，或按题×目标组合保留局部最佳候选。"],
  "Champion Mapping": ["冠军映射", "GEPAState 中从 frontier key 到达到该 key 最佳分数的候选集合的映射；它不是简单把所有分数向量做一次标准 Pareto 排序。"],
  "Frontier": ["前沿集合", "当前没有被其他候选完全支配的一组候选；deployable frontier 还要求通过留出验证 Gate。"],
  "Merge": ["多父合并", "把同一 Skill Package 谱系中多个父候选的互补改进合成子代；禁止跨不同 Skill Package 合并。"],
  "Rejected Edit": ["拒绝编辑记忆", "保存失败 Patch、触发的回归和证据，避免后续搜索反复犯同一种错误。"],
  "Deployable": ["可部署", "并非“代码能跑”，而是结构、安全、训练目标和严格 held-out validation 都通过的候选状态。"]
};

function currentPageId() {
  return document.body.dataset.page || "overview";
}

function completionKey(id) {
  return `gepase-course-complete:${id}`;
}

function isComplete(id) {
  return localStorage.getItem(completionKey(id)) === "1";
}

function sidebarMarkup() {
  const current = currentPageId();
  const completed = COURSE_PAGES.filter(([id]) => isComplete(id)).length;
  const percent = Math.round((completed / COURSE_PAGES.length) * 100);
  const nav = COURSE_PAGES.map(([id, file, title], index) => {
    const classes = [id === current ? "active" : "", isComplete(id) ? "completed" : ""].filter(Boolean).join(" ");
    return `<a class="${classes}" href="${file}"><span class="nav-index">${String(index).padStart(2, "0")}</span><span>${title}</span><span class="nav-check" aria-label="已完成">✓</span></a>`;
  }).join("");
  return `<aside class="course-sidebar" aria-label="课程目录">
    <a class="course-brand" href="index.html">GEPASE · FROM ZERO</a>
    <div class="course-brand-sub">从零掌握技能进化框架</div>
    <div class="course-progress-box">
      <div class="course-progress-label"><span>课程进度</span><strong>${completed}/${COURSE_PAGES.length} · ${percent}%</strong></div>
      <div class="course-progress-track"><span class="course-progress-fill" style="width:${percent}%"></span></div>
    </div>
    <nav class="course-nav">${nav}</nav>
    <div class="sidebar-actions"><button class="soft-button" type="button" data-theme-toggle>切换主题</button><button class="soft-button" type="button" onclick="window.print()">打印本章</button></div>
    <p class="sidebar-note">点击正文中虚线词语可随时查看解释。阅读进度只保存在当前浏览器。</p>
  </aside>`;
}

function footerMarkup() {
  const id = currentPageId();
  const index = COURSE_PAGES.findIndex(([pageId]) => pageId === id);
  const previous = COURSE_PAGES[index - 1];
  const next = COURSE_PAGES[index + 1];
  return `<footer class="course-footer">
    ${previous ? `<a href="${previous[1]}">← ${previous[2]}</a>` : "<span></span>"}
    <span>第 ${index + 1} / ${COURSE_PAGES.length} 课</span>
    ${next ? `<a class="next" href="${next[1]}">${next[2]} →</a>` : "<span></span>"}
  </footer>`;
}

function journeyMarkup() {
  const id = currentPageId();
  const [input, current, output] = JOURNEY[id] || ["上一章输出", "理解本章机制", "交给下一章的结构化结果"];
  return `<section class="journey-context" aria-label="课程主线中的当前位置">
    <div class="journey-item"><small>承接上一环节 · INPUT</small><b>${input}</b><p>先确认本章拿到的材料是什么。</p></div>
    <div class="journey-arrow" aria-hidden="true">→</div>
    <div class="journey-item current"><small>当前环节 · PROCESS</small><b>${current}</b><p>本章所有概念都服务于这一步，不是孤立知识点。</p></div>
    <div class="journey-arrow" aria-hidden="true">→</div>
    <div class="journey-item"><small>交付下一环节 · OUTPUT</small><b>${output}</b><p>学完后带着这个输出继续往下走。</p></div>
  </section>`;
}

function injectShell() {
  const main = document.querySelector("main.course-main");
  if (!main) return;
  const wrapper = document.createElement("div");
  wrapper.className = "course-shell";
  main.parentNode.insertBefore(wrapper, main);
  wrapper.innerHTML = sidebarMarkup();
  wrapper.appendChild(main);
  const hero = main.querySelector(".hero");
  if (hero) hero.insertAdjacentHTML("afterend", journeyMarkup());
  main.insertAdjacentHTML("beforeend", footerMarkup());
  document.body.insertAdjacentHTML("afterbegin", `<div class="reading-progress" aria-hidden="true"></div><header class="mobile-top"><a href="index.html">GEPASE 学习课程</a><button class="soft-button" type="button" data-theme-toggle>主题</button></header>`);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("gepase-course-theme", theme);
}

function initTheme() {
  const stored = localStorage.getItem("gepase-course-theme");
  const preferred = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  setTheme(stored || preferred);
  document.querySelectorAll("[data-theme-toggle]").forEach(button => {
    if (button.dataset.themeBound === "true") return;
    button.dataset.themeBound = "true";
    button.addEventListener("click", () => {
      setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  });
}

function initReadingProgress() {
  const bar = document.querySelector(".reading-progress");
  if (!bar) return;
  const update = () => {
    const total = document.documentElement.scrollHeight - window.innerHeight;
    const percent = total > 0 ? Math.min(100, Math.max(0, (window.scrollY / total) * 100)) : 100;
    bar.style.width = `${percent}%`;
  };
  update();
  window.addEventListener("scroll", update, { passive: true });
  window.addEventListener("resize", update);
}

function initSelectors() {
  document.querySelectorAll("[data-select-group]").forEach(button => {
    button.addEventListener("click", () => {
      const group = button.dataset.selectGroup;
      const selected = button.dataset.select;
      document.querySelectorAll(`[data-select-group="${group}"]`).forEach(peer => {
        const active = peer === button;
        peer.classList.toggle("active", active);
        peer.setAttribute("aria-pressed", String(active));
      });
      document.querySelectorAll(`[data-panel-group="${group}"]`).forEach(panel => {
        panel.classList.toggle("active", panel.dataset.panel === selected);
      });
    });
  });
}

function initCopyButtons() {
  document.querySelectorAll(".code-wrap").forEach(wrap => {
    const head = wrap.querySelector(".code-head");
    const code = wrap.querySelector("pre code");
    if (!head || !code || head.querySelector(".copy-button")) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "copy-button";
    button.textContent = "复制";
    button.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        button.textContent = "已复制";
      } catch (_) {
        button.textContent = "请手动复制";
      }
      setTimeout(() => { button.textContent = "复制"; }, 1500);
    });
    head.appendChild(button);
  });
}

function initTermDialog() {
  const dialog = document.createElement("dialog");
  dialog.className = "term-dialog";
  dialog.innerHTML = `<div class="term-dialog-body"><div class="term-dialog-head"><div><div class="eyebrow">术语解释</div><h2 data-term-title></h2></div><button type="button" class="close-dialog" aria-label="关闭">×</button></div><p data-term-english></p><p data-term-description></p><a href="00-glossary.html">前往完整术语表 →</a></div>`;
  document.body.appendChild(dialog);
  dialog.querySelector(".close-dialog").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  document.querySelectorAll(".term[data-term]").forEach(button => {
    button.addEventListener("click", () => {
      const key = button.dataset.term;
      const [cn, description] = TERM_BOOK[key] || ["课程术语", "本页暂未录入详细解释，请前往术语地图查看上下文。"];
      dialog.querySelector("[data-term-title]").textContent = cn;
      dialog.querySelector("[data-term-english]").textContent = key;
      dialog.querySelector("[data-term-description]").textContent = description;
      dialog.showModal();
    });
  });
}

function initGlossary() {
  const root = document.querySelector("[data-glossary-grid]");
  if (!root) return;
  root.innerHTML = Object.entries(TERM_BOOK).map(([english, [chinese, description]]) => `<article class="glossary-card" data-search="${english.toLowerCase()} ${chinese} ${description.toLowerCase()}"><span class="english">${english}</span><h3>${chinese}</h3><p>${description}</p></article>`).join("");
  const input = document.querySelector("[data-glossary-search]");
  const count = document.querySelector("[data-glossary-count]");
  const filter = () => {
    const query = (input?.value || "").trim().toLowerCase();
    let visible = 0;
    root.querySelectorAll(".glossary-card").forEach(card => {
      const match = !query || card.dataset.search.includes(query);
      card.classList.toggle("hidden", !match);
      if (match) visible += 1;
    });
    if (count) count.textContent = `${visible} / ${Object.keys(TERM_BOOK).length} 个术语`;
  };
  input?.addEventListener("input", filter);
  filter();
}

function initQuizzes() {
  document.querySelectorAll(".quiz").forEach(quiz => {
    const feedback = quiz.querySelector(".quiz-feedback");
    quiz.querySelectorAll(".quiz-option").forEach(option => {
      option.addEventListener("click", () => {
        quiz.querySelectorAll(".quiz-option").forEach(peer => peer.classList.remove("selected", "correct", "incorrect"));
        const correct = option.dataset.correct === "true";
        option.classList.add("selected", correct ? "correct" : "incorrect");
        if (feedback) feedback.textContent = correct ? `答对了。${option.dataset.explain || ""}` : `还差一步。${option.dataset.explain || "请回看上面的边界说明。"}`;
      });
    });
  });
}

function initCompletion() {
  const button = document.querySelector("[data-complete-lesson]");
  if (!button) return;
  const id = currentPageId();
  const render = () => {
    const done = isComplete(id);
    button.classList.toggle("completed", done);
    button.textContent = done ? "✓ 本章已完成（点击撤销）" : "标记本章已完成";
  };
  button.addEventListener("click", () => {
    localStorage.setItem(completionKey(id), isComplete(id) ? "0" : "1");
    render();
    const sidebar = document.querySelector(".course-sidebar");
    if (sidebar) sidebar.outerHTML = sidebarMarkup();
    initTheme();
  });
  render();
}

function initParetoLab() {
  const lab = document.querySelector("[data-pareto-lab]");
  if (!lab) return;
  const candidates = ["A", "B", "C", "D"];
  const values = {};
  const readValues = () => {
    candidates.forEach(id => {
      values[id] = {
        quality: Number(lab.querySelector(`[data-candidate="${id}"][data-objective="quality"]`).value),
        efficiency: Number(lab.querySelector(`[data-candidate="${id}"][data-objective="efficiency"]`).value)
      };
    });
  };
  const dominates = (left, right) => {
    const a = values[left];
    const b = values[right];
    return a.quality >= b.quality && a.efficiency >= b.efficiency && (a.quality > b.quality || a.efficiency > b.efficiency);
  };
  const render = () => {
    readValues();
    const frontier = candidates.filter(id => !candidates.some(other => other !== id && dominates(other, id)));
    candidates.forEach(id => {
      lab.querySelectorAll(`[data-value-for="${id}"]`).forEach(output => {
        output.textContent = values[id][output.dataset.objective];
      });
      const card = lab.querySelector(`[data-pareto-card="${id}"]`);
      card.classList.toggle("is-frontier", frontier.includes(id));
      card.classList.toggle("is-dominated", !frontier.includes(id));
      const circle = lab.querySelector(`[data-point="${id}"]`);
      const label = lab.querySelector(`[data-point-label="${id}"]`);
      const x = 42 + values[id].quality * 30;
      const y = 342 - values[id].efficiency * 30;
      circle.setAttribute("cx", x);
      circle.setAttribute("cy", y);
      circle.classList.toggle("frontier", frontier.includes(id));
      label.setAttribute("x", x + 9);
      label.setAttribute("y", y - 9);
    });
    const sortedFront = frontier.slice().sort((a, b) => values[a].quality - values[b].quality);
    const line = lab.querySelector("[data-front-line]");
    line.setAttribute("points", sortedFront.map(id => `${42 + values[id].quality * 30},${342 - values[id].efficiency * 30}`).join(" "));
    const dominatedPairs = [];
    candidates.forEach(right => candidates.forEach(left => {
      if (left !== right && dominates(left, right)) dominatedPairs.push(`${left} 支配 ${right}`);
    }));
    lab.querySelector("[data-pareto-readout]").textContent = `当前前沿：${frontier.join("、")}。${dominatedPairs.length ? `支配关系：${dominatedPairs.join("；")}。` : "四个候选互不支配。"}`;
  };
  lab.querySelectorAll("input[type=range]").forEach(input => input.addEventListener("input", render));
  render();
}

document.addEventListener("DOMContentLoaded", () => {
  injectShell();
  initTheme();
  initReadingProgress();
  initSelectors();
  initCopyButtons();
  initTermDialog();
  initGlossary();
  initQuizzes();
  initCompletion();
  initParetoLab();
});
