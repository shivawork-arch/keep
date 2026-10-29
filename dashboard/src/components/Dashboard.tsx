"use client";

import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";
import Graph from "@/components/Graph";
import { get, type GraphEdge, type GraphNode, type LedgerEntry, type Stats } from "@/lib/api";

type Tab = "graph" | "decisions" | "ledger" | "health" | "onboarding";

export default function Dashboard() {
  const [tab, setTab] = useState<Tab>("graph");
  const [stats, setStats] = useState<Stats | null>(null);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [health, setHealth] = useState("");
  const [onboarding, setOnboarding] = useState("");
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    try {
      setError("");
      const [s, g, l, h, o] = await Promise.all([
        get<Stats>("/stats"),
        get<{ nodes: GraphNode[]; edges: GraphEdge[] }>("/graph"),
        get<LedgerEntry[]>("/ledger"),
        get<{ markdown: string }>("/health-report"),
        get<{ markdown: string }>("/onboarding"),
      ]);
      setStats(s); setGraph(g); setLedger(l); setHealth(h.markdown); setOnboarding(o.markdown);
    } catch (e) {
      setError(`Can't reach the KEEP API (${(e as Error).message}). Is uvicorn running on :8000?`);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  const onSelect = useCallback((n: GraphNode | null) => setSelected(n), []);
  const decisions = (graph?.nodes ?? []).filter((n) => n.kind === "decision");

  return (
    <main className="wrap">
      <header className="top">
        <div>
          <h1>KEEP</h1>
          <p className="sub">Knowledge Engine for Engineering Precedent</p>
        </div>
        <button onClick={load}>Refresh</button>
      </header>

      {error && <p className="error">{error}</p>}

      {stats && (
        <section className="cards">
          <Card label="Active decisions" value={stats.active} />
          <Card label="Superseded" value={stats.superseded} />
          <Card label="Pattern rules" value={stats.patterns} />
          <Card label="Incidents" value={stats.incidents} />
          <Card label="Commits in ledger" value={stats.commits} />
          <Card label="Claude calls today" value={`${stats.usage.calls} / ${stats.usage.call_cap}`} />
        </section>
      )}

      <nav className="tabs">
        {(["graph", "decisions", "ledger", "health", "onboarding"] as Tab[]).map((t) => (
          <button key={t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{t}</button>
        ))}
      </nav>

      {tab === "graph" && graph && (
        <section className="graphrow">
          <div className="graphbox">
            <div className="legend">
              <span><i className="dot file" />file</span>
              <span><i className="dot decision" />decision</span>
              <span><i className="dot old" />superseded</span>
              <span><i className="dot pattern" />pattern rule</span>
              <span><i className="dot incident" />incident</span>
              <span><i className="dot person" />person</span>
            </div>
            <Graph nodes={graph.nodes} edges={graph.edges} onSelect={onSelect} />
          </div>
          <aside className="panel">
            {selected ? <Details n={selected} /> : <p className="muted">Click a node to see its details.</p>}
          </aside>
        </section>
      )}

      {tab === "decisions" && (
        <table>
          <thead><tr><th>ID</th><th>Title</th><th>Rule</th><th>Status</th><th>Confidence</th><th>Files</th></tr></thead>
          <tbody>
            {decisions.map((d) => (
              <tr key={d.id} className={d.status !== "active" ? "dim" : ""}>
                <td className="mono">{d.id}</td><td>{d.label}</td><td>{d.rule}</td>
                <td>{d.status}{d.decay === "likely_outdated" ? " (may be outdated)" : ""}</td>
                <td>{d.confidence}</td><td className="mono">{d.files?.join(", ")}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === "ledger" && (
        <table>
          <thead><tr><th>Commit</th><th>Branch</th><th>Author</th><th>Domains</th><th>Risk</th><th>Summary</th></tr></thead>
          <tbody>
            {ledger.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.id}</td><td>{c.branch}</td><td>{c.authors.join(", ")}</td>
                <td>{c.domains.join(", ")}</td><td className={`risk ${c.breaking_risk}`}>{c.breaking_risk}</td>
                <td>{c.summary}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {tab === "health" && <article className="md"><ReactMarkdown>{health}</ReactMarkdown></article>}
      {tab === "onboarding" && <article className="md"><ReactMarkdown>{onboarding}</ReactMarkdown></article>}
    </main>
  );
}

function Card({ label, value }: { label: string; value: number | string }) {
  return <div className="card"><div className="num">{value}</div><div className="lbl">{label}</div></div>;
}

function Details({ n }: { n: GraphNode }) {
  if (n.kind === "decision") {
    return (
      <div>
        <p className="mono id">{n.id}</p>
        <h3>{n.label}</h3>
        <p className="tags"><span>{n.status}</span><span>{n.confidence}</span>
          {n.security && <span className="sec">security</span>}
          {n.decay === "likely_outdated" && <span className="warn">may be outdated</span>}</p>
        <dl>
          <dt>Decision</dt><dd>{n.decision}</dd>
          {n.rejected && (<><dt>Rejected</dt><dd>{n.rejected}</dd></>)}
          <dt>Why</dt><dd>{n.reason}</dd>
          <dt>Rule</dt><dd>{n.rule}</dd>
          <dt>Decided by</dt><dd>{n.decided_by?.map((p) => "@" + p).join(", ")}</dd>
          <dt>Files</dt><dd className="mono">{n.files?.join(", ") || "none"}</dd>
          <dt>Source</dt><dd><a href={n.source} target="_blank" rel="noreferrer">{n.source}</a></dd>
        </dl>
        {!!n.history?.length && (
          <>
            <h4>History</h4>
            <ul className="hist">{n.history.map((h, i) => <li key={i}><span className="mono">{h.at.slice(0, 10)}</span> {h.change}</li>)}</ul>
          </>
        )}
      </div>
    );
  }
  if (n.kind === "pattern") {
    return (
      <div>
        <p className="mono id">{n.id}</p><h3>{n.label}</h3>
        <p className="tags"><span>{n.status}</span><span>{n.language}</span></p>
        <dl><dt>Why</dt><dd>{n.reason}</dd>
          {n.bad && (<><dt>Bad</dt><dd className="mono">{n.bad}</dd><dt>Good</dt><dd className="mono">{n.good}</dd></>)}</dl>
      </div>
    );
  }
  if (n.kind === "incident") {
    return (
      <div>
        <p className="mono id">{n.id}</p><h3>{n.label}</h3>
        <dl><dt>Summary</dt><dd>{n.summary}</dd>
          {!!n.suspects?.length && (<><dt>Check first</dt><dd className="mono">{n.suspects.join(", ")}</dd></>)}
          {n.url && (<><dt>Issue</dt><dd><a href={n.url} target="_blank" rel="noreferrer">{n.url}</a></dd></>)}</dl>
      </div>
    );
  }
  return <div><h3>{n.label}</h3><p className="muted">{n.kind}</p></div>;
}
