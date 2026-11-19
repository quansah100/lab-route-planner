"""Step 4: find, compare and check sequences for one prompt (free by default).

Give a START and an END, each with an optional description. The tool:

  1. takes candidate sequences from the literature graph in Neo4j: chains of
     steps where each step's output is the next step's input, every step
     reported by a paper (free, no AI needed),
     optionally adding suggestions from an AI model (--llm local / claude),
  2. translates them into your lab's names (the matches from map_names.py),
  3. checks each candidate:
       * every step is backed by at least one paper in the library,
       * every step's equipment is available AND accessible in the lab,
       * the noun each step produces is accepted by the next step's equipment
         (the first equipment accepts START, the last one produces END),
  4. prints a comparison of all candidates and saves the valid ones in Neo4j.

    python route_planner.py "print powder" "tensile test specimen" \\
        --start-desc "gas-atomized Ti-6Al-4V powder" --end-desc "ASTM E8 round specimen"
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import BaseModel, Field

from common import database, get_driver, lucene_query, setup_schema
from llm import EngineError, ask, check_local
from matching import resolve_endpoints


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


# --------------------------------------------------------------------------
# 1a. Candidates straight from the literature graph (free)
# --------------------------------------------------------------------------

LAB_QUERY = """
MATCH (e:Equipment)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN e.name AS name, e.available = true AND e.accessible = true AS usable,
       collect(DISTINCT a.name) AS accepts, collect(DISTINCT p.name) AS produces
ORDER BY name
"""

MAPPED_NOUNS = """
MATCH (n:Noun)-[:SAME_AS]->(l:LitNoun) WHERE toLower(n.name) = toLower($name)
RETURN collect(elementId(l)) AS ids
"""

NOUN_SEARCH = """
CALL db.index.fulltext.queryNodes('lit_noun_name', $q) YIELD node, score
RETURN elementId(node) AS id ORDER BY score DESC LIMIT $k
"""

CHAINS_QUERY = """
MATCH (a:LitNoun) WHERE elementId(a) IN $starts
MATCH (b:LitNoun) WHERE elementId(b) IN $ends AND a <> b
MATCH p = SHORTEST {k} (a)(()-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->()){{1,{hops}}}(b)
RETURN [t IN nodes(p) WHERE t:Transition | {{
          process: t.process,
          lit_equipment: [(t)-[:USES]->(e) | e.name][0],
          lit_output: [(t)-[:OUTPUTS]->(o) | o.name][0],
          paper: [(pp:Paper)-[:REPORTS]->(t) | pp.id][0],
          lab_equipment: [(t)-[:USES]->(:LitEquipment)<-[:SAME_AS]-(le:Equipment) | le.name],
          lab_output: [(t)-[:OUTPUTS]->(:LitNoun)<-[:SAME_AS]-(ln:Noun) | ln.name]
       }}] AS chain
"""


def lit_endpoints(session, name: str, desc: str) -> tuple[list[str], bool]:
    """Literature nouns for a start/end: the ones you matched in map_names.py, else a text search."""
    ids = session.run(MAPPED_NOUNS, name=name).single()["ids"]
    if ids:
        return ids, True
    q = lucene_query(name, desc)
    return ([r["id"] for r in session.run(NOUN_SEARCH, q=q, k=3)] if q else []), False


REACHABLE_QUERY = """
MATCH (a:LitNoun) WHERE elementId(a) IN $starts
MATCH (a)(()-[:INPUT_OF]->(:Transition)-[:OUTPUTS]->()){{1,{hops}}}(b:LitNoun)
WHERE b <> a
RETURN DISTINCT b.name AS name LIMIT 12
"""

NAMES_QUERY = "MATCH (n:LitNoun) WHERE elementId(n) IN $ids RETURN collect(n.name) AS names"


def graph_candidates(session, start: str, start_text: str, end: str, end_text: str,
                     lab: dict[str, dict], per_pair: int, max_steps: int) -> tuple[list[Candidate], dict]:
    """Literature chains from start to end. Also returns what was found, to explain an empty result."""
    starts, s_mapped = lit_endpoints(session, start, start_text)
    ends, e_mapped = lit_endpoints(session, end, end_text)
    info = {"start_lit": session.run(NAMES_QUERY, ids=starts).single()["names"],
            "end_lit": session.run(NAMES_QUERY, ids=ends).single()["names"],
            "start_mapped": s_mapped, "end_mapped": e_mapped, "reachable": []}
    if not starts or not ends:
        return [], info
    rows = list(session.run(CHAINS_QUERY.format(k=int(per_pair), hops=int(max_steps)), starts=starts, ends=ends))
    if not rows:
        info["reachable"] = [r["name"] for r in session.run(REACHABLE_QUERY.format(hops=int(max_steps)),
                                                            starts=starts)]

    merged: dict[tuple, Candidate] = {}
    for row in rows:
        incoming, steps = start, []
        for t in row["chain"]:
            equipment = pick_equipment(t["lab_equipment"], incoming, lab) or f"{t['lit_equipment']} (literature name)"
            produced = lab.get(equipment, {}).get("produces", [])
            outs = [o for o in t["lab_output"] if o.lower() in produced] or t["lab_output"]
            output = outs[0] if outs else f"{t['lit_output']} (literature name)"
            steps.append(Step(process=t["process"], equipment=equipment, output=output, citations=[t["paper"]]))
            incoming = output
        key = tuple((s.equipment.lower(), s.output.lower()) for s in steps)
        if key in merged:  # same route found via other papers: pool the citations
            for s, new in zip(merged[key].steps, steps):
                s.citations = sorted(set(s.citations) | set(new.citations))
        else:
            merged[key] = Candidate(steps=steps, rationale="Chain of steps reported in the literature; "
                                    "each step's output is the next step's input.", basis="")
    for c in merged.values():
        c.basis = "Papers: " + ", ".join(sorted({x for s in c.steps for x in s.citations}))
    return list(merged.values()), info


def explain_empty(typed_start: str, typed_end: str, info: dict, max_steps: int) -> str:
    """Plain-language reason why the literature gave no candidates."""
    if not info["start_lit"] and not info["end_lit"]:
        why = f"No paper in the library mentions anything like '{typed_start}' or '{typed_end}'."
    elif not info["start_lit"]:
        why = f"No paper in the library mentions anything like '{typed_start}'."
    elif not info["end_lit"]:
        why = f"No paper in the library mentions anything like '{typed_end}'."
    else:
        why = (f"The library has papers on '{typed_start}' (as: {', '.join(info['start_lit'])}) and on "
               f"'{typed_end}' (as: {', '.join(info['end_lit'])}), but no chain of reported steps "
               f"connects them within {max_steps} steps.")
        if info["reachable"]:
            why += f"\n  From '{typed_start}' the literature reaches: {', '.join(info['reachable'])}."
        else:
            why += f"\n  The literature has no steps that start from '{typed_start}'."
    tips = ["harvest papers on this topic:  python harvest_literature.py --topic \"...\" --target 100"
            "  (then extract_knowledge.py and map_names.py)",
            "ask an AI to fill the gap:  add --llm local"]
    if not info["start_mapped"] or not info["end_mapped"]:
        tips.insert(0, "link your lab's names to the literature's:  python map_names.py")
    return why + "\n  What you can do:\n" + "\n".join(f"    - {t}" for t in tips)


def pick_equipment(options: list[str], incoming: str, lab: dict[str, dict]) -> str | None:
    """Of the lab equipment matched to a literature step, prefer one that is usable and accepts the input."""
    if not options:
        return None
    def score(name: str) -> int:
        e = lab.get(name, {})
        return 2 * (incoming.lower() in e.get("accepts", [])) + bool(e.get("usable"))
    return max(options, key=score)


# --------------------------------------------------------------------------
# 1b. Optional: suggestions from an AI model, grounded in the papers
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

SYSTEM_PROMPT = """You are an expert in materials science, manufacturing processing and \
characterization. Given a START and an END (each possibly with a description), propose \
several distinct, technically sound sequences from START to END.

Ground every step in the literature you are given: cite, for each step, the ids of the \
papers that report that step or an equivalent one. A step with no supporting paper will be \
rejected, so cite whenever any given paper supports the step. Only cite ids from the list.

Each step is performed by exactly one piece of equipment and produces one noun (a material, \
part, specimen or result), which is what the next step takes in. The first step takes START; \
the last must produce END.

Use the lab's exact names for equipment and nouns when they mean the same thing. Propose on \
technical merit; a separate check compares your routes with the lab afterwards.

Answer only with JSON of the form {"candidates": [{"steps": [{"process": "...", \
"equipment": "...", "output": "...", "citations": ["W..."]}], "rationale": "...", "basis": "..."}]}."""


def llm_candidates(session, engine: str, start: str, start_desc: str, end: str, end_desc: str,
                   lab: dict[str, dict], n: int, k_papers: int) -> tuple[list[Candidate], int]:
    q = lucene_query(start, start_desc, end, end_desc)
    papers = [r.data() for r in session.run(PAPERS_QUERY, q=q, k=k_papers)] if q else []
    n_abstracts = 8 if engine == "claude" else 3
    lines = ["PAPERS (cite them by id):"]
    for i, p in enumerate(papers):
        lines.append(f"\n[{p['id']}] {p['title']} ({p['year']})")
        if i < n_abstracts:
            lines.append(f"  abstract: {p['abstract'][:1000]}")
        for s in p["steps"]:
            lines.append(f"  step: {s['input']} -> {s['process']} [{s['equipment']}] -> {s['output']}")
    lab_text = "\n".join(f"- {name}: accepts {', '.join(e['accepts']) or '?'}; "
                         f"produces {', '.join(e['produces']) or '?'}" for name, e in lab.items())
    user = ("\n".join(lines) + f"\n\nLAB EQUIPMENT AND NOUNS:\n{lab_text}\n\n"
            f"START: {start}" + (f" -- {start_desc}" if start_desc else "") + "\n"
            f"END: {end}" + (f" -- {end_desc}" if end_desc else "") + "\n\n"
            f"Propose up to {n} candidate sequences from START to END.")
    return ask(engine, SYSTEM_PROMPT, user, Proposal).candidates, len(papers)


# --------------------------------------------------------------------------
# 2. The check: literature support + the lab
# --------------------------------------------------------------------------

STEP_QUERY = """
UNWIND range(0, size($equipment) - 1) AS idx
WITH idx, $equipment[idx] AS want
OPTIONAL MATCH (e:Equipment) WHERE toLower(e.name) = toLower(want)
OPTIONAL MATCH (e)-[:ACCEPTS]->(a:Noun)
OPTIONAL MATCH (e)-[:PRODUCES]->(p:Noun)
RETURN idx, e IS NOT NULL AS found, e.name AS name,
       e.available AS available, e.accessible AS accessible,
       collect(DISTINCT toLower(a.name)) AS accepts,
       collect(DISTINCT toLower(p.name)) AS produces
ORDER BY idx
"""

KNOWN_PAPERS = "MATCH (p:Paper) WHERE p.id IN $ids RETURN collect(p.id) AS ids"

SAVE_QUERY = """
CREATE (q:RouteQuery {start: $start, startTyped: $start_typed, startDesc: $start_desc,
                      end: $end, endTyped: $end_typed, endDesc: $end_desc, createdAt: datetime()})
WITH q
UNWIND range(0, size($routes) - 1) AS r
WITH q, r, $routes[r] AS route
CREATE (q)-[:FOUND {rank: r + 1}]->(rt:Route {summary: route.summary, rationale: route.rationale,
                                              basis: route.basis, source: route.source})
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
            s.citations = [c for c in s.citations if c in known]  # never store made-up ids

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
# 3. Glue
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="what you start from, e.g. 'titanium powder'")
    ap.add_argument("end", help="what you want to end with, e.g. 'tensile specimen'")
    ap.add_argument("--start-desc", default="", help="description of the start object")
    ap.add_argument("--end-desc", default="", help="description of the end object")
    ap.add_argument("--llm", choices=["none", "local", "claude"], default="none",
                    help="also ask an AI for suggestions: local (free, Ollama) or claude (paid). Default: none")
    ap.add_argument("-n", "--num", type=int, default=5, help="how many AI suggestions to ask for (default 5)")
    ap.add_argument("--papers", type=int, help="papers given to the AI (default 12 local, 30 claude)")
    ap.add_argument("--max-steps", type=int, default=10, help="longest literature chain to consider (default 10)")
    ap.add_argument("--per-pair", type=int, default=20, help="literature chains per start/end match (default 20)")
    ap.add_argument("--candidates", help="JSON file of candidate sequences to check as well")
    ap.add_argument("--exact", action="store_true",
                    help="use start/end exactly as typed; don't translate them into your lab's names")
    ap.add_argument("--match", choices=["local", "claude", "words"], default="local",
                    help="how to translate start/end into lab names: local = free AI, checks every word "
                         "and the operational role (default); claude = paid AI; words = shared words only")
    ap.add_argument("--yes", action="store_true", help="never ask; take the best lab name automatically")
    ap.add_argument("--valid-only", action="store_true", help="list only valid candidates in the comparison")
    ap.add_argument("--show-rejected", action="store_true", help="also print rejected sequences in full")
    ap.add_argument("--no-literature-check", action="store_true",
                    help="don't require each step to be backed by a paper")
    ap.add_argument("--no-save", action="store_true", help="don't save the valid routes in Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    if args.llm == "local":
        try:
            check_local()
        except EngineError as e:
            print(e, file=sys.stderr)
            return 2

    driver = get_driver()
    with driver, driver.session(database=database()) as session:
        setup_schema(session)
        lab = {r["name"]: {"usable": r["usable"], "accepts": [a.lower() for a in r["accepts"]],
                           "produces": [p.lower() for p in r["produces"]]} for r in session.run(LAB_QUERY)}
        if not any(e["accepts"] or e["produces"] for e in lab.values()):
            print("No lab equipment with ACCEPTS/PRODUCES links in Neo4j. Load it first, e.g. with seed.cypher.",
                  file=sys.stderr)
            return 2

        # 0. what you typed -> your lab's names
        start, end, notes = args.start, args.end, []
        if not args.exact:
            ask_user = sys.stdin.isatty() and not args.yes and not args.json
            r_start, r_end, notes = resolve_endpoints(session, args.start, args.start_desc, args.end,
                                                      args.end_desc, args.match, ask_user)
            missing = [t for t, r in ((args.start, r_start), (args.end, r_end)) if r is None]
            start, end = r_start or args.start, r_end or args.end
            if missing:
                for note in notes:
                    print(f"  ({note})")
                nouns = [r["n"] for r in session.run("MATCH (n:Noun) RETURN n.name AS n ORDER BY n")]
                print("\nNothing in your lab matches " + " or ".join(f"'{m}'" for m in missing) + ".\n"
                      "  Your lab's nouns are: " + ", ".join(nouns) + "\n"
                      "  What you can do:\n"
                      "    - use one of those names (or a close wording of it)\n"
                      "    - if your lab really works with it, add it to seed.cypher (which equipment accepts or "
                      "produces it) and reload\n")
                return 1

        n_read = session.run("MATCH (p:Paper) WHERE p.extracted RETURN count(p) AS n").single()["n"]
        check_lit = not args.no_literature_check and n_read > 0
        if n_read == 0:
            print("note: the literature library is empty or unread; run harvest_literature.py and "
                  "extract_knowledge.py. The literature check is off.", file=sys.stderr)

        # 1. candidates, tagged with where they came from
        candidates: list[tuple[str, Candidate]] = []
        info = None
        if n_read:
            found, info = graph_candidates(session, start, f"{args.start} {args.start_desc}",
                                           end, f"{args.end} {args.end_desc}",
                                           lab, args.per_pair, args.max_steps)
            candidates += [("literature", c) for c in found]
        if args.llm != "none":
            k = args.papers or (30 if args.llm == "claude" else 12)
            try:
                found, _ = llm_candidates(session, args.llm, start, args.start_desc, end,
                                          args.end_desc, lab, args.num, k)
                tag = "claude" if args.llm == "claude" else "local AI"
                candidates += [(tag, c) for c in found]
            except EngineError as e:
                print(f"note: AI suggestions skipped: {e}", file=sys.stderr)
        if args.candidates:
            with open(args.candidates) as fh:
                candidates += [("file", Candidate(**c)) for c in json.load(fh)]

        # 2. check each; valid first, then shorter, then better supported
        results = [(src, c, validate(session, start, end, c, check_lit)) for src, c in candidates]
        results.sort(key=lambda r: (bool(r[2]), len(r[1].steps),
                                    -len({x for s in r[1].steps for x in s.citations})))
        valid = [(src, c) for src, c, p in results if not p]

        if valid and not args.no_save:
            routes = []
            for src, c in valid:
                inputs = [start] + [s.output for s in c.steps[:-1]]
                routes.append({"summary": summary(start, c), "rationale": c.rationale, "basis": c.basis,
                               "source": src,
                               "steps": [{**s.model_dump(), "input": i} for s, i in zip(c.steps, inputs)]})
            session.run(SAVE_QUERY, start=start, start_typed=args.start, start_desc=args.start_desc,
                        end=end, end_typed=args.end, end_desc=args.end_desc, routes=routes).consume()

        cited = sorted({x for _, c, _ in results for s in c.steps for x in s.citations})
        titles = {r["id"]: f"{r['t']} ({r['y']})" for r in session.run(
            "MATCH (p:Paper) WHERE p.id IN $ids RETURN p.id AS id, p.title AS t, p.year AS y", ids=cited)}

    if args.json:
        print(json.dumps({
            "start": start, "start_typed": args.start, "start_desc": args.start_desc,
            "end": end, "end_typed": args.end, "end_desc": args.end_desc, "notes": notes,
            "candidates": [{**c.model_dump(), "source": src, "valid": not p, "problems": p}
                           for src, c, p in results],
        }, indent=2))
        return 0 if valid else 1

    # --- comparison of all candidates ---
    print(f"\n{start}  ->  {end}")
    for note in notes:
        print(f"  ({note})")
    if args.start_desc or args.end_desc:
        print(f"  start: {args.start_desc or '-'}\n  end:   {args.end_desc or '-'}")
    by_src = {}
    for src, _, _ in results:
        by_src[src] = by_src.get(src, 0) + 1
    print(f"{len(results)} candidates" + (" (" + ", ".join(f"{n} from {s}" for s, n in by_src.items()) + ")"
                                          if by_src else "") + f", {len(valid)} valid in this lab\n")
    if not results:
        if info is not None:
            print("  No candidates found. " + explain_empty(args.start, args.end, info, args.max_steps) + "\n")
        else:
            print("  No candidates found: the literature library is empty. Build it first "
                  "(harvest_literature.py, then extract_knowledge.py).\n")
        return 1
    print(f"  {'#':>2}  {'result':<8} {'source':<10} {'steps':>5} {'papers':>6}  route")
    for i, (src, c, p) in enumerate(results, 1):
        if args.valid_only and p:
            continue
        n_p = len({x for s in c.steps for x in s.citations})
        route = " > ".join(s.equipment for s in c.steps)
        print(f"  {i:>2}  {'VALID' if not p else 'rejected':<8} {src:<10} {len(c.steps):>5} {n_p:>6}  {route}")
        if p:
            print(f"{'':31}why rejected: {p[0]}" + (f" (+{len(p) - 1} more)" if len(p) > 1 else ""))
    print()

    def show(c: Candidate, marker: str, src: str, problems: list[str]) -> None:
        print(f"  {marker} {start}   (from {src})")
        for i, s in enumerate(c.steps, 1):
            print(f"        {i}. [{s.equipment}] {s.process}  ->  {s.output}")
            for pid in s.citations[:3]:
                print(f"             {pid}: {titles.get(pid, '')}")
            if len(s.citations) > 3:
                print(f"             ... and {len(s.citations) - 3} more papers")
        if src != "literature":
            print(f"      why:   {c.rationale}")
            print(f"      basis: {c.basis}")
        for x in problems:
            print(f"      - {x}")
        print()

    if valid:
        print("Valid sequences:\n")
        for i, (src, c) in enumerate(valid, 1):
            show(c, f"[{i}]", src, [])
    else:
        print("None of the candidates can be run in this lab right now.\n")
    if args.show_rejected:
        for src, c, p in results:
            if p:
                show(c, "x", src, p)
    if valid and not args.no_save:
        print("Saved in Neo4j. View them, with their supporting papers, in the Neo4j Browser:")
        print("  MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x\n")
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main())
