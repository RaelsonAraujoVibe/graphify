"""html — moved verbatim from graphify/export.py."""
from __future__ import annotations

from graphify.exporters.base import COMMUNITY_COLORS  # noqa: E402,F401
from pathlib import Path
import html as _html
from graphify.analyze import _node_community_map
from graphify.paths import write_text_atomic
import json
import networkx as nx
from graphify.security import sanitize_label


MAX_NODES_FOR_VIZ = 5_000
_HTML_STALE_MARKER = ".graph.html.stale"
# Cutoff, in node count, for the *organic* server-side layout (spring_layout).
# networkx's spring_layout is not the O(n) win it might look like: for
# len(G) >= 500 it switches to its scipy-backed "energy" method (raises
# ImportError without scipy — caught below, safe no-op), and that method
# still scales badly in practice. Measured on this machine: 500 nodes ~2s,
# 1000 ~5.5s, 2000 ~12s, 3000 ~23s — clearly still superlinear. 1200 is
# picked to keep the one-time cost added to `graphify update`/`export html`
# in the single-digit seconds. Above it (or if spring_layout itself raises),
# `_precompute_layout` falls back to `_grid_layout` instead — see that
# function for why a cheap fallback beats leaving physics enabled.
_LAYOUT_MAX_NODES = 1_200
# Spread of the precomputed layout in vis-network canvas units. Both
# spring_layout and _grid_layout return coordinates roughly within [-1, 1];
# this scale keeps spacing in the same rough order of magnitude vis-network's
# own ForceAtlas2 physics used to produce (springLength=120 across thousands
# of nodes), so the static layout doesn't look cramped or absurdly sparse
# relative to node/label size.
_LAYOUT_SCALE = 800.0


def _grid_layout(G: nx.Graph) -> dict:
    """Deterministic O(n) fallback layout: no simulation, no iteration.

    Used above `_LAYOUT_MAX_NODES` (or if spring_layout itself raises)
    instead of leaving vis-network's client-side ForceAtlas2 physics
    enabled. That was the original tier-1 fallback, on the assumption it
    reproduced pre-existing behavior harmlessly — but it doesn't scale down
    gracefully, it hangs: reproduced against a real 19,706-node/23,177-edge
    graphify export, the browser tab never became responsive again within
    100+ seconds of the page's `load` event firing. A grid carries none of
    spring_layout's topology-aware clustering (nodes don't group visually by
    community/connectivity — see the graph.html docstring's Performance note
    for the tradeoff), but it's O(n), so the page loads in seconds and the
    filters panel stays fully usable, which is what actually matters once a
    graph is this large: finding and filtering specific nodes, not eyeballing
    an organic layout of tens of thousands of dots.
    """
    import math
    nodes = list(G.nodes())
    n = len(nodes)
    cols = max(1, math.ceil(math.sqrt(n)))
    span = max(cols - 1, 1)
    positions = {}
    for i, node in enumerate(nodes):
        row, col = divmod(i, cols)
        positions[node] = ((col / span) * 2 - 1, (row / span) * 2 - 1)
    return positions


def _precompute_layout(G: nx.Graph) -> dict | None:
    """Best-effort server-side node layout, computed once at export time.

    Letting vis-network run its ForceAtlas2 physics simulation client-side
    from a random start is the dominant cost on first paint for any graph
    with more than a couple thousand nodes — and past roughly `_LAYOUT_MAX_NODES`
    it doesn't just cost more, it hangs the tab outright (see `_grid_layout`).
    Precomputing positions here and shipping them in the node data lets the
    browser skip physics entirely (see the HAS_LAYOUT flag in `_html_script()`).

    Prefers the organic spring_layout up to `_LAYOUT_MAX_NODES` nodes, falls
    back to the cheap `_grid_layout` above that (or if spring_layout itself
    raises — e.g. missing scipy, a degenerate/disconnected graph shape).
    Returns None only for an empty graph, in which case there's nothing to
    lay out and the (harmless, since there are no nodes) physics-driven JS
    path runs instead.
    """
    n = G.number_of_nodes()
    if n == 0:
        return None
    if n <= _LAYOUT_MAX_NODES:
        try:
            return nx.spring_layout(G, seed=42)
        except Exception:
            pass
    return _grid_layout(G)


def _hub_alpha(base: float, hub_degree: int) -> float:
    """Dampen edge opacity for edges touching a high-degree hub node.

    Ported from grafo-explorer's `alfaPorGrau`: full opacity up to degree 6,
    decaying by sqrt(degree) beyond that, floored so hub fan-out edges fade
    rather than either vanishing or visually overwhelming the render. This is
    a static per-edge value computed once here at export time — no added
    client-side cost, unlike a zoom-driven level-of-detail system would be.
    """
    import math
    return max(0.15, base * min(1.0, math.sqrt(6.0 / max(hub_degree, 6))))


def _viz_node_limit() -> int:
    """Return the effective viz node limit, honoring GRAPHIFY_VIZ_NODE_LIMIT env var.

    Falls back to MAX_NODES_FOR_VIZ when the env var is unset, empty, or non-integer.
    Set to 0 to disable HTML viz unconditionally (useful for CI runners).
    """
    import os
    raw = os.environ.get("GRAPHIFY_VIZ_NODE_LIMIT")
    if raw is None or not raw.strip():
        return MAX_NODES_FOR_VIZ
    try:
        return int(raw)
    except ValueError:
        return MAX_NODES_FOR_VIZ

def _html_styles() -> str:
    return """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; height: 100vh; overflow: hidden; }
  #graph { flex: 1; }
  #sidebar { width: 280px; background: #1a1a2e; border-left: 1px solid #2a2a4e; display: flex; flex-direction: column; overflow: hidden; }
  #search-wrap { padding: 12px; border-bottom: 1px solid #2a2a4e; }
  #search { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 7px 10px; border-radius: 6px; font-size: 13px; outline: none; }
  #search:focus { border-color: #4E79A7; }
  #search-results { max-height: 140px; overflow-y: auto; padding: 4px 12px; border-bottom: 1px solid #2a2a4e; display: none; }
  .search-item { padding: 4px 6px; cursor: pointer; border-radius: 4px; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .search-item:hover { background: #2a2a4e; }
  #info-panel { padding: 14px; border-bottom: 1px solid #2a2a4e; min-height: 140px; }
  #info-panel h3 { font-size: 13px; color: #aaa; margin-bottom: 8px; text-transform: uppercase; letter-spacing: 0.05em; }
  #info-content { font-size: 13px; color: #ccc; line-height: 1.6; }
  #info-content .field { margin-bottom: 5px; }
  #info-content .field b { color: #e0e0e0; }
  #info-content .empty { color: #555; font-style: italic; }
  .neighbor-link { display: block; padding: 2px 6px; margin: 2px 0; border-radius: 3px; cursor: pointer; font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; border-left: 3px solid #333; }
  .neighbor-link:hover { background: #2a2a4e; }
  #neighbors-list { max-height: 160px; overflow-y: auto; margin-top: 4px; }
  #stats { padding: 10px 14px; border-top: 1px solid #2a2a4e; font-size: 11px; color: #555; }
  #filters-wrap { flex: 1; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 14px; min-height: 0; }
  .filters-header { display: flex; align-items: center; justify-content: space-between; }
  .filters-header h3 { font-size: 13px; color: #aaa; text-transform: uppercase; letter-spacing: 0.05em; display: flex; align-items: center; gap: 6px; }
  .filters-badge { display: none; background: #4E79A7; color: #fff; border-radius: 9px; padding: 1px 6px; font-size: 10px; font-weight: 600; }
  .filters-clear { background: none; border: none; color: #888; font-size: 11px; cursor: pointer; padding: 2px 4px; }
  .filters-clear:hover { color: #e0e0e0; }
  #facet-text-wrap { position: relative; }
  #facet-text { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 6px 9px; border-radius: 6px; font-size: 12px; outline: none; }
  #facet-text:focus { border-color: #4E79A7; }
  .facet-section { display: flex; flex-direction: column; gap: 4px; }
  .facet-section-title { display: flex; align-items: center; justify-content: space-between; font-size: 11px; color: #aaa; text-transform: uppercase; letter-spacing: 0.05em; }
  .facet-links button { background: none; border: none; color: #777; font-size: 10px; cursor: pointer; padding: 0 2px; }
  .facet-links button:hover { color: #e0e0e0; }
  .facet-list { max-height: 150px; overflow-y: auto; }
  .facet-item { display: flex; align-items: center; gap: 6px; padding: 3px 2px; border-radius: 4px; cursor: pointer; font-size: 12px; }
  .facet-item:hover { background: #2a2a4e; }
  .facet-item.inactive { opacity: 0.45; }
  .facet-item.zero { opacity: 0.35; }
  .facet-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
  .facet-label { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .facet-only { display: none; font-size: 10px; color: #888; padding: 0 4px; border-radius: 3px; }
  .facet-item:hover .facet-only { display: inline; }
  .facet-only:hover { color: #e0e0e0; background: #33335c; }
  .facet-count { font-size: 11px; color: #666; flex-shrink: 0; }
  .degree-inputs { display: flex; gap: 6px; }
  .degree-inputs input { width: 100%; background: #0f0f1a; border: 1px solid #3a3a5e; color: #e0e0e0; padding: 4px 7px; border-radius: 5px; font-size: 12px; outline: none; }
  .degree-inputs input:focus { border-color: #4E79A7; }
  .toggle-row { display: flex; align-items: center; gap: 8px; font-size: 12px; color: #ccc; cursor: pointer; user-select: none; }
  .chk { appearance: none; -webkit-appearance: none; width: 14px; height: 14px; border: 1.5px solid #3a3a5e; border-radius: 3px; background: #0f0f1a; cursor: pointer; position: relative; flex-shrink: 0; }
  .chk:checked { background: #4E79A7; border-color: #4E79A7; }
  .chk:checked::after { content: ''; position: absolute; left: 3.5px; top: 1px; width: 4px; height: 7px; border: solid #fff; border-width: 0 2px 2px 0; transform: rotate(45deg); }
</style>"""

def _hyperedge_script(hyperedges_json: str) -> str:
    return f"""<script>
// Render hyperedges as shaded regions
const hyperedges = {hyperedges_json};
// afterDrawing passes ctx already transformed to network coordinate space.
// Draw node positions raw — no manual pan/zoom/DPR math needed.

// Andrew's monotone chain. Returns the hull in counter-clockwise order, which
// is what the perimeter must be traced in. Collinear and duplicate points
// collapse to the extremes, so degenerate member sets render as a segment
// rather than a zero-area crossed path.
function convexHull(pts) {{
    const p = pts.slice().sort((a, b) => (a.x - b.x) || (a.y - b.y));
    if (p.length < 3) return p;
    const cross = (o, a, b) => (a.x - o.x) * (b.y - o.y) - (a.y - o.y) * (b.x - o.x);
    const build = seq => {{
        const out = [];
        for (const q of seq) {{
            while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], q) <= 0) out.pop();
            out.push(q);
        }}
        out.pop();
        return out;
    }};
    const hull = build(p).concat(build(p.slice().reverse()));
    return hull.length >= 3 ? hull : p;
}}
network.on('afterDrawing', function(ctx) {{
    hyperedges.forEach(h => {{
        const positions = h.nodes
            .map(nid => network.getPositions([nid])[nid])
            .filter(p => p !== undefined);
        if (positions.length < 2) return;
        ctx.save();
        ctx.globalAlpha = 0.12;
        ctx.fillStyle = '#6366f1';
        ctx.strokeStyle = '#6366f1';
        ctx.lineWidth = 2;
        ctx.beginPath();
        // Centroid and expanded hull in network coordinates.
        // The perimeter must follow hull order, not h.nodes order: tracing the
        // raw member order self-intersects whenever the layout does not happen
        // to place members in angular order, filling as crossed wedges.
        const cx = positions.reduce((s, p) => s + p.x, 0) / positions.length;
        const cy = positions.reduce((s, p) => s + p.y, 0) / positions.length;
        const hull = convexHull(positions);
        const expanded = hull.map(p => ({{
            x: cx + (p.x - cx) * 1.15,
            y: cy + (p.y - cy) * 1.15
        }}));
        ctx.moveTo(expanded[0].x, expanded[0].y);
        expanded.slice(1).forEach(p => ctx.lineTo(p.x, p.y));
        ctx.closePath();
        ctx.fill();
        ctx.globalAlpha = 0.4;
        ctx.stroke();
        // Label
        ctx.globalAlpha = 0.8;
        ctx.fillStyle = '#4f46e5';
        ctx.font = 'bold 11px sans-serif';
        ctx.textAlign = 'center';
        ctx.fillText(h.label, cx, cy - 5);
        ctx.restore();
    }});
}});
</script>"""

def _html_script(nodes_json: str, edges_json: str, legend_json: str) -> str:
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};

// HTML-escape helper — prevents XSS when injecting graph data into innerHTML
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Server-precomputed layout (see _precompute_layout() in html.py): when every
// node carries x/y, skip vis-network's client-side ForceAtlas2 physics
// entirely instead of re-simulating from a random start on every page load —
// this is the dominant cost on first paint for large graphs, and past a few
// thousand nodes it doesn't just cost more, it hangs the tab outright (a
// grid layout is the fallback there, not physics — see _grid_layout() in
// html.py). HAS_LAYOUT is only ever false for an empty graph, where there
// are no nodes for physics to hang on anyway.
const HAS_LAYOUT = RAW_NODES.length > 0 && RAW_NODES.every(n => typeof n.x === 'number' && typeof n.y === 'number');

// Build vis datasets
const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  ...(HAS_LAYOUT ? {{ x: n.x, y: n.y }} : {{}}),
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _kind: n.kind, _degree: n.degree,
}})));

const edgesDS = new vis.DataSet(RAW_EDGES.map((e, i) => ({{
  id: i, from: e.from, to: e.to,
  label: '',
  title: e.title,
  dashes: e.dashes,
  width: e.width,
  color: e.color,
  arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
}})));

const container = document.getElementById('graph');
const network = new vis.Network(container, {{ nodes: nodesDS, edges: edgesDS }}, {{
  physics: {{
    enabled: !HAS_LAYOUT,
    solver: 'forceAtlas2Based',
    forceAtlas2Based: {{
      gravitationalConstant: -60,
      centralGravity: 0.005,
      springLength: 120,
      springConstant: 0.08,
      damping: 0.4,
      avoidOverlap: 0.8,
    }},
    stabilization: {{ iterations: 200, fit: true }},
  }},
  interaction: {{
    hover: true,
    tooltipDelay: 100,
    hideEdgesOnDrag: true,
    navigationButtons: false,
    keyboard: false,
  }},
  nodes: {{ shape: 'dot', borderWidth: 1.5 }},
  edges: {{ smooth: {{ type: 'continuous', roundness: 0.2 }}, selectionWidth: 3 }},
}});

if (HAS_LAYOUT) {{
  // Physics never runs, so nothing else fits the camera to the precomputed
  // positions — do it once explicitly (mirrors what stabilization: {{fit: true}}
  // gives the physics-driven path below). Positions are already known at
  // construction time, so this can happen immediately, no event wait needed.
  network.fit({{ animation: false }});
}} else {{
  network.once('stabilizationIterationsDone', () => {{
    network.setOptions({{ physics: {{ enabled: false }} }});
  }});
}}

function showInfo(nodeId) {{
  const n = nodesDS.get(nodeId);
  if (!n) return;
  const neighborIds = network.getConnectedNodes(nodeId);
  const neighborItems = neighborIds.map(nid => {{
    const nb = nodesDS.get(nid);
    const color = nb ? nb.color.background : '#555';
    return `<span class="neighbor-link" style="border-left-color:${{esc(color)}}" data-nid="${{esc(nid)}}">${{esc(nb ? nb.label : nid)}}</span>`;
  }}).join('');
  document.getElementById('info-content').innerHTML = `
    <div class="field"><b>${{esc(n.label)}}</b></div>
    <div class="field">Type: ${{esc(n._file_type || 'unknown')}}</div>
    ${{n._kind ? `<div class="field">Subtipo: ${{esc(kindLabel(n._kind))}}</div>` : ''}}
    <div class="field">Community: ${{esc(n._community_name)}}</div>
    <div class="field">Source: ${{esc(n._source_file || '-')}}</div>
    <div class="field">Degree: ${{n._degree}}</div>
    ${{neighborIds.length ? `<div class="field" style="margin-top:8px;color:#aaa;font-size:11px">Neighbors (${{neighborIds.length}})</div><div id="neighbors-list">${{neighborItems}}</div>` : ''}}
  `;
}}

function focusNode(nodeId) {{
  network.focus(nodeId, {{ scale: 1.4, animation: true }});
  network.selectNodes([nodeId]);
  showInfo(nodeId);
}}

// Neighbor links use a data attribute + one delegated listener rather than an
// inline onclick. A node id/label sourced from a document or a scraped URL
// (graphify add) can contain a double-quote; dropping the stringified id
// unescaped into a quoted onclick both broke every link and allowed a hostile
// source to inject an event handler into the local report (stored XSS, #1838).
// esc() on data-nid keeps the value inside the attribute; the listener reads it
// back verbatim. Bound to document so it survives the innerHTML rebuild that
// recreates #neighbors-list on each showInfo().
document.addEventListener('click', e => {{
  const el = e.target.closest('.neighbor-link');
  if (el && el.dataset.nid !== undefined) focusNode(el.dataset.nid);
}});

// Track hovered node — hover detection is more reliable than click params
let hoveredNodeId = null;
network.on('hoverNode', params => {{
  hoveredNodeId = params.node;
  container.style.cursor = 'pointer';
}});
network.on('blurNode', () => {{
  hoveredNodeId = null;
  container.style.cursor = 'default';
}});
container.addEventListener('click', () => {{
  if (hoveredNodeId !== null) {{
    showInfo(hoveredNodeId);
    network.selectNodes([hoveredNodeId]);
  }}
}});
network.on('click', params => {{
  if (params.nodes.length > 0) {{
    showInfo(params.nodes[0]);
  }} else if (hoveredNodeId === null) {{
    document.getElementById('info-content').innerHTML = '<span class="empty">Click a node to inspect it</span>';
  }}
}});

const searchInput = document.getElementById('search');
const searchResults = document.getElementById('search-results');
searchInput.addEventListener('input', () => {{
  const q = searchInput.value.toLowerCase().trim();
  searchResults.innerHTML = '';
  if (!q) {{ searchResults.style.display = 'none'; return; }}
  const matches = RAW_NODES.filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
  if (!matches.length) {{ searchResults.style.display = 'none'; return; }}
  searchResults.style.display = 'block';
  matches.forEach(n => {{
    const el = document.createElement('div');
    el.className = 'search-item';
    el.textContent = n.label;
    el.style.borderLeft = `3px solid ${{n.color.background}}`;
    el.style.paddingLeft = '8px';
    el.onclick = () => {{
      network.focus(n.id, {{ scale: 1.5, animation: true }});
      network.selectNodes([n.id]);
      showInfo(n.id);
      searchResults.style.display = 'none';
      searchInput.value = '';
    }};
    searchResults.appendChild(el);
  }});
}});
document.addEventListener('click', e => {{
  if (!searchResults.contains(e.target) && e.target !== searchInput)
    searchResults.style.display = 'none';
}});

// ---------------------------------------------------------------------
// Filters: facet-based node/edge visibility. Ported from the pattern used
// by grafo-explorer's PainelFiltros (facets with live visible/total counts,
// a degree range, and a text quick-filter) and reimplemented here in plain
// JS, since this report is a single static HTML file with no build step.
// Community facets are derived straight from RAW_NODES (community /
// community_name / color are always present on every node) rather than
// from LEGEND, which is only populated when the caller passes explicit
// community_labels — deriving from RAW_NODES keeps the facet correct
// (and non-empty) in the common unlabeled-community case too.
// ---------------------------------------------------------------------
function fmt(n) {{
  return Number(n).toLocaleString('en-US');
}}

function distinctCounts(values) {{
  const counts = new Map();
  values.forEach(v => {{
    const key = v == null || v === '' ? '' : String(v);
    counts.set(key, (counts.get(key) || 0) + 1);
  }});
  return counts;
}}

const maxDegree = RAW_NODES.reduce((m, n) => Math.max(m, n.degree || 0), 0);

const communityFacetMap = new Map();
RAW_NODES.forEach(n => {{
  const cid = String(n.community);
  if (!communityFacetMap.has(cid)) {{
    communityFacetMap.set(cid, {{
      value: cid,
      label: n.community_name || `Community ${{cid}}`,
      color: n.color && n.color.background,
      total: 0,
    }});
  }}
  communityFacetMap.get(cid).total += 1;
}});

function facetItemsFrom(counts, emptyLabel) {{
  return [...counts.entries()]
    .map(([value, total]) => ({{ value, label: value || emptyLabel, total }}))
    .sort((a, b) => b.total - a.total);
}}

// Friendly pt-BR labels for the granular `metadata.kind` values populated by
// the VB6 extractor (graphify/extractors/vb6.py). Other extractors (notably
// sql.py) do not populate this field today, so their nodes simply fall back
// to '(sem subtipo)' — this facet is VB6-only for now, deliberately, since
// tree-sitter-sql cannot yet structurally classify this codebase's T-SQL
// dialect (see extractor code comments / GRAPH_REPORT for details).
const KIND_LABELS = {{
  file: 'Arquivo',
  project: 'Projeto VB6',
  external_reference: 'Referência externa',
  interface_reference: 'Implements (interface)',
  class: 'Classe VB6',
  form: 'Form VB6',
  module: 'Módulo VB6',
  sub: 'Sub',
  function: 'Function',
  'property get': 'Property Get',
  'property let': 'Property Let',
  'property set': 'Property Set',
  declare: 'Declare (API externa)',
  type: 'Type (estrutura)',
  enum: 'Enum',
  event: 'Event',
  const: 'Constante',
  field: 'Campo',
  variable: 'Variável',
}};
function kindLabel(v) {{
  return KIND_LABELS[v] || v;
}}
function facetItemsFromKind(counts) {{
  return [...counts.entries()]
    .map(([value, total]) => ({{ value, label: value ? kindLabel(value) : '(sem subtipo)', total }}))
    .sort((a, b) => b.total - a.total);
}}

const FACETS = [
  {{
    key: 'fileType', title: 'Node type', appliesTo: 'node',
    items: facetItemsFrom(distinctCounts(RAW_NODES.map(n => n.file_type)), '(unknown)'),
    read: n => n.file_type == null ? '' : String(n.file_type),
  }},
  {{
    key: 'kind', title: 'Subtipo (VB6)', appliesTo: 'node',
    items: facetItemsFromKind(distinctCounts(RAW_NODES.map(n => n.kind))),
    read: n => n.kind == null ? '' : String(n.kind),
  }},
  {{
    key: 'community', title: 'Community', appliesTo: 'node',
    items: [...communityFacetMap.values()].sort((a, b) => b.total - a.total),
    read: n => String(n.community),
  }},
  {{
    key: 'relation', title: 'Relation', appliesTo: 'edge',
    items: facetItemsFrom(distinctCounts(RAW_EDGES.map(e => e.label)), '(unlabeled)'),
    read: e => e.label == null ? '' : String(e.label),
  }},
  {{
    key: 'confidence', title: 'Confidence', appliesTo: 'edge',
    items: facetItemsFrom(distinctCounts(RAW_EDGES.map(e => e.confidence)), '(unset)'),
    read: e => e.confidence == null ? '' : String(e.confidence),
  }},
];

const filters = {{ text: '', degreeMin: 0, degreeMax: maxDegree, hideIsolated: false, active: {{}} }};
FACETS.forEach(f => {{ filters.active[f.key] = new Set(f.items.map(it => it.value)); }});

function normalize(s) {{
  return String(s ?? '').toLowerCase();
}}

function computeVisible() {{
  const term = normalize(filters.text).trim();
  const visibleNodes = new Set();
  RAW_NODES.forEach(n => {{
    for (const f of FACETS) {{
      if (f.appliesTo !== 'node' || !f.items.length) continue;
      if (!filters.active[f.key].has(f.read(n))) return;
    }}
    const deg = n.degree || 0;
    if (deg < filters.degreeMin || deg > filters.degreeMax) return;
    if (term) {{
      const hay = normalize(`${{n.label}} ${{n.source_file || ''}} ${{n.community_name || ''}}`);
      if (!hay.includes(term)) return;
    }}
    visibleNodes.add(n.id);
  }});

  const visibleEdges = new Set();
  const connected = new Set();
  RAW_EDGES.forEach((e, i) => {{
    for (const f of FACETS) {{
      if (f.appliesTo !== 'edge' || !f.items.length) continue;
      if (!filters.active[f.key].has(f.read(e))) return;
    }}
    if (!visibleNodes.has(e.from) || !visibleNodes.has(e.to)) return;
    visibleEdges.add(i);
    connected.add(e.from);
    connected.add(e.to);
  }});

  if (filters.hideIsolated) {{
    [...visibleNodes].forEach(id => {{
      if (!connected.has(id)) visibleNodes.delete(id);
    }});
  }}

  return {{ visibleNodes, visibleEdges }};
}}

function countActive() {{
  let n = 0;
  FACETS.forEach(f => {{ if (f.items.length && filters.active[f.key].size !== f.items.length) n++; }});
  if (filters.text.trim()) n++;
  if (filters.degreeMin > 0 || filters.degreeMax < maxDegree) n++;
  if (filters.hideIsolated) n++;
  return n;
}}

const facetGroupsEl = document.getElementById('facet-groups');
const filtersBadge = document.getElementById('filters-badge');

function renderFacetGroups() {{
  facetGroupsEl.innerHTML = '';
  FACETS.forEach(f => {{
    if (!f.items.length) return;
    const section = document.createElement('div');
    section.className = 'facet-section';
    section.innerHTML = `
      <div class="facet-section-title">
        <span>${{esc(f.title)}}</span>
        <span class="facet-links">
          <button type="button" data-all="${{esc(f.key)}}">all</button>&middot;<button type="button" data-none="${{esc(f.key)}}">none</button>
        </span>
      </div>
      <div class="facet-list" data-list="${{esc(f.key)}}"></div>
    `;
    facetGroupsEl.appendChild(section);
  }});
  facetGroupsEl.querySelectorAll('button[data-all]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const f = FACETS.find(x => x.key === btn.dataset.all);
      filters.active[f.key] = new Set(f.items.map(it => it.value));
      refresh();
    }});
  }});
  facetGroupsEl.querySelectorAll('button[data-none]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      const f = FACETS.find(x => x.key === btn.dataset.none);
      filters.active[f.key] = new Set();
      refresh();
    }});
  }});
}}

function updateFacetCounts(visibleNodes, visibleEdges) {{
  FACETS.forEach(f => {{
    const list = facetGroupsEl.querySelector(`[data-list="${{f.key}}"]`);
    if (!list) return;
    const visibleByValue = f.appliesTo === 'node'
      ? distinctCounts(RAW_NODES.filter(n => visibleNodes.has(n.id)).map(f.read))
      : distinctCounts(RAW_EDGES.filter((e, i) => visibleEdges.has(i)).map(f.read));
    list.innerHTML = '';
    f.items.forEach(it => {{
      const active = filters.active[f.key].has(it.value);
      const visible = visibleByValue.get(it.value) || 0;
      const row = document.createElement('div');
      row.className = 'facet-item' + (active ? '' : ' inactive') + (it.total === 0 ? ' zero' : '');
      row.innerHTML = `
        <input type="checkbox" class="chk" tabindex="-1" ${{active ? 'checked' : ''}}>
        ${{it.color ? `<span class="facet-dot" style="background:${{esc(it.color)}}"></span>` : ''}}
        <span class="facet-label" title="${{esc(it.label)}}">${{esc(it.label)}}</span>
        <span class="facet-only">only</span>
        <span class="facet-count">${{visible !== it.total ? `${{fmt(visible)}}/${{fmt(it.total)}}` : fmt(it.total)}}</span>
      `;
      row.addEventListener('click', (e) => {{
        if (e.target.classList.contains('facet-only')) {{
          filters.active[f.key] = new Set([it.value]);
        }} else {{
          const s = filters.active[f.key];
          if (s.has(it.value)) s.delete(it.value); else s.add(it.value);
        }}
        refresh();
      }});
      list.appendChild(row);
    }});
  }});
}}

// Previous visible sets, so refresh() below can send vis-network's DataSet
// only the nodes/edges whose hidden state actually flipped instead of
// rewriting every item on every filter click. DataSet.update() does real
// bookkeeping per item (change events, subscriber notification, redraw
// scheduling) — that per-item cost, multiplied by thousands of unchanged
// items on every click, is what made filtering feel sluggish on large
// graphs, not the plain array iteration used to compute the diff below.
// null is the "never run yet" sentinel forcing one full initial pass, since
// DataSet items have no `hidden` field at all until first set.
let prevVisibleNodes = null;
let prevVisibleEdges = null;

function refresh() {{
  const {{ visibleNodes, visibleEdges }} = computeVisible();

  if (prevVisibleNodes === null) {{
    nodesDS.update(RAW_NODES.map(n => ({{ id: n.id, hidden: !visibleNodes.has(n.id) }})));
  }} else {{
    const changed = [];
    RAW_NODES.forEach(n => {{
      const now = visibleNodes.has(n.id);
      if (prevVisibleNodes.has(n.id) !== now) changed.push({{ id: n.id, hidden: !now }});
    }});
    if (changed.length) nodesDS.update(changed);
  }}
  prevVisibleNodes = visibleNodes;

  if (prevVisibleEdges === null) {{
    edgesDS.update(RAW_EDGES.map((e, i) => ({{ id: i, hidden: !visibleEdges.has(i) }})));
  }} else {{
    const changed = [];
    RAW_EDGES.forEach((e, i) => {{
      const now = visibleEdges.has(i);
      if (prevVisibleEdges.has(i) !== now) changed.push({{ id: i, hidden: !now }});
    }});
    if (changed.length) edgesDS.update(changed);
  }}
  prevVisibleEdges = visibleEdges;

  updateFacetCounts(visibleNodes, visibleEdges);
  const active = countActive();
  filtersBadge.textContent = String(active);
  filtersBadge.style.display = active > 0 ? 'inline-block' : 'none';
  document.getElementById('degree-range-label').textContent = `${{fmt(filters.degreeMin)}}–${{fmt(filters.degreeMax)}}`;
}}

renderFacetGroups();

const facetTextInput = document.getElementById('facet-text');
// Debounced: computeVisible() + the facet-count recompute are O(nodes+edges),
// so on a large graph re-running that on every keystroke (vs. once ~200ms
// after typing pauses) is the difference between smooth and janky typing.
let facetTextDebounce = null;
facetTextInput.addEventListener('input', () => {{
  filters.text = facetTextInput.value;
  if (facetTextDebounce) clearTimeout(facetTextDebounce);
  facetTextDebounce = setTimeout(refresh, 200);
}});

const degreeMinInput = document.getElementById('degree-min');
const degreeMaxInput = document.getElementById('degree-max');
degreeMinInput.value = 0;
degreeMaxInput.value = maxDegree;
degreeMinInput.max = String(maxDegree);
degreeMaxInput.max = String(maxDegree);
degreeMinInput.addEventListener('input', () => {{
  filters.degreeMin = Math.max(0, Number(degreeMinInput.value) || 0);
  refresh();
}});
degreeMaxInput.addEventListener('input', () => {{
  const v = Number(degreeMaxInput.value);
  filters.degreeMax = degreeMaxInput.value !== '' && Number.isFinite(v) ? v : maxDegree;
  refresh();
}});

document.getElementById('hide-isolated-cb').addEventListener('change', (e) => {{
  filters.hideIsolated = e.target.checked;
  refresh();
}});

document.getElementById('filters-clear').addEventListener('click', () => {{
  filters.text = '';
  filters.degreeMin = 0;
  filters.degreeMax = maxDegree;
  filters.hideIsolated = false;
  FACETS.forEach(f => {{ filters.active[f.key] = new Set(f.items.map(it => it.value)); }});
  facetTextInput.value = '';
  degreeMinInput.value = 0;
  degreeMaxInput.value = maxDegree;
  document.getElementById('hide-isolated-cb').checked = false;
  refresh();
}});

refresh();
</script>"""


def _html_document_title(output_path: str) -> str:
    """Return a portable label for the graph.html <title>.

    Tracked artifacts must not embed the generator host absolute path
    (regression of #433; reported again as #2598 on Windows). Keep from the
    configured output-dir bare name (``graphify-out`` / ``GRAPHIFY_OUT``
    basename) onward — portable in every case; otherwise fall back to a
    cwd-relative label, and finally the filename only.
    """
    from graphify.paths import GRAPHIFY_OUT_NAME

    raw = str(output_path).replace("\\", "/")
    # Drop Windows drive prefix so Path parts are comparable on any OS.
    if len(raw) >= 3 and raw[1] == ":" and raw[0].isalpha() and raw[2] == "/":
        raw = raw[2:]  # "/Users/..." style after drive strip
    p = Path(raw)

    parts = list(Path(raw).parts)
    # Path("C:/Users/..") on POSIX may keep "C:" as first part — strip it.
    if parts and len(parts[0]) == 2 and parts[0][1] == ":" and parts[0][0].isalpha():
        parts = parts[1:]
    # Prefer keeping from the output-dir marker onward: portable in every
    # case, whereas a cwd-relative path still leaks host/user segments when
    # the graph is built from a directory ABOVE the project (#2598 follow-up).
    marker = GRAPHIFY_OUT_NAME
    for i, part in enumerate(parts):
        if part == marker or part.startswith("graphify-out"):
            return "/".join(parts[i:])

    # No standard out-dir marker (fully custom output path): fall back to a
    # cwd-relative label when the target is under cwd, else the bare filename.
    try:
        resolved = p if p.is_absolute() else (Path.cwd() / p)
        rel = resolved.resolve().relative_to(Path.cwd().resolve())
        label = rel.as_posix()
        if label and label != ".":
            return label
    except (ValueError, OSError, RuntimeError):
        pass

    name = p.name
    return name if name else "graph.html"

def to_html(
    G: nx.Graph,
    communities: dict[int, list[str]],
    output_path: str,
    community_labels: dict[int, str] | None = None,
    member_counts: dict[int, int] | None = None,
    node_limit: int | None = None,
    learning_overlay: dict | None = None,
) -> bool:
    """Generate an interactive vis.js HTML visualization of the graph.

    Features: node size by degree, click-to-inspect panel, search box,
    physics clustering by community, confidence-styled edges, and a
    facet-based filters panel (node type, VB6 subtype, community, relation,
    confidence, degree range, text quick-filter, hide-isolated) with live
    visible/total counts per facet value. Raises ValueError if graph exceeds
    MAX_NODES_FOR_VIZ.

    Performance: node positions are always precomputed server-side
    (`_precompute_layout`) so the browser never runs its ForceAtlas2 physics
    simulation, which — past roughly a couple thousand nodes — doesn't just
    cost more, it hangs the tab outright (confirmed against a real ~20k-node
    export). Up to `_LAYOUT_MAX_NODES` this is an organic spring_layout;
    above it, a cheap O(n) grid (`_grid_layout`) that carries no topological
    meaning (nodes don't cluster visually by community) but keeps the page
    loading in seconds and the filters panel fully usable — which is what
    matters once a graph is too large to eyeball anyway. Edges touching
    high-degree hub nodes get their opacity dampened (`_hub_alpha`) to
    reduce visual overdraw; and the client-side filter panel updates the
    vis.js DataSets incrementally (only nodes/edges whose visibility
    actually changed) with a debounced text filter, keeping interaction
    responsive on graphs with thousands of nodes.

    If member_counts is provided (aggregated community view), node sizes are
    based on community member counts rather than graph degree.

    If node_limit is set and the graph exceeds it, automatically builds an
    aggregated community-level meta-graph instead of raising ValueError.

    Returns True when the output was written. Returns False when an aggregated
    view would contain fewer than two communities and is intentionally skipped.
    """
    limit = node_limit if node_limit is not None else _viz_node_limit()
    if G.number_of_nodes() > limit:
        if node_limit is not None:
            # Build aggregated community meta-graph
            from collections import Counter as _Counter
            import networkx as _nx
            print(f"Graph has {G.number_of_nodes()} nodes (above {limit} limit). Building aggregated community view...")
            node_to_community = {nid: cid for cid, members in communities.items() for nid in members}
            meta = _nx.Graph()
            for cid, members in communities.items():
                meta.add_node(str(cid), label=(community_labels or {}).get(cid, f"Community {cid}"))
            edge_counts = _Counter()
            for u, v in G.edges():
                cu, cv = node_to_community.get(u), node_to_community.get(v)
                if cu is not None and cv is not None and cu != cv:
                    edge_counts[(min(cu, cv), max(cu, cv))] += 1
            for (cu, cv), w in edge_counts.items():
                meta.add_edge(str(cu), str(cv), weight=w,
                              relation=f"{w} cross-community edges", confidence="AGGREGATED")
            if meta.number_of_nodes() <= 1:
                print("Single community - aggregated view not useful. Skipping graph.html.")
                return False
            meta_communities = {cid: [str(cid)] for cid in communities}
            mc = {cid: len(members) for cid, members in communities.items()}
            # Remap hyperedges from semantic node IDs to community IDs
            raw_hyperedges = G.graph.get("hyperedges", [])
            if raw_hyperedges:
                remapped = []
                for he in raw_hyperedges:
                    he_members = he.get("nodes", [])
                    comm_ids, seen = [], set()
                    for nid in he_members:
                        c = node_to_community.get(nid)
                        if c is None:
                            continue
                        s = str(c)
                        if s in seen:
                            continue
                        seen.add(s)
                        comm_ids.append(s)
                    if len(comm_ids) < 2:
                        continue
                    remapped.append({
                        "id": he.get("id", ""),
                        "label": he.get("label") or he.get("relation", "").replace("_", " "),
                        "nodes": comm_ids,
                    })
                meta.graph["hyperedges"] = remapped
            written = to_html(meta, meta_communities, output_path,
                              community_labels=community_labels, member_counts=mc)
            if not written:
                return False
            print(f"graph.html written (aggregated: {meta.number_of_nodes()} community nodes, {meta.number_of_edges()} cross-community edges)")
            print("Tip: run with --obsidian for full node-level detail.")
            return True
        raise ValueError(
            f"Graph has {G.number_of_nodes()} nodes - too large for HTML viz "
            f"(limit: {limit}). Use --no-viz, raise GRAPHIFY_VIZ_NODE_LIMIT, "
            f"or reduce input size."
        )

    node_community = _node_community_map(communities)
    degree = dict(G.degree())
    max_deg = max(degree.values(), default=1) or 1
    max_mc = (max(member_counts.values(), default=1) or 1) if member_counts else 1
    positions = _precompute_layout(G)

    # Work-memory overlay (derived sidecar). When not passed explicitly, load it
    # best-effort from the sibling .graphify_learning.json next to the output
    # graph.html (which lives beside graph.json). Empty/missing => no learning
    # fields, so the un-annotated render is byte-identical to pre-feature.
    if learning_overlay is None:
        learning_overlay = {}
        try:
            from graphify.reflect import load_learning_overlay as _llo
            learning_overlay = _llo(Path(output_path))
        except Exception:
            learning_overlay = {}
    # Status -> ring color. preferred=green, contested=amber. Tentative gets no
    # ring (it's not yet trustworthy enough to highlight in the map).
    _RING = {"preferred": "#22c55e", "contested": "#f59e0b"}

    # Build nodes list for vis.js
    vis_nodes = []
    for node_id, data in G.nodes(data=True):
        cid = node_community.get(node_id, 0)
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        label = sanitize_label(data.get("label", node_id))
        deg = degree.get(node_id, 1)
        if member_counts:
            mc = member_counts.get(cid, 1)
            size = 10 + 30 * (mc / max_mc)
            font_size = 12
        else:
            size = 10 + 30 * (deg / max_deg)
            # Only show label for high-degree nodes by default; others show on hover
            font_size = 12 if deg >= max_deg * 0.15 else 0
        node = {
            "id": node_id,
            "label": label,
            "color": {"background": color, "border": color, "highlight": {"background": "#ffffff", "border": color}},
            "size": round(size, 1),
            "font": {"size": font_size, "color": "#ffffff"},
            "title": _html.escape(label),
            "community": cid,
            "community_name": sanitize_label((community_labels or {}).get(cid, f"Community {cid}")),
            "source_file": sanitize_label(str(data.get("source_file") or "")),
            "file_type": data.get("file_type", ""),
            "kind": sanitize_label(str((data.get("metadata") or {}).get("kind", "") or "")),
            "degree": deg,
        }
        if positions is not None and node_id in positions:
            px, py = positions[node_id]
            node["x"] = round(float(px) * _LAYOUT_SCALE, 1)
            node["y"] = round(float(py) * _LAYOUT_SCALE, 1)
        # Conditional learning fields — only present for annotated nodes, so
        # un-annotated output keeps the exact pre-feature node dict shape.
        entry = learning_overlay.get(str(node_id)) if learning_overlay else None
        if entry:
            status = sanitize_label(str(entry.get("status", "")))
            stale = bool(entry.get("stale"))
            node["learning_status"] = status
            node["learning_stale"] = stale
            ring = _RING.get(status)
            if ring:
                # Status-colored ring via the border; stale => desaturated +
                # dashed (vis.js supports per-node `shapeProperties.borderDashes`).
                if stale:
                    ring = "#9ca3af"
                    node["shapeProperties"] = {"borderDashes": [4, 4]}
                node["borderWidth"] = 3
                node["color"] = {
                    "background": color, "border": ring,
                    "highlight": {"background": "#ffffff", "border": ring},
                }
            # Lesson line appended to the hover title.
            if status == "contested":
                lesson = f"Lesson: contested (useful {entry.get('uses', 0)} / dead-end {entry.get('neg', 0)})"
            elif status == "preferred":
                lesson = f"Lesson: preferred source ({entry.get('uses', 0)} useful, score={entry.get('score', 0)})"
            else:
                lesson = f"Lesson: {status} ({entry.get('uses', 0)} useful)"
            if stale:
                lesson += " [code changed — re-verify]"
            node["title"] = _html.escape(label) + "\n" + _html.escape(sanitize_label(lesson))
        vis_nodes.append(node)

    # Build edges list. Restore original edge direction from _src/_tgt
    # (stashed by build.py for exactly this reason): undirected NetworkX
    # canonicalizes endpoint order, which would otherwise flip the arrow
    # for `calls` and `rationale_for` in the rendered graph (#563).
    vis_edges = []
    for u, v, data in G.edges(data=True):
        confidence = data.get("confidence", "EXTRACTED")
        relation = data.get("relation", "")
        true_src = data.get("_src", u)
        true_tgt = data.get("_tgt", v)
        base_opacity = 0.7 if confidence == "EXTRACTED" else 0.35
        hub_degree = max(degree.get(true_src, 0), degree.get(true_tgt, 0))
        vis_edges.append({
            "from": true_src,
            "to": true_tgt,
            "label": relation,
            "title": _html.escape(f"{relation} [{confidence}]"),
            "dashes": confidence != "EXTRACTED",
            "width": 2 if confidence == "EXTRACTED" else 1,
            "color": {"opacity": round(_hub_alpha(base_opacity, hub_degree), 3)},
            "confidence": confidence,
        })

    # Build community legend data
    legend_data = []
    for cid in sorted((community_labels or {}).keys()):
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        lbl = _html.escape(sanitize_label((community_labels or {}).get(cid, f"Community {cid}")))
        n = member_counts.get(cid, len(communities.get(cid, []))) if member_counts else len(communities.get(cid, []))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": n})

    # Escape </script> sequences so embedded JSON cannot break out of the script tag
    def _js_safe(obj) -> str:
        return json.dumps(obj).replace("</", "<\\/")

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)
    hyperedges_json = _js_safe(getattr(G, "graph", {}).get("hyperedges", []))
    title = _html.escape(sanitize_label(_html_document_title(output_path)))
    stats = f"{G.number_of_nodes()} nodes &middot; {G.number_of_edges()} edges &middot; {len(communities)} communities"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>graphify - {title}</title>
<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/vis-network.min.js"
        integrity="sha384-Ux6phic9PEHJ38YtrijhkzyJ8yQlH8i/+buBR8s3mAZOJrP1gwyvAcIYl3GWtpX1"
        crossorigin="anonymous"></script>
{_html_styles()}
</head>
<body>
<div id="graph"></div>
<div id="sidebar">
  <div id="search-wrap">
    <input id="search" type="text" placeholder="Search nodes..." autocomplete="off">
    <div id="search-results"></div>
  </div>
  <div id="info-panel">
    <h3>Node Info</h3>
    <div id="info-content"><span class="empty">Click a node to inspect it</span></div>
  </div>
  <div id="filters-wrap">
    <div class="filters-header">
      <h3>Filters <span id="filters-badge" class="filters-badge">0</span></h3>
      <button type="button" id="filters-clear" class="filters-clear">Clear</button>
    </div>
    <div id="facet-text-wrap">
      <input id="facet-text" type="text" placeholder="Filter by name, file, community&hellip;" autocomplete="off">
    </div>
    <div id="facet-groups"></div>
    <div class="facet-section" id="degree-section">
      <div class="facet-section-title"><span>Degree (connections)</span><span id="degree-range-label"></span></div>
      <div class="degree-inputs">
        <input type="number" id="degree-min" min="0" step="1" aria-label="Minimum degree">
        <input type="number" id="degree-max" min="0" step="1" aria-label="Maximum degree">
      </div>
    </div>
    <label class="toggle-row"><input type="checkbox" id="hide-isolated-cb" class="chk"> Hide isolated nodes</label>
  </div>
  <div id="stats">{stats}</div>
</div>
{_html_script(nodes_json, edges_json, legend_json)}
{_hyperedge_script(hyperedges_json)}
</body>
</html>"""

    write_text_atomic(output_path, html)
    return True
