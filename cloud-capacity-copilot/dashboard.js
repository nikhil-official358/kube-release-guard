"use strict";
const get=id=>document.getElementById(id);
const labels={near_or_above_request:"Near or above request",missing_request:"Missing request",low_usage_sample:"Low usage sample"};
const guidance={
  near_or_above_request:"Compare peak usage with configured limits, throttling, and memory pressure before tuning.",
  missing_request:"Review the workload owner and representative usage before choosing a request.",
  low_usage_sample:"Check workload peaks across a representative window before considering a smaller request."
};
const order=Object.keys(labels);
let current, paused=false, busy=false, interval=15, timer, history=[];
const set=(id,value)=>{get(id).textContent=value;};
function chart(){
  const canvas=get("chart"), width=canvas.clientWidth, height=canvas.clientHeight;
  if(!width||!height)return;
  const dpr=window.devicePixelRatio||1;
  canvas.width=width*dpr;canvas.height=height*dpr;
  const ctx=canvas.getContext("2d");ctx.scale(dpr,dpr);
  const max=Math.max(4,...history.map(p=>p.count)), left=30, right=width-14, bottom=height-28;
  ctx.font="11px system-ui";
  for(let i=0;i<=4;i++){
    const y=bottom-(bottom-12)*i/4;
    ctx.strokeStyle="#30435b";ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();
    ctx.fillStyle="#a2b2ca";ctx.fillText((max*i/4).toFixed(1).replace(/\.0$/,""),0,y+4);
  }
  const pts=history.map((p,i)=>[left+(right-left)*(history.length===1?.5:i/(history.length-1)),bottom-(bottom-12)*p.count/max]);
  ctx.beginPath();pts.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));
  ctx.lineWidth=2;ctx.strokeStyle="#6debd0";ctx.stroke();
  for(const [x,y] of pts){ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fillStyle="#6debd0";ctx.fill();}
  if(history.length){ctx.fillStyle="#a2b2ca";ctx.fillText(new Date(history[0].at).toLocaleTimeString(),left,height-7);}
  canvas.setAttribute("aria-label","Finding history: "+history.map(p=>p.count).join(", "));
}
function detail(f){
  set("detail-title",f.target);
  const list=document.createElement("dl");
  const fields={Resource:f.resource,Finding:labels[f.signal],Request:f.request??"Unset",Usage:f.usage??"Unavailable","Percent of request":f.usage_pct_of_request==null?"Unavailable":f.usage_pct_of_request+"%","Measured at":f.metrics_timestamp?new Date(f.metrics_timestamp).toLocaleString():"Unavailable"};
  for(const [key,value] of Object.entries(fields)){
    const label=document.createElement("dt"), data=document.createElement("dd");
    label.textContent=key;data.textContent=value;list.append(label,data);
  }
  const tip=document.createElement("p");tip.textContent=guidance[f.signal];
  get("detail-body").replaceChildren(list,tip);get("detail").showModal();
}
function evidence(){
  const rows=(current?.findings??[]).filter(f=>
    f.target.toLowerCase().includes(get("search").value.trim().toLowerCase())&&
    (!get("resource").value||f.resource===get("resource").value)&&
    (!get("signal").value||f.signal===get("signal").value)
  ).sort((a,b)=>order.indexOf(a.signal)-order.indexOf(b.signal)||
    (a.signal==="low_usage_sample"?1:-1)*((a.usage_pct_of_request??0)-(b.usage_pct_of_request??0)));
  if(get("sort").value==="ratio")rows.sort((a,b)=>(b.usage_pct_of_request??-1)-(a.usage_pct_of_request??-1));
  if(get("sort").value==="name")rows.sort((a,b)=>a.target.localeCompare(b.target));
  get("csv").disabled=!rows.length;
  set("matches",rows.length+" of "+(current?.findings.length??0)+" findings");
  const buttons=rows.map(f=>{
    const b=document.createElement("button");b.className="finding";b.type="button";
    const target=document.createElement("span"), type=document.createElement("span"), value=document.createElement("strong");
    target.textContent=f.target;type.className=f.signal;type.textContent=f.resource.toUpperCase()+" � "+labels[f.signal];
    value.textContent=f.usage_pct_of_request==null?"Unset":f.usage_pct_of_request+"%";
    b.append(target,type,value);b.addEventListener("click",()=>detail(f));return b;
  });
  get("results").replaceChildren(...buttons);
  if(!rows.length)set("results",current?"No findings match these filters.":"Waiting for evidence.");
  return rows;
}
function ask(query){
  get("ask").value=query;
  const q=query.toLowerCase();
  const signal=/missing|unset/.test(q)?"missing_request":/low|underus/.test(q)?"low_usage_sample":/high|above|pressure/.test(q)?"near_or_above_request":"";
  const resource=/\b(memory|ram)\b/.test(q)?"memory":/\bcpu\b/.test(q)?"cpu":"";
  const named=(current?.findings??[]).find(f=>q.includes(f.target.toLowerCase())||q.includes(f.target.split("/").pop().toLowerCase()));
  if(!signal&&!resource&&!named&&!/summary|overview/.test(q)){set("answer","Ask about high usage, low usage, missing requests, or a container in the queue.");return;}
  get("signal").value=signal;get("resource").value=resource;get("search").value=named?named.target:"";
  const rows=evidence();
  set("answer",rows.length+" matching findings. "+(rows[0]?"First: "+rows[0].target+" ("+rows[0].resource+"). "+guidance[rows[0].signal]:"Try a broader filter."));
}
async function refresh(){
  if(busy)return;
  clearTimeout(timer);busy=true;get("refresh").disabled=true;
  try{
    const response=await fetch("/api/snapshot",{cache:"no-store"});
    const data=await response.json();interval=data.refresh??interval;
    if(!response.ok)throw new Error(data.error);
    current=data.report;get("error").hidden=true;get("export").disabled=false;
    set("scope",data.scope);
    set("status",(paused?"Paused � ":current.source==="DEMO_FIXTURE"?"Demo � ":"Live � ")+new Date(current.observed_at).toLocaleTimeString());
    set("fresh",current.containers_with_metrics);
    set("coverage",current.containers_without_fresh_metrics+" without fresh metrics � API "+current.metrics_status);
    for(const [i,id] of ["high","missing","low"].entries())set(id,current.findings.filter(f=>f.signal===order[i]).length);
    if(history.at(-1)?.at!==current.observed_at)history=[...history,{at:current.observed_at,count:current.findings.length}].slice(-30);
    set("history-note",history.length+" collected snapshots � "+interval+"s interval");chart();
    get("ai").hidden=!current.local_ai_advice&&!current.local_ai_status;
    set("advice",current.local_ai_advice??"Local model unavailable.");
    if(answerQuery)ask(answerQuery);else evidence();
  }catch(error){
    set("status",current?"Stale � collection failed":"Collection failed");
    set("error",error.message);get("error").hidden=false;
  }finally{
    busy=false;get("refresh").disabled=false;
    if(!paused)timer=setTimeout(refresh,interval*1000);
  }
}
let answerQuery="";
get("question").addEventListener("submit",e=>{e.preventDefault();answerQuery=get("ask").value.trim();ask(answerQuery);});
document.querySelectorAll("[data-query]").forEach(b=>b.addEventListener("click",()=>{answerQuery=b.dataset.query;ask(answerQuery);}));
for(const id of ["search","resource","signal"])get(id).addEventListener("input",()=>{answerQuery="";evidence();});
get("clear").addEventListener("click",()=>{answerQuery="";for(const id of ["ask","search","resource","signal","sort"])get(id).value="";set("answer","Choose a question to narrow the evidence.");evidence();});
get("sort").addEventListener("change",evidence);
get("csv").addEventListener("click",()=>{
  const fields=["target","resource","signal","request","usage","usage_pct_of_request","metrics_timestamp"];
  const cell=value=>{
    let text=String(value??"");
    if(/^[=+@-]/.test(text))text="'"+text;
    return '"'+text.replaceAll('"','""')+'"';
  };
  const data=[fields.join(","),...evidence().map(row=>fields.map(key=>cell(row[key])).join(","))].join(String.fromCharCode(13,10));
  const url=URL.createObjectURL(new Blob([data],{type:"text/csv;charset=utf-8"}));
  const link=document.createElement("a");link.href=url;link.download="capacity-findings.csv";link.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
get("refresh").addEventListener("click",refresh);
get("pause").addEventListener("click",()=>{paused=!paused;set("pause",paused?"Resume":"Pause");if(paused){clearTimeout(timer);set("status","Paused");}else refresh();});
get("export").addEventListener("click",()=>{if(!current)return;const url=URL.createObjectURL(new Blob([JSON.stringify(current,null,2)],{type:"application/json"}));const link=document.createElement("a");link.href=url;link.download="capacity-snapshot.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
window.addEventListener("resize",chart);
refresh();
