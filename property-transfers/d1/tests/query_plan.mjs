import {buildQuery,pageResult} from '../search-worker.mjs';
let text='';for await(const chunk of process.stdin)text+=chunk;
const input=JSON.parse(text);
if(input.kind==='page')console.log(JSON.stringify(pageResult(input.rows,input.plan)));
else{const output=[];for(const query of input.queries){try{output.push(await buildQuery(new URLSearchParams(query)));}catch(error){output.push({error:error.message});}}console.log(JSON.stringify(output));}
