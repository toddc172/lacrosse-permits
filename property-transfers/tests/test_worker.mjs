import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';
const {default:worker}=await import(process.env.TRANSFERS_TEST_WORKER || '../cloudflare/worker.mjs');
class KV {
  values=new Map();
  async get(key,type){const item=this.values.get(key);if(!item)return null;if(type==='json')return JSON.parse(item.value);return item.value;}
  async put(key,value,options={}){this.values.set(key,{value,metadata:options.metadata});}
  async delete(key){this.values.delete(key);}
  async list(){return {list_complete:true,keys:[...this.values].filter(([k])=>k.startsWith('uploads/')).map(([name,v])=>({name,metadata:v.metadata}))};}
}
const origin='https://test.workers.dev';const env={FILES:new KV(),UPLOAD_PASSWORD:'test-password-long-enough',SESSION_SECRET:'test-secret-key-at-least-32-characters'};
let cookie='';
async function request(path,body,headers={}){const method=body===undefined?'GET':'POST';return worker.fetch(new Request(origin+path,{method,headers:{Origin:origin,...(cookie?{Cookie:cookie}:{}),...headers},body}),env);}
assert.equal((await request('/files')).status,401);
assert.equal((await request('/delete','{}')).status,401);
assert.equal((await request('/login',JSON.stringify({password:'wrong'}))).status,401);
const login=await request('/login',JSON.stringify({password:env.UPLOAD_PASSWORD}));assert.equal(login.status,200);cookie=login.headers.get('set-cookie').split(';')[0];
assert.equal((await request('/files')).status,200);
assert.equal((await request('/upload',new Uint8Array([1,2]),{'X-Filename':'RETRHistoricalReport202609.csv'})).status,400);
assert.equal((await request('/upload',gzipSync('x'),{'X-Filename':'bad.csv'})).status,400);
const packed=gzipSync('fake CSV; parser will validate on runner');
assert.equal((await request('/upload',packed,{'X-Filename':'RETRHistoricalReport202609.csv'})).status,200);
assert.equal((await (await request('/upload',packed,{'X-Filename':'RETRHistoricalReport202609.csv'})).json()).duplicate,true);
const files=await (await request('/files')).json();assert.equal(files.files.length,1);
const id=files.files[0].id;
assert.equal((await request('/delete',JSON.stringify({id}))).status,403);
await env.FILES.put('control:status',JSON.stringify({files:{[id]:{state:'rejected'}}}));
await env.FILES.put('state:current',JSON.stringify({files:{[id]:{}}}));
assert.equal((await request('/delete',JSON.stringify({id}))).status,403);
await env.FILES.put('state:current',JSON.stringify({files:{}}));
// A stale rejected status and an empty legacy state must never permit deletion.
const before=structuredClone([...env.FILES.values]);
assert.equal((await request('/delete',JSON.stringify({id}))).status,403);
assert.equal((await request('/delete','not JSON')).status,403);
assert.deepEqual([...env.FILES.values],before);
assert.equal(await env.FILES.get('removed/'+id),null);
assert.equal((await (await request('/upload',packed,{'X-Filename':'RETRHistoricalReport202609.csv'})).json()).duplicate,true);
assert.equal((await request('/delete',JSON.stringify({id}),{'Origin':'https://other.example'})).status,403);
const page=await (await request('/')).text();
assert.ok(!page.includes('Remove rejected file'));
assert.ok(!page.includes("api('/delete'"));
assert.ok(page.includes('Uploaded source files are retained.'));
assert.ok(page.includes('<h1>Property transfer uploads</h1>'));
assert.equal((await request('/logout','',{'Origin':'https://other.example'})).status,403);
assert.equal((await worker.fetch(new Request(origin+'/'),{})).status,503);
console.log('Worker authentication, upload identity, origin checks and deletion-disabled retention passed.');

// Exercise the served page script with a small DOM model and changing statuses.
const {default:vm}=await import('node:vm');
class Element {
  hidden=false;children=[];value='';disabled=false;textContent='';
  replaceChildren(...nodes){this.children=[...nodes];this.textContent='';}
  append(node){this.children.push(node);}
  set innerHTML(_){throw Error('Status content must be rendered as plain text.');}
}
const nodes=new Map();
const element=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
let shownFiles=[],historyUnavailable=false;
const context=vm.createContext({
  document:{getElementById:element,createElement:()=>new Element()},
  fetch:async path=>{
    assert.equal(path,'/files');
    if(historyUnavailable)throw Error('Status temporarily unavailable.');
    return {ok:true,json:async()=>({files:shownFiles})};
  }
});
const script=page.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script).runInContext(context);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(element('import-failure').hidden,true);
assert.ok(page.includes('If an upload is rejected, check the GitHub Actions import log for details.'));
assert.ok(page.includes('role="alert"'));
assert.ok(page.includes('https://github.com/toddc172/lacrosse-permits/actions/workflows/property-transfers-d1.yml'));
const hostileName='<img src=x onerror=alert(1)>.csv';
shownFiles=[
  {id:'a',name:'RETRHistoricalReport202101.csv',state:'rejected',message:'This source needs editorial review.'},
  {id:'b',name:hostileName,state:'rejected',message:'Review the source.'},
  {id:'c',name:'RETRHistoricalReport202102.csv',state:'stored',message:'Import paused at the daily budget.'},
  {id:'d',name:'RETRHistoricalReport202103.csv',state:'published',message:'Published.'}
];
await element('refresh').onclick();
assert.equal(element('import-failure').hidden,false);
assert.equal(element('failed-files').children.length,2);
assert.ok(element('failed-files').children[0].textContent.startsWith('RETRHistoricalReport202101.csv — Failed import.'));
assert.ok(element('failed-files').children[1].textContent.startsWith(hostileName));
assert.ok(element('history').children[0].textContent.includes('failed import — GitHub review required'));
historyUnavailable=true;
await element('refresh').onclick();
assert.equal(element('import-failure').hidden,false);
assert.equal(element('failed-files').children.length,2);
historyUnavailable=false;
shownFiles=shownFiles.slice(2);
await element('refresh').onclick();
assert.equal(element('import-failure').hidden,true);
assert.equal(element('failed-files').children.length,0);
shownFiles=[];
await element('refresh').onclick();
assert.equal(element('import-failure').hidden,true);
assert.equal(element('history').textContent,'No uploads yet.');
console.log('Failure notice: named rejected files, safe text, status refresh, recovery and budget-pause distinction passed.');
