"use client";

import { useEffect, useRef } from "react";
import type { GraphEdge, GraphNode } from "@/lib/api";

// "Wine and cheese" layout: files pinned in the left column, people pinned in the right column,
// decisions in the middle, each placed near the files it governs. Incidents and pattern rules sit
// beside the decision column.
const COL = { file: 0, pattern: 330, decision: 620, incident: 900, person: 1220 };
const GAP = 90;

function layout(nodes: GraphNode[], edges: GraphEdge[]) {
  const pos = new Map<string, { x: number; y: number }>();
  const byKind = (k: string) => nodes.filter((n) => n.kind === k);

  byKind("file").sort((a, b) => a.label.localeCompare(b.label))
    .forEach((n, i) => pos.set(n.id, { x: COL.file, y: i * GAP }));

  // Decisions: target y = average y of their files, then nudged apart so they don't overlap.
  const decisions = byKind("decision").map((d) => {
    const ys = edges.filter((e) => e.source === d.id && e.kind === "GOVERNS")
      .map((e) => pos.get(e.target)?.y).filter((y): y is number => y !== undefined);
    return { d, y: ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : Infinity };
  }).sort((a, b) => a.y - b.y || a.d.id.localeCompare(b.d.id));
  let last = -Infinity;
  decisions.forEach(({ d, y }, i) => {
    const target = Number.isFinite(y) ? y : i * GAP;
    const placed = Math.max(target, last + GAP * 0.8);
    pos.set(d.id, { x: COL.decision + (i % 2 ? 70 : -70), y: placed });
    last = placed;
  });

  const avgY = (ids: string[]) => {
    const ys = ids.map((i) => pos.get(i)?.y).filter((y): y is number => y !== undefined);
    return ys.length ? ys.reduce((a, b) => a + b, 0) / ys.length : 0;
  };
  const spread = (list: { id: string; y: number }[], x: number) => {
    let prev = -Infinity;
    list.sort((a, b) => a.y - b.y).forEach(({ id, y }) => {
      const placed = Math.max(y, prev + GAP * 0.8);
      pos.set(id, { x, y: placed });
      prev = placed;
    });
  };

  spread(byKind("person").map((p) => ({
    id: p.id, y: avgY(edges.filter((e) => e.source === p.id).map((e) => e.target)) })), COL.person);
  spread(byKind("incident").map((n) => ({
    id: n.id, y: avgY(edges.filter((e) => e.source === n.id).map((e) => e.target)) })), COL.incident);
  byKind("pattern").forEach((n, i) => pos.set(n.id, { x: COL.pattern, y: i * GAP }));
  return pos;
}

const STYLE = [
  { selector: "node", style: {
      label: "data(short)", color: "#e6edf3", "font-size": 11, "text-wrap": "wrap",
      "text-max-width": "150px", "text-valign": "bottom", "text-margin-y": 6, "border-width": 0 } },
  { selector: "node[kind = 'file']", style: {
      shape: "round-rectangle", "background-color": "#1f6feb", width: 26, height: 18,
      "text-halign": "right", "text-valign": "center", "text-margin-x": 8, "text-margin-y": 0 } },
  { selector: "node[kind = 'person']", style: {
      shape: "ellipse", "background-color": "#d29922", width: 26, height: 26,
      "text-halign": "left", "text-valign": "center", "text-margin-x": -8, "text-margin-y": 0 } },
  { selector: "node[kind = 'decision']", style: { shape: "ellipse", "background-color": "#3fb950", width: 34, height: 34 } },
  { selector: "node[kind = 'decision'][status != 'active']", style: { "background-color": "#6e7681", opacity: 0.7 } },
  { selector: "node[decay = 'likely_outdated']", style: { "border-width": 3, "border-color": "#d29922" } },
  { selector: "node[?security]", style: { "border-width": 3, "border-color": "#f85149" } },
  { selector: "node[kind = 'pattern']", style: { shape: "hexagon", "background-color": "#a371f7", width: 28, height: 28 } },
  { selector: "node[kind = 'incident']", style: { shape: "diamond", "background-color": "#f85149", width: 30, height: 30 } },
  { selector: "node:selected", style: { "border-width": 4, "border-color": "#e6edf3" } },
  { selector: "edge", style: { width: 1.5, "curve-style": "bezier", "line-color": "#30363d",
      "target-arrow-shape": "triangle", "target-arrow-color": "#30363d", "arrow-scale": 0.8 } },
  { selector: "edge[kind = 'GOVERNS']", style: { "line-color": "#1f6feb66", "target-arrow-color": "#1f6feb66" } },
  { selector: "edge[kind = 'DECIDED']", style: { "line-color": "#d2992266", "target-arrow-color": "#d2992266" } },
  { selector: "edge[kind = 'DEPENDS_ON']", style: { "line-color": "#3fb950", "target-arrow-color": "#3fb950", width: 2 } },
  { selector: "edge[kind = 'SUPERSEDES']", style: { "line-color": "#8b949e", "target-arrow-color": "#8b949e",
      "line-style": "dashed", width: 2 } },
  { selector: "edge[kind = 'RELATES_TO']", style: { "line-color": "#f85149", "target-arrow-color": "#f85149", width: 2 } },
  { selector: ".faded", style: { opacity: 0.12 } },
];

export default function Graph({ nodes, edges, onSelect }: {
  nodes: GraphNode[]; edges: GraphEdge[]; onSelect: (n: GraphNode | null) => void;
}) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cy: import("cytoscape").Core | undefined;
    let cancelled = false;
    (async () => {
      const cytoscape = (await import("cytoscape")).default; // browser-only library
      if (cancelled || !box.current) return;
      const pos = layout(nodes, edges);
      cy = cytoscape({
        container: box.current,
        elements: [
          ...nodes.map((n) => ({
            data: { ...n, short: n.kind === "decision" ? `${n.id}\n${n.label}` :
                    n.kind === "pattern" || n.kind === "incident" ? n.id : n.label },
            position: pos.get(n.id) ?? { x: 0, y: 0 } })),
          ...edges.map((e, i) => ({ data: { id: `e${i}`, ...e } })),
        ],
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        style: STYLE as any,
        layout: { name: "preset", fit: true, padding: 40 },
        wheelSensitivity: 0.3,
      });
      cy.on("tap", "node", (evt) => {
        const n = evt.target;
        cy!.elements().addClass("faded");
        n.closedNeighborhood().removeClass("faded");
        onSelect(nodes.find((x) => x.id === n.id()) ?? null);
      });
      cy.on("tap", (evt) => {
        if (evt.target === cy) {
          cy!.elements().removeClass("faded");
          onSelect(null);
        }
      });
    })();
    return () => {
      cancelled = true;
      cy?.destroy();
    };
  }, [nodes, edges, onSelect]);

  return <div ref={box} className="graph" />;
}
