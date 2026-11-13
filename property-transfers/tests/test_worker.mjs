import assert from 'node:assert/strict';
import {gzipSync} from 'node:zlib';
import worker from '../cloudflare/worker.mjs';
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
assert.equal((await request('/delete',JSON.stringify({id}))).status,409);
await env.FILES.put('control:status',JSON.stringify({files:{[id]:{state:'rejected'}}}));
await env.FILES.put('state:current',JSON.stringify({files:{[id]:{}}}));
assert.equal((await request('/delete',JSON.stringify({id}))).status,409);
await env.FILES.put('state:current',JSON.stringify({files:{}}));
assert.equal((await request('/delete',JSON.stringify({id}))).status,200);
assert.equal((await request('/upload',packed,{'X-Filename':'RETRHistoricalReport202609.csv'})).status,409);
assert.equal((await request('/logout','',{'Origin':'https://other.example'})).status,403);
assert.equal((await worker.fetch(new Request(origin+'/'),{})).status,503);
console.log('Worker authentication, upload identity, origin checks and published-file protection passed.');
