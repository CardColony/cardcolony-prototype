const L = require('../static/canvas_layout.js');
const cards=[
 {card_id:'root',parent:'',card_type:'剧本',title:'根',created_at:'2026-01-01'},
 {card_id:'a',parent:'root',card_type:'线索',title:'A',created_at:'2026-01-02'},
 {card_id:'b',parent:'root',card_type:'线索',title:'B',created_at:'2026-01-03'},
];
const positions={ b:{x:900.5,y:120.25,pinned:true} };
const sizes={ root:{w:360,h:130}, a:{w:280,h:120}, b:{w:280,h:120} };
const r1=L.computeLayout('root',cards,[],positions,new Set(['root']),sizes);
const r2=L.computeLayout('root',cards,[],positions,new Set(['root']),sizes);
console.log('r1', JSON.stringify(r1));
console.log('r2', JSON.stringify(r2));
console.log('positions', JSON.stringify(positions));
