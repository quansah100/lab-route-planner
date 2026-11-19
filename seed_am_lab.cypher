// Example lab: metal additive manufacturing, print powder -> tensile test specimen.
// This file only ADDS data (MERGE); it does not delete anything else in the database.
// Replace the names, availability and stock below with your real lab.
//
// (:Material {name})
// (:Process {name, description})
//   -[:INPUT]->(:Material)  -[:OUTPUT]->(:Material)
//   -[:NEEDS_EQUIPMENT]->(:Equipment {name, available})
//   -[:NEEDS_CONSUMABLE {qty}]->(:Consumable {name, stock, unit})

// ---- materials ----
UNWIND ['Print powder', 'Characterized powder', 'Sieved powder', 'As-built part',
        'Stress-relieved part', 'HIPed part', 'Tensile test specimen', 'Tensile test results'] AS m
MERGE (:Material {name: m});

// ---- equipment (available = can be used right now) ----
UNWIND [
  {name: 'Laser particle size analyzer', available: true},
  {name: 'SEM',                          available: true},
  {name: 'Hall flowmeter',               available: true},
  {name: 'Sieve shaker',                 available: true},
  {name: 'LPBF printer',                 available: true},
  {name: 'DED machine',                  available: false},
  {name: 'Vacuum furnace',               available: true},
  {name: 'HIP unit',                     available: false},
  {name: 'CNC lathe',                    available: true},
  {name: 'Wire EDM',                     available: true},
  {name: 'Universal testing machine',    available: true},
  {name: 'Extensometer',                 available: true}
] AS e
MERGE (x:Equipment {name: e.name}) SET x.available = e.available;

// ---- consumables (stock on hand) ----
UNWIND [
  {name: 'Argon gas',      stock: 3, unit: 'cylinders'},
  {name: 'Sieve mesh 63um',stock: 2, unit: 'pcs'},
  {name: 'Cutting inserts',stock: 4, unit: 'pcs'},
  {name: 'EDM brass wire', stock: 0, unit: 'spools'}
] AS c
MERGE (x:Consumable {name: c.name}) SET x.stock = c.stock, x.unit = c.unit;

// ---- processes ----
UNWIND [
  {name: 'Powder characterization', description: 'Particle size, morphology and flowability of the feedstock',
   in: ['Print powder'], out: 'Characterized powder',
   eq: ['Laser particle size analyzer', 'SEM', 'Hall flowmeter'], cons: []},
  {name: 'Powder sieving', description: 'Remove oversize particles before printing',
   in: ['Characterized powder'], out: 'Sieved powder',
   eq: ['Sieve shaker'], cons: [{name: 'Sieve mesh 63um', qty: 1}]},
  {name: 'Laser powder bed fusion', description: 'Print bars or blanks',
   in: ['Sieved powder'], out: 'As-built part',
   eq: ['LPBF printer'], cons: [{name: 'Argon gas', qty: 1}]},
  {name: 'Directed energy deposition', description: 'Deposit blanks with a DED head',
   in: ['Characterized powder'], out: 'As-built part',
   eq: ['DED machine'], cons: [{name: 'Argon gas', qty: 1}]},
  {name: 'Stress relief heat treatment', description: 'Relieve residual stress from printing',
   in: ['As-built part'], out: 'Stress-relieved part',
   eq: ['Vacuum furnace'], cons: [{name: 'Argon gas', qty: 1}]},
  {name: 'Hot isostatic pressing', description: 'Close internal porosity',
   in: ['Stress-relieved part'], out: 'HIPed part',
   eq: ['HIP unit'], cons: [{name: 'Argon gas', qty: 1}]},
  {name: 'CNC machining of tensile specimen', description: 'Machine an ASTM E8 dog-bone specimen',
   in: ['Stress-relieved part', 'HIPed part', 'As-built part'], out: 'Tensile test specimen',
   eq: ['CNC lathe'], cons: [{name: 'Cutting inserts', qty: 2}]},
  {name: 'Wire EDM specimen cutting', description: 'Cut a flat tensile specimen by wire EDM',
   in: ['Stress-relieved part', 'HIPed part', 'As-built part'], out: 'Tensile test specimen',
   eq: ['Wire EDM'], cons: [{name: 'EDM brass wire', qty: 1}]},
  {name: 'Tensile testing', description: 'Measure yield strength, UTS and elongation',
   in: ['Tensile test specimen'], out: 'Tensile test results',
   eq: ['Universal testing machine', 'Extensometer'], cons: []}
] AS p
MERGE (proc:Process {name: p.name}) SET proc.description = p.description
WITH proc, p
// clear old links so re-running this file updates the process cleanly
OPTIONAL MATCH (proc)-[old:INPUT|OUTPUT|NEEDS_EQUIPMENT|NEEDS_CONSUMABLE]->()
DELETE old
WITH DISTINCT proc, p
CALL (proc, p) {
  UNWIND p.in AS inName
  MATCH (m:Material {name: inName}) MERGE (proc)-[:INPUT]->(m)
}
CALL (proc, p) {
  MATCH (m:Material {name: p.out}) MERGE (proc)-[:OUTPUT]->(m)
}
CALL (proc, p) {
  UNWIND p.eq AS eqName
  MATCH (e:Equipment {name: eqName}) MERGE (proc)-[:NEEDS_EQUIPMENT]->(e)
}
CALL (proc, p) {
  UNWIND p.cons AS c
  MATCH (x:Consumable {name: c.name}) MERGE (proc)-[r:NEEDS_CONSUMABLE]->(x) SET r.qty = c.qty
};
