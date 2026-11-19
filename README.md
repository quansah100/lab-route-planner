# Lab Route Planner

Give a **start** and **end** material. You get back the process sequences your lab can actually run.

```
start, end ──► Claude (expert) proposes N sequences
                      │
                      ▼
               Neo4j checks each one against the lab:
                 • every step is a process the lab has
                 • step outputs chain into the next step's inputs
                 • start → … → end
                 • all equipment is available
                 • total consumables needed ≤ stock
                      │
                      ▼
               valid sequences (printed + saved in Neo4j)
```

## Setup

```bash
pip install -r requirements.txt
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export NEO4J_URI=bolt://localhost:7687 NEO4J_USER=neo4j NEO4J_PASSWORD=labpassword
```

Load the example lab, or your own data using the same model (see the top of `seed.cypher`):

```bash
cypher-shell -u neo4j -p labpassword -f seed.cypher
```

### Example 2: print powder → tensile test specimen (metal 3D printing)

`seed_am_lab.cypher` adds a metal additive-manufacturing lab. It only adds data and does not delete anything:

```bash
docker exec -i neo4j-lab cypher-shell -u neo4j -p labpassword < seed_am_lab.cypher
```

```bash
python route_planner.py "print powder" "tensile test specimen" --candidates example_am_candidates.json --show-rejected
```

Use `"tensile test results"` as the end material to include the tensile test itself as the last step.

## Run

```bash
python route_planner.py "Alumina powder" "Coated disc"
```

Options:

| flag | what it does |
|---|---|
| `-n 8` | ask for up to 8 sequences (default 5) |
| `--show-rejected` | also show rejected sequences and why |
| `--candidates file.json` | check your own sequences instead of asking the LLM |
| `--json` | machine-readable output |
| `--no-save` | don't write the valid routes to Neo4j |

## Graph model

```
(:Process {name, description})
  -[:INPUT]->(:Material {name})
  -[:OUTPUT]->(:Material)
  -[:NEEDS_EQUIPMENT]->(:Equipment {name, available: bool})
  -[:NEEDS_CONSUMABLE {qty}]->(:Consumable {name, stock, unit})
```

To match what's in the lab right now, update `available` and `stock`, e.g.:

```cypher
MATCH (e:Equipment {name:'Cold isostatic press'}) SET e.available = true;
MATCH (c:Consumable {name:'Plaster moulds'}) SET c.stock = 10;
```

Valid results are saved as:

```
(:RouteQuery {start, end, createdAt})-[:FOUND {rank}]->(:Route {steps, rationale})-[:STEP {order}]->(:Process)
```

```cypher
MATCH (q:RouteQuery)-[f:FOUND]->(r:Route) RETURN q.start, q.end, f.rank, r.steps ORDER BY q.createdAt DESC, f.rank;
```

## Notes

- The LLM sees the lab's process catalog, so it uses your process names. It may still suggest a process the lab lacks; that route is rejected with "the lab has no such process", which tells you what the lab is missing.
- The Claude call uses server-side refusal fallbacks (`fallbacks="default"`). If the request is declined, the API retries it on a fallback model. Remove the `betas`/`fallbacks` lines in `propose_sequences` to turn this off.
