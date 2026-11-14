import {READER_HTML} from './reader.mjs';
const encoder=new TextEncoder();
const fail=message=>{throw Object.assign(new Error(message),{status:400});};
const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',encoder.encode(value))),b=>b.toString(16).padStart(2,'0')).join('');
const encode=value=>btoa(JSON.stringify(value)).replaceAll('+','-').replaceAll('/','_').replace(/=+$/,'');
const decode=value=>JSON.parse(atob(value.replaceAll('-','+').replaceAll('_','/')));
function amount(value){if(!/^\d{1,12}(\.\d{1,2})?$/.test(value))fail('Enter a valid price.');const [a,b='']=value.split('.');return Number(a)*100+Number(b.padEnd(2,'0'));}
function date(value){const d=new Date(value+'T00:00:00Z');if(!/^\d{4}-\d{2}-\d{2}$/.test(value)||Number.isNaN(d.valueOf())||d.toISOString().slice(0,10)!==value)fail('Enter a valid date.');return value;}

export async function buildQuery(params){
 const allowed=new Set(['q','county','municipality','type','use','from','to','min','max','sort','cursor','limit','rev']);
 for(const key of params.keys())if(!allowed.has(key)||params.getAll(key).length!==1)fail('Unrecognized search option.');
 const criteria={};for(const key of allowed)if(!['cursor','limit','rev'].includes(key))criteria[key]=params.get(key)||'';
 if(Object.values(criteria).some(v=>v.length>200)||criteria.q.length>120)fail('Search is too long.');
 const sort=criteria.sort||'new';if(!['new','old','high'].includes(sort))fail('Invalid sort.');criteria.sort=sort;
 const limit=Number(params.get('limit')||25);if(![25,100].includes(limit))fail('Invalid page size.');
 const revision=params.get('rev')||'';if(revision!=='empty'&&!/^[a-f0-9]{64}$/.test(revision))fail('Refresh the database and try again.');
 const scope=await hash(JSON.stringify(criteria));const where=["b.status='active'"];const bind=[];
 for(const [key,col] of [['county','county'],['municipality','municipality'],['type','conveyance_type'],['use','property_use']])if(criteria[key]){where.push('t.'+col+'=?');bind.push(criteria[key]);}
 if(criteria.from){where.push('t.recorded_date>=?');bind.push(date(criteria.from));}
 if(criteria.to){where.push('t.recorded_date<=?');bind.push(date(criteria.to));}
 if(criteria.from&&criteria.to&&criteria.from>criteria.to)fail('The start date must precede the end date.');
 if(criteria.min){where.push('t.sale_price_cents>=?');bind.push(amount(criteria.min));}
 if(criteria.max){where.push('t.sale_price_cents<=?');bind.push(amount(criteria.max));}
 if(criteria.min&&criteria.max&&amount(criteria.min)>amount(criteria.max))fail('The minimum price must not exceed the maximum.');
 if(criteria.q.trim()){
  const tokens=criteria.q.normalize('NFKD').replace(/\p{M}/gu,'').match(/[\p{L}\p{N}]+/gu)||[];
  if(!tokens.length||tokens.length>12)fail('Enter up to 12 search words.');
  if(tokens.every(t=>t.length<2))fail('Enter at least two letters or digits.');
  where.push('t.rowid IN (SELECT rowid FROM transfer_search WHERE transfer_search MATCH ?)');
  bind.push(tokens.map(t=>'"'+t+'"*').join(' AND '));
 }
 const cursor=params.get('cursor');
 if(cursor){
  if(cursor.length>1500)fail('Invalid page cursor.');let c;try{c=decode(cursor);}catch{fail('Invalid page cursor.');}
  if(!c||typeof c!=='object'||c.scope!==scope||c.rev!==revision||typeof c.id!=='string'||!/^.{1,180}$/.test(c.id)||typeof c.date!=='string')fail('Refresh this search.');date(c.date);
  const op=sort==='old'?'>':'<';const tail=`(t.recorded_date${op}? OR (t.recorded_date=? AND t.transfer_id${op}?))`;
  if(sort==='high'){
   if(c.price!==null&&!Number.isSafeInteger(c.price))fail('Invalid page cursor.');
   if(c.price===null){where.push('(t.sale_price_cents IS NULL AND '+tail+')');bind.push(c.date,c.date,c.id);}
   else{where.push('(t.sale_price_cents<? OR t.sale_price_cents IS NULL OR (t.sale_price_cents=? AND '+tail+'))');bind.push(c.price,c.price,c.date,c.date,c.id);}
  }else{where.push(tail);bind.push(c.date,c.date,c.id);}
 }
 const order=sort==='high'?'t.sale_price_cents DESC,t.recorded_date DESC,t.transfer_id DESC':sort==='old'?'t.recorded_date ASC,t.transfer_id ASC':'t.recorded_date DESC,t.transfer_id DESC';
 const sql='SELECT t.record_json,t.recorded_date,t.transfer_id,t.sale_price_cents FROM transfers t JOIN batches b ON b.id=t.batch_id WHERE '+where.join(' AND ')+' ORDER BY '+order+' LIMIT ?';
 bind.push(limit+1);return {sql,bind,scope,revision,limit};
}

export function pageResult(rows,plan){
 const more=rows.length>plan.limit,visible=rows.slice(0,plan.limit),last=visible.at(-1);
 return {records:visible.map(r=>JSON.parse(r.record_json)),has_more:more,
  next_cursor:more?encode({scope:plan.scope,rev:plan.revision,id:last.transfer_id,date:last.recorded_date,price:last.sale_price_cents}):null,revision:plan.revision};
}

export default {async fetch(request,env,ctx){
 const url=new URL(request.url);const site='https://lacrosse-tribune-property-transfers.pages.dev';
 const headers={'Content-Type':'application/json','Cache-Control':'no-store','Access-Control-Allow-Origin':site,'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer'};
 const send=(body,status=200)=>new Response(JSON.stringify(body),{status,headers});
 if(request.method!=='GET')return send({error:'Read-only service.'},405);
 if(url.pathname==='/')return new Response(READER_HTML,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-cache','X-Content-Type-Options':'nosniff','Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'self' https://lacrossetribune.com https://*.lacrossetribune.com"}});
 if(!['/metadata','/search'].includes(url.pathname))return send({error:'Not found.'},404);
 if(!env.DB)return send({error:'Database setup is incomplete.'},503);
 try{
  if(url.pathname==='/metadata'){
   if(url.search)return send({error:'Unrecognized option.'},400);
   if(typeof caches!=='undefined'){const cached=await caches.default.match(request);if(cached)return cached;}
   const row=await env.DB.prepare("SELECT value FROM settings WHERE name='public'").first();
   const response=send(JSON.parse(row.value));response.headers.set('Cache-Control','public, max-age=30');
   if(typeof caches!=='undefined')ctx.waitUntil(caches.default.put(request,response.clone()));return response;
  }
  const plan=await buildQuery(url.searchParams);
  if(typeof caches!=='undefined'){const cached=await caches.default.match(request);if(cached)return cached;}
  // Revision checks around the query protect long exports against mixed
  // publication versions, including deployments during a pagination request.
  const result=await env.DB.batch([
   env.DB.prepare("SELECT value FROM settings WHERE name='public'"),
   env.DB.prepare(plan.sql).bind(...plan.bind),
   env.DB.prepare("SELECT value FROM settings WHERE name='public'")]);
  if(result.some(r=>r.success===false))throw new Error('database');
  const before=JSON.parse(result[0].results[0].value).revision,after=JSON.parse(result[2].results[0].value).revision;
  if(before!==plan.revision||after!==before)return send({error:'The database changed. Refresh the search and try again.'},409);
  const response=send(pageResult(result[1].results,plan));response.headers.set('Cache-Control','public, max-age=30');
  if(typeof caches!=='undefined')ctx.waitUntil(caches.default.put(request,response.clone()));return response;
 }catch(error){return send({error:error.status===400?error.message:'Search is temporarily unavailable. Please try again.'},error.status===400?400:503);}
}};
