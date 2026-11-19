"""Trace backwards from the END to the START through your lab (free).

Starting at the end object, ask: which actions in the lab produce it, and from
what? Then repeat for each of those inputs, back to the start. For every action
(a verb: "turn X into Y") it lists ALL the lab equipment that can perform it,
and it counts how many different paths lead from the end back to the start.

    python trace_back.py "Ti64 powder" "dogbone"
    python trace_back.py "Print powder" "Tensile test results" --start-desc "..." --end-desc "..."

An action is identified by what goes in and what comes out. Any equipment that
accepts X and produces Y can perform "X -> Y", so equipment is grouped by action.
The verb comes from the papers (what they call that step); where the papers
don't cover it, the local AI names it (or a plain description is used).
Results are saved in Neo4j to view as a graph.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict

from pydantic import BaseModel

from common import database, get_driver, setup_schema
from llm import EngineError, ask, check_local
from matching import resolve_endpoints

EDGES = """
MATCH (e:Equipment)-[:ACCEPTS]->(a:Noun), (e)-[:PRODUCES]->(b:Noun)
WHERE a <> b
RETURN a.name AS input, b.name AS output, e.name AS equipment,
       coalesce(e.available, false) AS available, coalesce(e.accessible, false) AS accessible
"""

# what the papers call the action "input -> output", and the equipment they use for it
ACTION_LIT = """
MATCH (:Noun {name: $inp})-[:SAME_AS]->(:LitNoun)-[:INPUT_OF]->(t:Transition)
      -[:OUTPUTS]->(:LitNoun)<-[:SAME_AS]-(:Noun {name: $out})
MATCH (p:Paper)-[:REPORTS]->(t)
OPTIONAL MATCH (t)-[:USES]->(le:LitEquipment)
OPTIONAL MATCH (lab:Equipment)-[:SAME_AS]->(le)
RETURN t.process AS process, p.id AS paper, le.name AS lit_equipment, lab.name AS lab_equipment
"""

# fallback: what papers call steps done with this equipment that make this output
EQUIP_LIT = """
MATCH (:Equipment {name: $eq})-[:SAME_AS]->(:LitEquipment)<-[:USES]-(t:Transition)
      -[:OUTPUTS]->(:LitNoun)<-[:SAME_AS]-(:Noun {name: $out})
MATCH (p:Paper)-[:REPORTS]->(t)
RETURN t.process AS process, p.id AS paper
"""

SAVE = """
CREATE (bt:BackTrace {start: $start, end: $end, startTyped: $start_typed, endTyped: $end_typed,
                      paths: $n_paths, combinations: $n_combos, runnableCombinations: $n_runnable,
                      createdAt: datetime()})
WITH bt
UNWIND $actions AS a
MATCH (i:Noun {name: a.input}), (o:Noun {name: a.output})
CREATE (bt)-[:HAS_ACTION]->(act:Action {key: a.key, verb: a.verb, papers: size(a.papers)})
CREATE (i)-[:INPUT_OF]->(act), (act)-[:YIELDS]->(o)
WITH bt, act, a
CALL (act, a) {
  UNWIND a.equipment AS eq
  MATCH (e:Equipment {name: eq.name})
  CREATE (act)-[:CAN_BE_DONE_BY {usable: eq.usable}]->(e)
}
CALL (act, a) {
  UNWIND a.papers AS pid
  MATCH (p:Paper {id: pid})
  CREATE (act)-[:SUPPORTED_BY]->(p)
}
WITH DISTINCT bt
UNWIND range(0, size($paths) - 1) AS k
CREATE (bt)-[:PATH {rank: k + 1}]->(:TracePath {rank: k + 1, actions: $paths[k], runnable: $runnable[k]})
"""


class VerbName(BaseModel):
    input: str
    output: str
    verb: str


class VerbList(BaseModel):
    verbs: list[VerbName]


VERB_PROMPT = """You name manufacturing and testing actions. For each action you get what \
goes in, what comes out and the equipment that does it. Give a short verb phrase (2-5 \
words, lower case) for what the action does, e.g. 'stress relief heat treatment', \
'cut from build plate', 'turn to round specimen', 'measure particle size', 'tensile test'. \
Answer only with JSON: {"verbs": [{"input": "...", "output": "...", "verb": "..."}]}."""


def name_verbs(session, actions: dict, engine: str) -> None:
    """Give every action a verb and its supporting papers, in place."""
    unnamed = []
    for (inp, out), act in actions.items():
        rows = [r.data() for r in session.run(ACTION_LIT, inp=inp, out=out)]
        if not rows:
            rows = [r.data() for eq in act["equipment"]
                    for r in session.run(EQUIP_LIT, eq=eq["name"], out=out)]
        act["papers"] = list(dict.fromkeys(r["paper"] for r in rows))[:8]
        if rows:
            act["verb"] = Counter(r["process"] for r in rows if r["process"]).most_common(1)[0][0]
            act["verb_from"] = "papers"
            # equipment the papers use for this action that isn't listed for it in your lab:
            #  - in your lab, but not recorded as taking this input / making this output (check your lab data)
            #  - not in your lab at all (could be bought, borrowed or outsourced)
            lab_names = {e["name"] for e in act["equipment"]}
            hints = set()
            for r in rows:
                le, lab = r.get("lit_equipment"), r.get("lab_equipment")
                if not le or le == "unspecified" or lab in lab_names:
                    continue
                hints.add(f"{lab} (in your lab, but not recorded as taking {inp})" if lab
                          else f"{le} (not in your lab)")
            act["lit_only"] = sorted(hints)[:3]
        else:
            unnamed.append((inp, out, act))
    if not unnamed:
        return
    if engine != "none":
        try:
            if engine == "local":
                check_local()
            user = "\n".join(f"- in: {i} | out: {o} | equipment: {', '.join(e['name'] for e in a['equipment'])}"
                             for i, o, a in unnamed)
            named = {(v.input.lower(), v.output.lower()): v.verb
                     for v in ask(engine, VERB_PROMPT, user, VerbList, effort="low").verbs}
            for i, o, a in unnamed:
                if (i.lower(), o.lower()) in named:
                    a["verb"], a["verb_from"] = named[(i.lower(), o.lower())], "AI"
        except EngineError as e:
            print(f"note: verbs not named by AI ({e})", file=sys.stderr)
    for i, o, a in unnamed:
        a.setdefault("verb", f"turn {i.lower()} into {o.lower()}")
        a.setdefault("verb_from", "plain description")


def backward_paths(preds: dict[str, set[str]], start: str, end: str, max_steps: int, cap: int):
    """All simple paths from END back to START, as noun lists in forward order."""
    paths: list[list[str]] = []

    def walk(node: str, trail: list[str]) -> None:
        if len(paths) >= cap:
            return
        if node == start:
            paths.append(list(reversed(trail)))
            return
        if len(trail) > max_steps:
            return
        for p in sorted(preds.get(node, ())):
            if p not in trail:
                walk(p, trail + [p])

    walk(end, [end])
    return paths


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("start", help="what you start from")
    ap.add_argument("end", help="what you want to end with")
    ap.add_argument("--start-desc", default="", help="description of the start object")
    ap.add_argument("--end-desc", default="", help="description of the end object")
    ap.add_argument("--match", choices=["local", "claude", "words"], default="local",
                    help="how to translate start/end into lab names (default: local AI)")
    ap.add_argument("--verbs", choices=["local", "claude", "none"], default="local",
                    help="who names actions the papers don't cover (default: local AI; none = plain description)")
    ap.add_argument("--usable-only", action="store_true", help="ignore equipment that isn't usable today")
    ap.add_argument("--max-steps", type=int, default=12, help="longest path to consider (default 12)")
    ap.add_argument("--max-paths", type=int, default=200, help="stop counting after this many paths (default 200)")
    ap.add_argument("--show", type=int, default=5, help="paths printed in full (default 5; the rest in short)")
    ap.add_argument("--yes", action="store_true", help="never ask; take the best lab names automatically")
    ap.add_argument("--no-save", action="store_true", help="don't save the trace in Neo4j")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    args = ap.parse_args()

    driver = get_driver()
    with driver, driver.session(database=database()) as session:
        setup_schema(session)
        ask_user = sys.stdin.isatty() and not args.yes and not args.json
        start, end, notes = resolve_endpoints(session, args.start, args.start_desc, args.end, args.end_desc,
                                              args.match, ask_user)
        if start is None or end is None:
            missing = [t for t, r in ((args.start, start), (args.end, end)) if r is None]
            nouns = [r["n"] for r in session.run("MATCH (n:Noun) RETURN n.name AS n ORDER BY n")]
            if args.json:
                print(json.dumps({"error": "no_lab_match", "unmatched": missing, "lab_nouns": nouns, "notes": notes}))
            else:
                for n in notes:
                    print(f"  ({n})")
                print(f"\nNothing in your lab matches {' or '.join(repr(m) for m in missing)}.\n"
                      f"  Your lab's nouns are: {', '.join(nouns)}")
            return 1

        # the lab as actions: (input, output) -> equipment that can do it
        actions: dict[tuple[str, str], dict] = {}
        for r in session.run(EDGES):
            usable = r["available"] and r["accessible"]
            if args.usable_only and not usable:
                continue
            why = [] if usable else [w for w, ok in (("not available", r["available"]),
                                                     ("not accessible", r["accessible"])) if not ok]
            act = actions.setdefault((r["input"], r["output"]), {"equipment": []})
            act["equipment"].append({"name": r["equipment"], "usable": usable, "why": ", ".join(why)})
        for act in actions.values():
            act["equipment"].sort(key=lambda e: (not e["usable"], e["name"]))

        preds: dict[str, set[str]] = defaultdict(set)
        for inp, out in actions:
            preds[out].add(inp)
        paths = backward_paths(preds, start, end, args.max_steps, args.max_paths)

        used = {(p[i], p[i + 1]) for p in paths for i in range(len(p) - 1)}
        used_actions = {k: actions[k] for k in used}
        name_verbs(session, used_actions, args.verbs)

        def combos(path, usable_only=False):
            return math.prod(len([e for e in actions[(path[i], path[i + 1])]["equipment"]
                                  if e["usable"] or not usable_only]) for i in range(len(path) - 1))

        paths.sort(key=lambda p: (combos(p, True) == 0, len(p), -combos(p, True)))  # runnable, short first
        n_combos = sum(combos(p) for p in paths)
        n_runnable = sum(combos(p, True) for p in paths)

        # what the end is made from (first step backwards), even if no path reaches the start
        last_step = sorted({inp for inp, out in actions if out == end})

        if paths and not args.no_save:
            akey = {k: f"{k[0]} -> {k[1]}" for k in used_actions}
            session.run(SAVE, start=start, end=end, start_typed=args.start, end_typed=args.end,
                        n_paths=len(paths), n_combos=n_combos, n_runnable=n_runnable,
                        actions=[{"key": akey[k], "input": k[0], "output": k[1], "verb": a["verb"],
                                  "papers": a["papers"], "equipment": a["equipment"]}
                                 for k, a in used_actions.items()],
                        paths=[[akey[(p[i], p[i + 1])] for i in range(len(p) - 1)] for p in paths],
                        runnable=[combos(p, True) > 0 for p in paths]).consume()

    def step_info(a, b):
        act = actions[(a, b)]
        return {"input": a, "output": b, "verb": act["verb"], "verb_from": act["verb_from"],
                "equipment": act["equipment"], "papers": act["papers"], "papers_also_use": act.get("lit_only", [])}

    if args.json:
        print(json.dumps({
            "start": start, "end": end, "start_typed": args.start, "end_typed": args.end, "notes": notes,
            "paths_found": len(paths), "capped": len(paths) >= args.max_paths,
            "equipment_combinations": n_combos, "runnable_combinations": n_runnable,
            "paths": [{"runnable_combinations": combos(p, True), "combinations": combos(p),
                       "steps": [step_info(p[i], p[i + 1]) for i in range(len(p) - 1)]} for p in paths],
            "end_is_made_from": last_step,
        }, indent=2))
        return 0 if paths else 1

    # ---------------- printed report ----------------
    print(f"\nTracing back from  '{end}'  to  '{start}'")
    for n in notes:
        print(f"  ({n})")
    if not paths:
        print(f"\nNo path in your lab leads from '{start}' to '{end}'.")
        if last_step:
            print(f"  '{end}' is made in your lab from: {', '.join(last_step)}")
            print(f"  but none of those can be traced back to '{start}' with your equipment.")
        else:
            print(f"  None of your equipment produces '{end}'.")
        print("  Try route_planner.py for literature routes and the fix list.\n")
        return 1

    capped = " (stopped counting at --max-paths)" if len(paths) >= args.max_paths else ""
    print(f"\n{len(paths)} different paths{capped}, {n_combos} equipment combinations "
          f"({n_runnable} runnable today)\n")

    # backward view: one line per action, end first
    print("BACKWARD, from the end (each action, and every piece of equipment that can do it):\n")
    shown = set()
    frontier = [end]
    level = 0
    while frontier:
        nxt = []
        for out in frontier:
            for inp in sorted(preds.get(out, ())):
                k = (inp, out)
                if k not in used_actions or k in shown:
                    continue
                shown.add(k)
                a = used_actions[k]
                eqs = ", ".join(e["name"] + ("" if e["usable"] else f" ({e['why']})") for e in a["equipment"])
                src = {"papers": f"{len(a['papers'])} papers", "AI": "named by AI",
                       "plain description": "no paper"}[a["verb_from"]]
                print(f"  {'   ' * level}{out}  <=  {a['verb']}  <=  {inp}")
                print(f"  {'   ' * level}     by: {eqs}   [{src}]")
                if a.get("lit_only"):
                    print(f"  {'   ' * level}     papers also use: {', '.join(a['lit_only'])}")
                if inp != start:
                    nxt.append(inp)
        frontier = list(dict.fromkeys(nxt))
        level += 1
    print()

    print("PATHS, in working order (start -> end), runnable and shortest first:\n")
    for n, p in enumerate(paths, 1):
        run, tot = combos(p, True), combos(p)
        if n > args.show:
            verbs = " > ".join(actions[(p[i], p[i + 1])]["verb"] for i in range(len(p) - 1))
            print(f"  Path {n}: {len(p) - 1} steps, {run}/{tot} runnable: {verbs}")
            continue
        print(f"  Path {n}: {len(p) - 1} steps, {tot} equipment combinations ({run} runnable today)")
        for i in range(len(p) - 1):
            a = actions[(p[i], p[i + 1])]
            eqs = " | ".join(e["name"] + ("" if e["usable"] else " (x)") for e in a["equipment"])
            print(f"     {i + 1}. {a['verb']:<32} {p[i]} -> {p[i + 1]}")
            print(f"        {'':32} by: {eqs}")
        print()
    if len(paths) > args.show:
        print(f"\n  (paths after {args.show} shown in short; --show {len(paths)} prints all in full)\n")
    print("(x) = in the lab but not usable today.")
    if not args.no_save:
        print("\nSaved in Neo4j. View the trace in the Neo4j Browser:")
        print("  MATCH (bt:BackTrace)-[:HAS_ACTION]->(a:Action) WITH bt ORDER BY bt.createdAt DESC LIMIT 1\n"
              "  MATCH (bt)-[:HAS_ACTION]->(a)-[r]-(x) RETURN a, r, x")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
