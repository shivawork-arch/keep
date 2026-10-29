"use client";

import { useEffect, useRef } from "react";
import type { GraphEdge, GraphNode } from "@/lib/api";

// "Wine and cheese" layout: files pinned in the left column, people pinned in the right column,
// decisions in the middle, each placed near the files it governs. Incidents and pattern rules sit
// beside the decision column.
const COL = { file: 0, pattern: 360, decision: 700, incident: 1020, person: 1380 };
const GAP = 120;

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

const CHALK = "#eaf2fb", LINE = "#a9c7e8", RED = "#ff4f6d", AMBER = "#f2c14e", RULE = "#b9a6ff", PAPER = "#0a2444";

const STYLE = [
  { selector: "node", style: {
      label: "data(short)", color: CHALK, "font-family": "Archivo, system-ui, sans-serif", "font-size": 15,
      "text-wrap": "wrap", "text-max-width": "190px", "text-valign": "bottom", "text-margin-y": 8,
      "text-background-color": PAPER, "text-background-opacity": 0.85, "text-background-padding": "3px",
      "border-width": 2, "background-color": PAPER, "min-zoomed-font-size": 0 } },
  { selector: "node[kind = 'file']", style: {
      shape: "rectangle", "border-color": LINE, width: 22, height: 22, "font-family": "IBM Plex Mono, monospace",
      "font-size": 14, "text-halign": "right", "text-valign": "center", "text-margin-x": 10, "text-margin-y": 0 } },
  { selector: "node[kind = 'person']", style: {
      shape: "ellipse", "border-color": AMBER, "border-width": 3, width: 30, height: 30,
      "text-halign": "left", "text-valign": "center", "text-margin-x": -10, "text-margin-y": 0, color: AMBER } },
  { selector: "node[kind = 'decision']", style: {
      shape: "ellipse", "background-color": CHALK, "border-color": CHALK, width: 46, height: 46, "font-weight": 600 } },
  { selector: "node[kind = 'decision'][status != 'active']", style: {
      "background-color": PAPER, "border-color": RED, "border-style": "dashed", "border-width": 3, color: "#c9d8ea" } },
  { selector: "node[decay = 'likely_outdated']", style: { "border-color": AMBER, "border-width": 4 } },
  { selector: "node[?security]", style: { "border-color": RED, "border-width": 5 } },
  { selector: "node[kind = 'pattern']", style: {
      shape: "hexagon", "background-color": RULE, "border-color": RULE, width: 34, height: 34, color: RULE } },
  { selector: "node[kind = 'incident']", style: {
      shape: "diamond", "background-color": RED, "border-color": RED, width: 38, height: 38, color: RED } },
  { selector: "node:selected", style: { "overlay-color": CHALK, "overlay-opacity": 0.18, "overlay-padding": 8 } },
  { selector: "edge", style: { width: 2, "curve-style": "bezier", "line-color": LINE, opacity: 0.45,
      "target-arrow-shape": "triangle", "target-arrow-color": LINE, "arrow-scale": 1 } },
  { selector: "edge[kind = 'DECIDED']", style: { "line-color": AMBER, "target-arrow-color": AMBER, opacity: 0.4 } },
  { selector: "edge[kind = 'DEPENDS_ON']", style: { "line-color": CHALK, "target-arrow-color": CHALK, opacity: 0.9, width: 3 } },
  { selector: "edge[kind = 'SUPERSEDES']", style: { "line-color": RED, "target-arrow-color": RED, opacity: 0.9,
      "line-style": "dashed", width: 3 } },
  { selector: "edge[kind = 'RELATES_TO']", style: { "line-color": RED, "target-arrow-color": RED, opacity: 0.9, width: 3 } },
  { selector: ".faded", style: { opacity: 0.08 } },
  { selector: "edge.lit", style: { opacity: 1 } },
];

export default function Graph({ nodes, edges, onSelect, highlight }: {
  nodes: GraphNode[]; edges: GraphEdge[]; onSelect: (n: GraphNode | null) => void; highlight?: string[];
}) {
  const box = useRef<HTMLDivElement>(null);
  const cyRef = useRef<import("cytoscape").Core | null>(null);

  // Light up decisions an answer cited, plus everything directly connected to them.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) return;
    cy.elements().removeClass("faded lit");
    if (!highlight?.length) return;
    const hit = cy.collection(highlight.map((id) => cy.getElementById(id)).filter((e) => e.nonempty()));
    if (hit.empty()) return;
    cy.elements().addClass("faded");
    hit.closedNeighborhood().removeClass("faded");
    hit.connectedEdges().addClass("lit");
  }, [highlight]);

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
        layout: { name: "preset", fit: true, padding: 70 },
        wheelSensitivity: 0.3,
        minZoom: 0.3,
        maxZoom: 2.2,
      });
      cyRef.current = cy;
      if (cy.zoom() > 1.1) { cy.zoom(1.1); cy.center(); }
      // Leave the bottom-right corner clear for the title block on wide screens.
      if (box.current.clientWidth > 860) cy.panBy({ x: -140, y: -50 });
      cy.on("tap", "node", (evt) => {
        const n = evt.target;
        cy!.elements().addClass("faded").removeClass("lit");
        n.closedNeighborhood().removeClass("faded");
        n.connectedEdges().addClass("lit");
        onSelect(nodes.find((x) => x.id === n.id()) ?? null);
      });
      cy.on("tap", (evt) => {
        if (evt.target === cy) {
          cy!.elements().removeClass("faded lit");
          onSelect(null);
        }
      });
    })();
    return () => {
      cancelled = true;
      cyRef.current = null;
      cy?.destroy();
    };
  }, [nodes, edges, onSelect]);

  return <div ref={box} className="graph" />;
}
