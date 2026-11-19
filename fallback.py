"""Making sure the planner always produces a workflow.

1. lab_routes:      search the LAB itself (equipment accepts X, produces Y) for routes
                    from start to end. Routes found with usable equipment pass the lab
                    rule by construction; routes through unusable equipment come back
                    too, so their fix list can be shown.
2. support_steps:   attach literature evidence (papers) to each step of a route.
3. repair:          when a literature/AI route breaks at a step, bridge the gap with a
                    route through the lab and check again.
4. capability_gap:  when the lab cannot reach the end at all, say how far it gets and
                    which steps the papers use to cover the rest.
"""

from __future__ import annotations

from collections import Counter

from models import Candidate, Step, route_key

USABLE = "e.available = true AND e.accessible = true"

LAB_PATHS = """
MATCH (s:Noun) WHERE toLower(s.name) = toLower($a)
MATCH (t:Noun) WHERE toLower(t.name) = toLower($b) AND s <> t
MATCH p = SHORTEST {k} (s)((x:Noun)<-[:ACCEPTS]-(e:Equipment)-[:PRODUCES]->(y:Noun)
                           WHERE x <> y {usable}){{1,{hops}}}(t)
RETURN [n IN nodes(p) WHERE n:Equipment | n.name] AS equipment,
       [n IN nodes(p) WHERE n:Noun | n.name] AS nouns
"""

# literature support for one lab step, strongest first:
#   3 = papers use this equipment to make this output
#   2 = papers turn this input into this output (any equipment)
#   1 = papers use this equipment (for something else)
STEP_SUPPORT = """
CALL () {
  MATCH (:Equipment {name: $eq})-[:SAME_AS]->(:LitEquipment)<-[:USES]-(t:Transition)
        -[:OUTPUTS]->(:LitNoun)<-[:SAME_AS]-(:Noun {name: $out})
  RETURN t, 3 AS level
  UNION
  MATCH (:Noun {name: $inp})-[:SAME_AS]->(:LitNoun)-[:INPUT_OF]->(t:Transition)
        -[:OUTPUTS]->(:LitNoun)<-[:SAME_AS]-(:Noun {name: $out})
  RETURN t, 2 AS level
  UNION
  MATCH (:Equipment {name: $eq})-[:SAME_AS]->(:LitEquipment)<-[:USES]-(t:Transition)
  RETURN t, 1 AS level
}
WITH t, max(level) AS level
MATCH (p:Paper)-[:REPORTS]->(t)
RETURN level, t.process AS process, p.id AS paper
ORDER BY level DESC, p.citedBy DESC
LIMIT 60
"""

REACH_FROM = """
MATCH (s:Noun) WHERE toLower(s.name) = toLower($a)
MATCH (s)((x:Noun)<-[:ACCEPTS]-(e:Equipment)-[:PRODUCES]->(y:Noun) WHERE x <> y AND {usable}){{1,{hops}}}(m:Noun)
RETURN DISTINCT m.name AS name
"""

LEADS_TO = """
MATCH (t:Noun) WHERE toLower(t.name) = toLower($b)
MATCH (m:Noun)((x:Noun)<-[:ACCEPTS]-(e:Equipment)-[:PRODUCES]->(y:Noun) WHERE x <> y){{1,{hops}}}(t)
RETURN DISTINCT m.name AS name
"""

# literature steps that leave what the lab can reach, toward what leads to the end
GAP_STEPS = """
MATCH (:Noun {name: $from})-[:SAME_AS]->(a:LitNoun)-[:INPUT_OF]->(t:Transition)-[:OUTPUTS]->(b:LitNoun)
MATCH (t)-[:USES]->(le:LitEquipment)
OPTIONAL MATCH (n:Noun)-[:SAME_AS]->(b)
WITH a, b, t, le, collect(n.name) AS lab_names
WHERE any(x IN lab_names WHERE x IN $targets) OR size($targets) = 0
MATCH (p:Paper)-[:REPORTS]->(t)
RETURN a.name AS from, t.process AS process, le.name AS equipment, b.name AS to, lab_names,
       count(DISTINCT p) AS papers
ORDER BY papers DESC LIMIT 5
"""


def lab_paths(session, a: str, b: str, k: int, hops: int, usable_only: bool) -> list[tuple[list[str], list[str]]]:
    q = LAB_PATHS.format(k=int(k), hops=int(hops), usable=f"AND {USABLE}" if usable_only else "")
    return [(r["equipment"], r["nouns"]) for r in session.run(q, a=a, b=b)]


def support_steps(session, steps: list[Step], start: str) -> None:
    """Fill in citations (and a better process name) from the literature, in place."""
    incoming = start
    for s in steps:
        rows = [r.data() for r in session.run(STEP_SUPPORT, eq=s.equipment, inp=incoming, out=s.output)]
        strong = [r for r in rows if r["level"] >= 2]
        if strong:
            best = strong[0]["level"]
            top = [r for r in strong if r["level"] == best]
            s.citations = list(dict.fromkeys(r["paper"] for r in top))[:5]
            s.process = Counter(r["process"] for r in top if r["process"]).most_common(1)[0][0]
        incoming = s.output


def make_candidate(session, start: str, equipment: list[str], nouns: list[str], why: str) -> Candidate:
    steps = [Step(process=f"{eq} step", equipment=eq, output=out) for eq, out in zip(equipment, nouns[1:])]
    support_steps(session, steps, start)
    return Candidate(steps=steps, rationale=why, basis="")


def lab_routes(session, start: str, end: str, k: int = 5, hops: int = 12) -> list[tuple[str, Candidate]]:
    """Routes through the lab itself: first with usable equipment, then any (to show fixes)."""
    out, seen = [], set()
    for usable, src, why in ((True, "lab", "A route your lab can run: each machine takes what the previous one made."),
                             (False, "lab (fix)", "A route through your lab that needs equipment fixed first.")):
        for equipment, nouns in lab_paths(session, start, end, k, hops, usable):
            c = make_candidate(session, start, equipment, nouns, why)
            if route_key(c) not in seen:
                seen.add(route_key(c))
                out.append((src, c))
    return out


def repair(session, start: str, end: str, cand: Candidate, lab_nouns: set[str], step_ok,
           hops: int = 8) -> Candidate | None:
    """Replace broken stretches of a route with routes through the lab.

    step_ok(incoming, step) -> bool says whether a step works in the lab as is.
    Returns a repaired copy, or None if it can't be repaired or nothing changed.
    """
    steps = [s.model_copy(deep=True) for s in cand.steps]
    if steps and steps[-1].output.lower() != end.lower():
        steps.append(Step(process="reach the end", equipment="?", output=end))
    lab_lower = {n.lower(): n for n in lab_nouns}
    changed, i, incoming = False, 0, start
    while i < len(steps):
        if step_ok(incoming, steps[i]):
            incoming = steps[i].output
            i += 1
            continue
        # the first later output the lab knows becomes the target to bridge to
        j = next((j for j in range(i, len(steps)) if steps[j].output.lower() in lab_lower), None)
        if j is None:
            return None
        target = lab_lower[steps[j].output.lower()]
        if target.lower() == incoming.lower():  # already there: drop the broken steps
            bridge = []
        else:
            paths = lab_paths(session, incoming, target, 1, hops, usable_only=True)
            if not paths:
                return None
            equipment, nouns = paths[0]
            bridge = [Step(process=f"{eq} step (lab bridge)", equipment=eq, output=o)
                      for eq, o in zip(equipment, nouns[1:])]
            support_steps(session, bridge, incoming)
        steps[i:j + 1] = bridge
        changed = True
        i += len(bridge)
        incoming = target
    if not changed or not steps:
        return None
    return Candidate(steps=steps, rationale=cand.rationale + " Repaired with lab steps where it broke.",
                     basis=cand.basis)


def capability_gap(session, start: str, end: str, hops: int = 12) -> dict:
    """How far the lab gets from START, what leads to END, and how papers bridge the gap."""
    reach = [r["name"] for r in session.run(REACH_FROM.format(hops=hops, usable=USABLE), a=start)]
    feeds = [r["name"] for r in session.run(LEADS_TO.format(hops=hops), b=end)]
    frontier = [start] + reach
    bridges = []
    for f in frontier:
        for r in session.run(GAP_STEPS, **{"from": f, "targets": feeds + [end]}):
            bridges.append({"lab_from": f, **r.data()})
    bridges.sort(key=lambda b: -b["papers"])
    return {"reach": reach, "feeds_end": feeds, "bridges": bridges[:5]}
