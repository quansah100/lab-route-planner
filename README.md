# Lab Route Planner

Give a **start** and an **end**. Claude, as the expert, proposes process sequences from its own
knowledge. Each one is compared with what your lab has, and Neo4j presents the ones you can run.

```
start, end ──► Claude (expert) proposes sequences from its own knowledge:
               each step = process + equipment it needs + consumables it uses
                      │
                      ▼
               compare with the lab inventory in Neo4j:
                 • the lab has every piece of equipment, and it's available
                 • the lab has every consumable, with enough stock for the whole route
                      │
                      ▼
               valid sequences: printed, and saved in Neo4j to view as a graph
```

## Setup

```bash
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export NEO4J_URI=bolt://localhost:7688 NEO4J_USER=neo4j NEO4J_PASSWORD=labpassword
```

Load your lab inventory. `seed.cypher` is an example; edit it to match your lab. It only adds or
updates items and never deletes anything, so you can re-run it after every edit.

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
| `--show-rejected` | also show rejected sequences and what the lab is missing |
| `--details` | show each step's equipment and consumables |
| `--candidates file.json` | check sequences from a file instead of asking Claude (no API key needed) |
| `--json` | machine-readable output |
| `--no-save` | don't save the valid routes in Neo4j |

Test without an API key:

```bash
python route_planner.py "print powder" "tensile test specimen" --candidates example_candidates.json --show-rejected
```

## Lab inventory in Neo4j

Only two kinds of node:

```
(:Equipment  {name, available})    available = can be used right now (true/false)
(:Consumable {name, stock, unit})  stock     = how much is on hand
```

Update it as the lab changes, in `seed.cypher` or directly in the Neo4j Browser:

```cypher
MATCH (e:Equipment {name:'HIP unit'}) SET e.available = true;
MATCH (c:Consumable {name:'EDM brass wire'}) SET c.stock = 2;
MERGE (e:Equipment {name:'Electropolishing station'}) SET e.available = true;
```

Claude is given the inventory names only so it can use your lab's names for the same item
(e.g. "LPBF printer" rather than "laser powder bed fusion machine"). It still proposes routes on
technical merit, including ones that need things the lab lacks; those are rejected and tell you
what's missing.

## Viewing the valid routes in Neo4j

```cypher
MATCH (q:RouteQuery)-[f:FOUND]->(r:Route)-[s:STEP]->(st:Step)-[u]->(x) RETURN q, f, r, s, st, u, x
```

Saved as:

```
(:RouteQuery {start, end, createdAt})-[:FOUND {rank}]->(:Route {summary, rationale, basis})
  -[:STEP {order}]->(:Step {order, process})
     -[:USES]->(:Equipment)
     -[:CONSUMES {qty}]->(:Consumable)
```

## Notes

- Use Chrome or Firefox for the Neo4j Browser; Safari blocks its unencrypted local connection.
- The Claude call uses server-side refusal fallbacks (`fallbacks="default"`): if the request is
  declined, the API retries on a fallback model. Remove the `betas`/`fallbacks` lines in
  `propose_sequences` to turn this off.
