import {readFileSync} from 'node:fs';
import {createHash,webcrypto} from 'node:crypto';
import vm from 'node:vm';
import assert from 'node:assert/strict';

class Element {
  constructor(){this.children=[];this.attributes={};this.value='';this._text='';}
  set textContent(v){this._text=String(v??'');this.children=[];}
  get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this._text='';this.children=[...nodes];}
  get firstChild(){return this.children[0];}
  setAttribute(k,v){this.attributes[k]=v;}
  addEventListener(){}
}
const html=readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
const elements={};for(const match of html.matchAll(/id="([^"]+)"/g))elements[match[1]]=new Element();
elements.sort.value='new';
const row={id:'la_crosse:123',county:'La Crosse',municipality:'La Crosse, City of',document_number:'123',
 recorded_date:'2026-09-02',conveyance_date:'2026-09-01',property_address:'123 MAIN ST',property_address_reported:'123 MAIN ST',
 parcel_number:'001-00001-000',grantor_name:'SOURCE NAME',grantee_name:'OTHER NAME',sale_price:'100000.00',estimated_value:'0.00',
 conveyance_type:'Sale',document_type:'Warranty deed',property_use:'Single family',review_flags:'',source_month:'2026-09'};
const records=[row,{...row,id:'la_crosse:124',document_number:'124',property_address:'125 MAIN ST'}];
let manifest,files={},requests=0,active=0,maxActive=0;
function configure(groups){
 files={};const entries=groups.map((rows,i)=>{const bytes=Buffer.from(JSON.stringify({schema_version:1,records:rows}));
 const sha256=createHash('sha256').update(bytes).digest('hex');const file='data/2026-09-'+String(i+1).padStart(5,'0')+'-'+sha256+'.json';files[file]=bytes;
 return {file,sha256,bytes:bytes.length,record_count:rows.length};});
 manifest={schema_version:2,record_count:groups.flat().length,data_files:entries,generated_at:'2026-11-12T22:00:00Z',methodology:'Source method'};
}
configure(records.map(r=>[r]));
const context={document:{getElementById:id=>elements[id],createElement:()=>new Element(),hidden:false},
 Intl,URL,Blob,crypto:webcrypto,TextDecoder,setTimeout,setInterval:()=>0,
 fetch:async path=>{requests++;if(path==='transfers.json')return {ok:true,json:async()=>structuredClone(manifest)};
 active++;maxActive=Math.max(active,maxActive);await new Promise(resolve=>setImmediate(resolve));active--;
 const bytes=files[path];return {ok:!!bytes,arrayBuffer:async()=>Uint8Array.from(bytes).buffer};}};
vm.createContext(context);vm.runInContext(script,context);
for(let i=0;i<50&&elements.count.textContent!=='2 matching transfers';i++)await new Promise(r=>setTimeout(r,5));
assert.equal(elements.count.textContent,'2 matching transfers');assert.equal(elements.results.children.length,2);
assert.ok(maxActive<=3);const firstRequests=requests;
await vm.runInContext('load()',context);assert.equal(requests,firstRequests+1);
for(const tr of elements.results.children)assert.deepEqual(tr.children.map(c=>c.attributes['data-label']),['Recorded','Property','Parties','Reported sale price','Transfer']);
// Repeated row IDs in a new batch must preserve the previous usable view.
configure([[row,{...row}]]);await vm.runInContext('load()',context);
assert.equal(elements.count.textContent,'2 matching transfers');assert.match(elements.status.textContent,/previously loaded records remain available/);
// A mismatching content hash must also leave the previous records available.
configure([[{...row,id:'la_crosse:999',document_number:'999'}]]);
files[manifest.data_files[0].file][0]=32;await vm.runInContext('load()',context);
assert.equal(elements.count.textContent,'2 matching transfers');assert.match(elements.status.textContent,/previously loaded records remain available/);
console.log('Public reader chunk loading, caching, labels and failed-update retention passed.');
