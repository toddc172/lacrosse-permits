const PAGE = "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width\"><title>Property transfer uploads</title>\n<style>body{font:17px system-ui;max-width:850px;margin:35px auto;padding:20px;color:#183248;background:#f5f7fa}section{background:white;padding:24px;border-radius:12px;margin:20px 0}button,input{font:inherit;padding:10px;margin:6px}button{background:#174d70;color:white;border:0;border-radius:5px;cursor:pointer}button:disabled{opacity:.5}pre{white-space:pre-wrap}li{margin:15px 0}a{color:#174d70}</style>\n<h1>Property transfer uploads</h1><p>La Crosse Tribune · Six-county database</p>\n<section id=\"signin\"><label>Newsroom upload password <input id=\"password\" type=\"password\" autocomplete=\"current-password\"></label><button id=\"login\">Sign in</button></section>\n<section id=\"workspace\" hidden><h2>Add a month</h2><p>Download the original monthly CSV from <a href=\"https://www.revenue.wi.gov/Pages/RETR/Home.aspx\" target=\"_blank\" rel=\"noopener\">Wisconsin DOR</a>. Select it below. Keep the original filename.</p>\n<input id=\"file\" type=\"file\" accept=\".csv\"><button id=\"review\">Review file</button><pre id=\"summary\"></pre><button id=\"approve\" disabled>Approve for next import</button><p>Approval queues the file for the daily publishing job. The server validates it again before publication. Staff do not need GitHub access. Changed versions of an existing month require editor review.</p><h2>Upload history</h2><button id=\"refresh\">Refresh status</button><ul id=\"history\"></ul><button id=\"logout\">Sign out</button></section><p id=\"message\" role=\"status\"></p>\n<script>\nconst $=id=>document.getElementById(id),counties=['La Crosse','Jackson','Trempealeau','Monroe','Vernon','Buffalo'];let prepared=null;\nasync function api(path,options={}){const r=await fetch(path,options);const b=await r.json();if(!r.ok)throw Error(b.error||'Request failed.');return b;}\nfunction notice(s){$('message').textContent=s;}\nfunction csv(text){const rows=[];let row=[],field='',quoted=false;for(let i=0;i<text.length;i++){const c=text[i];if(quoted){if(c==='\"'&&text[i+1]==='\"'){field+='\"';i++;}else if(c==='\"')quoted=false;else field+=c;}else if(c==='\"'){if(field)throw Error('Unexpected CSV quote.');quoted=true;}else if(c===','){row.push(field);field='';}else if(c==='\\n'||c==='\\r'){if(c==='\\r'&&text[i+1]==='\\n')i++;row.push(field);rows.push(row);row=[];field='';}else field+=c;}if(quoted)throw Error('Unclosed CSV quote.');if(field||row.length){row.push(field);rows.push(row);}return rows;}\nasync function history(){const b=await api('/files');$('signin').hidden=true;$('workspace').hidden=false;$('history').replaceChildren();for(const f of b.files){const li=document.createElement('li');li.textContent=f.name+' — '+f.state+'. '+f.message;if(f.state==='rejected'){const btn=document.createElement('button');btn.textContent='Remove rejected file';btn.onclick=async()=>{if(!confirm('Remove this never-published rejected file?'))return;try{await api('/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:f.id})});await history();}catch(e){notice(e.message);}};li.append(btn);}$('history').append(li);}if(!b.files.length)$('history').textContent='No uploads yet.';}\n$('login').onclick=async()=>{try{await api('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:$('password').value})});$('password').value='';await history();notice('Signed in.');}catch(e){notice(e.message);}};\n$('file').onchange=()=>{prepared=null;$('approve').disabled=true;$('summary').textContent='';};\n$('review').onclick=async()=>{prepared=null;$('approve').disabled=true;try{const f=$('file').files[0];if(!f||!/^RETRHistoricalReport\\d{6}\\.csv$/.test(f.name))throw Error('Select the original RETRHistoricalReportYYYYMM.csv file.');if(f.size>64*1024*1024)throw Error('File exceeds the supported size.');notice('Reading file…');const text=(await f.text()).replace(/^\\uFEFF/,'');const rows=csv(text),headers=rows.shift();if(headers.length!==78||headers[0]!=='County'||headers[2]!=='Recorded Date')throw Error('Unrecognized export layout. Ask the editor to review.');const counts=Object.fromEntries(counties.map(c=>[c,0]));let sales=0,zeroSales=0;const type=headers.indexOf('Conveyance Type'),price=headers.indexOf('Sale Price');for(const r of rows){if(r.length!==78)throw Error('A CSV row has the wrong number of fields.');const c=r[0].trim();if(c in counts){counts[c]++;if(r[type]==='Sale'){sales++;if(r[price]==='$0.00')zeroSales++;}}}const selected=Object.values(counts).reduce((a,b)=>a+b,0);if(!selected)throw Error('No records for the selected counties.');if(typeof CompressionStream==='undefined')throw Error('Use a current Chrome or Edge browser to prepare this file.');const packed=await new Response(f.stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer();if(packed.byteLength>8*1024*1024)throw Error('Compressed file exceeds the supported size. Ask the editor to review.');prepared={name:f.name,packed};$('summary').textContent=f.name+'\\n'+rows.length+' statewide records; '+selected+' selected\\n'+Object.entries(counts).map(([k,v])=>k+': '+v).join('\\n')+'\\nLabeled Sale: '+sales+'; zero-price sales: '+zeroSales+'\\nAll transfer types will be retained.';$('approve').disabled=false;notice('Review the counts, then approve the import.');}catch(e){notice(e.message);}};\n$('approve').onclick=async()=>{if(!prepared)return;$('approve').disabled=true;try{await api('/upload',{method:'POST',headers:{'X-Filename':prepared.name,'Content-Type':'application/gzip'},body:prepared.packed});prepared=null;notice('Approved and stored. Publication follows a successful publishing run.');await history();}catch(e){$('approve').disabled=false;notice(e.message);}};\n$('refresh').onclick=()=>history().catch(e=>notice(e.message));$('logout').onclick=async()=>{await api('/logout',{method:'POST'});location.reload();};history().catch(()=>{});\n</script></html>";
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
