"""Generate a dependency-free interactive HTML/SVG PackageGraph report."""

# ruff: noqa: E501 -- embedded HTML/CSS remains legible as a standalone template

from __future__ import annotations

import html
import json

from gepase.package.ir import PackageGraph


def render_graph_report(graph: PackageGraph) -> str:
    nodes = list(graph.nodes)
    node_positions: dict[str, tuple[int, int]] = {}
    columns = 5
    for index, node in enumerate(nodes):
        node_positions[node.node_id] = (90 + (index % columns) * 230, 90 + (index // columns) * 100)
    width = 1_180
    height = max(360, 170 + ((len(nodes) + columns - 1) // columns) * 100)
    edge_lines: list[str] = []
    for edge in graph.edges:
        source = node_positions[edge.source]
        target = node_positions[edge.target]
        edge_lines.append(
            f'<line class="edge {edge.layer}" x1="{source[0]}" y1="{source[1]}" '
            f'x2="{target[0]}" y2="{target[1]}" data-kind="{edge.kind.value}"><title>'
            f"{html.escape(edge.kind.value)}</title></line>"
        )
    node_groups: list[str] = []
    for node in nodes:
        x, y = node_positions[node.node_id]
        payload = html.escape(
            json.dumps(
                {
                    "node_id": node.node_id,
                    "kind": node.kind.value,
                    "path": node.path,
                    "locator": node.locator,
                    "content_hash": node.content_hash,
                    "metadata": node.metadata,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        label = html.escape(node.label[:27])
        node_groups.append(
            f'<g class="node kind-{node.kind.value}" data-payload="{payload}" '
            f'transform="translate({x},{y})" tabindex="0">'
            '<rect x="-84" y="-25" width="168" height="50" rx="8"/>'
            f'<text text-anchor="middle" y="-3">{label}</text>'
            f'<text class="kind" text-anchor="middle" y="14">{node.kind.value}</text></g>'
        )
    graph_data = json.dumps(graph.model_dump(mode="json"), ensure_ascii=False).replace(
        "</", "<\\/"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(graph.package_id)} Package Graph</title>
<style>
:root{{--bg:#0b1020;--panel:#121a2e;--text:#e9eefc;--muted:#9fb0d0;--static:#526581;
--planned:#e7a83e;--observed:#32c48d;--node:#20304d}}*{{box-sizing:border-box}}body{{margin:0;
font:14px/1.45 ui-sans-serif,system-ui;background:var(--bg);color:var(--text)}}header{{position:sticky;
top:0;z-index:3;background:#0b1020e8;border-bottom:1px solid #273553;padding:16px 24px}}h1{{margin:0;
font-size:20px}}.legend{{display:flex;gap:16px;color:var(--muted);margin-top:8px}}.swatch{{display:inline-block;
width:18px;height:3px;vertical-align:middle;margin-right:5px}}main{{display:grid;grid-template-columns:minmax(0,1fr)
320px;min-height:calc(100vh - 80px)}}.canvas{{overflow:auto}}aside{{border-left:1px solid #273553;
padding:18px;background:var(--panel);position:sticky;top:80px;height:calc(100vh - 80px);overflow:auto}}
svg{{min-width:100%;height:auto}}.edge{{stroke:var(--static);stroke-width:1;opacity:.36}}.edge.planned{{stroke:var(--planned);
stroke-width:1.8}}.edge.observed{{stroke:var(--observed);stroke-width:2.2}}.node rect{{fill:var(--node);
stroke:#6882ad;stroke-width:1}}.node:hover rect,.node:focus rect{{stroke:#fff;stroke-width:2}}.node text{{fill:var(--text);
font-size:11px;pointer-events:none}}.node text.kind{{fill:var(--muted);font-size:9px}}pre{{white-space:pre-wrap;
word-break:break-word;color:#c8d5ef}}.counts{{color:var(--muted)}}
</style></head><body><header><h1>{html.escape(graph.package_id)} Package Graph</h1>
<div class="legend"><span><i class="swatch" style="background:var(--static)"></i>static</span>
<span><i class="swatch" style="background:var(--planned)"></i>planned E1</span>
<span><i class="swatch" style="background:var(--observed)"></i>observed E2/E3</span>
<span class="counts">{len(nodes)} nodes · {len(graph.edges)} edges · {len(graph.diagnostics)} diagnostics</span></div>
</header><main><section class="canvas"><svg viewBox="0 0 {width} {height}" role="img" aria-label="Package dependency graph">
{''.join(edge_lines)}{''.join(node_groups)}</svg></section><aside><h2>Inspector</h2>
<p>Select a node to inspect its stable locator, content hash and evidence metadata.</p><pre id="details"></pre></aside></main>
<script type="application/json" id="graph-data">{graph_data}</script><script>
const details=document.getElementById('details');document.querySelectorAll('.node').forEach(n=>{{
const show=()=>details.textContent=JSON.stringify(JSON.parse(n.dataset.payload),null,2);
n.addEventListener('click',show);n.addEventListener('keydown',e=>{{if(e.key==='Enter'||e.key===' ')show()}})}});
</script></body></html>"""
