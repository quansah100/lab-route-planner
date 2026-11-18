// Example lab: ceramic disc processing.
// Replace with your own lab data — keep the same labels/relationships.
//
// (:Material {name})
// (:Process {name, description})
//   -[:INPUT]->(:Material)          what the step starts from
//   -[:OUTPUT]->(:Material)         what the step produces
//   -[:NEEDS_EQUIPMENT]->(:Equipment {name, available})
//   -[:NEEDS_CONSUMABLE {qty}]->(:Consumable {name, stock, unit})

MATCH (n) DETACH DELETE n;

CREATE
  (powder:Material {name:'Alumina powder'}),
  (gran:Material   {name:'Granulated powder'}),
  (green:Material  {name:'Green pellet'}),
  (sint:Material   {name:'Sintered disc'}),
  (pol:Material    {name:'Polished disc'}),
  (coat:Material   {name:'Coated disc'}),

  (dryer:Equipment   {name:'Spray dryer',            available:true}),
  (press:Equipment   {name:'Hydraulic press',        available:true}),
  (cip:Equipment     {name:'Cold isostatic press',   available:false}),  // under maintenance
  (furnace:Equipment {name:'Box furnace 1700C',      available:true}),
  (sps:Equipment     {name:'Spark plasma sinterer',  available:true}),
  (polisher:Equipment{name:'Lapping/polishing machine', available:true}),
  (sputter:Equipment {name:'Sputter coater',         available:true}),
  (dip:Equipment     {name:'Dip coater',             available:true}),

  (pva:Consumable      {name:'PVA binder',       stock:200, unit:'g'}),
  (moulds:Consumable   {name:'Plaster moulds',   stock:0,   unit:'pcs'}),
  (foil:Consumable     {name:'Graphite foil',    stock:2,   unit:'sheets'}),
  (diamond:Consumable  {name:'Diamond paste',    stock:10,  unit:'g'}),
  (au:Consumable       {name:'Gold target',      stock:1,   unit:'pcs'}),
  (sol:Consumable      {name:'Sol-gel precursor',stock:0,   unit:'mL'}),

  (p1:Process {name:'Spray drying',            description:'Granulate powder with binder'}),
  (p2:Process {name:'Uniaxial pressing',       description:'Press granules into a green pellet'}),
  (p3:Process {name:'Cold isostatic pressing', description:'Isostatically press granules into a green pellet'}),
  (p4:Process {name:'Slip casting',            description:'Cast powder slurry into a green pellet'}),
  (p5:Process {name:'Conventional sintering',  description:'Fire green pellet at 1600C'}),
  (p6:Process {name:'Spark plasma sintering',  description:'Sinter powder directly into a dense disc'}),
  (p7:Process {name:'Lapping and polishing',   description:'Polish disc surface'}),
  (p8:Process {name:'Sputter coating',         description:'Deposit a thin metal film'}),
  (p9:Process {name:'Sol-gel dip coating',     description:'Dip-coat an oxide film'}),

  (p1)-[:INPUT]->(powder), (p1)-[:OUTPUT]->(gran),  (p1)-[:NEEDS_EQUIPMENT]->(dryer),   (p1)-[:NEEDS_CONSUMABLE {qty:50}]->(pva),
  (p2)-[:INPUT]->(gran),   (p2)-[:OUTPUT]->(green), (p2)-[:NEEDS_EQUIPMENT]->(press),
  (p3)-[:INPUT]->(gran),   (p3)-[:OUTPUT]->(green), (p3)-[:NEEDS_EQUIPMENT]->(cip),
  (p4)-[:INPUT]->(powder), (p4)-[:OUTPUT]->(green), (p4)-[:NEEDS_CONSUMABLE {qty:1}]->(moulds),
  (p5)-[:INPUT]->(green),  (p5)-[:OUTPUT]->(sint),  (p5)-[:NEEDS_EQUIPMENT]->(furnace),
  (p6)-[:INPUT]->(powder), (p6)-[:OUTPUT]->(sint),  (p6)-[:NEEDS_EQUIPMENT]->(sps),     (p6)-[:NEEDS_CONSUMABLE {qty:1}]->(foil),
  (p7)-[:INPUT]->(sint),   (p7)-[:OUTPUT]->(pol),   (p7)-[:NEEDS_EQUIPMENT]->(polisher),(p7)-[:NEEDS_CONSUMABLE {qty:3}]->(diamond),
  (p8)-[:INPUT]->(pol),    (p8)-[:OUTPUT]->(coat),  (p8)-[:NEEDS_EQUIPMENT]->(sputter), (p8)-[:NEEDS_CONSUMABLE {qty:1}]->(au),
  (p9)-[:INPUT]->(pol),    (p9)-[:OUTPUT]->(coat),  (p9)-[:NEEDS_EQUIPMENT]->(dip),     (p9)-[:NEEDS_CONSUMABLE {qty:20}]->(sol);
