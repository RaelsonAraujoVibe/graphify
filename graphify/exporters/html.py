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
# `_precompute_layout` falls back to `_radial_layout` instead — see that
# function for why a cheap fallback beats leaving physics enabled.
_LAYOUT_MAX_NODES = 1_200
# Spread of the organic (spring_layout) precomputed layout, in vis-network
# canvas units. spring_layout returns coordinates roughly within [-1, 1];
# this scale keeps spacing in the same rough order of magnitude vis-network's
# own ForceAtlas2 physics used to produce (springLength=120 across thousands
# of nodes), so the static layout doesn't look cramped or absurdly sparse
# relative to node/label size. `_radial_layout` does its own, size-aware
# spacing instead of using this constant — see its docstring for why.
_LAYOUT_SCALE = 800.0
# Node radius is always in [10, 40] canvas px by construction — both size
# formulas in to_html()'s node loop (`10 + 30 * deg/max_deg` and, for the
# aggregated view, `10 + 30 * mc/max_mc`) are ratios against their own
# maximum, so the single largest node in *any* graph is always exactly 40.
# `_radial_layout` uses this ceiling — not the actual per-node size, which
# would need a second pass — to size its spiral spacing so that even two
# adjacent maximum-size nodes never touch.
_MAX_NODE_RADIUS = 40.0
# Spread of each *per-community* local layout (Camada 4 drill-down), in the
# same canvas-unit space as _LAYOUT_SCALE but deliberately smaller: a local
# layout only has to separate one community's own members from each other,
# not the whole graph. Every observed real-world community (pd-desenv's
# largest is under 400 members) is comfortably inside _LAYOUT_MAX_NODES, so
# the per-community spring_layout in `_precompute_community_layouts` is
# always fast even though the whole-graph layout above it isn't.
_LOCAL_LAYOUT_SCALE = 300.0
# Hard cap on how many real (drilled-down) nodes may be expanded onto the
# canvas at once, across however many communities are open simultaneously.
# Matches _LAYOUT_MAX_NODES deliberately: that's the point past which even a
# single server-side spring_layout call stops being a sub-second cost, and
# past which vis-network's own rendering gets sluggish with everything
# unhidden at once. Enforced client-side in `_html_script()`.
_DRILLDOWN_NODE_CAP = 1_200


def _radial_layout(G: nx.Graph) -> dict:
    """Deterministic O(n) fallback layout: no simulation, no iteration.

    Used above `_LAYOUT_MAX_NODES` (or if spring_layout itself raises)
    instead of leaving vis-network's client-side ForceAtlas2 physics
    enabled, which — reproduced against a real 19,706-node/23,177-edge
    graphify export — hangs the tab outright rather than just running slow
    (the browser tab never became responsive again within 100+ seconds of
    the page's `load` event firing).

    Arranges nodes on a sunflower/phyllotaxis spiral (radius grows with
    sqrt(index), angle advances by the golden angle each step — the same
    trick that packs sunflower seeds or camera-lens elements with no visible
    seams) instead of a rectangular grid: an early version of this fallback
    used literal rows and columns, which is O(n) and never overlaps but
    reads as a dense rectangular block with none of spring_layout's organic,
    roughly-circular silhouette. The spiral keeps the O(n)/no-iteration
    guarantee while looking much closer to what spring_layout would have
    produced. It carries none of spring_layout's topology-aware clustering
    (nodes don't group visually by community/connectivity — see the
    graph.html docstring's Performance note for the tradeoff), but that's
    still the right trade once a graph is this large: finding and filtering
    specific nodes matters more than eyeballing organic clustering among
    tens of thousands of dots.

    Nodes with no edges at all — nothing to relate them to any neighbor's
    position — are sorted to the end of the spiral and given an extra
    outward radius push past the connected cluster's own outer edge, so
    they form a sparser ring further out: still panned-to and clickable,
    but visually set apart from the connected mass at the center instead of
    interleaved throughout it.

    Unlike spring_layout, this returns *final* canvas-unit coordinates
    directly (the caller must not re-apply `_LAYOUT_SCALE`) — point spacing
    is derived from `_MAX_NODE_RADIUS`, not a fixed total canvas size, so
    the canvas grows with node count instead of compressing everything into
    the same fixed span no matter how many nodes it holds, in exchange for
    every node actually being visible.
    """
    import math
    nodes = list(G.nodes())
    n = len(nodes)
    if n == 0:
        return {}
    degree = dict(G.degree())
    # Highest-degree nodes get the smallest spiral indices — and therefore
    # the smallest radii, closest to center — so the result reads as a
    # hub-centric cluster the way spring_layout's spring pull naturally
    # produces (well-connected nodes get tugged toward the center of mass),
    # rather than an arbitrary ring wherever hubs happened to fall in the
    # graph's iteration order. This also gives the zoomed-out LOD level in
    # `_html_script()` a coherent overview instead of a scattered sample.
    connected = sorted(
        (nd for nd in nodes if degree.get(nd, 0) > 0),
        key=lambda nd: -degree.get(nd, 0),
    )
    isolated = [nd for nd in nodes if degree.get(nd, 0) == 0]

    cell = _MAX_NODE_RADIUS * 2.25  # diameter + a small margin, canvas px
    # Phyllotaxis packing: point i sits at radius c*sqrt(i), so the disc
    # covered by the first i points grows linearly with i (area ~ pi*c^2*i).
    # Solving for one point's share of that area to be at least `cell`'s
    # worth of room — with a safety margin, since the golden-angle spiral is
    # an even *average* density rather than an exact circle-packing — keeps
    # neighbors from touching in practice.
    c = (cell / math.sqrt(math.pi)) * 1.15
    golden_angle = math.pi * (3.0 - math.sqrt(5.0))

    positions: dict = {}
    for i, node in enumerate(connected):
        r = c * math.sqrt(i + 1)
        theta = i * golden_angle
        positions[node] = (r * math.cos(theta), r * math.sin(theta))

    if isolated:
        boundary_r = c * math.sqrt(len(connected)) if connected else 0.0
        gap = cell * 3  # visible separation from the connected cluster
        for j, node in enumerate(isolated):
            r = boundary_r + gap + c * math.sqrt(j + 1)
            theta = (len(connected) + j) * golden_angle
            positions[node] = (r * math.cos(theta), r * math.sin(theta))

    return positions


def _precompute_layout(G: nx.Graph) -> dict | None:
    """Best-effort server-side node layout, computed once at export time.

    Letting vis-network run its ForceAtlas2 physics simulation client-side
    from a random start is the dominant cost on first paint for any graph
    with more than a couple thousand nodes — and past roughly `_LAYOUT_MAX_NODES`
    it doesn't just cost more, it hangs the tab outright (see `_radial_layout`).
    Precomputing positions here and shipping them in the node data lets the
    browser skip physics entirely (see the HAS_LAYOUT flag in `_html_script()`).

    Prefers the organic spring_layout up to `_LAYOUT_MAX_NODES` nodes, falls
    back to the cheap `_radial_layout` above that (or if spring_layout itself
    raises — e.g. missing scipy, a degenerate/disconnected graph shape).
    Returns None only for an empty graph, in which case there's nothing to
    lay out and the (harmless, since there are no nodes) physics-driven JS
    path runs instead.

    Always returns *final* canvas-unit coordinates — `_LAYOUT_SCALE` is
    applied here for the spring_layout branch; `_radial_layout` computes its
    own final units directly. Callers must not scale the result further.
    """
    n = G.number_of_nodes()
    if n == 0:
        return None
    if n <= _LAYOUT_MAX_NODES:
        try:
            raw = nx.spring_layout(G, seed=42)
            return {node: (x * _LAYOUT_SCALE, y * _LAYOUT_SCALE) for node, (x, y) in raw.items()}
        except Exception:
            pass
    return _radial_layout(G)


def _precompute_community_layouts(G: nx.Graph, communities: dict[int, list[str]]) -> dict:
    """Per-community local layout, for Camada 4 drill-down node placement.

    Each community's members get their own small spring_layout, computed
    independently of every other community. That's what lets community
    expansion happen incrementally in the browser with zero client-side
    layout math: the server already knows exactly where each real node
    should sit *relative to its own community's center*, and the browser
    only has to add the community's meta-node's own current x/y (its
    resting position in the already-rendered outer view) once, at expand
    time — see `expandCommunity()` in `_html_script()`.

    Every community observed in practice (pd-desenv's largest is well under
    400 members) is comfortably inside `_LAYOUT_MAX_NODES`, so the
    spring_layout call here is always fast — but a defensive spiral fallback
    (`_radial_layout`, already zero-centered by construction) still applies
    per-community, in case some other codebase produces one outsized
    community. Single-node communities are skipped: nothing to lay out
    relative to (the sole member simply sits at the community's own center,
    offset 0).

    Returns *local*, not-yet-offset canvas-unit coordinates, keyed by node
    id, for every node that belongs to a community with 2+ members.
    """
    positions: dict = {}
    for cid, members in communities.items():
        sub_nodes = [m for m in members if G.has_node(m)]
        if len(sub_nodes) <= 1:
            continue
        sub = G.subgraph(sub_nodes)
        if len(sub_nodes) <= _LAYOUT_MAX_NODES:
            try:
                raw = nx.spring_layout(sub, seed=42)
                for node, (x, y) in raw.items():
                    positions[node] = (x * _LOCAL_LAYOUT_SCALE, y * _LOCAL_LAYOUT_SCALE)
                continue
            except Exception:
                pass
        positions.update(_radial_layout(sub))
    return positions


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


def _build_vis_nodes(
    G: nx.Graph,
    node_community: dict,
    degree: dict,
    max_deg: int,
    community_labels: dict[int, str] | None,
    member_counts: dict[int, int] | None,
    max_mc: int,
    positions: dict | None,
    learning_overlay: dict,
) -> list[dict]:
    """Build the vis.js node dict list for one graph.

    Pulled out of `to_html()` so the exact same node-shaping logic can be
    reused for two different graphs in the same export: whichever graph
    `to_html()` was actually called with (the primary render), and — when
    Camada 4 drill-down data is being embedded — the *original*, pre-
    aggregation per-symbol graph (see `to_html()`'s `full_graph` parameter).
    Keeping both call sites in lockstep means a future field added to a node
    (a new facet, say) only needs to be added once.
    """
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
            "is_meta": bool(data.get("is_meta", False)),
        }
        if member_counts:
            node["member_count"] = member_counts.get(cid, 0)
        if positions is not None and node_id in positions:
            # _precompute_layout / _precompute_community_layouts already return
            # final canvas-unit coordinates — no further scaling here.
            px, py = positions[node_id]
            node["x"] = round(float(px), 1)
            node["y"] = round(float(py), 1)
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
    return vis_nodes


def _build_vis_edges(G: nx.Graph, degree: dict) -> list[dict]:
    """Build the vis.js edge dict list for one graph. See `_build_vis_nodes`."""
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
    return vis_edges


_RING = {"preferred": "#22c55e", "contested": "#f59e0b"}


def _html_styles() -> str:
    return """<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; display: flex; height: 100vh; overflow: hidden; }
  #graph { flex: 1; }
  /* vis-network takes ownership of #graph's innerHTML on construction (wipes
     anything already inside it), so this can't be a child of #graph - it's a
     sibling instead, fixed-positioned to overlay the same area (viewport
     minus the fixed 280px sidebar). */
  #empty-state { position: fixed; top: 0; left: 0; right: 280px; bottom: 0; display: none; align-items: center; justify-content: center; pointer-events: none; text-align: center; padding: 32px; }
  #empty-state span { color: #6a6a8e; font-size: 14px; line-height: 1.6; max-width: 360px; }
  #empty-state b { color: #93c5fd; font-weight: 600; }
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

def _html_script(
    nodes_json: str,
    edges_json: str,
    legend_json: str,
    full_nodes_json: str = "[]",
    full_edges_json: str = "[]",
    drilldown_cap: int = _DRILLDOWN_NODE_CAP,
) -> str:
    return f"""<script>
const RAW_NODES = {nodes_json};
const RAW_EDGES = {edges_json};
const LEGEND = {legend_json};
// Camada 4 (semantic zoom / community drill-down): only non-empty when this
// export is an aggregated community view built with `--node-limit` AND the
// full per-symbol graph was embedded alongside it — see `to_html()`'s
// `full_graph` parameter in html.py. Empty on every other export (the
// common case), in which case every HAS_DRILLDOWN branch below is dead code
// and behavior is byte-for-byte the pre-Camada-4 behavior.
const FULL_NODES = {full_nodes_json};
const FULL_EDGES = {full_edges_json};
const DRILLDOWN_NODE_CAP = {drilldown_cap};

// HTML-escape helper — prevents XSS when injecting graph data into innerHTML
function esc(s) {{
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

// Server-precomputed layout (see _precompute_layout() in html.py): when every
// node carries x/y, skip vis-network's client-side ForceAtlas2 physics
// entirely instead of re-simulating from a random start on every page load —
// this is the dominant cost on first paint for large graphs, and past a few
// thousand nodes it doesn't just cost more, it hangs the tab outright (a
// spiral layout is the fallback there, not physics — see _radial_layout()
// in html.py). HAS_LAYOUT is only ever false for an empty graph, where there
// are no nodes for physics to hang on anyway.
const HAS_LAYOUT = RAW_NODES.length > 0 && RAW_NODES.every(n => typeof n.x === 'number' && typeof n.y === 'number');

// Build vis datasets
const nodesDS = new vis.DataSet(RAW_NODES.map(n => ({{
  id: n.id, label: n.label, color: n.color, size: n.size,
  font: n.font, title: n.title,
  ...(HAS_LAYOUT ? {{ x: n.x, y: n.y }} : {{}}),
  _community: n.community, _community_name: n.community_name,
  _source_file: n.source_file, _file_type: n.file_type, _kind: n.kind, _degree: n.degree,
  _is_meta: !!n.is_meta,
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

// Captured once, right after the camera first shows the whole graph — the
// LOD block below (after Camada 4's activeNodeArray() is defined) uses this
// as its zoom-level baseline, so its thresholds scale to however zoomed-out
// "see everything" actually is for this particular graph/layout instead of
// hardcoding an absolute vis-network scale number.
let lodInitialScale = 1;

if (HAS_LAYOUT) {{
  // Physics never runs, so nothing else fits the camera to the precomputed
  // positions — do it once explicitly (mirrors what stabilization: {{fit: true}}
  // gives the physics-driven path below). Positions are already known at
  // construction time, so this can happen immediately, no event wait needed.
  network.fit({{ animation: false }});
  lodInitialScale = network.getScale();
}} else {{
  network.once('stabilizationIterationsDone', () => {{
    network.setOptions({{ physics: {{ enabled: false }} }});
  }});
}}

// ---------------------------------------------------------------------
// Camada 4: semantic zoom / community drill-down. Double-click a community
// meta-node to swap it for its real members (positioned via the per-
// community local layout precomputed server-side in
// `_precompute_community_layouts`, offset by the meta-node's own current
// canvas position — zero client-side layout math, so this never risks the
// hang the precomputed top-level layout was built to avoid in the first
// place). Double-click any expanded real node to collapse its community
// back down. Everything in this block is a no-op when HAS_DRILLDOWN is
// false (every export except an aggregated `--node-limit` view).
// ---------------------------------------------------------------------
const HAS_DRILLDOWN = FULL_NODES.length > 0;

const communityMembers = new Map();
FULL_NODES.forEach(n => {{
  if (!communityMembers.has(n.community)) communityMembers.set(n.community, []);
  communityMembers.get(n.community).push(n);
}});

const metaNodeByCommunity = new Map();
const metaNodeById = new Map();
RAW_NODES.forEach(n => {{
  if (n.is_meta) {{
    metaNodeByCommunity.set(n.community, n);
    metaNodeById.set(n.id, n);
  }}
}});

const expandedCommunities = new Set();
const activeRealNodeIds = new Set();

// FACET_NODE_SOURCE / FACET_EDGE_SOURCE: the universe facets are computed
// over. Per the agreed scope, facets always reflect the *whole* codebase
// from the start (not just whatever's currently expanded on the canvas) —
// see the FACETS block below, which reads from these instead of RAW_NODES /
// RAW_EDGES directly whenever drill-down data is present.
const FACET_NODE_SOURCE = HAS_DRILLDOWN ? FULL_NODES : RAW_NODES;
const FACET_EDGE_SOURCE = HAS_DRILLDOWN ? FULL_EDGES : RAW_EDGES;

// Precomputed once: the non-drilldown edge array, in the same {{_id, ...}}
// shape activeEdgeArray() returns for the drilldown case, so refresh()'s
// hot loop never has to branch on HAS_DRILLDOWN per item.
const _plainEdgeItems = RAW_EDGES.map((e, i) => Object.assign({{ _id: i }}, e));

function activeNodeArray() {{
  if (!HAS_DRILLDOWN) return RAW_NODES;
  const arr = RAW_NODES.filter(n => !(n.is_meta && expandedCommunities.has(n.community)));
  expandedCommunities.forEach(cid => {{
    (communityMembers.get(cid) || []).forEach(n => arr.push(n));
  }});
  return arr;
}}

function activeEdgeArray() {{
  if (!HAS_DRILLDOWN) return _plainEdgeItems;
  const arr = [];
  RAW_EDGES.forEach((e, i) => {{
    const fromMeta = metaNodeById.get(e.from);
    const toMeta = metaNodeById.get(e.to);
    const fromExpanded = fromMeta && expandedCommunities.has(fromMeta.community);
    const toExpanded = toMeta && expandedCommunities.has(toMeta.community);
    if (!fromExpanded && !toExpanded) arr.push(Object.assign({{ _id: i }}, e));
  }});
  FULL_EDGES.forEach((e, i) => {{
    if (activeRealNodeIds.has(e.from) && activeRealNodeIds.has(e.to)) {{
      arr.push(Object.assign({{ _id: 'full:' + i }}, e));
    }}
  }});
  return arr;
}}

// Structural sync of edgesDS for the FULL_EDGES-derived portion only:
// meta-level edges are already DataSet items from construction and only
// ever need their `hidden` flag toggled (handled by refresh() below), but
// a real edge between two just-expanded nodes doesn't exist in edgesDS yet
// at all — DataSet.update() on a nonexistent id would create a bare item
// missing `from`/`to`/color/etc, so it has to be add()ed explicitly the
// first time, and remove()d once neither endpoint is active any more.
function refreshDrilldownEdges() {{
  const wantedIds = new Set();
  FULL_EDGES.forEach((e, i) => {{
    if (activeRealNodeIds.has(e.from) && activeRealNodeIds.has(e.to)) wantedIds.add('full:' + i);
  }});
  const currentIds = new Set(edgesDS.getIds().filter(id => typeof id === 'string' && id.startsWith('full:')));
  const toRemove = [...currentIds].filter(id => !wantedIds.has(id));
  if (toRemove.length) edgesDS.remove(toRemove);
  const toAdd = [];
  wantedIds.forEach(id => {{
    if (currentIds.has(id)) return;
    const e = FULL_EDGES[Number(id.slice(5))];
    toAdd.push({{
      id, from: e.from, to: e.to, label: '', title: e.title,
      dashes: e.dashes, width: e.width, color: e.color,
      arrows: {{ to: {{ enabled: true, scaleFactor: 0.5 }} }},
    }});
  }});
  if (toAdd.length) edgesDS.add(toAdd);
}}

function expandCommunity(cid) {{
  if (!HAS_DRILLDOWN || expandedCommunities.has(cid)) return;
  const members = communityMembers.get(cid) || [];
  if (!members.length) return;
  const remainingCap = DRILLDOWN_NODE_CAP - activeRealNodeIds.size;
  if (members.length > remainingCap) {{
    showDrilldownWarning(`Expandir esta comunidade adicionaria ${{fmt(members.length)}} nós, acima do limite de ${{fmt(DRILLDOWN_NODE_CAP)}} nós simultâneos (${{fmt(activeRealNodeIds.size)}} já expandidos). Recolha outra comunidade antes de continuar.`);
    return;
  }}
  const meta = metaNodeByCommunity.get(cid);
  if (!meta) return;
  const livePos = network.getPositions([meta.id])[meta.id] || {{ x: meta.x || 0, y: meta.y || 0 }};

  nodesDS.add(members.map(n => ({{
    id: n.id, label: n.label, color: n.color, size: n.size, font: n.font, title: n.title,
    x: livePos.x + (n.x || 0), y: livePos.y + (n.y || 0),
    _community: n.community, _community_name: n.community_name,
    _source_file: n.source_file, _file_type: n.file_type, _kind: n.kind, _degree: n.degree,
    _is_meta: false,
  }})));
  members.forEach(n => activeRealNodeIds.add(n.id));
  nodesDS.remove(meta.id);
  expandedCommunities.add(cid);

  refreshDrilldownEdges();
  recomputeLod();
  prevVisibleNodes = null;
  prevVisibleEdges = null;
  refresh();
  updateDrilldownStatus();
}}

function collapseCommunity(cid) {{
  if (!expandedCommunities.has(cid)) return;
  const members = communityMembers.get(cid) || [];
  nodesDS.remove(members.map(n => n.id));
  members.forEach(n => activeRealNodeIds.delete(n.id));
  expandedCommunities.delete(cid);

  const meta = metaNodeByCommunity.get(cid);
  if (meta && !nodesDS.get(meta.id)) {{
    nodesDS.add({{
      id: meta.id, label: meta.label, color: meta.color, size: meta.size, font: meta.font, title: meta.title,
      x: meta.x, y: meta.y,
      _community: meta.community, _community_name: meta.community_name,
      _source_file: meta.source_file, _file_type: meta.file_type, _kind: meta.kind, _degree: meta.degree,
      _is_meta: true,
    }});
  }}

  refreshDrilldownEdges();
  recomputeLod();
  prevVisibleNodes = null;
  prevVisibleEdges = null;
  refresh();
  updateDrilldownStatus();
}}

network.on('doubleClick', params => {{
  if (!HAS_DRILLDOWN || params.nodes.length !== 1) return;
  const node = nodesDS.get(params.nodes[0]);
  if (!node) return;
  if (node._is_meta) {{
    expandCommunity(node._community);
  }} else if (activeRealNodeIds.has(node.id) && expandedCommunities.has(node._community)) {{
    collapseCommunity(node._community);
  }}
}});

const drilldownPanel = document.getElementById('drilldown-panel');
const drilldownCountEl = document.getElementById('drilldown-count');
if (HAS_DRILLDOWN) {{
  drilldownPanel.style.display = 'flex';
  document.getElementById('drilldown-collapse-all').addEventListener('click', () => {{
    [...expandedCommunities].forEach(cid => collapseCommunity(cid));
  }});
  drilldownCountEl.textContent = `0 comunidades · 0/${{fmt(DRILLDOWN_NODE_CAP)}} nós`;
}}
function updateDrilldownStatus() {{
  if (!HAS_DRILLDOWN) return;
  drilldownCountEl.textContent = `${{fmt(expandedCommunities.size)}} comunidades · ${{fmt(activeRealNodeIds.size)}}/${{fmt(DRILLDOWN_NODE_CAP)}} nós`;
}}

let _drilldownWarningTimer = null;
function showDrilldownWarning(msg) {{
  const el = document.getElementById('drilldown-warning');
  if (!el) return;
  el.textContent = msg;
  el.style.display = 'block';
  if (_drilldownWarningTimer) clearTimeout(_drilldownWarningTimer);
  _drilldownWarningTimer = setTimeout(() => {{ el.style.display = 'none'; }}, 6000);
}}

// ---------------------------------------------------------------------
// Level of detail (LOD): a no-op below LOD_ACTIVATION_THRESHOLD active
// nodes — small/typical graphs render exactly as before. Above it (the
// scenario profiled directly: a 19,706-node/23,177-edge real export),
// network.redraw() cost was measured at ~1s with every edge visible vs
// ~700ms with zero edges — most of even the *edgeless* cost is drawing
// tens of thousands of node circles every frame, which vis-network has no
// built-in way to bound (it always redraws everything not `hidden`, with
// no automatic viewport culling the way a WebGL renderer would have).
//
// Three zoom-driven levels, ported from grafo-explorer's own
// nivelDeDetalhe system, progressively relax what's drawn:
//   - zoomed all the way out ("mapa"): only high-degree hub nodes, no
//     edges at all — a coarse map of the graph's structure, not a hairball.
//   - a middle zoom ("bairro"): nodes within the current viewport (+ a
//     margin so a small pan doesn't require an immediate recompute) above
//     a moderate degree floor; edges touching the very highest-degree hubs
//     are still suppressed, since a hub's fan-out is most of any large
//     graph's edge count and the single biggest render cost.
//   - zoomed in close ("rua"): full detail, but still only for whatever
//     node/edge actually falls in the current viewport — this is what
//     bounds render cost independent of total graph size once zoomed in,
//     the piece plain hidden-flag toggling can't give on its own.
//
// Degree cutoffs are computed from percentiles of whatever's currently
// active (not hardcoded absolute values), so they adapt to any graph size
// rather than being tuned to one dataset.
// ---------------------------------------------------------------------
const LOD_ACTIVATION_THRESHOLD = 2000;
// Hard ceiling on edges actually drawn at once whenever LOD is active,
// independent of how many pass the per-level node/degree/viewport rules
// above. Those rules alone still let edge count balloon — a viewport full
// of only moderately-connected nodes can still have thousands of edges
// between them — and edges are the more expensive half of the two costs
// profiled (network.redraw() with a large edge set vs. with it capped).
// Picked for legibility, not just raw redraw cost: 1,500 already measured
// fast (~41ms redraw) but still read as a dense hairball on a real densely
// cross-referenced codebase; 500 is comfortably inside the performance
// budget with real headroom to spare and reads as an actual diagram.
const LOD_EDGE_CAP = 500;
let lodNodeIds = null;         // null = LOD inactive; every active node eligible
let lodAllowedEdgeIds = null;  // null = no LOD edge restriction; otherwise the exact allowed edge ids

function lodActive() {{
  return activeNodeArray().length > LOD_ACTIVATION_THRESHOLD;
}}

function lodDegreeThresholds(nodeArr) {{
  const degs = nodeArr.map(n => n.degree || 0).slice().sort((a, b) => a - b);
  const pct = p => degs.length ? degs[Math.min(degs.length - 1, Math.floor(p * (degs.length - 1)))] : 0;
  return {{ hub: pct(0.98), mid: pct(0.85), hubSuppress: pct(0.995) }};
}}

function lodViewportRect(marginFactor) {{
  const w = container.clientWidth, h = container.clientHeight;
  const topLeft = network.DOMtoCanvas({{ x: 0, y: 0 }});
  const bottomRight = network.DOMtoCanvas({{ x: w, y: h }});
  const mx = (bottomRight.x - topLeft.x) * (marginFactor - 1) / 2;
  const my = (bottomRight.y - topLeft.y) * (marginFactor - 1) / 2;
  return {{ minX: topLeft.x - mx, maxX: bottomRight.x + mx, minY: topLeft.y - my, maxY: bottomRight.y + my }};
}}

function recomputeLod() {{
  if (!lodActive()) {{
    lodNodeIds = null;
    lodAllowedEdgeIds = null;
    return;
  }}
  const nodeArr = activeNodeArray();
  const scale = network.getScale();
  const th = lodDegreeThresholds(nodeArr);
  let edgesEnabled = true;
  let hiddenHubIds = null;

  if (scale < lodInitialScale * 3) {{
    lodNodeIds = new Set(nodeArr.filter(n => (n.degree || 0) >= th.hub).map(n => n.id));
    edgesEnabled = false;
  }} else if (scale < lodInitialScale * 10) {{
    const rect = lodViewportRect(1.4);
    lodNodeIds = new Set(nodeArr.filter(n =>
      (n.degree || 0) >= th.mid &&
      n.x >= rect.minX && n.x <= rect.maxX && n.y >= rect.minY && n.y <= rect.maxY
    ).map(n => n.id));
    hiddenHubIds = new Set(nodeArr.filter(n => (n.degree || 0) >= th.hubSuppress).map(n => n.id));
  }} else {{
    const rect = lodViewportRect(1.2);
    lodNodeIds = new Set(nodeArr.filter(n =>
      n.x >= rect.minX && n.x <= rect.maxX && n.y >= rect.minY && n.y <= rect.maxY
    ).map(n => n.id));
  }}

  if (!edgesEnabled) {{
    lodAllowedEdgeIds = new Set();
    return;
  }}

  // nodeArr may be FULL_NODES or RAW_NODES depending on drill-down state,
  // so this is built fresh from whatever's actually active rather than
  // assuming a single global degree source.
  const posById = new Map(nodeArr.map(n => [n.id, n]));

  const candidates = [];
  activeEdgeArray().forEach(e => {{
    if (!lodNodeIds.has(e.from) || !lodNodeIds.has(e.to)) return;
    if (hiddenHubIds && (hiddenHubIds.has(e.from) || hiddenHubIds.has(e.to))) return;
    candidates.push(e);
  }});

  if (candidates.length > LOD_EDGE_CAP) {{
    // Rank by on-canvas endpoint distance, ascending: an edge whose two
    // endpoints sit close together reads as one clean local link, while an
    // edge stretching across most of the viewport is what actually produces
    // the "hairball" — a dense mesh of long crossing lines — regardless of
    // how many total edges are technically candidates. Degree alone (the
    // earlier heuristic) doesn't capture this: two moderately-connected
    // nodes can still sit on opposite sides of the screen, especially under
    // the sunflower-spiral fallback layout where graph adjacency and
    // spiral-index proximity aren't the same thing. Capping by distance
    // keeps the shortest, most legible edges and drops exactly the long
    // reaching ones that make a capped-but-still-dense view look tangled.
    candidates.sort((a, b) => {{
      const pa1 = posById.get(a.from), pa2 = posById.get(a.to);
      const pb1 = posById.get(b.from), pb2 = posById.get(b.to);
      const da = Math.hypot(pa1.x - pa2.x, pa1.y - pa2.y);
      const db = Math.hypot(pb1.x - pb2.x, pb1.y - pb2.y);
      return da - db;
    }});
    candidates.length = LOD_EDGE_CAP;
  }}
  lodAllowedEdgeIds = new Set(candidates.map(e => e._id));
}}

const emptyStateEl = document.getElementById('empty-state');
const lodStatusEl = document.getElementById('lod-status');
// Takes the SAME visibleNodes/visibleEdges refresh() just computed (post
// facet-filtering), not just the raw LOD candidate sets - otherwise this
// banner can claim nodes/edges are "showing" that the facet panel has
// actually filtered out (e.g. right after the graph opens with every facet
// unchecked, or whenever a facet selection narrows the LOD-eligible set
// further). Only called from refresh() itself, so it's always in sync with
// whatever actually just got sent to the DataSets.
function updateLodStatus(visibleNodes, visibleEdges) {{
  if (!lodStatusEl) return;
  if (!lodActive() || !visibleNodes.size) {{ lodStatusEl.style.display = 'none'; return; }}
  lodStatusEl.style.display = 'block';
  const totalNodes = activeNodeArray().length;
  const shownNodes = visibleNodes.size;
  const shownEdges = visibleEdges.size;
  // Whether LOD itself truncated at the cap (independent of anything facets
  // then filtered back out) - that's what the "+" suffix should reflect, not
  // whether the post-facet count happens to reach the cap.
  const lodCapped = lodAllowedEdgeIds !== null && lodAllowedEdgeIds.size >= LOD_EDGE_CAP;
  const edgePart = `, ${{fmt(shownEdges)}}${{lodCapped ? '+' : ''}} arestas`;
  lodStatusEl.textContent = `Grafo grande: mostrando ${{fmt(shownNodes)}} de ${{fmt(totalNodes)}} nós${{edgePart}} — zoom/pan para ver mais`;
}}

let lodDebounce = null;
function scheduleLodRecompute() {{
  if (!lodActive() && lodNodeIds === null) return; // never activated, nothing to do
  if (lodDebounce) clearTimeout(lodDebounce);
  lodDebounce = setTimeout(() => {{
    recomputeLod();
    prevVisibleNodes = null;
    prevVisibleEdges = null;
    refresh();
  }}, 180);
}}
network.on('zoom', scheduleLodRecompute);
network.on('dragEnd', scheduleLodRecompute);

recomputeLod();

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
  const matches = activeNodeArray().filter(n => n.label.toLowerCase().includes(q)).slice(0, 20);
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

const maxDegree = FACET_NODE_SOURCE.reduce((m, n) => Math.max(m, n.degree || 0), 0);

const communityFacetMap = new Map();
FACET_NODE_SOURCE.forEach(n => {{
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
    items: facetItemsFrom(distinctCounts(FACET_NODE_SOURCE.map(n => n.file_type)), '(unknown)'),
    read: n => n.file_type == null ? '' : String(n.file_type),
  }},
  {{
    key: 'kind', title: 'Subtipo (VB6)', appliesTo: 'node',
    items: facetItemsFromKind(distinctCounts(FACET_NODE_SOURCE.map(n => n.kind))),
    read: n => n.kind == null ? '' : String(n.kind),
  }},
  {{
    key: 'community', title: 'Community', appliesTo: 'node',
    items: [...communityFacetMap.values()].sort((a, b) => b.total - a.total),
    read: n => String(n.community),
  }},
  {{
    key: 'relation', title: 'Relation', appliesTo: 'edge',
    items: facetItemsFrom(distinctCounts(FACET_EDGE_SOURCE.map(e => e.label)), '(unlabeled)'),
    read: e => e.label == null ? '' : String(e.label),
  }},
  {{
    key: 'confidence', title: 'Confidence', appliesTo: 'edge',
    items: facetItemsFrom(distinctCounts(FACET_EDGE_SOURCE.map(e => e.confidence)), '(unset)'),
    read: e => e.confidence == null ? '' : String(e.confidence),
  }},
];

// Facets start with every checkbox OFF: the canvas opens empty and the
// user opts in to what they want to see, rather than opening on the full
// (possibly huge) graph and having to opt out. This is a second, orthogonal
// lever from the zoom-driven LOD system above — LOD bounds render cost once
// a large-enough subset IS selected, this bounds it by simply not selecting
// anything until asked. Degree range and "hide isolated" are left at their
// permissive defaults since they aren't part of this opt-in model.
const filters = {{ text: '', degreeMin: 0, degreeMax: maxDegree, hideIsolated: false, active: {{}} }};
FACETS.forEach(f => {{ filters.active[f.key] = new Set(); }});

function normalize(s) {{
  return String(s ?? '').toLowerCase();
}}

// Shared by computeVisible() (over whatever's currently active/rendered)
// and updateCommunityBadges() (over every member of a collapsed community,
// rendered or not) — factored out so both always agree on what "matches
// the current filters" means.
function nodeMatchesFilters(n) {{
  for (const f of FACETS) {{
    if (f.appliesTo !== 'node' || !f.items.length) continue;
    if (!filters.active[f.key].has(f.read(n))) return false;
  }}
  const deg = n.degree || 0;
  if (deg < filters.degreeMin || deg > filters.degreeMax) return false;
  const term = normalize(filters.text).trim();
  if (term) {{
    const hay = normalize(`${{n.label}} ${{n.source_file || ''}} ${{n.community_name || ''}}`);
    if (!hay.includes(term)) return false;
  }}
  return true;
}}

function computeVisible() {{
  const visibleNodes = new Set();
  activeNodeArray().forEach(n => {{
    if (!nodeMatchesFilters(n)) return;
    if (lodNodeIds !== null && !lodNodeIds.has(n.id)) return;
    visibleNodes.add(n.id);
  }});

  const visibleEdges = new Set();
  const connected = new Set();
  activeEdgeArray().forEach(e => {{
    for (const f of FACETS) {{
      if (f.appliesTo !== 'edge' || !f.items.length) continue;
      if (!filters.active[f.key].has(f.read(e))) return;
    }}
    if (!visibleNodes.has(e.from) || !visibleNodes.has(e.to)) return;
    if (lodAllowedEdgeIds !== null && !lodAllowedEdgeIds.has(e._id)) return;
    visibleEdges.add(e._id);
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
      ? distinctCounts(FACET_NODE_SOURCE.filter(n => visibleNodes.has(n.id)).map(f.read))
      : distinctCounts(FACET_EDGE_SOURCE.filter((e, i) => visibleEdges.has(HAS_DRILLDOWN ? ('full:' + i) : i)).map(f.read));
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

function updateCommunityBadges() {{
  if (!HAS_DRILLDOWN) return;
  const showBadges = countActive() > 0;
  const updates = [];
  metaNodeByCommunity.forEach((meta, cid) => {{
    if (expandedCommunities.has(cid) || !nodesDS.get(meta.id)) return;
    const members = communityMembers.get(cid) || [];
    let matchLabel = meta.label;
    if (showBadges) {{
      let match = 0;
      members.forEach(n => {{ if (nodeMatchesFilters(n)) match++; }});
      if (match !== members.length) matchLabel = `${{meta.label}} (${{fmt(match)}}/${{fmt(members.length)}})`;
    }}
    if (nodesDS.get(meta.id).label !== matchLabel) updates.push({{ id: meta.id, label: matchLabel }});
  }});
  if (updates.length) nodesDS.update(updates);
}}

function refresh() {{
  const {{ visibleNodes, visibleEdges }} = computeVisible();
  const nodeItems = activeNodeArray();
  const edgeItems = activeEdgeArray();

  if (prevVisibleNodes === null) {{
    nodesDS.update(nodeItems.map(n => ({{ id: n.id, hidden: !visibleNodes.has(n.id) }})));
  }} else {{
    const changed = [];
    nodeItems.forEach(n => {{
      const now = visibleNodes.has(n.id);
      if (prevVisibleNodes.has(n.id) !== now) changed.push({{ id: n.id, hidden: !now }});
    }});
    if (changed.length) nodesDS.update(changed);
  }}
  prevVisibleNodes = visibleNodes;

  if (prevVisibleEdges === null) {{
    edgesDS.update(edgeItems.map(e => ({{ id: e._id, hidden: !visibleEdges.has(e._id) }})));
  }} else {{
    const changed = [];
    edgeItems.forEach(e => {{
      const now = visibleEdges.has(e._id);
      if (prevVisibleEdges.has(e._id) !== now) changed.push({{ id: e._id, hidden: !now }});
    }});
    if (changed.length) edgesDS.update(changed);
  }}
  prevVisibleEdges = visibleEdges;

  updateFacetCounts(visibleNodes, visibleEdges);
  updateCommunityBadges();
  const active = countActive();
  filtersBadge.textContent = String(active);
  filtersBadge.style.display = active > 0 ? 'inline-block' : 'none';
  document.getElementById('degree-range-label').textContent = `${{fmt(filters.degreeMin)}}–${{fmt(filters.degreeMax)}}`;
  if (emptyStateEl) emptyStateEl.style.display = visibleNodes.size === 0 ? 'flex' : 'none';
  updateLodStatus(visibleNodes, visibleEdges);
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

document.getElementById('filters-show-all').addEventListener('click', () => {{
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

// Mirror of "Show all": returns to the initial opt-in state (every facet
// checkbox off) without touching text/degree/hide-isolated, which aren't
// part of that opt-in model. Lets the user start over after checking a few
// boxes, without unchecking each facet group one at a time via its "none"
// link.
document.getElementById('filters-hide-all').addEventListener('click', () => {{
  FACETS.forEach(f => {{ filters.active[f.key] = new Set(); }});
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
    full_graph: nx.Graph | None = None,
    full_communities: dict[int, list[str]] | None = None,
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
    above it, a cheap O(n) sunflower-spiral fallback (`_radial_layout`) that
    carries no topological meaning (nodes don't cluster visually by
    community) but keeps a roughly circular silhouette — with unconnected
    nodes pushed out to their own ring rather than interleaved throughout —
    while keeping the page loading in seconds and the filters panel fully
    usable, which is what matters once a graph is too large to eyeball
    anyway. Edges touching
    high-degree hub nodes get their opacity dampened (`_hub_alpha`) to
    reduce visual overdraw; and the client-side filter panel updates the
    vis.js DataSets incrementally (only nodes/edges whose visibility
    actually changed) with a debounced text filter, keeping interaction
    responsive on graphs with thousands of nodes.

    Above a client-side node-count threshold, a zoom-driven level-of-detail
    system (ported from grafo-explorer's own approach) additionally bounds
    what's actually drawn each frame: zoomed all the way out only
    high-degree hub nodes are shown with no edges at all; a middle zoom adds
    a viewport-bounded, moderate-degree set of nodes with hub-touching edges
    still suppressed; zoomed in close shows full detail but only for
    whatever's in the current viewport. This exists because even with every
    edge hidden, redrawing tens of thousands of node circles every frame has
    a real cost vis-network's hidden-flag toggling alone doesn't bound —
    profiled directly against a real ~20k-node export.

    If member_counts is provided (aggregated community view), node sizes are
    based on community member counts rather than graph degree.

    If node_limit is set and the graph exceeds it, automatically builds an
    aggregated community-level meta-graph instead of raising ValueError. That
    meta-graph render also embeds the *complete* original per-symbol graph
    (`full_graph`/`full_communities`, threaded through automatically — not
    meant to be passed by external callers) so the browser can drill down:
    double-clicking a community meta-node swaps it for its real members,
    positioned via a per-community local layout precomputed server-side
    (`_precompute_community_layouts`) — never client-side, preserving the
    "the page never hangs" guarantee. Facets are computed from this full
    embedded dataset from the start (so e.g. every VB6 subtype value is
    listed and filterable even before anything is expanded), and up to
    `_DRILLDOWN_NODE_CAP` real nodes may be expanded onto the canvas at once,
    across as many simultaneously-expanded communities as fit under that cap.

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
                meta.add_node(str(cid), label=(community_labels or {}).get(cid, f"Community {cid}"), is_meta=True)
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
                              community_labels=community_labels, member_counts=mc,
                              full_graph=G, full_communities=communities)
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

    # Escape </script> sequences so embedded JSON cannot break out of the script tag
    def _js_safe(obj) -> str:
        return json.dumps(obj).replace("</", "<\\/")

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

    vis_nodes = _build_vis_nodes(
        G, node_community, degree, max_deg, community_labels,
        member_counts, max_mc, positions, learning_overlay,
    )

    # Build edges list. Restore original edge direction from _src/_tgt
    # (stashed by build.py for exactly this reason): undirected NetworkX
    # canonicalizes endpoint order, which would otherwise flip the arrow
    # for `calls` and `rationale_for` in the rendered graph (#563).
    vis_edges = _build_vis_edges(G, degree)

    # Build community legend data
    legend_data = []
    for cid in sorted((community_labels or {}).keys()):
        color = COMMUNITY_COLORS[cid % len(COMMUNITY_COLORS)]
        lbl = _html.escape(sanitize_label((community_labels or {}).get(cid, f"Community {cid}")))
        n = member_counts.get(cid, len(communities.get(cid, []))) if member_counts else len(communities.get(cid, []))
        legend_data.append({"cid": cid, "color": color, "label": lbl, "count": n})

    nodes_json = _js_safe(vis_nodes)
    edges_json = _js_safe(vis_edges)
    legend_json = _js_safe(legend_data)
    hyperedges_json = _js_safe(getattr(G, "graph", {}).get("hyperedges", []))
    title = _html.escape(sanitize_label(_html_document_title(output_path)))
    stats = f"{G.number_of_nodes()} nodes &middot; {G.number_of_edges()} edges &middot; {len(communities)} communities"

    # Camada 4: embed the complete original per-symbol graph alongside the
    # aggregated meta-graph, so the browser can drill down into any
    # community without a server round-trip. See `to_html()`'s docstring.
    full_nodes_json = "[]"
    full_edges_json = "[]"
    if full_graph is not None and full_communities is not None and full_graph.number_of_nodes() > 0:
        full_node_community = _node_community_map(full_communities)
        full_degree = dict(full_graph.degree())
        full_max_deg = max(full_degree.values(), default=1) or 1
        full_positions = _precompute_community_layouts(full_graph, full_communities)
        full_vis_nodes = _build_vis_nodes(
            full_graph, full_node_community, full_degree, full_max_deg,
            community_labels, None, 1, full_positions, learning_overlay,
        )
        full_vis_edges = _build_vis_edges(full_graph, full_degree)
        full_nodes_json = _js_safe(full_vis_nodes)
        full_edges_json = _js_safe(full_vis_edges)

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
<div id="empty-state"><span>Nenhum nó selecionado.<br>Marque ao menos um item em <b>Node type</b>, <b>Subtipo</b> e <b>Community</b> na barra lateral (os três ao mesmo tempo) para começar a explorar o grafo — ou clique em <b>Show all</b> para ver tudo de uma vez.</span></div>
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
      <span class="filters-header-actions">
        <button type="button" id="filters-show-all" class="filters-clear">Show all</button>&middot;<button type="button" id="filters-hide-all" class="filters-clear">Hide all</button>
      </span>
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
    <div class="facet-section" id="drilldown-panel" style="display:none">
      <div class="facet-section-title"><span>Drill-down</span><span id="drilldown-count">0</span></div>
      <button type="button" id="drilldown-collapse-all" class="filters-clear" style="align-self:flex-start">Recolher tudo</button>
      <div id="drilldown-warning" style="display:none;color:#f59e0b;font-size:11px;line-height:1.4;"></div>
    </div>
  </div>
  <div id="lod-status" style="display:none;padding:8px 14px;font-size:11px;color:#93c5fd;background:#16213e;border-top:1px solid #2a2a4e;line-height:1.4;"></div>
  <div id="stats">{stats}</div>
</div>
{_html_script(nodes_json, edges_json, legend_json, full_nodes_json, full_edges_json, _DRILLDOWN_NODE_CAP)}
{_hyperedge_script(hyperedges_json)}
</body>
</html>"""

    write_text_atomic(output_path, html)
    return True
