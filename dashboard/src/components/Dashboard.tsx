"use client";

import { useCallback, useEffect, useState, type FormEvent } from "react";
import ReactMarkdown from "react-markdown";
import Graph from "@/components/Graph";
import { ask, get, type AskAnswer, type GraphEdge, type GraphNode, type LedgerEntry, type Stats } from "@/lib/api";

const SHEETS = ["Map", "Decisions", "Changes", "Health", "Onboarding"] as const;
type Sheet = (typeof SHEETS)[number];

export default function Dashboard() {
  const [sheet, setSheet] = useState<Sheet>("Map");
  const [stats, setStats] = useState<Stats | null>(null);
  const [graph, setGraph] = useState<{ nodes: GraphNode[]; edges: GraphEdge[] } | null>(null);
  const [ledger, setLedger] = useState<LedgerEntry[]>([]);
  const [health, setHealth] = useState("");
  const [onboarding, setOnboarding] = useState("");
  const [selected, setSelected] = useState<GraphNode | null>(null);
  const [error, setError] = useState("");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [reply, setReply] = useState<{ q: string; a?: AskAnswer; err?: string } | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const q = question.trim();
    if (q.length < 3 || asking) return;
    setAsking(true); setSelected(null); setSheet("Map");
    try {
      setReply({ q, a: await ask(q) });
      load(); // the call shows up in today's Claude count
    } catch (err) {
      setReply({ q, err: (err as Error).message });
    } finally {
      setAsking(false);
    }
  };

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
      setError(`Can't reach the KEEP API at localhost:8000 (${(e as Error).message}). Start uvicorn, then refresh.`);
    }
  }, []);

  useEffect(() => { load(); }, [load]);
  const onSelect = useCallback((n: GraphNode | null) => setSelected(n), []);
  const decisions = (graph?.nodes ?? []).filter((n) => n.kind === "decision")
    .sort((a, b) => a.id.localeCompare(b.id));

  return (
    <main className="sheet">
      <header className="head">
        <h1 className="title">KEEP<small>What this team decided, and why</small></h1>
        <nav className="index" aria-label="Sheets">
          {SHEETS.map((s) => (
            <button key={s} aria-current={sheet === s ? "page" : undefined} onClick={() => setSheet(s)}>{s}</button>
          ))}
        </nav>
        <button className="refresh" onClick={load}>Refresh</button>
      </header>

      <form className="ask" onSubmit={submit}>
        <label htmlFor="q">Ask the team&apos;s memory</label>
        <div className="askrow">
          <input id="q" value={question} onChange={(e) => setQuestion(e.target.value)}
                 placeholder="Why can't we use exponential backoff?" autoComplete="off" />
          <button type="submit" disabled={asking || question.trim().length < 3}>{asking ? "Asking…" : "Ask"}</button>
        </div>
      </form>

      {error && <p className="error">{error}</p>}

      {sheet === "Map" && graph && (
        <section className="map" aria-label="Decision map">
          {!selected && !reply && <p className="hint">Files on the left, people on the right, decisions between them. Click any mark.</p>}
          <Graph nodes={graph.nodes} edges={graph.edges} onSelect={onSelect}
                 highlight={!selected && reply?.a ? reply.a.cited.map((c) => c.id) : undefined} />
          {selected && <Callout n={selected} onClose={() => setSelected(null)} />}
          {!selected && reply && <Answer r={reply} onClose={() => setReply(null)}
            onOpen={(id) => setSelected(graph.nodes.find((n) => n.id === id) ?? null)} />}
          {stats && !selected && !reply && <TitleBlock s={stats} />}
        </section>
      )}

      {sheet === "Decisions" && (
        <section className="page">
          <table>
            <thead><tr><th>ID</th><th>Decision</th><th>Rule for future code</th><th>Confidence</th><th>Files</th></tr></thead>
            <tbody>
              {decisions.map((d) => (
                <tr key={d.id} className={d.status !== "active" ? "old" : ""}>
                  <td className="mono">{d.id}</td>
                  <td>{d.label}{d.status !== "active" && <><br /><span className="risk-HIGH">{d.status}</span></>}</td>
                  <td>{d.rule}</td>
                  <td>{d.confidence}{d.decay === "likely_outdated" && <><br /><span className="risk-MEDIUM">may be outdated</span></>}</td>
                  <td className="mono">{d.files?.join(", ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      {sheet === "Changes" && (
        <section className="page">
          <table>
            <thead><tr><th>Commit</th><th>Branch</th><th>Author</th><th>Areas</th><th>Risk</th><th>What changed</th></tr></thead>
            <tbody>
              {ledger.map((c) => (
                <tr key={c.id}>
                  <td className="mono">{c.id}</td><td>{c.branch}</td><td>{c.authors.join(", ")}</td>
                  <td>{c.domains.join(", ")}</td><td className={`risk-${c.breaking_risk}`}>{c.breaking_risk.toLowerCase()}</td>
                  <td>{c.summary}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!ledger.length && <p className="hint">No pushes recorded yet. The next push to the repo appears here.</p>}
        </section>
      )}

      {sheet === "Health" && <section className="page doc"><ReactMarkdown>{health}</ReactMarkdown></section>}
      {sheet === "Onboarding" && <section className="page doc"><ReactMarkdown>{onboarding}</ReactMarkdown></section>}
    </main>
  );
}

function Answer({ r, onClose, onOpen }: {
  r: { q: string; a?: AskAnswer; err?: string }; onClose: () => void; onOpen: (id: string) => void;
}) {
  return (
    <aside className="callout" aria-label="Answer" aria-live="polite">
      <button className="close" onClick={onClose} aria-label="Close answer">×</button>
      <div className="id">You asked</div>
      <h2>{r.q}</h2>
      {r.err && <p className="error">{r.err}</p>}
      {r.a && (
        <>
          <p>{r.a.answer}</p>
          {!!r.a.cited.length && (
            <dl><dt>From these decisions, lit up on the map</dt>
              {r.a.cited.map((c) => (
                <dd key={c.id}>
                  <button className="cite" onClick={() => onOpen(c.id)}>
                    <span className="mono">{c.id}</span> {c.title}{c.status !== "active" ? ` (${c.status})` : ""}
                  </button>
                </dd>
              ))}
            </dl>
          )}
        </>
      )}
    </aside>
  );
}

function TitleBlock({ s }: { s: Stats }) {
  const rows: [string, string | number][] = [
    ["Active decisions", s.active],
    ["Superseded", s.superseded],
    ["Coding rules", s.patterns],
    ["Incidents", s.incidents],
    ["Commits recorded", s.commits],
    ["Claude calls today", `${s.usage.calls} of ${s.usage.call_cap}`],
  ];
  return (
    <aside className="titleblock" aria-label="Memory summary">
      <div className="name">KEEP memory</div>
      {rows.map(([k, v]) => <div className="row" key={k}><span className="k">{k}</span><span className="v">{v}</span></div>)}
      <div className="key">
        <span><i className="sw file" />file</span><span><i className="sw decision" />decision</span>
        <span><i className="sw old" />superseded</span><span><i className="sw rule" />rule</span>
        <span><i className="sw incident" />incident</span><span><i className="sw person" />person</span>
      </div>
    </aside>
  );
}

function Callout({ n, onClose }: { n: GraphNode; onClose: () => void }) {
  return (
    <aside className="callout" aria-label="Details">
      <button className="close" onClick={onClose} aria-label="Close details">×</button>
      {n.kind === "decision" && (
        <>
          <div className="id">{n.id}</div>
          <h2>{n.label}</h2>
          <div className="badges">
            <span className={`badge ${n.status !== "active" ? "red" : ""}`}>{n.status}</span>
            <span className="badge">{n.confidence?.toLowerCase()} confidence</span>
            {n.security && <span className="badge red">security</span>}
            {n.decay === "likely_outdated" && <span className="badge amber">may be outdated</span>}
          </div>
          <dl>
            <dt>Decision</dt><dd>{n.decision}</dd>
            {n.rejected && <><dt>Rejected</dt><dd>{n.rejected}</dd></>}
            <dt>Why</dt><dd>{n.reason}</dd>
            <dt>Rule for future code</dt><dd>{n.rule}</dd>
            <dt>Decided by</dt><dd>{n.decided_by?.map((p) => "@" + p).join(", ")}</dd>
            {!!n.files?.length && <><dt>Files</dt><dd className="mono">{n.files.join(", ")}</dd></>}
            <dt>Where it was decided</dt><dd><a href={n.source} target="_blank" rel="noreferrer">{n.source}</a></dd>
          </dl>
          {!!n.history?.length && (
            <><dt style={{ color: "var(--dim)", fontSize: "0.82rem", marginTop: 12 }}>History</dt>
              <ol>{n.history.map((h, i) => <li key={i}>{h.at.slice(0, 10)}: {h.change}</li>)}</ol></>
          )}
        </>
      )}
      {n.kind === "pattern" && (
        <>
          <div className="id">{n.id}</div><h2>{n.label}</h2>
          <div className="badges"><span className="badge">{n.status}</span><span className="badge">{n.language}</span></div>
          <dl><dt>Why</dt><dd>{n.reason}</dd>
            {n.bad && <><dt>Instead of</dt><dd className="mono">{n.bad}</dd><dt>Write</dt><dd className="mono">{n.good}</dd></>}</dl>
        </>
      )}
      {n.kind === "incident" && (
        <>
          <div className="id">{n.id}</div><h2>{n.label}</h2>
          <dl><dt>What KEEP found</dt><dd>{n.summary}</dd>
            {!!n.suspects?.length && <><dt>Check these recent changes first</dt><dd className="mono">{n.suspects.join(", ")}</dd></>}
            {n.url && <><dt>Issue</dt><dd><a href={n.url} target="_blank" rel="noreferrer">{n.url}</a></dd></>}</dl>
        </>
      )}
      {(n.kind === "file" || n.kind === "person") && (
        <><div className="id">{n.kind}</div><h2 className={n.kind === "file" ? "mono" : ""}>{n.label}</h2>
          <p style={{ color: "var(--dim)" }}>Highlighted on the map: every decision connected to this {n.kind}.</p></>
      )}
    </aside>
  );
}
