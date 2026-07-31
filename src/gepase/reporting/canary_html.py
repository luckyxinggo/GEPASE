"""Dependency-free Chinese HTML renderer for the sealed R5 canary report."""

# ruff: noqa: E501, RUF001 -- Chinese copy and standalone HTML are intentionally readable.

from __future__ import annotations

import html
import json
from typing import Any


def _script_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def render_canary_report(data: dict[str, Any]) -> str:
    headline = data["headline"]
    deployable = data["deployable"]
    success_count = sum(bool(item) for item in data["success_gates"].values())
    title = html.escape(str(data["title_zh"]))
    candidate = html.escape(str(deployable["candidate_id"]))
    delta = float(headline["validation_mean_delta"])
    package_file_count = len(deployable["files"])
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light"><title>{title}</title>
<style>
:root{{--paper:#f3f0e8;--surface:#fffdf8;--ink:#20211f;--muted:#676961;--line:#d8d4c9;--line-strong:#b8b4aa;--soft:#ebe7de;--night:#1d2521;--night2:#27312b;--coral:#b94f38;--coral-soft:#f3ded4;--teal:#17685b;--teal-soft:#dcebe5;--amber:#966314;--amber-soft:#f2e7cf;--red:#9e3d38;--red-soft:#f3ddda;--blue:#315d80;--mono:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace;--serif:Georgia,"Songti SC","STSong",serif;--sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Noto Sans CJK SC","Microsoft YaHei",sans-serif}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth;scroll-padding-top:72px}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.68 var(--sans)}}a{{color:var(--teal);text-underline-offset:3px}}button,select,input{{font:inherit}}button{{cursor:pointer}}code,pre,.mono{{font-family:var(--mono)}}.topbar{{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:18px;padding:11px clamp(18px,4vw,64px);background:#f3f0e8ed;border-bottom:1px solid var(--line);backdrop-filter:blur(14px)}}.brand{{font:700 12px/1 var(--mono);letter-spacing:.17em;color:var(--coral);white-space:nowrap}}.topbar nav{{display:flex;gap:5px;overflow:auto;scrollbar-width:none}}.topbar nav a{{white-space:nowrap;color:var(--muted);text-decoration:none;padding:7px 9px;border-radius:7px;font-size:13px}}.topbar nav a:hover,.topbar nav a.active{{background:var(--surface);color:var(--ink)}}.hero{{padding:clamp(58px,9vw,128px) clamp(22px,7vw,110px) 56px;background:radial-gradient(circle at 88% 16%,#d7e6de 0,transparent 28%),linear-gradient(140deg,#fffdf8 0%,#f0eadf 68%,#e8e2d7 100%);border-bottom:1px solid var(--line)}}.eyebrow{{font:700 12px/1 var(--mono);letter-spacing:.15em;text-transform:uppercase;color:var(--coral)}}h1{{font:500 clamp(38px,6vw,76px)/1.05 var(--serif);letter-spacing:-.035em;max-width:1100px;margin:20px 0 22px}}.lead{{font-size:clamp(17px,2vw,21px);line-height:1.6;max-width:900px;color:var(--muted)}}.hero-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-top:38px;max-width:1120px}}.metric{{border-top:2px solid var(--ink);padding:13px 2px 4px}}.metric strong{{display:block;font:500 clamp(27px,4vw,45px)/1.1 var(--serif)}}.metric span{{display:block;margin-top:7px;color:var(--muted);font-size:13px}}.scope-note{{margin-top:26px;max-width:920px;padding:14px 16px;border-left:3px solid var(--amber);background:#f7eedda8;color:#6c4b19}}main{{max-width:1480px;margin:auto;padding:0 clamp(18px,4vw,64px) 100px}}section{{padding:74px 0 20px;border-bottom:1px solid var(--line)}}.section-kicker{{font:700 11px/1 var(--mono);letter-spacing:.15em;color:var(--coral);text-transform:uppercase}}h2{{font:500 clamp(30px,4vw,48px)/1.15 var(--serif);letter-spacing:-.02em;margin:14px 0 12px}}h3{{font-size:19px;line-height:1.35;margin:0}}.section-intro{{max-width:900px;color:var(--muted);font-size:16px;margin:0 0 28px}}.panel{{background:var(--surface);border:1px solid var(--line);border-radius:16px;overflow:hidden}}.panel-pad{{padding:22px}}.status-grid,.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}}.status-card{{padding:18px;border:1px solid var(--line);border-radius:13px;background:var(--surface)}}.status-card b{{font:700 12px/1.2 var(--mono);color:var(--teal)}}.status-card p{{margin:10px 0 0;color:var(--muted)}}.pill{{display:inline-flex;align-items:center;gap:6px;padding:5px 9px;border-radius:99px;background:var(--soft);font:650 11px/1.3 var(--mono)}}.pill.ok{{background:var(--teal-soft);color:var(--teal)}}.pill.bad{{background:var(--red-soft);color:var(--red)}}.pill.warn{{background:var(--amber-soft);color:var(--amber)}}.toolbar{{display:flex;flex-wrap:wrap;align-items:center;gap:10px;padding:13px 15px;background:#f8f5ee;border-bottom:1px solid var(--line)}}.toolbar label{{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:13px}}select,input[type=search]{{border:1px solid var(--line-strong);border-radius:8px;background:white;color:var(--ink);padding:8px 10px}}input[type=search]{{min-width:min(320px,100%);flex:1}}.btn{{border:1px solid var(--ink);border-radius:8px;padding:8px 12px;background:var(--ink);color:white}}.btn.secondary{{background:white;color:var(--ink);border-color:var(--line-strong)}}.graph-layout{{display:grid;grid-template-columns:minmax(0,1fr) 340px;min-height:620px;background:var(--night);color:#eef1eb}}.graph-canvas{{overflow:auto;min-height:620px}}#package-svg{{display:block;min-width:100%;min-height:620px}}.graph-inspector{{padding:19px;border-left:1px solid #465048;background:var(--night2);overflow:auto;max-height:700px}}.graph-inspector h3{{font:600 14px var(--mono);color:#e9b39f}}.graph-inspector pre{{white-space:pre-wrap;word-break:break-word;color:#d5ddd5;font-size:11px}}.edge{{stroke:#66726a;stroke-width:1;opacity:.32}}.edge.observed{{stroke:#71c9ad;stroke-width:1.7;opacity:.7}}.node rect{{fill:#303c35;stroke:#6d7a71}}.node text{{fill:#ecf0e9;font:10px var(--mono);pointer-events:none}}.node .kind{{fill:#aab5ac;font-size:8px}}.node.accessed rect{{stroke:#62c5a7;stroke-width:2}}.node.modified rect{{stroke:#ee8b68;stroke-width:2.8}}.node.merge rect{{stroke:#e5bd66;stroke-width:2.2}}.node.active rect,.node:hover rect{{fill:#435048;stroke:#fff}}.legend{{display:flex;flex-wrap:wrap;gap:12px;color:#c7d0c8;font-size:12px}}.legend i{{display:inline-block;width:10px;height:10px;border:2px solid #777;border-radius:2px;margin-right:5px;vertical-align:-1px}}.case-tabs{{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}}.case-tab{{border:1px solid var(--line);background:var(--surface);border-radius:99px;padding:8px 12px;color:var(--muted)}}.case-tab.active{{background:var(--ink);color:white;border-color:var(--ink)}}.case-head{{display:flex;justify-content:space-between;align-items:flex-start;gap:18px;padding:22px;border-bottom:1px solid var(--line)}}.case-head p{{margin:8px 0 0;color:var(--muted);max-width:920px}}.gif-grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:1px;background:var(--line)}}.gif-card{{background:var(--surface);padding:18px;min-width:0}}.gif-card.deployable{{box-shadow:inset 0 3px 0 var(--teal)}}.gif-stage{{height:280px;display:grid;place-items:center;background:radial-gradient(circle,#fff 0,#ece8df 100%);border-radius:11px;overflow:hidden;margin:13px 0}}.gif-stage img{{max-width:100%;max-height:260px;image-rendering:auto}}.gif-meta{{display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px;color:var(--muted)}}.gif-meta b{{display:block;color:var(--ink);font:600 12px var(--mono)}}.evidence-details{{margin-top:12px;border-top:1px solid var(--soft);padding-top:10px}}details summary{{cursor:pointer;font-weight:650;color:var(--ink)}}details pre{{white-space:pre-wrap;word-break:break-word;background:#232925;color:#e8eee7;border-radius:8px;padding:12px;max-height:280px;overflow:auto;font-size:11px}}.assertion-list{{display:grid;gap:6px;margin-top:10px}}.assertion{{display:grid;grid-template-columns:22px 1fr auto;gap:8px;padding:8px;border-radius:7px;background:var(--soft);font-size:12px}}.assertion.pass strong{{color:var(--teal)}}.assertion.fail strong{{color:var(--red)}}.pair-strip{{padding:15px 20px;background:#f5efe5;border-top:1px solid var(--line);display:flex;flex-wrap:wrap;gap:18px;align-items:center}}.pair-strip strong{{font:500 24px var(--serif);color:var(--teal)}}.stage-flow{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;position:relative;margin:28px 0}}.stage-flow:before{{content:"";position:absolute;left:8%;right:8%;top:19px;border-top:1px solid var(--line-strong);z-index:-1}}.stage-step{{background:var(--paper);text-align:center}}.stage-step i{{display:grid;place-items:center;width:39px;height:39px;margin:auto;border-radius:50%;background:var(--teal);color:white;font:700 12px var(--mono)}}.stage-step b{{display:block;margin-top:10px}}.stage-step span{{display:block;color:var(--muted);font-size:12px}}.dag-shell{{background:var(--night);border-radius:15px;overflow:auto;padding:12px;color:#eef1eb}}#dag-svg{{display:block;min-width:900px;width:100%;height:340px}}.dag-edge{{stroke:#7d8a81;stroke-width:1.5;fill:none}}.dag-edge.merge{{stroke:#e1b85d;stroke-width:2.4}}.dag-node rect{{fill:#313c36;stroke:#77847a}}.dag-node text{{fill:white;font:10px var(--mono);pointer-events:none}}.dag-node.accepted rect{{stroke:#58c4a2;stroke-width:3}}.dag-node.rejected rect{{stroke:#d46c62;stroke-width:2}}.dag-node.seed rect{{stroke:#9da79f}}.candidate-table,.data-table{{width:100%;border-collapse:collapse}}.candidate-table th,.candidate-table td,.data-table th,.data-table td{{padding:11px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;font-size:13px}}.candidate-table th,.data-table th{{color:var(--muted);font-weight:600;background:#f7f4ed}}.delta.pos{{color:var(--teal);font-weight:700}}.delta.neg{{color:var(--red);font-weight:700}}.score-layout{{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px}}.score-bars{{display:grid;gap:16px}}.objective{{display:grid;grid-template-columns:150px 1fr;gap:14px;align-items:center}}.objective-name b{{display:block}}.objective-name small{{color:var(--muted)}}.bar-stack{{display:grid;gap:5px}}.bar-row{{display:grid;grid-template-columns:76px 1fr 55px;gap:8px;align-items:center;font-size:11px}}.bar-track{{height:8px;background:var(--soft);border-radius:99px;overflow:hidden}}.bar-fill{{height:100%;border-radius:99px}}.bar-row.no-skill .bar-fill{{background:#8b918c}}.bar-row.original .bar-fill{{background:var(--coral)}}.bar-row.candidate .bar-fill{{background:var(--teal)}}.score-note{{padding:18px;background:var(--surface);border:1px solid var(--line);border-radius:14px}}.score-note strong{{font:500 34px var(--serif);color:var(--teal)}}.trace{{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}}.trace-step{{position:relative;padding:16px;border:1px solid var(--line);border-radius:12px;background:var(--surface);min-height:190px}}.trace-step:not(:last-child):after{{content:"→";position:absolute;right:-14px;top:50%;z-index:2;width:18px;height:28px;background:var(--paper);text-align:center;color:var(--coral);font-size:20px}}.trace-step .num{{font:700 11px var(--mono);color:var(--coral)}}.trace-step p{{font-size:13px;color:var(--muted)}}.trace-step code{{font-size:11px;word-break:break-all}}.funnel{{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;align-items:end}}.funnel-step{{background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px;text-align:center}}.funnel-step strong{{font:500 38px var(--serif)}}.funnel-step span{{display:block;color:var(--muted);font-size:12px}}.runtime-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px}}.runtime-card{{border-top:2px solid var(--ink);padding:13px 3px}}.runtime-card strong{{font:500 30px var(--serif)}}.runtime-card span{{display:block;color:var(--muted);font-size:12px}}.alert{{padding:16px;border-radius:10px;background:var(--amber-soft);color:#6c4b19;border-left:3px solid var(--amber)}}.hash-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(270px,1fr));gap:9px}}.hash-card{{padding:13px;border:1px solid var(--line);border-radius:9px;background:var(--surface)}}.hash-card b{{display:block;font-size:12px}}.hash-card code{{display:block;color:var(--muted);font-size:10px;word-break:break-all;margin-top:5px}}.download{{display:grid;grid-template-columns:minmax(0,1.3fr) minmax(300px,.7fr);gap:20px;padding:24px;background:var(--night);color:#edf1eb;border-radius:16px}}.download h3{{font:500 31px var(--serif)}}.download p{{color:#bfc9c0}}.download .btn{{display:inline-block;text-decoration:none;background:#edf3ec;color:var(--night);border-color:#edf3ec}}.file-list{{font-size:11px;color:#cbd3cc;max-height:240px;overflow:auto}}.commands{{display:grid;gap:9px}}.command{{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px;align-items:center;padding:10px 12px;border:1px solid var(--line);border-radius:9px;background:#242a26;color:#eaf0e9}}.command code{{overflow:auto;white-space:nowrap;font-size:11px}}.copy{{border:1px solid #667069;background:transparent;color:white;border-radius:6px;padding:5px 8px;font-size:11px}}.limitations{{display:grid;gap:8px}}.limitation{{padding:12px 14px;background:#f8eee5;border-left:3px solid var(--coral);color:#69493e}}footer{{padding:34px clamp(22px,7vw,110px);border-top:1px solid var(--line);color:var(--muted);font-size:13px}}.noscript{{margin:20px;padding:16px;background:var(--red-soft);color:var(--red)}}@media(max-width:1050px){{.hero-grid{{grid-template-columns:1fr 1fr}}.graph-layout,.score-layout,.download{{grid-template-columns:1fr}}.graph-inspector{{border-left:0;border-top:1px solid #465048;max-height:300px}}.gif-grid{{grid-template-columns:1fr}}.trace{{grid-template-columns:1fr 1fr}}.trace-step:after{{display:none}}}}@media(max-width:700px){{.topbar nav{{display:none}}.hero-grid,.funnel,.stage-flow,.trace{{grid-template-columns:1fr}}.stage-flow:before{{display:none}}.objective{{grid-template-columns:1fr}}.gif-stage{{height:230px}}section{{padding-top:52px}}}}@media print{{.topbar,.toolbar,.case-tabs,.copy,.btn.secondary{{display:none!important}}body{{background:white}}section{{break-inside:avoid}}.panel,.status-card{{box-shadow:none}}}}
</style></head><body>
<div class="topbar"><span class="brand">GEPASE / R5</span><nav aria-label="报告目录"><a href="#overview">结论</a><a href="#package-graph">Package Graph</a><a href="#gif-comparison">GIF 对照</a><a href="#evolution">进化 DAG</a><a href="#scores">评分</a><a href="#traceability">追溯</a><a href="#gate-funnel">Gate</a><a href="#runtime">运行时间</a><a href="#provenance">来源</a><a href="#deployable">部署包</a></nav></div>
<header class="hero"><div class="eyebrow">Graph-Enhanced Package-Aware Skill Evolution</div><h1>不是“看起来更好”，而是一次可以沿证据链复算的 Skill 进化。</h1><p class="lead">{title}。本页只消费已经封存的 R2–R4 evidence：不重新运行 Executor，不重新搜索候选，也不调用 Headless/API。每个结论都保留到真实 GIF、评分向量、Package Graph、Patch 与 GateDecision 的路径。</p><div class="hero-grid"><div class="metric"><strong class="delta pos">+{delta:.4f}</strong><span>frozen validation mean paired delta</span></div><div class="metric"><strong>{headline['validation_wins']}/{headline['validation_wins']+headline['validation_ties']+headline['validation_losses']}</strong><span>held-out case 严格胜出</span></div><div class="metric"><strong>{success_count}/6</strong><span>R5 成功 Gate</span></div><div class="metric"><strong>{package_file_count}</strong><span>deployable Package 文件</span></div></div><div class="scope-note"><b>结论边界：</b>当前证明的是一个公开 Skill、一个 frozen EvalPlan、一个模型快照与一次搜索运行上的严格提升；它不是跨 Skill、跨模型或 package-aware 优于单文件优化的普遍性结论。</div></header>
<main>
<section id="overview"><div class="section-kicker">01 / Outcome</div><h2>三层结论，分开陈述</h2><p class="section-intro">代码已经实现、工程机制通过测试、算法效果得到验证是三件不同的事。本次 R5 不扩大结论，只把已有证据组织成可检查的最终呈现。</p><div class="status-grid"><div class="status-card"><b>代码实现</b><p>Python Core/CLI 从 sealed evidence 生成、封存并复验静态报告；报告本身不拥有候选、评分或 Gate 状态。</p></div><div class="status-card"><b>工程机制</b><p>R2–R4 artifact seal、真实 GIF/trace、角色隔离、图定位、typed Patch、strict Gate 与同 Package Merge 均有机器证据。</p></div><div class="status-card"><b>算法效果</b><p><code>{candidate}</code> 在 3 个 held-out case 上全部胜出，mean paired delta 为 <b>+{delta:.5f}</b>，进入 deployable frontier。</p></div></div><div id="success-gates" class="cards" style="margin-top:14px"></div></section>
<section id="package-graph"><div class="section-kicker">02 / Package</div><h2>完整 Package 不是一段被截断的 Prompt</h2><p class="section-intro">图同时保留原始 snapshot 与 deployable snapshot。绿色表示真实执行访问过的节点，珊瑚色表示 deployable edit 的影响节点，金色表示 merge contribution；点击节点可查看稳定 node id、路径、hash 与 metadata。</p><div class="panel"><div class="toolbar"><label>图版本 <select id="graph-version"><option value="original">原始 Package</option><option value="deployable">Deployable Package</option></select></label><label><input id="graph-fine" type="checkbox"> 显示细粒度节点</label><input id="graph-search" type="search" placeholder="搜索 path、label、node id…"><button class="btn secondary" id="graph-reset">重置</button><div class="legend"><span><i style="border-color:#62c5a7"></i>observed</span><span><i style="border-color:#ee8b68"></i>modified</span><span><i style="border-color:#e5bd66"></i>merge</span></div></div><div class="graph-layout"><div class="graph-canvas"><svg id="package-svg" role="img" aria-label="Skill Package dependency graph"></svg></div><aside class="graph-inspector"><h3>NODE INSPECTOR</h3><p id="graph-counts"></p><pre id="graph-detail">点击节点查看详细 provenance。</pre></aside></div></div></section>
<section id="gif-comparison"><div class="section-kicker">03 / Real outputs</div><h2>同一任务，三种真实产物</h2><p class="section-intro">no-skill 与 original 来自 R3 的隔离 reference anchor；deployable 来自 R4 的 fresh candidate execution。下方 GIF 是原始 artifact 的哈希校验副本，不是截图或框架合成结果。逐 case 可展开 E3、Grader、transcript、trace 与 Package access。</p><div class="case-tabs" id="case-tabs"></div><div id="case-view"></div></section>
<section id="evolution"><div class="section-kicker">04 / Evolution</div><h2>GEPA 反馈、多分支与同 Package Merge</h2><p class="section-intro">候选来自同一个 source snapshot 和 lineage root。多父表示同一 Skill 的两个候选版本，不允许跨 Skill 合并；merge child 仍然重新经过完整 Gate 0–3。</p><div id="stage-flow" class="stage-flow"></div><div class="dag-shell"><svg id="dag-svg" role="img" aria-label="Candidate lineage DAG"></svg></div><div class="panel" style="margin-top:16px;overflow:auto"><table class="candidate-table"><thead><tr><th>候选</th><th>Operator / 父代</th><th>Train Δ</th><th>Validation Δ</th><th>结果</th><th>原因</th></tr></thead><tbody id="candidate-rows"></tbody></table></div></section>
<section id="scores"><div class="section-kicker">05 / Scores</div><h2>六维向量，不让 assertion pass rate 冒充综合质量</h2><p class="section-intro">横条展示 3 个 validation case 的六维均值。Skill gain 以 no-skill 为零基线；strict Gate 另外使用 candidate vs frozen original 的逐 case paired delta。选择 case 可查看具体向量。</p><div class="toolbar"><label>评分范围 <select id="score-case"><option value="mean">Validation 均值</option></select></label></div><div class="score-layout"><div class="panel panel-pad"><div id="score-bars" class="score-bars"></div></div><aside class="score-note"><span class="pill ok">STRICT HELD-OUT</span><p><strong>+{delta:.5f}</strong><br>mean paired delta</p><p id="score-stats"></p><p style="color:var(--muted);font-size:13px">Primary paired score 由 frozen correctness/quality policy 与 fresh blind Comparator 构成；它不等于六维向量的简单平均。</p></aside></div><div class="panel" style="margin-top:16px;overflow:auto"><table class="data-table"><thead><tr><th>Case / category</th><th>Candidate score</th><th>Original score</th><th>Paired Δ</th><th>AB/BA</th><th>Risk</th></tr></thead><tbody id="paired-rows"></tbody></table></div></section>
<section id="traceability"><div class="section-kicker">06 / Causality</div><h2>失败如何变成一个有界、可回滚的 PackagePatch</h2><p class="section-intro">下面不是事后故事，而是 raw evidence 中保存的 failure → graph node → operation → graph diff → Gate 路径。accepted edit 实际位于 SKILL.md；跨文件编辑能力并不因此被包装成已验证效果。</p><div id="trace-view" class="trace"></div><details style="margin-top:18px"><summary>查看完整 Patch operations 与 evidence refs</summary><pre id="trace-raw"></pre></details></section>
<section id="gate-funnel"><div class="section-kicker">07 / Strict Gate</div><h2>值得保留的修改，必须经得住 held-out</h2><p class="section-intro">Train 上看起来有希望不等于可部署。恢复分支 C 在 train 为正但 validation timeout 后被拒；Merge 总均值为正，仍因 emoji_animation 越过预注册 floor 被拒。</p><div id="funnel" class="funnel"></div><div class="panel" style="margin-top:18px;overflow:auto"><table class="data-table"><thead><tr><th>Rejected candidate</th><th>Failed Gate</th><th>Score Δ</th><th>Nodes / operations</th><th>Reason</th></tr></thead><tbody id="rejected-rows"></tbody></table></div></section>
<section id="runtime"><div class="section-kicker">08 / Runtime</div><h2>运行时间与角色使用量</h2><p class="section-intro">这里呈现端到端墙钟、累计 Agent duration、调用数、estimated token、cache 与失败，便于理解运行规模和后续工程优化。宿主没有 enqueue timestamp，因此 queue wait 保持 unobserved。</p><div id="runtime-cards" class="runtime-grid"></div><div class="panel" style="margin-top:16px;overflow:auto"><table class="data-table"><thead><tr><th>角色</th><th>调用</th><th>累计时长</th><th>Input token</th><th>Output token</th><th>Tool calls</th></tr></thead><tbody id="role-rows"></tbody></table></div></section>
<section id="provenance"><div class="section-kicker">09 / Provenance</div><h2>来源、hash 与结论边界</h2><p class="section-intro">R5 对三个上游 run 先做 artifact-index 全量校验，再复制少量展示资产。报告生成时 Agent/API 调用均为 0，所有数字可由 report-data 与 raw evidence 复算。</p><div id="hash-grid" class="hash-grid"></div><h3 style="margin:28px 0 12px">必须保留的限制</h3><div id="limitations" class="limitations"></div></section>
<section id="deployable"><div class="section-kicker">10 / Deployable package</div><h2>唯一进入 frontier 的完整 Package</h2><div class="download"><div><span class="pill ok">DEPLOYABLE FRONTIER</span><h3>{candidate}</h3><p>归档包含 7 个原始 Package 文件及其 mode；每个文件 hash 与 Candidate manifest 一致。accepted change 仅涉及 <code>SKILL.md</code>，原始公开 Package 未被原地修改。</p><a class="btn" href="{html.escape(str(deployable['archive_path']))}" download>下载 deployable Package</a><p class="mono" style="font-size:11px;word-break:break-all">SHA-256 · {html.escape(str(deployable['archive_sha256']))}</p></div><div><h3 style="font-size:17px">Package files</h3><div id="deployable-files" class="file-list"></div></div></div></section>
<section id="reproduce"><div class="section-kicker">11 / Reproduce</div><h2>从 sealed evidence 重新生成这份报告</h2><p class="section-intro">下面前三条命令只读取 R2–R4 artifact；不会重跑 R3/R4、不会搜索候选、不会调用 Headless/API。完整上游 Agent-native 命令保存在 R3/R4 commands.log 中。</p><div id="commands" class="commands"></div></section>
</main><noscript><div class="noscript">本报告的证据数据已经内嵌，但交互式图、表格与 GIF 选择需要启用 JavaScript。</div></noscript><footer>GEPASE R5 · sealed evidence report · 自包含 CSS/JavaScript · 无外部分析脚本。</footer>
<script type="application/json" id="report-data">{_script_json(data)}</script>
<script>
const D=JSON.parse(document.getElementById('report-data').textContent);const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
const fmt=(n,d=4)=>n==null?'—':Number(n).toFixed(d);const delta=n=>`<span class="delta ${{Number(n)>=0?'pos':'neg'}}">${{Number(n)>=0?'+':''}}${{fmt(n,5)}}</span>`;
const ms=v=>{{const n=Number(v);if(n>=3600000)return `${{(n/3600000).toFixed(2)}} h`;if(n>=60000)return `${{(n/60000).toFixed(1)}} min`;return `${{(n/1000).toFixed(1)}} s`}};const bytes=v=>{{const n=Number(v);return n>=1048576?`${{(n/1048576).toFixed(2)}} MiB`:`${{(n/1024).toFixed(1)}} KiB`}};
const successLabels={{end_to_end_complete:'全链完成',real_artifacts_verified:'真实产物已核验',strict_improvement_observed:'held-out 严格提升',merge_path_exercised:'多父 Merge 已执行',report_reproducible:'报告可复算',release_candidate_ready:'Release candidate ready'}};
$('#success-gates').innerHTML=Object.entries(D.success_gates).map(([k,v])=>`<div class="status-card"><span class="pill ${{v?'ok':'bad'}}">${{v?'✓ PASS':'✕ FAIL'}}</span><h3 style="margin-top:10px">${{esc(successLabels[k]||k)}}</h3><p class="mono">${{esc(k)}} = ${{v}}</p></div>`).join('');
function graphData(){{return $('#graph-version').value==='deployable'?D.package.deployable_graph:D.package.original_graph}}
function drawGraph(){{const graph=graphData(),fine=$('#graph-fine').checked,q=$('#graph-search').value.trim().toLowerCase(),access=D.package.access_counts||{{}},modified=new Set(D.package.modified_node_ids||[]),merge=new Set(D.package.merge_node_ids||[]);let nodes=graph.nodes.filter(n=>fine||['package','file','dependency'].includes(n.kind));if(q)nodes=nodes.filter(n=>JSON.stringify(n).toLowerCase().includes(q));const visible=new Set(nodes.map(n=>n.node_id)),cols=fine?8:4,cellW=fine?180:260,cellH=fine?78:105,pos=new Map(nodes.map((n,i)=>[n.node_id,[110+(i%cols)*cellW,68+Math.floor(i/cols)*cellH]])),width=Math.max(920,cols*cellW),height=Math.max(620,130+Math.ceil(nodes.length/cols)*cellH),svg=$('#package-svg');svg.setAttribute('viewBox',`0 0 ${{width}} ${{height}}`);svg.innerHTML='';graph.edges.filter(e=>visible.has(e.source)&&visible.has(e.target)).forEach(e=>{{const p1=pos.get(e.source),p2=pos.get(e.target);if(!p1||!p2)return;const line=document.createElementNS('http://www.w3.org/2000/svg','line');for(const [k,v] of Object.entries({{x1:p1[0],y1:p1[1],x2:p2[0],y2:p2[1]}}))line.setAttribute(k,v);line.setAttribute('class',`edge ${{access[e.source]||access[e.target]?'observed':''}}`);svg.appendChild(line)}});nodes.forEach(n=>{{const [x,y]=pos.get(n.node_id),g=document.createElementNS('http://www.w3.org/2000/svg','g');let cls='node';if(access[n.node_id])cls+=' accessed';if(modified.has(n.node_id))cls+=' modified';if(merge.has(n.node_id))cls+=' merge';g.setAttribute('class',cls);g.setAttribute('transform',`translate(${{x}},${{y}})`);g.setAttribute('tabindex','0');g.innerHTML=`<rect x="-82" y="-27" width="164" height="54" rx="8"></rect><text text-anchor="middle" y="-4">${{esc((n.label||n.path||n.node_id).slice(0,24))}}</text><text class="kind" text-anchor="middle" y="14">${{esc(n.kind)}} · access ${{access[n.node_id]||0}}</text>`;const show=()=>{{$$('.node').forEach(x=>x.classList.remove('active'));g.classList.add('active');$('#graph-detail').textContent=JSON.stringify({{...n,observed_access_count:access[n.node_id]||0,deployable_modified:modified.has(n.node_id),merge_contribution:merge.has(n.node_id)}},null,2)}};g.addEventListener('click',show);g.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' ')show()}});svg.appendChild(g)}});$('#graph-counts').textContent=`${{nodes.length}} / ${{graph.nodes.length}} nodes · ${{graph.edges.length}} edges · ${{graph.diagnostics.length}} diagnostics`}}
['graph-version','graph-fine','graph-search'].forEach(id=>$('#'+id).addEventListener('input',drawGraph));$('#graph-reset').onclick=()=>{{$('#graph-search').value='';$('#graph-fine').checked=false;drawGraph()}};drawGraph();
const variantLabels={{'no-skill':'No-skill','original':'Original Skill','candidate':'Deployable'}};let activeCase=0;
function assertionHTML(a){{return `<div class="assertion ${{a.passed?'pass':'fail'}}"><strong>${{a.passed?'✓':'×'}}</strong><span><b>${{esc(a.assertion_id)}}</b><br>${{esc(a.detail)}}</span><span>${{Number(a.weight).toFixed(2)}}</span></div>`}}
function variantCard(v){{const x=v.variants[v.key],ins=x.inspection||{{}},access=x.package_access||[];return `<article class="gif-card ${{v.key==='candidate'?'deployable':''}}"><div style="display:flex;justify-content:space-between;gap:8px"><h3>${{variantLabels[v.key]}}</h3><span class="pill ${{v.key==='candidate'?'ok':''}}">${{esc(x.work_id.slice(0,13))}}…</span></div><div class="gif-stage"><img src="${{esc(x.asset_path)}}" alt="${{esc(variantLabels[v.key]+' '+v.task)}}"></div><div class="gif-meta"><span><b>E3 correctness</b>${{fmt(x.deterministic_score,3)}} · ${{x.assertions_passed}}/${{x.assertions_total}}</span><span><b>Independent quality</b>${{fmt(x.grader_score,3)}}</span><span><b>Artifact</b>${{bytes(x.artifact_size_bytes)}}</span><span><b>Runtime</b>${{ms(x.usage.duration_ms)}}</span></div><div class="evidence-details"><details><summary>内容级 assertions</summary><div class="assertion-list">${{x.assertions.map(assertionHTML).join('')}}</div></details><details><summary>Independent Grader</summary><p>${{esc(x.grader_feedback_zh)}}</p></details><details><summary>Transcript</summary><pre>${{esc(x.transcript)}}</pre></details><details><summary>Observed trace / Package access</summary><pre>${{esc(JSON.stringify({{observed_trace:x.observed_trace,package_access:access}},null,2))}}</pre></details><details><summary>Raw evidence refs</summary><pre>${{esc(JSON.stringify(x.raw_refs,null,2))}}</pre></details></div></article>`}}
function renderCase(index){{activeCase=index;$$('.case-tab').forEach((b,i)=>b.classList.toggle('active',i===index));const c=D.validation_cases[index],p=c.candidate_vs_original;$('#case-view').innerHTML=`<article class="panel"><div class="case-head"><div><span class="pill">${{esc(c.case_family)}} · ${{esc(c.risk)}} risk</span><h3 style="margin-top:10px">${{esc(c.requested_output.description_zh)}}</h3><p>${{esc(c.prompt)}}</p></div><button class="btn secondary" id="replay-gifs">同步重播 GIF</button></div><div class="gif-grid">${{['no-skill','original','candidate'].map(key=>variantCard({{...c,key,task:c.task_id}})).join('')}}</div><div class="pair-strip"><span>Deployable vs frozen original</span><strong>${{delta(p.paired_delta)}}</strong><span>Correctness Δ ${{fmt(p.correctness_delta,3)}} · Quality Δ ${{fmt(p.quality_delta,3)}} · Comparator ${{c.comparator.ab_candidate_outcome}} / ${{c.comparator.ba_candidate_outcome}}</span></div></article>`;$('#replay-gifs').onclick=()=>$$('#case-view img').forEach(img=>{{const src=img.src;img.src='';requestAnimationFrame(()=>img.src=src)}});const scoreSelect=$('#score-case');if([...scoreSelect.options].some(option=>option.value===c.task_id)){{scoreSelect.value=c.task_id;renderScores()}}}}
$('#case-tabs').innerHTML=D.validation_cases.map((c,i)=>`<button class="case-tab ${{i===0?'active':''}}" data-i="${{i}}">${{esc(c.requested_output.filename)}}</button>`).join('');$$('.case-tab').forEach(b=>b.onclick=()=>renderCase(Number(b.dataset.i)));renderCase(0);
$('#stage-flow').innerHTML=D.method.stages.map(s=>`<div class="stage-step"><i>${{esc(s.stage)}}</i><b>${{esc(s.label)}}</b><span>${{esc(s.status)}}</span></div>`).join('');
function drawDAG(){{const svg=$('#dag-svg'),nodes=D.method.lineage_nodes,byGen={{}};nodes.forEach(n=>(byGen[n.generation]??=[]).push(n));const maxGen=Math.max(...nodes.map(n=>n.generation)),pos=new Map();Object.entries(byGen).forEach(([g,rows])=>rows.forEach((n,i)=>pos.set(n.candidate_id,[120+Number(g)*(760/Math.max(1,maxGen)),58+i*(245/Math.max(1,rows.length-1||1))])));svg.setAttribute('viewBox','0 0 1000 340');svg.innerHTML='<defs><marker id="arrow" markerWidth="7" markerHeight="7" refX="6" refY="3.5" orient="auto"><path d="M0,0 L7,3.5 L0,7 Z" fill="#7d8a81"/></marker></defs>';D.method.lineage_edges.forEach(e=>{{const a=pos.get(e.source),b=pos.get(e.target);if(!a||!b)return;const p=document.createElementNS('http://www.w3.org/2000/svg','path');p.setAttribute('d',`M${{a[0]+78}},${{a[1]}} C${{a[0]+150}},${{a[1]}} ${{b[0]-150}},${{b[1]}} ${{b[0]-78}},${{b[1]}}`);p.setAttribute('class',`dag-edge ${{nodes.find(n=>n.candidate_id===e.target)?.operator.includes('merge')?'merge':''}}`);p.setAttribute('marker-end','url(#arrow)');svg.appendChild(p)}});nodes.forEach(n=>{{const [x,y]=pos.get(n.candidate_id),g=document.createElementNS('http://www.w3.org/2000/svg','g');g.setAttribute('class',`dag-node ${{n.verdict}}`);g.setAttribute('transform',`translate(${{x}},${{y}})`);g.innerHTML=`<rect x="-78" y="-29" width="156" height="58" rx="9"></rect><text text-anchor="middle" y="-6">${{esc(n.label)}}</text><text text-anchor="middle" y="12">${{esc(n.short_id)}} · gen ${{n.generation}}</text>`;svg.appendChild(g)}})}}drawDAG();
$('#candidate-rows').innerHTML=D.candidates.sort((a,b)=>a.generation-b.generation||a.label.localeCompare(b.label)).map(c=>`<tr><td><b>${{esc(c.label)}}</b><br><code>${{esc(c.short_id)}}</code></td><td>${{esc(c.operator)}}<br><span class="mono">${{c.parent_ids.map(x=>x.slice(-8)).join(' + ')}}</span></td><td>${{delta(c.train_mean_delta)}}<br>${{c.train_strict_wins}} strict wins</td><td>${{c.validation_mean_delta==null?'未运行':delta(c.validation_mean_delta)}}<br>${{c.validation_wins}}W / ${{c.validation_ties}}T / ${{c.validation_losses}}L</td><td><span class="pill ${{c.verdict==='accepted'?'ok':'bad'}}">${{esc(c.verdict)}}</span></td><td>${{esc(c.reason_codes.join(', '))}}</td></tr>`).join('');
D.validation_cases.forEach(c=>$('#score-case').insertAdjacentHTML('beforeend',`<option value="${{esc(c.task_id)}}">${{esc(c.requested_output.filename)}}</option>`));
function normalizedObjective(k,v){{return k==='skill_gain'?Math.max(0,Math.min(1,(Number(v)+1)/2)):Math.max(0,Math.min(1,Number(v)))}}
function renderScores(){{const selected=$('#score-case').value,rows=selected==='mean'?D.scores.validation_means:Object.fromEntries(['no-skill','original','candidate'].map(v=>[v,D.validation_cases.find(c=>c.task_id===selected).variants[v].score_vector]));$('#score-bars').innerHTML=D.scores.objective_order.map(k=>`<div class="objective"><div class="objective-name"><b>${{esc(D.scores.objective_labels_zh[k])}}</b><small>${{esc(k)}}</small></div><div class="bar-stack">${{['no-skill','original','candidate'].map(v=>`<div class="bar-row ${{v}}"><span>${{variantLabels[v]}}</span><div class="bar-track"><div class="bar-fill" style="width:${{normalizedObjective(k,rows[v][k])*100}}%"></div></div><code>${{fmt(rows[v][k],3)}}</code></div>`).join('')}}</div></div>`).join('');$('#score-stats').innerHTML=`3/3 wins · 0 ties · 0 losses<br>95% bootstrap CI ${{fmt(D.headline.bootstrap_95_ci[0],5)}} – ${{fmt(D.headline.bootstrap_95_ci[1],5)}}<br>delta variance ${{fmt(D.scores.delta_variance,6)}}`}}$('#score-case').onchange=renderScores;renderScores();
$('#paired-rows').innerHTML=D.validation_cases.map(c=>{{const p=c.candidate_vs_original;return `<tr><td><b>${{esc(c.requested_output.filename)}}</b><br>${{esc(c.case_family)}}</td><td>${{fmt(p.candidate_score,5)}}</td><td>${{fmt(p.reference_score,5)}}</td><td>${{delta(p.paired_delta)}}</td><td>${{esc(c.comparator.ab_candidate_outcome)}} / ${{esc(c.comparator.ba_candidate_outcome)}}</td><td>${{esc(c.risk)}}</td></tr>`}}).join('');
const t=D.traceability,fail=t.failure||{{issue_zh:'未找到匹配 failure',confidence:0}},op=t.operations[0]||{{}},diff=t.graph_diff||{{}};const traceSteps=[['01 FAILURE',fail.issue_zh||'',`confidence ${{fmt(fail.confidence,2)}}`],['02 GRAPH NODE',(t.target_nodes||[]).map(n=>n.label||n.path||n.node_id).join('\\n'),(t.target_nodes||[]).map(n=>n.node_id).join('\\n')],['03 PACKAGE PATCH',t.patch_summary,`${{t.patch_id}}\\n${{op.op||''}}`],['04 GRAPH DIFF',`${{(t.file_changes||[]).map(x=>x.path+' · '+x.change).join('\\n')}}`,`+${{(diff.added_nodes||[]).length}} nodes · +${{(diff.added_edges||[]).length}} edges · blast ${{diff.blast_radius||0}}`],['05 STRICT GATE',(t.gate_path||[]).map(x=>`${{x.level}} · ${{x.outcome}}`).join('\\n'),D.headline.validation_mean_delta>0?`held-out +${{fmt(D.headline.validation_mean_delta,5)}}`:'rejected']];$('#trace-view').innerHTML=traceSteps.map(([n,p,c])=>`<div class="trace-step"><span class="num">${{n}}</span><p>${{esc(p).replaceAll('\\n','<br>')}}</p><code>${{esc(c).replaceAll('\\n','<br>')}}</code></div>`).join('');$('#trace-raw').textContent=JSON.stringify({{operations:t.operations,evidence_refs:t.evidence_refs,gate_path:t.gate_path}},null,2);
const funnelLabels={{proposed:'候选',gate_0_passed:'Gate 0',gate_1_passed:'Gate 1',gate_2_passed:'Gate 2',gate_3_passed:'Gate 3',accepted:'Frontier'}};$('#funnel').innerHTML=Object.entries(D.gates.funnel).map(([k,v])=>`<div class="funnel-step"><strong>${{v}}</strong><span>${{funnelLabels[k]||k}}</span></div>`).join('');
$('#rejected-rows').innerHTML=D.gates.rejected_edits.map(r=>`<tr><td><b>${{esc((D.method.candidate_labels||{{}})[r.candidate_id]||r.candidate_id)}}</b><br><code>${{esc((r.candidate_id||'').slice(-12))}}</code></td><td>${{esc(r.failed_gate)}}</td><td>${{r.score_delta==null?'—':delta(r.score_delta)}}</td><td>${{esc(r.node_ids.join(', '))}}<br>${{esc(r.operation_signatures.join(', '))}}</td><td>${{esc(r.reason_codes.join(', '))}}</td></tr>`).join('');
const R=D.runtime,U=R.usage;const runtimeCards=[['端到端墙钟',ms(R.wall_clock_duration_ms)],['累计 Agent 时长',ms(R.cumulative_agent_duration_ms)],['Agent-native 调用',U.agent_calls],['Estimated tokens',Number(U.estimated_tokens).toLocaleString()],['Reference cache',`${{U.cache_hits}} hit / ${{U.cache_misses}} miss`],['Evaluation failure',R.evaluation_failures],['Queue wait','unobserved']];$('#runtime-cards').innerHTML=runtimeCards.map(([a,b])=>`<div class="runtime-card"><strong>${{esc(b)}}</strong><span>${{esc(a)}}</span></div>`).join('');$('#role-rows').innerHTML=Object.entries(R.roles).map(([name,x])=>`<tr><td>${{esc(name)}}</td><td>${{x.calls}}</td><td>${{ms(x.duration_ms)}}</td><td>${{Number(x.input_tokens).toLocaleString()}}</td><td>${{Number(x.output_tokens).toLocaleString()}}</td><td>${{x.tool_calls}}</td></tr>`).join('');
const hashRows=[['Source repository',D.package.source_url],['Source commit',D.package.source_commit],['Upstream tree',D.package.source_tree],['Source license',D.package.source_license],['Original Package snapshot',D.package.source_snapshot_hash],['Deployable Package snapshot',D.package.deployable_snapshot_hash],['Frozen EvalPlan',D.provenance.frozen_plan_hash],['Scoring policy',D.provenance.scoring_policy_sha256],['Reference key',D.provenance.reference_key_hash],['R4 audit',D.provenance.r4_audit_sha256],['Deployable archive',D.deployable.archive_sha256],...Object.entries(D.provenance.stages).map(([k,v])=>[`${{k}} stage report`,v.stage_report_sha256])];$('#hash-grid').innerHTML=hashRows.map(([k,v])=>`<div class="hash-card"><b>${{esc(k)}}</b><code>${{esc(v)}}</code></div>`).join('');$('#limitations').innerHTML=D.limitations_zh.map(x=>`<div class="limitation">${{esc(x)}}</div>`).join('');
$('#deployable-files').innerHTML=D.deployable.files.map(f=>`<div>${{esc(f.path)}}<br><span class="mono">${{esc(f.sha256.slice(0,16))}}… · mode ${{f.mode}}</span></div>`).join('<br>');
const commandRows=[D.commands.rebuild_report,D.commands.verify_report,D.commands.recompute_gates,...D.commands.verify_upstream];$('#commands').innerHTML=commandRows.map((c,i)=>`<div class="command"><code>${{esc(c)}}</code><button class="copy" data-i="${{i}}">复制</button></div>`).join('');$$('.copy').forEach(b=>b.onclick=async()=>{{try{{await navigator.clipboard.writeText(commandRows[Number(b.dataset.i)]);b.textContent='已复制'}}catch{{b.textContent='请手动复制'}}}});
const sections=$$('main section');const nav=$$('.topbar nav a');const observer=new IntersectionObserver(entries=>entries.forEach(e=>{{if(e.isIntersecting)nav.forEach(a=>a.classList.toggle('active',a.getAttribute('href')==='#'+e.target.id))}}),{{rootMargin:'-25% 0px -65%'}});sections.forEach(s=>observer.observe(s));
</script></body></html>"""


def _render_outcome_report_classic(data: dict[str, Any]) -> str:
    """Render a compact report for complete and budget-incomplete outcomes."""

    title = html.escape(str(data["title_zh"]))
    outcome = str(data["outcome"])
    outcome_labels = {
        "strict_improvement": "找到 held-out strict improvement",
        "no_strict_improvement": "未找到 strict improvement",
        "budget_incomplete": "预算检查点：搜索尚未完成",
    }
    frontier = list(data["deployable_frontier"])
    candidates = list(data["candidates"])
    gallery = list(data.get("evidence_gallery", []))
    merge = dict(data["merge"])
    runtime = dict(data["runtime"])
    policy_evaluation = dict(data.get("policy_evaluation", {}))
    frontier_ranking = dict(data.get("frontier_ranking", {}))
    frontier_html = "".join(
        (
            "<article><h3>"
            + html.escape(str(item["candidate_id"]))
            + "</h3><p>"
            + ("临时验证证据" if item["provisional"] else "可部署 frontier")
            + "</p><code>"
            + html.escape(str(item["archive_sha256"]))
            + "</code><p><a href=\""
            + html.escape(str(item["archive_path"]))
            + "\">下载确定性 Package 归档</a></p></article>"
        )
        for item in frontier
    )
    if not frontier_html:
        frontier_html = (
            "<article><h3>没有可部署 Package 归档</h3>"
            "<p>完整证据链中没有通过 held-out strict Gate 的候选; "
            "报告不会为负结果合成业务产物。</p></article>"
        )
    if not policy_evaluation:
        candidate_rows = "".join(
            "<tr><td>"
            + html.escape(str(item["candidate_id"]))
            + "</td><td>"
            + html.escape(str(item["gate_status"]))
            + "</td><td>"
            + html.escape(str(item["train_mean_delta"]))
            + f"<br><small>{item.get('train_wins', 0)}W / {item.get('train_ties', 0)}T / {item.get('train_losses', 0)}L</small>"
            + "</td><td>"
            + html.escape(str(item["validation_mean_delta"]))
            + f"<br><small>{item.get('validation_wins', 0)}W / {item.get('validation_ties', 0)}T / {item.get('validation_losses', 0)}L</small>"
            + "</td><td>"
            + html.escape(", ".join(item["rejection_reasons"]) or "—")
            + "<details><summary>六维、Patch 与 Graph path</summary><pre>"
            + html.escape(json.dumps(item, ensure_ascii=False, indent=2))
            + "</pre></details></td></tr>"
            for item in candidates
        )
    else:
        candidate_rows_parts: list[str] = []
        for item in candidates:
            relative = item.get("relative_efficiency") or {}
            ratio = relative.get("relative_cost_ratio")
            score = relative.get("relative_efficiency_score")
            relative_summary = (
                f"ratio={float(ratio):.4f}<br>score={float(score):.4f}"
                if ratio is not None and score is not None
                else "unavailable / inconclusive"
            )
            if item.get("display_rank") is not None:
                relative_summary += (
                    f"<br>layer {item['pareto_layer']} / rank {item['display_rank']}"
                )
            candidate_rows_parts.append(
                "<tr><td>"
                + html.escape(str(item["candidate_id"]))
                + "</td><td>"
                + html.escape(str(item["gate_status"]))
                + "</td><td>"
                + html.escape(str(item["train_mean_delta"]))
                + f"<br><small>{item.get('train_wins', 0)}W / {item.get('train_ties', 0)}T / {item.get('train_losses', 0)}L</small>"
                + "</td><td>"
                + html.escape(str(item["validation_mean_delta"]))
                + f"<br><small>{item.get('validation_wins', 0)}W / {item.get('validation_ties', 0)}T / {item.get('validation_losses', 0)}L</small>"
                + "</td><td>"
                + relative_summary
                + "</td><td>"
                + html.escape(", ".join(item["rejection_reasons"]) or "—")
                + "<details><summary>六维、相对效率、Patch 与 Graph path</summary><pre>"
                + html.escape(json.dumps(item, ensure_ascii=False, indent=2))
                + "</pre></details></td></tr>"
            )
        candidate_rows = "".join(candidate_rows_parts)
    gallery_html = "".join(
        "<article class=\"gif-card\"><h3>"
        + html.escape(str(item["task_id"]))
        + "</h3><p>"
        + html.escape(
            f"{item['split']} · {item['variant']}"
            + (f" · {item['candidate_id']}" if item.get("candidate_id") else "")
            + f" · {item['execution_status']}"
            + (f" ({item['failure_kind']})" if item.get("failure_kind") else "")
        )
        + "</p><img src=\""
        + html.escape(str(item["report_path"]))
        + "\" alt=\""
        + html.escape(str(item["label_zh"]))
        + "\"><details><summary>原始证据</summary><code>"
        + html.escape(str(item["source_ref"]))
        + "</code><br><code>sha256: "
        + html.escape(str(item["sha256"]))
        + "</code></details></article>"
        for item in gallery
    )
    if not gallery_html:
        gallery_html = "<p>当前结局没有 Core-accepted task-native GIF。</p>"
    pending = ", ".join(str(item) for item in data["pending_work_ids"]) or "无"
    merge_rejections = json.dumps(
        merge.get("rejection_reason_counts", {}), ensure_ascii=False
    )
    if policy_evaluation.get("policy_id") == "relative_efficiency_v2":
        efficiency_html = (
            "<div class=\"cards\"><article><h3>Original Skill 基准</h3>"
            "<p>逐 held-out task 以同一 frozen reference 的 original Skill 为分母。"
            "默认比较 duration_ms 与 tool_calls；token 仅在 measurement kind 相同且"
            "双方可用时加入。</p></article><article><h3>稳健聚合</h3>"
            "<p>每轴先取 task ratio 中位数，再对可用轴等权平均。资源分数为 "
            "<code>1/(1+ratio)</code>；ratio 0.5 / 1 / 2 对应约 0.667 / 0.5 / "
            "0.333。</p></article><article><h3>极端退化线</h3><p>只有可比聚合成本达到或超过 "
            + html.escape(
                str(policy_evaluation.get("max_relative_cost_ratio", "unavailable"))
            )
            + " 倍 original 时才触发 "
            "<code>extreme_relative_cost_regression</code>。产物大小默认仅报告一次。"
            "</p></article></div><div class=\"panel\"><h3>Policy、逐轴证据与 Pareto 排名</h3>"
            "<p><strong>v1 TaskScoreVector.efficiency</strong> 仅作为“v1 绝对预算诊断”"
            "保留，不参与 v2 混算。</p><pre>"
            + html.escape(
                json.dumps(
                    {
                        "policy": policy_evaluation,
                        "frontier_ranking": frontier_ranking,
                        "candidate_relative_evidence": {
                            item["candidate_id"]: item.get("relative_efficiency")
                            for item in candidates
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre></div>"
        )
        efficiency_section = (
            '<section id="efficiency"><div class="eyebrow">RELATIVE EFFICIENCY</div>'
            f"<h2>资源效率证据与策略边界</h2>{efficiency_html}</section>\n"
        )
        candidate_header = "<th>相对效率</th><th>拒绝理由</th>"
    else:
        efficiency_section = ""
        candidate_header = "<th>拒绝理由</th>"
    embedded = _script_json(data)
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>
:root{{--paper:#f4f0e8;--surface:#fffdf8;--ink:#202823;--muted:#687069;--line:#d8d4c8;
--accent:#17685b;--warn:#9a6214}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);
color:var(--ink);font:15px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}}
header,main{{max-width:1180px;margin:auto;padding:48px 28px}}header{{padding-top:82px}}
h1{{font:500 clamp(36px,6vw,70px)/1.08 Georgia,"Songti SC",serif;margin:12px 0}}
h2{{font:500 34px/1.2 Georgia,"Songti SC",serif}}.eyebrow{{color:var(--accent);
font:700 12px/1.2 ui-monospace;letter-spacing:.13em}}section{{padding:42px 0;border-top:1px solid var(--line)}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:14px}}
article,.panel{{background:var(--surface);border:1px solid var(--line);border-radius:14px;padding:20px}}
.gallery{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}}
.gif-card img{{display:block;width:100%;height:240px;object-fit:contain;background:#eee9df;border-radius:10px}}
table{{width:100%;border-collapse:collapse;background:var(--surface)}}th,td{{padding:12px;
border:1px solid var(--line);text-align:left;vertical-align:top}}code{{font:12px/1.5 ui-monospace;
overflow-wrap:anywhere}}.boundary{{border-left:4px solid var(--warn);padding:14px 18px;background:#f4e7cf}}
.metric{{font:500 28px Georgia,serif}}a{{color:var(--accent)}}
</style></head><body><header id="outcome"><div class="eyebrow">GEPASE / SEALED OUTCOME</div>
<h1>{html.escape(outcome_labels[outcome])}</h1><p>{title}</p>
<p class="boundary">{html.escape(str(data["claim_boundary_zh"]))}</p></header><main>
<section id="deployable"><div class="eyebrow">DEPLOYABLE FRONTIER</div><h2>产物与效果边界</h2>
<div class="cards">{frontier_html}</div></section>
<section id="evidence"><div class="eyebrow">CORE-ACCEPTED GIF EVIDENCE</div><h2>fresh no-skill / original / candidate 真实产物</h2>
<p>以下 GIF 全部来自本轮 TaskScoreVector 回链的 E2 ExecutionBundle，并按原始 SHA-256 复制进报告 seal；失败或未被 Core 接受的 raw workspace 不会混入。</p>
<div class="gallery">{gallery_html}</div></section>
{efficiency_section}<section id="candidates"><div class="eyebrow">CANDIDATES</div><h2>候选与 Gate 漏斗</h2>
<div class="panel"><p><span class="metric">{len(candidates)}</span> 个候选; frontier {len(frontier)} 个</p>
<table><thead><tr><th>Candidate</th><th>Gate</th><th>Train Δ</th><th>Validation Δ</th>
{candidate_header}</tr></thead><tbody>{candidate_rows}</tbody></table></div></section>
<section id="merge"><div class="eyebrow">CONDITIONAL MERGE</div><h2>多父 Merge 终态</h2>
<div class="panel"><p>状态: <code>{html.escape(str(merge["status"]))}</code></p>
<p>拒绝理由计数: {html.escape(merge_rejections)}</p>
<p>枚举证据: <code>{html.escape(str(merge.get("enumeration_ref")))}</code></p></div></section>
<section id="process"><div class="eyebrow">GRAPH / PATCH / LINEAGE</div><h2>图引导搜索与进化证据</h2>
<div class="panel"><pre>{html.escape(json.dumps(data.get("process_evidence", dict()), ensure_ascii=False, indent=2))}</pre></div></section>
<section id="runtime"><div class="eyebrow">ACTIVE SESSION</div><h2>运行时、预算与恢复</h2>
<div class="panel"><pre>{html.escape(json.dumps(runtime, ensure_ascii=False, indent=2))}</pre>
<p>待处理 work: <code>{html.escape(pending)}</code></p>
<h3>Provenance</h3><pre>{html.escape(json.dumps(data["provenance"], ensure_ascii=False, indent=2))}</pre></div></section>
<script type="application/json" id="report-data">{embedded}</script></main></body></html>"""


def _outcome_number(value: object, digits: int = 4) -> str:
    if value is None:
        return "不可用"
    if isinstance(value, float):
        return f"{value:+.{digits}f}"
    return str(value)


def _outcome_candidate_card(candidate: dict[str, Any]) -> str:
    train = dict(candidate["train"])
    validation = dict(candidate["validation"])
    relative = dict(candidate["relative_efficiency"])
    objective_rows = "".join(
        "<div class=\"objective-row\"><span>"
        + html.escape(str(row["label_zh"]))
        + "</span><div class=\"diverging\"><i class=\"neg\"></i><i class=\"axis\"></i>"
        + (
            "<i class=\"value "
            + html.escape(str(row["validation_bar"]["side"]))
            + "\" style=\"--w:"
            + f"{float(row['validation_bar']['percent']):.3f}"
            + "%\"></i>"
            if row["validation_bar"]["available"]
            else ""
        )
        + "</div><code>"
        + html.escape(_outcome_number(row["validation_delta"], 3))
        + "</code></div>"
        for row in candidate["objective_deltas"]
    )
    axis_rows = "".join(
        "<li><b>"
        + html.escape(str(row["label_zh"]))
        + "</b><span>"
        + (
            f"中位数 {float(row['median_ratio']):.3f}×"
            if row["median_ratio"] is not None
            else "共同排除"
        )
        + "</span><small>"
        + html.escape(
            f"纳入 {row['included_tasks']} 项 / 排除 {row['excluded_tasks']} 项"
            + (
                " · " + "、".join(str(value) for value in row["exclusion_reasons"])
                if row["exclusion_reasons"]
                else ""
            )
        )
        + "</small></li>"
        for row in relative["axes"]
    ) or "<li><b>相对效率</b><span>不可用</span><small>没有足够的可比证据</small></li>"
    operations = "".join(
        "<article class=\"operation\"><div><span class=\"op\">"
        + html.escape(str(operation.get("op") or "未知操作"))
        + "</span><b>"
        + html.escape(str(operation.get("path") or "路径不可用"))
        + "</b></div><p>"
        + html.escape(str(operation.get("rationale") or "没有可展示的 rationale"))
        + "</p><small>预期："
        + html.escape(str(operation.get("expected_benefit") or "不可用"))
        + " · 风险："
        + html.escape(str(operation.get("regression_risk") or "不可用"))
        + "</small></article>"
        for operation in candidate["operations"]
    ) or "<p class=\"empty\">该候选没有可展示的 typed Patch 操作。</p>"
    chain = "".join(
        "<li><b>"
        + html.escape(str(item["step"]))
        + "</b><span>"
        + html.escape(str(item["summary"]))
        + "</span></li>"
        for item in candidate["causal_chain"]
    )
    reason_text = "；".join(candidate["reasons_zh"]) or "没有额外拒绝理由"
    parent_text = " + ".join(candidate["parent_aliases_zh"]) or "原始 Skill"
    files = "、".join(candidate["modified_files"]) or "无实际文件修改"
    rank = (
        f"Pareto 层 {candidate['pareto_layer']} · 排名 {candidate['display_rank']}"
        if candidate["display_rank"] is not None
        else "未进入 deployable 排名"
    )
    ratio = relative["relative_cost_ratio"]
    return (
        "<article class=\"candidate-detail tone-"
        + html.escape(str(candidate["status_tone"]))
        + "\" id=\"candidate-"
        + html.escape(str(candidate["short_id"]))
        + "\"><header><div><span class=\"candidate-kind\">"
        + html.escape(str(candidate["operator_zh"]))
        + "</span><h3>"
        + html.escape(str(candidate["alias_zh"]))
        + "</h3><p>父代："
        + html.escape(parent_text)
        + " · 修改："
        + html.escape(files)
        + "</p></div><span class=\"status\">"
        + html.escape(str(candidate["status_zh"]))
        + "</span></header><div class=\"candidate-metrics\"><div><b>"
        + html.escape(_outcome_number(train["mean_delta"]))
        + "</b><span>train Δ · "
        + f"{train['wins']}胜 {train['ties']}平 {train['losses']}负"
        + "</span></div><div><b>"
        + html.escape(_outcome_number(validation["mean_delta"]))
        + "</b><span>validation Δ · "
        + f"{validation['wins']}胜 {validation['ties']}平 {validation['losses']}负"
        + "</span></div><div><b>"
        + (f"{float(ratio):.3f}×" if ratio is not None else "不可用")
        + "</b><span>相对 original 成本</span></div><div><b>"
        + html.escape(rank)
        + "</b><span>质量—成本前沿</span></div></div><div class=\"candidate-grid\"><div><h4>六维 validation Δ</h4>"
        + objective_rows
        + "</div><div><h4>相对效率轴</h4><ul class=\"axis-list\">"
        + axis_rows
        + "</ul></div></div><div class=\"reason-note\"><b>Gate 解释：</b>"
        + html.escape(reason_text)
        + "</div><ol class=\"causal-chain\">"
        + chain
        + "</ol><details><summary>查看 Patch 操作与技术引用</summary><p>"
        + html.escape(str(candidate["patch_summary"]))
        + "</p><div class=\"operations\">"
        + operations
        + "</div><pre>"
        + html.escape(json.dumps(candidate["technical_refs"], ensure_ascii=False, indent=2))
        + "</pre></details></article>"
    )


def _outcome_gallery_task(task: dict[str, Any]) -> str:
    assets = []
    for asset in task["assets"]:
        evidence = dict(asset.get("typed_evidence", {}))
        deterministic = dict(evidence.get("deterministic", {}))
        grader = dict(evidence.get("grader", {}))
        comparator = evidence.get("comparator")
        usage = dict(evidence.get("usage") or {})
        assertion_rows = "".join(
            "<li><span aria-hidden=\"true\">"
            + ("✓" if row.get("passed") else "×")
            + "</span><b>"
            + html.escape(str(row.get("assertion_id") or "assertion"))
            + "</b><small>"
            + html.escape(str(row.get("detail") or "没有说明"))
            + "</small></li>"
            for row in deterministic.get("assertions", [])
        ) or "<li><small>确定性 assertion 明细不可用</small></li>"
        comparator_text = (
            f"AB {comparator.get('ab_candidate_outcome')} / "
            f"BA {comparator.get('ba_candidate_outcome')}"
            if isinstance(comparator, dict)
            else "未预注册或不可用"
        )
        label = {
            "no-skill": "不使用 Skill",
            "original": "原始 Skill",
            "candidate": asset.get("candidate_alias_zh") or "候选",
        }.get(str(asset["variant"]), str(asset["variant"]))
        candidate_attr = html.escape(str(asset.get("candidate_id") or "reference"))
        assets.append(
            "<article class=\"artifact-card\" data-variant=\""
            + html.escape(str(asset["variant"]))
            + "\" data-candidate=\""
            + candidate_attr
            + "\"><header><h4>"
            + html.escape(str(label))
            + "</h4><span>"
            + html.escape(str(asset.get("candidate_status_zh") or "Reference"))
            + "</span></header><div class=\"gif-frame\"><img loading=\"lazy\" src=\""
            + html.escape(str(asset["report_path"]))
            + "\" alt=\""
            + html.escape(str(asset["label_zh"]))
            + "\"></div><div class=\"artifact-metrics\"><span>assertions <b>"
            + (
                f"{deterministic.get('passed', 0)}/{deterministic.get('total', 0)}"
                if evidence.get("available")
                else "不可用"
            )
            + "</b></span><span>独立评分 <b>"
            + (
                f"{float(grader['score']):.3f}"
                if grader.get("score") is not None
                else "不可用"
            )
            + "</b></span><span>时长 <b>"
            + (
                f"{int(usage['duration_ms']) / 1000:.1f}s"
                if usage.get("duration_ms") is not None
                else "不可用"
            )
            + "</b></span><span>工具 <b>"
            + str(usage.get("tool_calls", "不可用"))
            + "</b></span></div><p class=\"grader-copy\">"
            + html.escape(str(grader.get("feedback_zh") or "独立 Grader 反馈不可用。"))
            + "</p><details><summary>assertion、匿名比较与证据</summary><ul class=\"assertions\">"
            + assertion_rows
            + "</ul><p><b>匿名比较：</b>"
            + html.escape(comparator_text)
            + "</p><code>sha256: "
            + html.escape(str(asset["sha256"]))
            + "</code><pre>"
            + html.escape(
                json.dumps(
                    {
                        "source_ref": asset["source_ref"],
                        "vector_ref": evidence.get("vector_ref"),
                        "usage": evidence.get("usage"),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            + "</pre></details></article>"
        )
    prompt = next(
        (
            str(asset.get("typed_evidence", {}).get("prompt_zh"))
            for asset in task["assets"]
            if asset.get("typed_evidence", {}).get("prompt_zh")
        ),
        "任务摘要不可用",
    )
    return (
        "<details class=\"task-group\" data-split=\""
        + html.escape(str(task["split"]))
        + "\" "
        + ("open" if task["default_open"] else "")
        + "><summary><span><b>"
        + html.escape(str(task["task_alias_zh"]))
        + "</b><small>"
        + html.escape(str(task["task_id"]))
        + "</small></span><span>"
        + f"{len(task['assets'])} 份真实产物"
        + "</span></summary><p class=\"task-prompt\">"
        + html.escape(prompt)
        + "</p><div class=\"artifact-grid\">"
        + "".join(assets)
        + "</div></details>"
    )


def _render_outcome_report_narrative(data: dict[str, Any]) -> str:
    presentation = dict(data["presentation"])
    headline = dict(presentation["headline"])
    outcome = dict(presentation["outcome"])
    candidates = list(presentation["candidates"])
    first = headline.get("first")
    second = headline.get("second")
    funnel = "".join(
        "<li><strong>"
        + html.escape(str(item["count"]))
        + "</strong><span>"
        + html.escape(str(item["label_zh"]))
        + "</span></li>"
        for item in presentation["funnel"]
    )
    lineage = "".join(
        "<article class=\"lineage-node tone-"
        + html.escape(str(node["status_tone"]))
        + "\"><small>generation "
        + str(node["generation"])
        + "</small><b>"
        + html.escape(str(node["alias_zh"]))
        + "</b><code>"
        + html.escape(str(node["candidate_id"])[-12:])
        + "</code></article>"
        for node in presentation["lineage"]["nodes"]
    )
    edges = "".join(
        "<li><code>"
        + html.escape(str(edge["source"])[-12:])
        + "</code><span>→</span><code>"
        + html.escape(str(edge["target"])[-12:])
        + "</code><small>"
        + ("多父合并" if edge["kind"] == "merge" else "结构化变异")
        + "</small></li>"
        for edge in presentation["lineage"]["edges"]
    )
    candidate_cards = "".join(_outcome_candidate_card(row) for row in candidates)
    scatter = dict(presentation["charts"]["scatter"])
    scatter_points = "".join(
        "<a class=\"scatter-point tone-"
        + html.escape(str(row["status_tone"]))
        + "\" style=\"--x:"
        + f"{float(row['scatter']['x_percent']):.3f}"
        + "%;--y:"
        + f"{float(row['scatter']['y_percent']):.3f}"
        + "%\" href=\"#candidate-"
        + html.escape(str(row["short_id"]))
        + "\" aria-label=\""
        + html.escape(
            f"{row['alias_zh']}，validation delta {row['validation']['mean_delta']}，"
            f"相对成本 {row['relative_efficiency']['relative_cost_ratio']}"
        )
        + "\"><span>"
        + html.escape(str(row["alias_zh"]))
        + "</span></a>"
        for row in scatter["points"]
    ) or "<p class=\"empty\">没有可比较的质量—成本点。</p>"
    task_groups = "".join(
        _outcome_gallery_task(task) for task in presentation["tasks"]
    ) or "<p class=\"empty\">当前结局没有可展示的 task-native GIF。</p>"
    candidate_options = "".join(
        "<option value=\""
        + html.escape(str(row["candidate_id"]))
        + ("\" selected" if row.get("display_rank") == 1 else "\"")
        + ">"
        + html.escape(str(row["alias_zh"]))
        + " · "
        + html.escape(str(row["status_zh"]))
        + "</option>"
        for row in candidates
    )
    frontier_cards = "".join(
        "<article><span class=\"rank\">#"
        + str(row.get("display_rank") or "—")
        + "</span><h3>"
        + html.escape(str(row["alias_zh"]))
        + "</h3><p>validation Δ <b>"
        + html.escape(_outcome_number(row["validation"]["mean_delta"]))
        + "</b> · relative cost <b>"
        + (
            f"{float(row['relative_efficiency']['relative_cost_ratio']):.3f}×"
            if row["relative_efficiency"]["relative_cost_ratio"] is not None
            else "不可用"
        )
        + "</b></p></article>"
        for row in (first, second)
        if row is not None
    ) or "<article><h3>没有 deployable 候选</h3><p>报告保留完整负结果。</p></article>"
    package = dict(presentation["package"])
    package_truth = (
        "本轮实际修改文件：" + "、".join(package["modified_files"])
        if package["modified_files"]
        else "本轮没有 materialized 文件修改"
    )
    graph_slices = "".join(
        "<article><h3>"
        + html.escape(str(row["alias_zh"]))
        + "</h3><p>父代图 binding "
        + str(row["graph"]["binding_count"])
        + " · observed access "
        + str(row["graph"]["mapped_access_events"])
        + " · target nodes "
        + str(len(row["graph"]["target_nodes"]))
        + "</p><div class=\"node-list\">"
        + "".join(
            "<code>" + html.escape(str(node)) + "</code>"
            for node in row["graph"]["target_nodes"]
        )
        + "</div></article>"
        for row in candidates
        if row["graph"]["available"] or row["graph"]["target_nodes"]
    ) or "<p class=\"empty\">没有足够的 selector graph 切片证据。</p>"
    runtime = dict(presentation["runtime"])
    raw_evidence = html.escape(
        json.dumps(
            {
                "provenance": data.get("provenance"),
                "policy_evaluation": data.get("policy_evaluation"),
                "frontier_ranking": data.get("frontier_ranking"),
                "process_evidence": data.get("process_evidence"),
                "runtime": data.get("runtime"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    embedded = _script_json(data)
    title = html.escape(str(data["title_zh"]))
    first_delta = (
        _outcome_number(first["validation"]["mean_delta"]) if first is not None else "不可用"
    )
    first_cost = (
        f"{float(first['relative_efficiency']['relative_cost_ratio']):.3f}×"
        if first is not None
        and first["relative_efficiency"]["relative_cost_ratio"] is not None
        else "不可用"
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light"><title>{title}</title>
<style>
:root{{--paper:#f3f0e8;--surface:#fffdf8;--ink:#202622;--muted:#687069;--line:#d8d4c8;--night:#202924;--coral:#b9533c;--coral-soft:#f3ddd4;--teal:#17685b;--teal-soft:#dcebe5;--amber:#936217;--amber-soft:#f3e7cc;--red:#9e4039;--red-soft:#f2ddda;--mono:ui-monospace,SFMono-Regular,Menlo,monospace;--serif:Georgia,"Songti SC",serif;--sans:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}}*{{box-sizing:border-box}}html{{scroll-behavior:smooth;scroll-padding-top:68px}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.68 var(--sans)}}a{{color:var(--teal);text-underline-offset:3px}}a:focus-visible,button:focus-visible,select:focus-visible,summary:focus-visible{{outline:3px solid #e29773;outline-offset:3px}}code,pre{{font-family:var(--mono)}}.topbar{{position:sticky;top:0;z-index:10;display:flex;align-items:center;gap:18px;padding:11px clamp(18px,4vw,64px);background:#f3f0e8ed;border-bottom:1px solid var(--line);backdrop-filter:blur(12px)}}.brand{{font:700 11px var(--mono);letter-spacing:.16em;color:var(--coral);white-space:nowrap}}nav{{display:flex;gap:4px;overflow:auto}}nav a{{white-space:nowrap;padding:7px 9px;border-radius:7px;color:var(--muted);text-decoration:none;font-size:13px}}nav a:hover{{background:var(--surface);color:var(--ink)}}.hero{{padding:clamp(58px,8vw,112px) clamp(22px,7vw,108px) 56px;background:radial-gradient(circle at 88% 18%,#d7e7df 0,transparent 27%),linear-gradient(145deg,#fffdf8 0%,#eee8de 74%);border-bottom:1px solid var(--line)}}.eyebrow{{font:700 11px var(--mono);letter-spacing:.15em;color:var(--coral);text-transform:uppercase}}h1{{font:500 clamp(40px,6vw,76px)/1.04 var(--serif);letter-spacing:-.035em;max-width:1100px;margin:18px 0}}h2{{font:500 clamp(30px,4vw,48px)/1.14 var(--serif);margin:12px 0}}h3{{margin:0;font-size:19px}}h4{{margin:0 0 10px}}.lead{{max-width:880px;font-size:18px;color:var(--muted)}}.hero-metrics,.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px}}.hero-metrics{{margin-top:34px;max-width:1100px}}.metric{{border-top:2px solid var(--ink);padding:13px 2px}}.metric b{{display:block;font:500 clamp(26px,4vw,43px)/1.1 var(--serif)}}.metric span{{display:block;margin-top:7px;color:var(--muted);font-size:12px}}.boundary{{max-width:980px;margin-top:24px;padding:15px 17px;border-left:3px solid var(--amber);background:var(--amber-soft);color:#6b4b1b}}main{{max-width:1440px;margin:auto;padding:0 clamp(18px,4vw,64px) 90px}}section{{padding:68px 0 24px;border-bottom:1px solid var(--line)}}.intro{{max-width:900px;color:var(--muted);font-size:16px}}.panel,.cards>article,.candidate-detail,.task-group,.graph-grid>article{{background:var(--surface);border:1px solid var(--line);border-radius:15px}}.panel{{padding:20px}}.three-layers article{{padding:18px}}.three-layers b{{font:700 11px var(--mono);color:var(--teal)}}.rank{{display:inline-grid;place-items:center;width:35px;height:35px;border-radius:50%;background:var(--teal);color:white;font-weight:700}}.funnel{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;padding:0;list-style:none}}.funnel li{{padding:16px;background:var(--surface);border:1px solid var(--line);border-radius:12px;text-align:center}}.funnel strong{{display:block;font:500 36px var(--serif)}}.funnel span{{color:var(--muted);font-size:12px}}.lineage-wrap{{display:grid;grid-template-columns:1.4fr .8fr;gap:14px}}.lineage-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;padding:16px;background:var(--night);border-radius:14px}}.lineage-node{{display:flex;min-height:98px;flex-direction:column;justify-content:center;padding:13px;border:2px solid #66746b;border-radius:10px;background:#2c3731;color:#eef2ed}}.lineage-node small,.lineage-node code{{color:#afbbb2;font-size:10px}}.lineage-node.tone-accepted{{border-color:#5ec4a6}}.lineage-node.tone-rejected{{border-color:#d07168}}.lineage-node.tone-incomplete{{border-color:#e1b75f}}.lineage-node.tone-root{{border-color:#a9b2ac}}.edge-list{{margin:0;padding:15px 24px;background:var(--surface);border:1px solid var(--line);border-radius:14px}}.edge-list li{{display:grid;grid-template-columns:1fr auto 1fr;gap:7px;padding:7px 0;border-bottom:1px solid var(--line)}}.edge-list small{{grid-column:1/-1;color:var(--muted)}}.candidate-detail{{margin:16px 0;padding:22px;border-left:5px solid var(--line)}}.candidate-detail.tone-accepted{{border-left-color:var(--teal)}}.candidate-detail.tone-rejected{{border-left-color:var(--red)}}.candidate-detail.tone-incomplete{{border-left-color:var(--amber)}}.candidate-detail>header{{display:flex;justify-content:space-between;gap:14px}}.candidate-detail header p{{margin:8px 0;color:var(--muted)}}.candidate-kind{{font:700 10px var(--mono);letter-spacing:.1em;color:var(--coral)}}.status{{height:max-content;padding:5px 9px;border-radius:99px;background:var(--teal-soft);color:var(--teal);font-size:12px;font-weight:700}}.tone-rejected .status{{background:var(--red-soft);color:var(--red)}}.tone-incomplete .status{{background:var(--amber-soft);color:var(--amber)}}.candidate-metrics{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:19px 0}}.candidate-metrics div{{padding:12px;border-top:2px solid var(--ink)}}.candidate-metrics b{{display:block;font:500 23px var(--serif)}}.candidate-metrics span{{font-size:11px;color:var(--muted)}}.candidate-grid{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.objective-row{{display:grid;grid-template-columns:150px minmax(130px,1fr) 62px;gap:8px;align-items:center;margin:7px 0;font-size:12px}}.diverging{{position:relative;height:9px;border-radius:99px;background:#ebe7de}}.diverging .axis{{position:absolute;left:50%;top:-3px;width:1px;height:15px;background:#767c76}}.diverging .value{{position:absolute;top:1px;height:7px;width:var(--w);background:var(--teal)}}.diverging .value.positive{{left:50%}}.diverging .value.negative{{right:50%;background:var(--coral)}}.axis-list{{display:grid;gap:8px;padding:0;list-style:none}}.axis-list li{{display:grid;grid-template-columns:1fr auto;padding:10px;background:#f5f1e9;border-radius:9px}}.axis-list small{{grid-column:1/-1;color:var(--muted)}}.reason-note{{margin:16px 0;padding:12px;background:#f6efe2;border-left:3px solid var(--amber)}}.causal-chain{{display:grid;grid-template-columns:repeat(5,1fr);gap:9px;padding:0;list-style:none;counter-reset:chain}}.causal-chain li{{position:relative;padding:12px;border:1px solid var(--line);border-radius:10px;background:#faf8f2}}.causal-chain b,.causal-chain span{{display:block}}.causal-chain span{{margin-top:7px;color:var(--muted);font-size:12px}}details summary{{cursor:pointer;font-weight:700}}details pre{{max-height:330px;overflow:auto;padding:13px;border-radius:9px;background:#222925;color:#e8eee8;white-space:pre-wrap;word-break:break-word;font-size:11px}}.operations{{display:grid;gap:8px}}.operation{{padding:13px;border:1px solid var(--line);border-radius:9px}}.operation .op{{margin-right:8px;color:var(--coral);font:700 10px var(--mono)}}.operation p{{color:var(--muted)}}.scatter{{position:relative;height:390px;margin-top:18px;border-left:2px solid var(--ink);border-bottom:2px solid var(--ink);background:linear-gradient(90deg,transparent 44%,#b9533c22 44%,#b9533c22 45%,transparent 45%),linear-gradient(0deg,#17685b0a,#17685b00)}}.scatter:before{{content:"original 1.0×";position:absolute;left:43%;bottom:-28px;color:var(--muted);font-size:11px}}.scatter:after{{content:"extreme cost line";position:absolute;right:8%;top:8px;color:var(--red);font-size:11px}}.scatter-point{{position:absolute;left:var(--x);bottom:var(--y);transform:translate(-50%,50%);display:grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--teal);border:3px solid white;box-shadow:0 0 0 1px var(--teal);text-decoration:none}}.scatter-point.tone-rejected{{background:var(--red);box-shadow:0 0 0 1px var(--red)}}.scatter-point span{{position:absolute;top:24px;white-space:nowrap;padding:2px 5px;background:var(--surface);color:var(--ink);font-size:11px}}.chart-legend{{display:flex;justify-content:space-between;color:var(--muted);font-size:12px}}.toolbar{{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:16px 0;padding:12px;background:#f8f5ee;border:1px solid var(--line);border-radius:10px}}select{{padding:7px 9px;border:1px solid #aaa69d;border-radius:7px;background:white;font:inherit}}.task-group{{margin:12px 0;overflow:hidden}}.task-group>summary{{display:flex;justify-content:space-between;gap:16px;padding:16px 18px;background:#f8f5ee}}.task-group>summary b,.task-group>summary small{{display:block}}.task-group>summary small{{color:var(--muted);font:10px var(--mono)}}.task-prompt{{max-width:920px;padding:0 18px;color:var(--muted)}}.artifact-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1px;background:var(--line)}}.artifact-card{{min-width:0;padding:16px;background:var(--surface)}}.artifact-card>header{{display:flex;justify-content:space-between;gap:8px}}.artifact-card>header span{{color:var(--muted);font-size:11px}}.gif-frame{{height:250px;margin:12px 0;display:grid;place-items:center;overflow:hidden;border-radius:10px;background:radial-gradient(circle,#fff,#ece8df)}}.gif-frame img{{max-width:100%;max-height:238px}}.artifact-metrics{{display:grid;grid-template-columns:1fr 1fr;gap:7px;font-size:11px;color:var(--muted)}}.artifact-metrics b{{display:block;color:var(--ink)}}.grader-copy{{min-height:72px;color:var(--muted);font-size:12px}}.assertions{{padding:0;list-style:none}}.assertions li{{display:grid;grid-template-columns:20px 1fr;gap:6px;padding:5px 0}}.assertions small{{grid-column:2;color:var(--muted)}}.graph-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}}.graph-grid>article{{padding:16px;background:var(--night);color:#eef2ed}}.graph-grid p{{color:#b8c2bb}}.node-list{{display:flex;flex-wrap:wrap;gap:5px}}.node-list code{{padding:4px 6px;border:1px solid #56625a;border-radius:5px;font-size:9px}}.package-truth{{padding:15px;border-left:3px solid var(--coral);background:var(--coral-soft)}}.runtime-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px}}.runtime-grid div{{padding:12px;border-top:2px solid var(--ink)}}.runtime-grid b{{display:block;font:500 25px var(--serif)}}.evidence-zone>details{{margin:9px 0;padding:13px;border:1px solid var(--line);border-radius:9px;background:var(--surface)}}.empty{{padding:18px;background:var(--amber-soft);color:#6b4b1b}}footer{{padding:32px clamp(22px,7vw,108px);color:var(--muted);border-top:1px solid var(--line)}}@media(max-width:980px){{.lineage-wrap,.candidate-grid{{grid-template-columns:1fr}}.candidate-metrics{{grid-template-columns:1fr 1fr}}.causal-chain{{grid-template-columns:1fr 1fr}}}}@media(max-width:650px){{nav{{display:none}}.candidate-metrics,.causal-chain{{grid-template-columns:1fr}}.objective-row{{grid-template-columns:105px 1fr 54px}}.artifact-grid{{grid-template-columns:1fr}}section{{padding-top:52px}}}}@media print{{.topbar,.toolbar{{display:none}}body{{background:white}}section,.candidate-detail{{break-inside:avoid}}details{{display:block}}}}
</style></head><body>
<div class="topbar"><span class="brand">GEPASE / OUTCOME</span><nav aria-label="报告目录"><a href="#overview">结论</a><a href="#process">过程</a><a href="#candidates">候选</a><a href="#scores">评分与效率</a><a href="#tasks">任务产物</a><a href="#package">Graph / Patch</a><a href="#runtime">运行</a><a href="#evidence">证据与复现</a></nav></div>
<header class="hero" id="overview"><div class="eyebrow">Graph-Enhanced Package-Aware Skill Evolution</div><h1>{html.escape(str(outcome['label_zh']))}</h1><p class="lead">{title}。首屏只呈现已经由 Core 决定的 outcome、候选漏斗与质量—成本排名；浏览器不重新评分，也不改变 Gate。</p><div class="hero-metrics"><div class="metric"><b>{headline['proposed']}</b><span>进入搜索的候选</span></div><div class="metric"><b>{headline['deployable']}</b><span>deployable frontier</span></div><div class="metric"><b>{html.escape(first_delta)}</b><span>第一名 validation Δ</span></div><div class="metric"><b>{html.escape(first_cost)}</b><span>第一名相对 original 成本</span></div></div><p class="boundary"><b>结论边界：</b>{html.escape(str(outcome['boundary_zh']))}</p></header>
<main><section><div class="eyebrow">01 / THREE LAYERS</div><h2>三层结论，分别成立</h2><div class="cards three-layers"><article><b>代码实现</b><p>通用 builder 从 typed evidence 派生展示投影；HTML 只负责呈现和筛选。</p></article><article><b>工程机制</b><p>报告、Package 归档与任务原生产物纳入新 seal，旧 evidence 保持只读。</p></article><article><b>算法效果</b><p>本页展示既有 sealed run 在当前 policy 下的 Gate 与 frontier，不新增实验分数。</p></article></div><h3 style="margin-top:28px">第一名与第二名</h3><div class="cards">{frontier_cards}</div></section>
<section id="process"><div class="eyebrow">02 / SEARCH STORY</div><h2>从 Reference 到部署前沿</h2><p class="intro">流程依次经过失败证据、Package Graph 定位、结构化 Patch、train Gate、Reflection/Pareto、generation-2、条件 Merge 和 held-out Gate。</p><ol class="funnel">{funnel}</ol><div class="lineage-wrap"><div class="lineage-grid" aria-label="候选父子谱系">{lineage}</div><ol class="edge-list" aria-label="谱系边">{edges}</ol></div></section>
<section id="candidates"><div class="eyebrow">03 / CANDIDATES</div><h2>每个候选为何入选或淘汰</h2><p class="intro">中文别名只用于阅读；完整 Candidate ID、Patch refs、Graph refs 和 reason code 保留在折叠技术区。</p>{candidate_cards}</section>
<section id="scores"><div class="eyebrow">04 / QUALITY × COST</div><h2>质量提升与相对资源成本</h2><p class="intro">横轴是 Candidate 相对 frozen original 的成本比，纵轴是 held-out validation Δ。1.0× 是 original 基准，policy 中的极端成本线为 {float(scatter['extreme_ratio']):.1f}×。六维条形由 Python 投影预先计算，浏览器不决定排序或 Gate。</p><div class="panel"><div class="chart-legend"><span>更省资源 ← relative cost → 更高成本</span><span>validation Δ 越高越好 ↑</span></div><div class="scatter" role="img" aria-label="候选质量提升与相对资源成本散点图">{scatter_points}</div></div></section>
<section id="tasks"><div class="eyebrow">05 / TASK-NATIVE OUTPUTS</div><h2>按任务对照真实 GIF</h2><p class="intro">默认展示 held-out validation 与排名第一的 Candidate。每组将 no-skill、original 和所选 Candidate 放在同一任务下；证据不完整的候选会保留状态标记，不会混成 accepted 结果。</p><div class="toolbar"><label>Split <select id="split-filter"><option value="validation">仅 validation</option><option value="all">全部</option><option value="train">仅 train</option></select></label><label>Candidate <select id="candidate-filter"><option value="all">全部候选</option>{candidate_options}</select></label></div><div id="task-groups">{task_groups}</div></section>
<section id="package"><div class="eyebrow">06 / PACKAGE GRAPH & PATCH</div><h2>失败 → 图定位 → Patch → 评测 → Gate</h2><p class="package-truth"><b>事实边界：</b>完整 Package 进入 snapshot、graph 和访问分析；selector 实际使用 {package['graph_binding_count']} 个 graph binding，定位 {package['target_node_count']} 个修改目标。{html.escape(package_truth)}。package-aware 不等于所有文件都被修改。</p><div class="graph-grid">{graph_slices}</div></section>
<section id="runtime"><div class="eyebrow">07 / RUNTIME</div><h2>中性呈现实际运行规模</h2><div class="runtime-grid"><div><b>{runtime.get('agent_calls', '不可用')}</b><span>历史 Agent calls</span></div><div><b>{runtime.get('estimated_tokens', '不可用')}</b><span>保守 estimated tokens</span></div><div><b>{runtime.get('active_wall_clock_ms', '不可用')}</b><span>active ms</span></div><div><b>{runtime.get('repairs', '不可用')}</b><span>repairs</span></div><div><b>0</b><span>本报告新增 Agent/API/Eval</span></div></div></section>
<section id="evidence" class="evidence-zone"><div class="eyebrow">08 / EVIDENCE & REPRODUCTION</div><h2>证据与复现</h2><p class="intro">路径、hash、policy、Runtime 与机器 JSON 默认折叠；它们仍被复制到 report-data 和 artifact seal 中，可用于逐字节复核。</p><details><summary>Policy、frontier ranking、process 与 Runtime 原始投影</summary><pre>{raw_evidence}</pre></details><details><summary>完整 report-data.json</summary><p><a href="report-data.json">下载或查看机器可读报告数据</a></p><code>{html.escape(str(data.get('outcome_input_ref')))}</code></details><details><summary>可部署 Package 归档</summary><div class="cards">{"".join(f'<article><b>{html.escape(str(item["candidate_id"]))}</b><p><a href="{html.escape(str(item["archive_path"]))}">下载 Package ZIP</a></p><code>{html.escape(str(item["archive_sha256"]))}</code></article>' for item in data.get('deployable_frontier', [])) or '<p class="empty">没有可部署 Package 归档。</p>'}</div></details></section>
<script type="application/json" id="report-data">{embedded}</script></main><footer>GEPASE · self-contained sealed report · 无 CDN / 无远程字体 / 无浏览器端评分</footer>
<script>
const split=document.getElementById('split-filter'),candidate=document.getElementById('candidate-filter');function filterTasks(){{const s=split.value,c=candidate.value;document.querySelectorAll('.task-group').forEach(group=>{{group.hidden=s!=='all'&&group.dataset.split!==s;group.querySelectorAll('.artifact-card').forEach(card=>{{card.hidden=card.dataset.variant==='candidate'&&c!=='all'&&card.dataset.candidate!==c}})}})}}split.addEventListener('change',filterTasks);candidate.addEventListener('change',filterTasks);filterTasks();
</script></body></html>"""


def render_outcome_report(data: dict[str, Any]) -> str:
    """Render the classic report or the opt-in generic narrative projection."""

    if data.get("presentation") is None:
        return _render_outcome_report_classic(data)
    return _render_outcome_report_narrative(data)
