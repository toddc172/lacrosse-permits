import { PAGE } from './upload-page.mjs';
const MAX = 8 * 1024 * 1024;
const encoder = new TextEncoder();
const hex = bytes => [...new Uint8Array(bytes)].map(v => v.toString(16).padStart(2, '0')).join('');
const digest = async bytes => hex(await crypto.subtle.digest('SHA-256', bytes));
async function signature(text, secret) {
  const key = await crypto.subtle.importKey('raw', encoder.encode(secret), {name:'HMAC',hash:'SHA-256'}, false, ['sign']);
  return hex(await crypto.subtle.sign('HMAC', key, encoder.encode(text)));
}
function equal(a,b) { if (a.length !== b.length) return false; let x=0; for(let i=0;i<a.length;i++)x|=a.charCodeAt(i)^b.charCodeAt(i);return x===0; }
async function limited(request, max) {
  if (!request.body) return new Uint8Array();
  const reader=request.body.getReader();let length=0;const chunks=[];
  while(true){const {done,value}=await reader.read();if(done)break;length+=value.length;if(length>max){await reader.cancel();throw new Error('size');}chunks.push(value);}
  const bytes=new Uint8Array(length);let offset=0;for(const c of chunks){bytes.set(c,offset);offset+=c.length;}return bytes;
}
export default {async fetch(request,env) {
  const headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"};
  const send=(value,status=200,extra={})=>new Response(JSON.stringify(value),{status,headers:{...headers,'Content-Type':'application/json',...extra}});
  if(!env.FILES || !env.UPLOAD_PASSWORD || env.UPLOAD_PASSWORD.length<16 || !env.SESSION_SECRET || env.SESSION_SECRET.length<32)return send({error:'Setup is incomplete.'},503);
  const url=new URL(request.url);
  if(url.protocol!=='https:' && url.hostname!=='localhost')return send({error:'HTTPS required.'},400);
  if(request.method!=='GET' && request.headers.get('Origin')!==url.origin)return send({error:'Request origin rejected.'},403);
  try {
    if(url.pathname==='/' && request.method==='GET')return new Response(PAGE,{headers:{...headers,'Content-Type':'text/html; charset=utf-8'}});
    if(url.pathname==='/login' && request.method==='POST'){
      const body=JSON.parse(new TextDecoder().decode(await limited(request,2048)));
      if(typeof body.password!=='string' || !equal(await digest(encoder.encode(body.password)),await digest(encoder.encode(env.UPLOAD_PASSWORD))))return send({error:'Sign-in failed.'},401);
      const expires=String(Math.floor(Date.now()/1000)+8*3600);const token=expires+'.'+await signature(expires,env.SESSION_SECRET);
      return send({ok:true},200,{'Set-Cookie':`transfers_session=${token}; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=28800`});
    }
    const token=(request.headers.get('Cookie')||'').split(';').map(s=>s.trim()).find(s=>s.startsWith('transfers_session='))?.slice(18)||'';
    const match=/^(\d+)\.([a-f0-9]{64})$/.exec(token);const now=Math.floor(Date.now()/1000);
    if(!match || Number(match[1])<=now || Number(match[1])>now+28800 || !equal(match[2],await signature(match[1],env.SESSION_SECRET)))return send({error:'Sign in to continue.'},401);
    if(url.pathname==='/logout' && request.method==='POST')return send({ok:true},200,{'Set-Cookie':'transfers_session=; HttpOnly; Secure; SameSite=Strict; Path=/; Max-Age=0'});
    if(url.pathname==='/files' && request.method==='GET'){
      const status=await env.FILES.get('control:status','json')||{};let cursor;const files=[];
      do{const page=await env.FILES.list({prefix:'uploads/',cursor});for(const key of page.keys){const m=key.metadata;if(m)files.push({id:m.id,name:m.name,uploaded_at:m.uploaded_at,...(status.files?.[m.id]||{state:'stored',message:'Queued for the next publishing run.'})});}cursor=page.list_complete?undefined:page.cursor;}while(cursor);
      return send({files:files.sort((a,b)=>b.uploaded_at.localeCompare(a.uploaded_at)),last_run:status.last_run||null});
    }
    if(url.pathname==='/upload' && request.method==='POST'){
      const name=request.headers.get('X-Filename')||'';
      if(!/^RETRHistoricalReport\d{4}(0[1-9]|1[0-2])\.csv$/.test(name))return send({error:'Use the original DOR filename.'},400);
      const raw=await limited(request,MAX);
      if(raw.length<2 || raw[0]!==31 || raw[1]!==139)return send({error:'Compressed upload required. Use this upload page.'},400);
      const sha256=await digest(raw);const id=await digest(encoder.encode(name+'\n'+sha256));
      const key='uploads/'+id;
      if(await env.FILES.get('removed/'+id))return send({error:'This rejected upload was removed. Ask the editor to review it.'},409);
      const exists=await env.FILES.get(key,'arrayBuffer');if(exists)return send({ok:true,duplicate:true});
      const metadata={schema_version:1,id,name,bytes:raw.length,sha256,uploaded_at:new Date().toISOString(),media_type:'text/csv'};
      await env.FILES.put(key,raw,{metadata});return send({ok:true});
    }
    if(url.pathname==='/delete' && request.method==='POST'){
      const body=JSON.parse(new TextDecoder().decode(await limited(request,1024)));
      if(!/^[a-f0-9]{64}$/.test(body.id||''))return send({error:'Invalid file.'},400);
      const status=await env.FILES.get('control:status','json');const state=await env.FILES.get('state:current','json');
      if(state?.files?.[body.id] || status?.files?.[body.id]?.state!=='rejected')return send({error:'Only never-published, rejected files can be removed.'},409);
      await env.FILES.put('removed/'+body.id,JSON.stringify({schema_version:1,id:body.id}));await env.FILES.delete('uploads/'+body.id);return send({ok:true});
    }
    return send({error:'Not found.'},404);
  }catch{return send({error:'The request could not be completed. Check file size and retry.'},400);}
}};
