"""Graph data for the dashboard, read from MongoDB (the source of truth).

Node kinds: decision, file, person, pattern, incident.
Edge kinds: GOVERNS (decision->file), DECIDED (person->decision), DEPENDS_ON, SUPERSEDES,
RELATES_TO (incident->decision).
"""

import re

from app.store import from_mongo

OVERRIDES = re.compile(r"^overrides (KEEP-D-\d{3,})$")


async def graph(db) -> dict:
    nodes, edges = {}, []

    def node(id_, kind, label, **data):
        nodes.setdefault(id_, {"id": id_, "kind": kind, "label": label, **data})

    async for doc in db.decisions.find({}):
        d = from_mongo(doc)
        node(d.id, "decision", d.title, status=d.status.value, confidence=doc.get("confidence", d.confidence.value),
             decision=d.decision, rejected=d.rejected, reason=d.reason, rule=d.future_implication,
             source=str(d.source.url), decided_by=d.decided_by, files=d.files,
             created_at=d.created_at.isoformat(), security=d.security_relevant,
             decay=(doc.get("decay") or {}).get("verdict"),
             history=[{"at": h.at.isoformat(), "change": h.change} for h in d.history])
        for f in d.files:
            node(f"file:{f}", "file", f)
            edges.append({"source": d.id, "target": f"file:{f}", "kind": "GOVERNS"})
        for p in d.decided_by:
            node(f"person:{p}", "person", "@" + p)
            edges.append({"source": f"person:{p}", "target": d.id, "kind": "DECIDED"})
        for t in d.depends_on:
            edges.append({"source": d.id, "target": t, "kind": "DEPENDS_ON"})
        for h in d.history:
            m = OVERRIDES.match(h.change)
            if m:
                edges.append({"source": d.id, "target": m.group(1), "kind": "SUPERSEDES"})

    async for p in db.patterns.find({}):
        node(p["_id"], "pattern", p["rule"], status=p.get("status"), language=p.get("language"),
             reason=p.get("reason"), bad=p.get("bad_example"), good=p.get("good_example"))

    async for i in db.incidents.find({}):
        node(i["_id"], "incident", i.get("title") or i["_id"], url=i.get("url"),
             summary=i.get("summary"), suspects=i.get("suspects", []))
        for d in i.get("decisions", []):
            edges.append({"source": i["_id"], "target": d, "kind": "RELATES_TO"})

    # Drop edges to nodes that do not exist (e.g. a deleted decision).
    edges = [e for e in edges if e["source"] in nodes and e["target"] in nodes]
    return {"nodes": list(nodes.values()), "edges": edges}
