import assert from 'node:assert/strict';
import vm from 'node:vm';
import worker,{buildQuery,pageResult} from '../search-worker.mjs';
import {READER_HTML} from '../reader.mjs';

const revision='a'.repeat(64),row={id:'la_crosse:123',recorded_date:'2026-09-02',county:'La Crosse',municipality:'La Crosse, City of',document_number:'123',parcel_number:'001-00001-000',property_address:'123 MAIN ST',property_address_reported:'123 MAIN ST',grantor_name:'SOURCE NAME',grantee_name:'OTHER NAME',sale_price:'100000.00',estimated_value:'0.00',conveyance_type:'Sale',property_use:'Single family',review_flags:''};
const meta={revision,record_count:58000,source_count:69,oldest_recorded_date:'2021-01-01',latest_recorded_date:'2026-09-30',generated_at:'2026-11-13T22:00:00Z',options:{county:['La Crosse'],municipality:['La Crosse, City of'],conveyance_type:['Sale'],property_use:['Single family']}};
let lastSql='';const DB={prepare(sql){lastSql=sql;return {bind(){return this;},async first(){return {value:JSON.stringify(meta)};}};},async batch(){return [{success:true,results:[{value:JSON.stringify(meta)}]},{success:true,results:[{record_json:JSON.stringify(row),recorded_date:row.recorded_date,transfer_id:row.id,sale_price_cents:10000000}]},{success:true,results:[{value:JSON.stringify(meta)}]}];}};
async function request(path,method='GET',env={DB}){return worker.fetch(new Request('https://test.owner.workers.dev'+path,{method}),env,{waitUntil(){}});}
assert.equal((await request('/search','POST')).status,405);
assert.equal((await request('/admin')).status,404);
assert.equal((await request('/metadata','GET',{})).status,503);
assert.equal((await request('/metadata')).status,200);
assert.equal((await request('/search?rev='+revision+'&limit=100000')).status,400);
assert.equal((await request('/search?rev='+revision+'&from=2026-09-99')).status,400);
assert.equal((await request('/search?rev='+revision+'&q=a')).status,400);
assert.equal((await request('/search?rev='+'b'.repeat(64))).status,409);
const response=await request('/search?rev='+revision);
assert.equal(response.status,200);assert.equal((await response.json()).records.length,1);
assert.equal(response.headers.get('Access-Control-Allow-Origin'),'https://lacrosse-tribune-property-transfers.pages.dev');
const plan=await buildQuery(new URLSearchParams({rev:revision,q:'Smith',county:'La Crosse'}));
assert.ok(plan.sql.includes("b.status='active'"));assert.ok(!plan.sql.includes('Smith'));assert.ok(plan.bind.some(v=>String(v).includes('Smith')));

class Element{
 constructor(){this.children=[];this.attributes={};this.value='';this._text='';this.disabled=false;}
 set textContent(v){this._text=String(v??'');this.children=[];}get textContent(){return this._text+this.children.map(c=>c.textContent||'').join('');}
 append(...n){this.children.push(...n);}replaceChildren(...n){this.children=[...n];this._text='';}get firstChild(){return this.children[0];}setAttribute(k,v){this.attributes[k]=v;}addEventListener(){}click(){}
}
const elements={};for(const m of READER_HTML.matchAll(/id="([^"]+)"/g))elements[m[1]]=new Element();elements.sort.value='new';
let calls=[],bad=false;const client={document:{getElementById:id=>elements[id],createElement:()=>new Element(),hidden:false},location:{origin:'https://test.owner.workers.dev'},Intl,URL,URLSearchParams,Blob,setTimeout,clearTimeout,setInterval:()=>0,
 fetch:async url=>{const u=new URL(url);calls.push(u);if(u.pathname==='/metadata')return {ok:true,json:async()=>meta};if(u.pathname==='/search')return {ok:!bad,json:async()=>bad?{error:'Test outage.'}:{records:[row],next_cursor:null,has_more:false,revision}};throw Error('Unexpected full-archive fetch.');}};
vm.createContext(client);vm.runInContext(READER_HTML.match(/<script>([\s\S]*?)<\/script>/)[1],client);
await new Promise(r=>setImmediate(r));await new Promise(r=>setImmediate(r));
assert.equal(elements.results.children.length,1);assert.ok(elements.status.textContent.includes('58,000 transfers'));
assert.ok(calls.every(u=>['/metadata','/search'].includes(u.pathname)));assert.equal(calls.find(u=>u.pathname==='/search').searchParams.get('limit'),'25');
assert.equal(elements.results.children[0].children[1].attributes['data-label'],'Property');
bad=true;await vm.runInContext('search(true)',client);assert.equal(elements.results.children.length,1);assert.match(elements.count.textContent,/Previously displayed records remain/);
console.log('Read-only API, input limits, revision checks, CORS, paged reader and failed-search retention passed.');
