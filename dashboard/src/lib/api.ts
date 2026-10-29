// All reads go to the KEEP FastAPI backend. The browser calls it directly (CORS allows localhost:3000).
export const API = process.env.NEXT_PUBLIC_KEEP_API ?? "http://localhost:8000";

export type NodeKind = "decision" | "file" | "person" | "pattern" | "incident";

export interface GraphNode {
  id: string;
  kind: NodeKind;
  label: string;
  status?: string;
  confidence?: string;
  decision?: string;
  rejected?: string | null;
  reason?: string;
  rule?: string;
  source?: string;
  decided_by?: string[];
  files?: string[];
  created_at?: string;
  security?: boolean;
  decay?: string | null;
  history?: { at: string; change: string }[];
  language?: string;
  bad?: string;
  good?: string;
  url?: string;
  summary?: string;
  suspects?: string[];
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: "GOVERNS" | "DECIDED" | "DEPENDS_ON" | "SUPERSEDES" | "RELATES_TO";
}

export interface Stats {
  active: number;
  superseded: number;
  patterns: number;
  incidents: number;
  commits: number;
  usage: { calls: number; call_cap: number; tokens: number; cost_usd: number };
}

export interface LedgerEntry {
  id: string;
  branch: string;
  authors: string[];
  date: string;
  domains: string[];
  breaking_risk: string;
  summary: string;
}

export async function get<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`${path} -> ${r.status}`);
  return r.json() as Promise<T>;
}

export interface AskAnswer {
  answer: string;
  cited: { id: string; title: string; status: string }[];
}

export async function ask(question: string): Promise<AskAnswer> {
  const r = await fetch(`${API}/ask`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
  });
  if (!r.ok) {
    const detail = await r.json().then((j) => j.detail).catch(() => r.statusText);
    throw new Error(typeof detail === "string" ? detail : `Ask failed (${r.status})`);
  }
  return r.json() as Promise<AskAnswer>;
}
