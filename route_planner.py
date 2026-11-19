"""Step 3: propose, compare and check sequences for one prompt.

Give a START and an END, each with an optional description. The tool:

  1. finds the relevant papers and step chains in the literature graph,
  2. has Claude, as the expert, propose candidate sequences grounded in that
     literature, citing the papers behind every step,
  3. checks each candidate:
       * every step is backed by at least one paper in the library,
       * every step's equipment is available AND accessible in the lab,
       * the noun each step produces is accepted by the next step's equipment
         (the first equipment accepts START, the last one produces END),
  4. prints a comparison of all candidates and saves the valid ones in Neo4j.

    python route_planner.py "print powder" "tensile test specimen" \\
        --start-desc "gas-atomized Ti-6Al-4V powder, 15-45 um" \\
        --end-desc "ASTM E8 round tensile specimen"
"""

from __future__ import annotations

import argparse
import json
import sys

import anthropic
from pydantic import BaseModel, Field

from common import MODEL, database, get_driver, lucene_query, setup_schema


# --------------------------------------------------------------------------
# 1. Literature evidence from Neo4j
# --------------------------------------------------------------------------

PAPERS_QUERY = """
CALL db.index.fulltext.queryNodes('paper_text', $q) YIELD node, score
WITH node AS p, score ORDER BY score DESC LIMIT $k
OPTIONAL MATCH (i:LitNoun)-[:INPUT_OF]->(t:Transition)<-[:REPORTS]-(p),
               (t)-[:OUTPUTS]->(o:LitNoun), (t)-[:USES]->(e:LitEquipment)
WITH p, score, t, i, o, e ORDER BY t.order
RETURN p.id AS id, p.title AS title, p.year AS year, p.abstract AS abstract, score,
       collect(CASE WHEN t IS NULL THEN NULL ELSE
               {input: i.name, process: t.process, equipment: e.name, output: o.name,
                material: t.material, conditions: t.conditions} END) AS steps
ORDER BY score DESC
"""

NOUN_QUERY = """
CALL db.index.fulltext.queryNodes('lit_noun_name', $q) YIELD node, score
RETURN elementId(node) AS id, node.name AS name ORDER BY score DESC LIMIT $k
"""

# Step chains in the literature from a start-like noun to an end-like noun.
PATHS_QUERY = """
MATCH (a:LitNoun) WHERE elementId(a) IN $starts
MATCH (b:LitNoun) WHERE elementId(b) IN $ends AND a <> b
MATCH p = SHORTEST 2 (a)(()-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->()){1,8}(b)
WITH p LIMIT 12
RETURN [n IN nodes(p) WHERE n:Transition |
        {input: [(x)-[:INPUT_OF]->(n) | x.name][0],
         process: n.process,
         equipment: [(n)-[:USES]->(e) | e.name][0],
         output: [(n)-[:OUTPUTS]->(y) | y.name][0],
         paper: [(pp:Paper)-[:REPORTS]->(n) | pp.id][0]}] AS chain
"""


def gather_evidence(session, start: str, start_desc: str, end: str, end_desc: str,
                    k_papers: int) -> dict:
    q = lucene_query(start, start_desc, end, end_desc)
    papers = [r.data() for r in session.run(PAPERS_QUERY, q=q, k=k_papers)] if q else []
    starts = [r["id"] for r in session.run(NOUN_QUERY, q=lucene_query(start, start_desc), k=4)]
    ends = [r["id"] for r in session.run(NOUN_QUERY, q=lucene_query(end, end_desc), k=4)]
    chains = []
    if starts and ends:
        try:
            chains = [r["chain"] for r in session.run(PATHS_QUERY, starts=starts, ends=ends)]
        except Exception as e:  # e.g. Neo4j older than 5.21 (no SHORTEST)
            print(f"note: literature chain search skipped ({type(e).__name__})", file=sys.stderr)
    return {"papers": papers, "chains": chains}


def evidence_text(ev: dict, n_abstracts: int = 8) -> str:
    lines = ["PAPERS (cite them by id):"]
    for i, p in enumerate(ev["papers"]):
        lines.append(f"\n[{p['id']}] {p['title']} ({p['year']})")
        if i < n_abstracts:
            lines.append(f"  abstract: {p['abstract'][:1200]}")
        for s in p["steps"]:
            extra = "; ".join(x for x in (s["material"], s["conditions"]) if x)
            lines.append(f"  step: {s['input']} -> {s['process']} [{s['equipment']}] -> {s['output']}"
                         + (f"  ({extra})" if extra else ""))
    if ev["chains"]:
        lines.append("\nCHAINS FOUND IN THE LITERATURE GRAPH (each step: paper id):")
        for c in ev["chains"]:
            lines.append("  " + "  =>  ".join(
                f"{s['input']} -> {s['process']} [{s['equipment']}] -> {s['output']} ({s['paper']})" for s in c))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 2. The expert: propose candidate sequences grounded in the literature
# --------------------------------------------------------------------------

class Step(BaseModel):
    process: str = Field(description="Short description of what this step does")
    equipment: str = Field(description="The single piece of equipment that performs this step")
    output: str = Field(description="The noun this step produces, which the next step takes in")
    citations: list[str] = Field(default_factory=list,
                                 description="Ids of the papers (e.g. W2741809807) that support this step")


class Candidate(BaseModel):
    steps: list[Step] = Field(description="Ordered steps; the first takes START, the last produces END")
    rationale: str = Field(description="One or two sentences on why this route makes sense")
    basis: str = Field(description="Standards and literature this route is based on")


class Proposal(BaseModel):
    candidates: list[Candidate]


SYSTEM_PROMPT = """You are an expert in materials science, manufacturing processing and \
characterization. Given a START and an END (each possibly with a description), propose \
several distinct, technically sound sequences from START to END.

Ground every step in the literature you are given: cite, for each step, the ids of the \
papers that report that step or an equivalent one. Prefer steps and orderings the papers \
actually use; the literature chains show steps that already connect. You may add your own \
expert knowledge (standards such as ASTM/ISO) to fill gaps, but a step with no supporting \
paper will be rejected, so cite whenever any given paper supports the step. Only cite ids \
from the list you are given.

Each step is performed by exactly one piece of equipment and produces one noun (a material, \
part, specimen or result), which is what the next step takes in. The first step takes START; \
the last must produce END.

Propose on technical merit, not on what the lab owns: a separate check compares your routes \
with the lab afterwards. You are given the lab's equipment and nouns only for naming: when \
the lab has equipment that does the job, use its exact name, and when a noun matches one of \
the lab's nouns, use that exact name. Make the routes genuinely different."""


def propose_sequences(client: anthropic.Anthropic, start: str, start_desc: str, end: str,
                      end_desc: str, lab: list[dict], evidence: str, n: int) -> list[Candidate]:
    lab_text = "\n".join(
        f"- {e['name']}: accepts {', '.join(e['accepts']) or '?'}; produces {', '.join(e['produces']) or '?'}"
        for e in lab
    )
    user = (
        f"{evidence}\n\n"
        f"LAB EQUIPMENT AND NOUNS (for naming only):\n{lab_text}\n\n"
        f"START: {start}" + (f" -- {start_desc}" if start_desc else "") + "\n"
        f"END: {end}" + (f" -- {end_desc}" if end_desc else "") + "\n\n"
        f"Propose up to {n} candidate sequences from START to END."
    )
    with client.beta.messages.stream(
        model=MODEL,
        max_tokens=32000,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if the request is declined, the API retries on a fallback model
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user}],
        output_format=Proposal,
    ) as stream:
        response = stream.get_final_message()
    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined to propose routes for this request.")
    if response.parsed_output is None:
        raise RuntimeError(f"Could not read the model's answer (stop_reason={response.stop_reason}).")
    return response.parsed_output.candidates


# --------------------------------------------------------------------------
# 3. The check: literature support + the lab
# --------------------------------------------------------------------------

LAB_QUERY = """
MATCH (e:Equipment)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN e.name AS name, collect(DISTINCT a.name) AS accepts, collect(DISTINCT p.name) AS produces
ORDER BY name
"""

STEP_QUERY = """
UNWIND range(0, size($equipment) - 1) AS idx
WITH idx, $equipment[idx] AS want
OPTIONAL MATCH (e:Equipment) WHERE toLower(e.name) = toLower(want)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN idx, want, e IS NOT NULL AS found, e.name AS name,
       e.available AS available, e.accessible AS accessible,
       collect(DISTINCT toLower(a.name)) AS accepts,
       collect(DISTINCT toLower(p.name)) AS produces
ORDER BY idx
"""

KNOWN_PAPERS = "MATCH (p:Paper) WHERE p.id IN $ids RETURN collect(p.id) AS ids"

SAVE_QUERY = """
CREATE (q:RouteQuery {start: $start, startDesc: $start_desc, end: $end, endDesc: $end_desc,
                      createdAt: datetime()})
WITH q
UNWIND range(0, size($routes) - 1) AS r
WITH q, r, $routes[r] AS route
CREATE (q)-[:FOUND {rank: r + 1}]->(rt:Route {summary: route.summary, rationale: route.rationale,
                                              basis: route.basis})
WITH rt, route
UNWIND range(0, size(route.steps) - 1) AS s
WITH rt, s, route.steps[s] AS step
MATCH (e:Equipment) WHERE toLower(e.name) = toLower(step.equipment)
MATCH (i:Noun) WHERE toLower(i.name) = toLower(step.input)
MATCH (o:Noun) WHERE toLower(o.name) = toLower(step.output)
CREATE (rt)-[:STEP {order: s + 1}]->(st:Step {order: s + 1, process: step.process})
CREATE (st)-[:TAKES]->(i)
CREATE (st)-[:USES]->(e)
CREATE (st)-[:GIVES]->(o)
WITH st, step
CALL (st, step) {
  UNWIND step.citations AS pid
  MATCH (p:Paper {id: pid})
  CREATE (st)-[:SUPPORTED_BY]->(p)
}
"""


def validate(session, start: str, end: str, cand: Candidate, check_literature: bool) -> list[str]:
    """Check one route. An empty list means the route is valid."""
    if not cand.steps:
        return ["empty sequence"]
    problems: list[str] = []

    if check_literature:
        cited = sorted({c for s in cand.steps for c in s.citations})
        known = set(session.run(KNOWN_PAPERS, ids=cited).single()["ids"]) if cited else set()
        for i, s in enumerate(cand.steps, 1):
            if not any(c in known for c in s.citations):
                problems.append(f"step {i} ({s.equipment}): no supporting paper in the library")
            # keep only real citations, so made-up ids never reach Neo4j
            s.citations = [c for c in s.citations if c in known]

    facts = [r.data() for r in session.run(STEP_QUERY, equipment=[s.equipment for s in cand.steps])]
    incoming = start
    for step, f in zip(cand.steps, facts):
        label = f"step {f['idx'] + 1} ({step.equipment})"
        if not f["found"]:
            problems.append(f"{label}: the lab doesn't have this equipment")
        else:
            if f["available"] is not True:
                problems.append(f"{label}: not available")
            if f["accessible"] is not True:
                problems.append(f"{label}: not accessible")
            if incoming.lower() not in f["accepts"]:
                problems.append(f"{label}: does not accept '{incoming}'")
            if step.output.lower() not in f["produces"]:
                problems.append(f"{label}: does not produce '{step.output}'")
        incoming = step.output

    if cand.steps[-1].output.lower() != end.lower():
        problems.append(f"route ends with '{cand.steps[-1].output}', not '{end}'")
    return problems


def summary(start: str, cand: Candidate) -> str:
    return " -> ".join([start] + [f"[{s.equipment}] -> {s.output}" for s in cand.steps])


# --------------------------------------------------------------------------
# 4. Glue
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="what you start from, e.g. 'print powder'")
    ap.add_argument("end", help="what you want to end with, e.g. 'tensile test specimen'")
    ap.add_argument("--start-desc", default="", help="description of the start object")
    ap.add_argument("--end-desc", default="", help="description of the end object")
    ap.add_argument("-n", "--num", type=int, default=5, help="how many sequences to ask for (default 5)")
    ap.add_argument("--papers", type=int, default=30, help="how many relevant papers to give Claude (default 30)")
    ap.add_argument("--candidates", help="JSON file of candidate sequences to check instead of asking Claude")
    ap.add_argument("--show-rejected", action="store_true", help="also print rejected sequences in full")
    ap.add_argument("--show-evidence", action="store_true", help="print the literature evidence given to Claude")
    ap.add_argument("--no-literature-check", action="store_true",
                    help="don't require each step to be backed by a paper")
    ap.add_argument("--no-save", action="store_true", help="don't save the valid routes in Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    driver = get_driver()
    with driver, driver.session(database=database()) as session:
        setup_schema(session)
        lab = [r.data() for r in session.run(LAB_QUERY)]
        if not any(e["accepts"] or e["produces"] for e in lab):
            print("No lab equipment with ACCEPTS/PRODUCES links in Neo4j. Load it first, e.g. with seed.cypher.",
                  file=sys.stderr)
            return 2
        n_read = session.run("MATCH (p:Paper) WHERE p.extracted RETURN count(p) AS n").single()["n"]
        check_lit = not args.no_literature_check and n_read > 0
        if n_read == 0 and not args.no_literature_check:
            print("note: the literature library is empty or unread, so the literature check is off. "
                  "Run harvest_literature.py and extract_knowledge.py to turn it on.", file=sys.stderr)

        # 1-2. evidence and candidates
        evidence = gather_evidence(session, args.start, args.start_desc, args.end, args.end_desc, args.papers)
        ev_text = evidence_text(evidence)
        if args.show_evidence:
            print(ev_text + "\n")
        if args.candidates:
            with open(args.candidates) as fh:
                candidates = [Candidate(**c) for c in json.load(fh)]
        else:
            candidates = propose_sequences(anthropic.Anthropic(), args.start, args.start_desc, args.end,
                                           args.end_desc, lab, ev_text, args.num)

        # 3. check each
        results = [(c, validate(session, args.start, args.end, c, check_lit)) for c in candidates]
        valid = [c for c, p in results if not p]

        # save the valid ones in Neo4j
        if valid and not args.no_save:
            routes = []
            for c in valid:
                inputs = [args.start] + [s.output for s in c.steps[:-1]]
                routes.append({
                    "summary": summary(args.start, c), "rationale": c.rationale, "basis": c.basis,
                    "steps": [{**s.model_dump(), "input": i} for s, i in zip(c.steps, inputs)],
                })
            session.run(SAVE_QUERY, start=args.start, start_desc=args.start_desc, end=args.end,
                        end_desc=args.end_desc, routes=routes).consume()
        titles = {p["id"]: f"{p['title']} ({p['year']})" for p in evidence["papers"]}
        cited = sorted({x for c in valid for s in c.steps for x in s.citations} - set(titles))
        if cited:
            for r in session.run("MATCH (p:Paper) WHERE p.id IN $ids RETURN p.id AS id, p.title AS t, p.year AS y",
                                 ids=cited):
                titles[r["id"]] = f"{r['t']} ({r['y']})"

    if args.json:
        print(json.dumps({
            "start": args.start, "start_desc": args.start_desc, "end": args.end, "end_desc": args.end_desc,
            "papers_used": len(evidence["papers"]),
            "candidates": [{**c.model_dump(), "valid": not p, "problems": p} for c, p in results],
        }, indent=2))
        return 0 if valid else 1

    # --- comparison of all candidates ---
    print(f"\n{args.start}  ->  {args.end}")
    if args.start_desc or args.end_desc:
        print(f"  start: {args.start_desc or '-'}\n  end:   {args.end_desc or '-'}")
    print(f"{len(evidence['papers'])} relevant papers, {len(evidence['chains'])} literature chains used\n")
    print(f"  {'#':>2}  {'result':<8} {'steps':>5} {'papers':>6}  route")
    for i, (c, p) in enumerate(results, 1):
        n_papers = len({x for s in c.steps for x in s.citations})
        route = " > ".join(s.equipment for s in c.steps)
        print(f"  {i:>2}  {'VALID' if not p else 'rejected':<8} {len(c.steps):>5} {n_papers:>6}  {route}")
        if p:
            print(f"{'':20}why rejected: {p[0]}" + (f" (+{len(p) - 1} more)" if len(p) > 1 else ""))
    print()

    def show(c: Candidate, marker: str, problems: list[str]) -> None:
        print(f"  {marker} {args.start}")
        for i, s in enumerate(c.steps, 1):
            print(f"        {i}. [{s.equipment}] {s.process}  ->  {s.output}")
            for pid in s.citations:
                print(f"             {pid}: {titles.get(pid, '')}")
        print(f"      why:   {c.rationale}")
        print(f"      basis: {c.basis}")
        for x in problems:
            print(f"      - {x}")
        print()

    if valid:
        print("Valid sequences:\n")
        for i, (c, p) in enumerate([(c, p) for c, p in results if not p], 1):
            show(c, f"[{i}]", p)
    else:
        print("None of the proposed sequences can be run in this lab right now.\n")
    if args.show_rejected:
        rejected = [(c, p) for c, p in results if p]
        if rejected:
            print("Rejected sequences:\n")
            for c, p in rejected:
                show(c, "x", p)
    if valid and not args.no_save:
        print("Saved in Neo4j. View them, with their supporting papers, in the Neo4j Browser:")
        print("  MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x\n")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
