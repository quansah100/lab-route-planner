// Lab inventory: what the lab HAS. Edit this to match your lab.
// Only two kinds of node are needed:
//   (:Equipment  {name, available})      available = can be used right now
//   (:Consumable {name, stock, unit})    stock     = how much is on hand
//
// This file only adds/updates items (MERGE). It never deletes anything,
// so it is safe to re-run after editing.

UNWIND [
  {name: 'Laser particle size analyzer', available: true},
  {name: 'Scanning electron microscope', available: true},
  {name: 'Hall flowmeter',               available: true},
  {name: 'Sieve shaker',                 available: true},
  {name: 'LPBF printer',                 available: true},
  {name: 'DED machine',                  available: false},
  {name: 'Vacuum furnace',               available: true},
  {name: 'Box furnace 1700C',            available: true},
  {name: 'HIP unit',                     available: false},
  {name: 'CNC lathe',                    available: true},
  {name: 'Wire EDM',                     available: true},
  {name: 'Band saw',                     available: true},
  {name: 'Universal testing machine',    available: true},
  {name: 'Extensometer',                 available: true},
  {name: 'Hydraulic press',              available: true},
  {name: 'Spray dryer',                  available: true},
  {name: 'Lapping/polishing machine',    available: true},
  {name: 'Sputter coater',               available: true}
] AS e
MERGE (x:Equipment {name: e.name}) SET x.available = e.available;

UNWIND [
  {name: 'Argon gas',         stock: 3,   unit: 'cylinders'},
  {name: 'Sieve mesh 63um',   stock: 2,   unit: 'pcs'},
  {name: 'Cutting inserts',   stock: 4,   unit: 'pcs'},
  {name: 'EDM brass wire',    stock: 0,   unit: 'spools'},
  {name: 'SEM stubs',         stock: 50,  unit: 'pcs'},
  {name: 'Carbon tape',       stock: 1,   unit: 'rolls'},
  {name: 'PVA binder',        stock: 200, unit: 'g'},
  {name: 'Diamond paste',     stock: 10,  unit: 'g'},
  {name: 'Gold target',       stock: 1,   unit: 'pcs'}
] AS c
MERGE (x:Consumable {name: c.name}) SET x.stock = c.stock, x.unit = c.unit;
