// Lab inventory: the equipment the lab has, and what each piece of equipment
// accepts and produces. Edit this to match your lab.
//
//   (:Equipment {name, available, accessible})
//      available  = working / free to use right now      (true/false)
//      accessible = the lab can get to it (not off-site, not restricted) (true/false)
//   (:Equipment)-[:ACCEPTS]->(:Noun {name})    what it can take in
//   (:Equipment)-[:PRODUCES]->(:Noun {name})   what it gives out
//
// A route is valid when every step's equipment is available AND accessible, and
// the noun each step produces is accepted by the next step's equipment.
//
// This file only adds/updates (MERGE). It never deletes anything, so it is
// safe to re-run after editing. (To remove an accepts/produces link, delete it
// in the Neo4j Browser.)

UNWIND [
  {name: 'Laser particle size analyzer', available: true,  accessible: true,
   accepts: ['Print powder'], produces: ['Characterized powder']},
  {name: 'Scanning electron microscope', available: true,  accessible: false,   // shared facility, not booked
   accepts: ['Print powder'], produces: ['Characterized powder']},
  {name: 'Sieve shaker',                 available: true,  accessible: true,
   accepts: ['Characterized powder'], produces: ['Sieved powder']},
  {name: 'LPBF printer',                 available: true,  accessible: true,
   accepts: ['Sieved powder', 'Characterized powder'], produces: ['As-built part']},
  {name: 'DED machine',                  available: false, accessible: true,    // under maintenance
   accepts: ['Characterized powder'], produces: ['As-built part']},
  {name: 'Vacuum furnace',               available: true,  accessible: true,
   accepts: ['As-built part'], produces: ['Stress-relieved part']},
  {name: 'HIP unit',                     available: false, accessible: true,
   accepts: ['Stress-relieved part'], produces: ['HIPed part']},
  {name: 'Band saw',                     available: true,  accessible: true,
   accepts: ['As-built part', 'Stress-relieved part', 'HIPed part'], produces: ['Specimen blank']},
  {name: 'CNC lathe',                    available: true,  accessible: true,
   accepts: ['Specimen blank'], produces: ['Tensile test specimen']},
  {name: 'Wire EDM',                     available: true,  accessible: true,
   accepts: ['As-built part', 'Stress-relieved part', 'HIPed part'], produces: ['Tensile test specimen']},
  {name: 'Universal testing machine',    available: true,  accessible: true,
   accepts: ['Tensile test specimen'], produces: ['Tensile test results']}
] AS e
MERGE (x:Equipment {name: e.name})
SET x.available = e.available, x.accessible = e.accessible
WITH x, e
CALL (x, e) {
  UNWIND e.accepts AS n
  MERGE (noun:Noun {name: n})
  MERGE (x)-[:ACCEPTS]->(noun)
}
CALL (x, e) {
  UNWIND e.produces AS n
  MERGE (noun:Noun {name: n})
  MERGE (x)-[:PRODUCES]->(noun)
};
