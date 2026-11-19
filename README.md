# Lab Route Planner

Give a **start** and an **end**. Claude, as the expert, proposes sequences from its own knowledge.
Neo4j checks each one against your lab and presents the valid ones.

```
start, end ──► Claude (expert) proposes sequences from its own knowledge:
               each step = one piece of equipment + the noun it produces
                      │
                      ▼
               Neo4j checks every step against the lab:
                 • the equipment is available AND accessible
                 • the noun the step produces is accepted by the next step's equipment
                   (first equipment accepts START, last one produces END)
                      │
                      ▼
               valid routes: printed, and saved in Neo4j to view as a graph
```

## Setup

```bash
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export NEO4J_URI=bolt://localhost:7688 NEO4J_USER=neo4j NEO4J_PASSWORD=labpassword
```

Load your lab. `seed.cypher` is an example; edit it to match your lab. It only adds or updates and
never deletes anything, so you can re-run it after every edit.

```bash
docker exec -i neo4j-lab cypher-shell -u neo4j -p labpassword < seed.cypher
```

## Run

```bash
python route_planner.py "print powder" "tensile test specimen"
```

| flag | what it does |
|---|---|
| `-n 8` | ask for up to 8 sequences (default 5) |
| `--show-rejected` | also show rejected sequences and why |
| `--candidates file.json` | check sequences from a file instead of asking Claude (no API key needed) |
| `--json` | machine-readable output |
| `--no-save` | don't save the valid routes in Neo4j |

Test without an API key:

```bash
python route_planner.py "print powder" "tensile test specimen" --candidates example_candidates.json --show-rejected
```

## The lab in Neo4j

```
(:Equipment {name, available, accessible})
   available  = working / free to use right now (true/false)
   accessible = the lab can get to it          (true/false)
(:Equipment)-[:ACCEPTS]->(:Noun {name})    what it takes in
(:Equipment)-[:PRODUCES]->(:Noun {name})   what it gives out
```

A noun is anything that moves between equipment: a powder, a part, a specimen, a result.

Update the lab as it changes, in `seed.cypher` or in the Neo4j Browser:

```cypher
MATCH (e:Equipment {name:'HIP unit'}) SET e.available = true;
MATCH (e:Equipment {name:'Scanning electron microscope'}) SET e.accessible = true;
MATCH (e:Equipment {name:'CNC lathe'}) MERGE (n:Noun {name:'As-built part'}) MERGE (e)-[:ACCEPTS]->(n);
```

Claude is shown the lab's equipment and noun names only so it uses the same names. It still
proposes routes on technical merit, including ones the lab can't run; those are rejected with the
reason.

## Viewing the valid routes in Neo4j

```cypher
MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x
```

Saved as:

```
(:RouteQuery {start, end, createdAt})-[:FOUND {rank}]->(:Route {summary, rationale, basis})
  -[:STEP {order}]->(:Step {order, process})
     -[:TAKES]->(:Noun)  -[:USES]->(:Equipment)  -[:GIVES]->(:Noun)
```

## Notes

- Use Chrome or Firefox for the Neo4j Browser; Safari blocks its unencrypted local connection.
- Names are matched ignoring upper/lower case, but otherwise must be the same.
- The Claude call uses server-side refusal fallbacks (`fallbacks="default"`): if the request is
  declined, the API retries on a fallback model. Remove the `betas`/`fallbacks` lines in
  `propose_sequences` to turn this off.
