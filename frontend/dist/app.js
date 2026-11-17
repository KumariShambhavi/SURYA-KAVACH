/* SURYA-KAVACH dashboard — no-build React (htm) + Plotly.
   Single API client layer (the ONLY place that knows endpoint shapes) => no data/API mismatch. */
const html = htm.bind(React.createElement);
const { useState, useEffect, useRef, useCallback } = React;

/* ============================ API CLIENT (single source of truth) ============================ */
const api = {
  async _get(path){ const r = await fetch(path); if(!r.ok) throw new Error(path+" "+r.status); return r.json(); },
  async _post(path){ try{ await fetch(path,{method:"POST"}); }catch(e){} },
  summary(){ return this._get("/api/summary"); },
  monitor(){ return this._get("/api/monitor"); },
  catalogue(limit=5000){ return this._get("/api/catalogue?limit="+limit); },
  alerts(limit=1000){ return this._get("/api/alerts?limit="+limit); },
  validation(){ return this._get("/api/validation"); },
  forecastTimeline(){ return this._get("/api/forecast/timeline?limit=1500"); },
  classDist(){ return this._get("/api/stats/class-distribution"); },
  triggerIngest(){ return this._post("/api/ingest?force=true"); },
  triggerValidate(){ return this._post("/api/validate"); },
};

/* ============================ helpers / formatting ============================ */
const T = (s)=> s==null? null : new Date(String(s).replace(" ","T"));     // backend uses "YYYY-MM-DD HH:MM:SS+00:00"
const fmtTime = (s)=>{ const d=T(s); return d&&!isNaN(d)? d.toISOString().replace("T"," ").slice(0,19)+" UTC" : "—"; };
const fmtFlux = (v)=> v==null? "—" : Number(v).toExponential(2);
const pct = (v)=> v==null||isNaN(v)? "—" : (v*100).toFixed(v<0.1?1:0)+"%";
const num = (v,d=0)=> v==null||isNaN(v)? "—" : Number(v).toFixed(d);
const clsLetter = (c)=> (c&&String(c).trim()[0]) || "U";
const CLS_COLOR = {X:"#ff5d73",M:"#ff7a2f",C:"#ffcf4d",B:"#4aa8ff",A:"#8893a7",U:"#5a6478"};

/* ============================ Plotly base ============================ */
const AXES = {gridcolor:"#16203a", zerolinecolor:"#16203a", linecolor:"#1f2a40",
  tickfont:{color:"#8893a7",size:10}, titlefont:{color:"#8893a7",size:11}};
const PLOT_CFG = {displaylogo:false, responsive:true, scrollZoom:true,
  modeBarButtonsToRemove:["lasso2d","select2d"],
  toImageButtonOptions:{filename:"surya-kavach"}};

/* ============================ Live Monitor (3 shared-x panels) ============================ */
function LiveMonitor(){
  const ref = useRef(null);
  const wsRef = useRef(null);
  const prog = useRef(false);            // guard: our own relayout vs user's
  const follow = useRef(true);
  const startT = useRef(null), lastT = useRef(null), thr = useRef(0.5);
  const [status,setStatus] = useState("connecting");
  const [paused,setPaused] = useState(false);
  const [speed,setSpeed] = useState(8);
  const [following,setFollowing] = useState(true);

  const layout = useCallback(()=>({
    height:520, margin:{l:62,r:16,t:10,b:30}, paper_bgcolor:"rgba(0,0,0,0)", plot_bgcolor:"rgba(0,0,0,0)",
    showlegend:false, hovermode:"x unified",
    xaxis:{...AXES, anchor:"y3", type:"date"},
    yaxis:{...AXES, domain:[0.69,1.0], title:"SXR  W/m²", type:"log", exponentformat:"e"},
    yaxis2:{...AXES, domain:[0.37,0.64], title:"HXR  counts/s"},
    yaxis3:{...AXES, domain:[0.04,0.31], title:"P(flare ≤30m)", range:[0,1]},
    shapes:[{type:"line", xref:"paper", x0:0, x1:1, yref:"y3", y0:thr.current, y1:thr.current,
      line:{color:"#ff5d73", width:1.2, dash:"dash"}}],
    annotations:[
      panelLabel("Soft X-ray (SoLEXS)",0.99,"#ff7a2f"),
      panelLabel("Hard X-ray (HEL1OS)",0.63,"#4aa8ff"),
      panelLabel("Forecast probability (EXPERIMENTAL - no demonstrated skill)",0.30,"#9b8cff"),
    ],
  }),[]);
  const panelLabel = (txt,y,color)=>({xref:"paper",yref:"paper",x:0.008,y,xanchor:"left",yanchor:"top",
    text:txt,showarrow:false,font:{size:10.5,color},bgcolor:"rgba(10,16,28,.6)"});

  const init = useCallback(()=>{
    const traces=[
      {x:[],y:[],type:"scattergl",mode:"lines",line:{color:"#ff7a2f",width:1.3},yaxis:"y",name:"SXR",connectgaps:false},
      {x:[],y:[],type:"scattergl",mode:"markers",marker:{color:"#33d18b",size:8,symbol:"triangle-up",line:{color:"#0a0",width:0}},yaxis:"y",name:"flare"},
      {x:[],y:[],type:"scattergl",mode:"lines",line:{color:"#4aa8ff",width:1.3},yaxis:"y2",name:"HXR",connectgaps:false},
      {x:[],y:[],type:"scattergl",mode:"lines",line:{color:"#9b8cff",width:1.6},yaxis:"y3",name:"P",connectgaps:false},
    ];
    Plotly.newPlot(ref.current, traces, layout(), PLOT_CFG);
    ref.current.on("plotly_relayout",(e)=>{           // user zoom/pan -> stop following
      if(prog.current) return;
      if(e && (e["xaxis.range[0]"]!==undefined || e["xaxis.autorange"]!==undefined || e["xaxis.range"]!==undefined)){
        follow.current=false; setFollowing(false);
      }
    });
  },[layout]);

  const send=(cmd,value)=>{ const ws=wsRef.current; if(ws&&ws.readyState===1) ws.send(JSON.stringify({cmd,value})); };

  useEffect(()=>{
    init();
    let alive=true, ws;
    const connect=()=>{
      const proto = location.protocol==="https:"?"wss":"ws";
      ws = new WebSocket(`${proto}://${location.host}/ws/live`); wsRef.current=ws;
      ws.onopen=()=>{ setStatus("live"); send("speed",speed); };
      ws.onclose=()=>{ setStatus("disconnected"); if(alive) setTimeout(connect,2000); };
      ws.onerror=()=>{ try{ws.close();}catch(e){} };
      ws.onmessage=(ev)=>{
        const m=JSON.parse(ev.data);
        if(m.type==="init"){
          if(m.empty){ setStatus("no data"); return; }
          if(m.threshold!=null){ thr.current=m.threshold; }
          startT.current=m.time_start; init();
        } else if(m.type==="reset"){ init(); }
        else if(m.type==="frame"){
          if(!m.t || !m.t.length) return;
          Plotly.extendTraces(ref.current, {x:[m.t,m.t,m.t], y:[m.sxr,m.hxr,m.proba]}, [0,2,3]);
          if(m.events && m.events.length){
            const tt=m.t[m.t.length-1];
            const vals=(m.sxr||[]).filter(v=>v!=null);
            if(vals.length) Plotly.extendTraces(ref.current,{x:[[tt]],y:[[Math.max.apply(null,vals)]]},[1]);
          }
          lastT.current=m.t[m.t.length-1];
          if(follow.current && startT.current){
            prog.current=true;
            Plotly.relayout(ref.current,{"xaxis.range":[startT.current,lastT.current]}).then(()=>{prog.current=false;});
          }
        }
      };
    };
    connect();
    return ()=>{ alive=false; if(ws) ws.close(); };
  },[]);

  const onPause=()=>{ const p=!paused; setPaused(p); send(p?"pause":"resume"); };
  const onRestart=()=>{ init(); send("restart"); };
  const onSpeed=(e)=>{ const v=+e.target.value; setSpeed(v); send("speed",v); };
  const onFollow=()=>{ follow.current=true; setFollowing(true);
    if(startT.current&&lastT.current){ prog.current=true;
      Plotly.relayout(ref.current,{"xaxis.range":[startT.current,lastT.current],"yaxis.autorange":true,"yaxis2.autorange":true}).then(()=>{prog.current=false;}); } };

  const dot = status==="live"?"var(--green)":status==="disconnected"?"var(--red)":"var(--faint)";
  return html`
    <div class="card">
      <h3>Live X-ray Monitor
        <span class="t"><span class="chip ${status==='live'?'c-live':'c-off'}"><span class="d" style=${{background:dot}}></span>${status}</span></span></h3>
      <div class="ctrls">
        <button class=${paused?"pri":""} onClick=${onPause}>${paused?"▶  Resume":"⏸  Pause"}</button>
        <button onClick=${onRestart}>↻  Restart</button>
        <button class=${following?"act":""} onClick=${onFollow}>${following?"⦿ Following":"⊙ Follow live"}</button>
        <span class="faint">speed</span>
        <select value=${speed} onChange=${onSpeed}>${[1,2,4,8,16,32,64].map(s=>html`<option key=${s} value=${s}>${s}×</option>`)}</select>
        <span class="hint" style=${{marginLeft:"auto"}}>Pause, then scroll to zoom · drag to pan · use the toolbar (top-right of chart)</span>
      </div>
      <div ref=${ref} class="chart"></div>
    </div>`;
}

/* ============================ Forecast gauge + timeline ============================ */
function Gauge({value, threshold}){
  const ref=useRef(null);
  useEffect(()=>{
    const v=(value==null?0:value)*100, th=(threshold==null?50:threshold*100);
    Plotly.react(ref.current,[{
      type:"indicator", mode:"gauge+number", value:v, number:{suffix:"%",font:{color:"#e6ecf6",size:30}},
      gauge:{axis:{range:[0,100],tickcolor:"#8893a7"}, bar:{color:v>=th?"#ff5d73":"#33d18b"},
        bgcolor:"rgba(0,0,0,0)", borderwidth:0,
        steps:[{range:[0,th],color:"#13243a"},{range:[th,100],color:"#2a1622"}],
        threshold:{line:{color:"#ff5d73",width:3}, value:th}},
    }],{height:210,margin:{l:24,r:24,t:18,b:8},paper_bgcolor:"rgba(0,0,0,0)",font:{color:"#8893a7"}},
    {displayModeBar:false,responsive:true});
  },[value,threshold]);
  return html`<div ref=${ref}></div>`;
}

function ForecastTimeline({data}){
  const ref=useRef(null);
  useEffect(()=>{
    if(!data) return;
    const pts=data.points||[]; const th=data.threshold!=null?data.threshold:0.5;
    const x=pts.map(p=>p.t), y=pts.map(p=>p.proba==null?null:p.proba*100);
    const fx=(data.flares||[]).map(f=>T(f.t)).filter(Boolean);
    const fc=(data.flares||[]).map(f=>CLS_COLOR[clsLetter(f.cls)]);
    const traces=[
      {x,y,type:"scattergl",mode:"lines",line:{color:"#9b8cff",width:1.4},fill:"tozeroy",
        fillcolor:"rgba(155,140,255,.12)",name:"P(flare)",connectgaps:false},
      {x:fx,y:fx.map(()=>100),type:"scattergl",mode:"markers",
        marker:{color:fc,size:9,symbol:"triangle-down",line:{color:"#0a0e18",width:.5}},name:"actual flare"},
    ];
    Plotly.react(ref.current,traces,{
      height:300,margin:{l:50,r:16,t:10,b:30},paper_bgcolor:"rgba(0,0,0,0)",plot_bgcolor:"rgba(0,0,0,0)",
      showlegend:false,hovermode:"x unified",
      xaxis:{...AXES,type:"date"}, yaxis:{...AXES,title:"P(flare ≤30m)  %",range:[0,105]},
      shapes:[{type:"line",xref:"paper",x0:0,x1:1,yref:"y",y0:th*100,y1:th*100,line:{color:"#ff5d73",width:1.2,dash:"dash"}}],
    },PLOT_CFG);
  },[data]);
  return html`<div ref=${ref}></div>`;
}

/* ============================ KPIs ============================ */
/* Headline the NOWCAST — that is the validated deliverable. The forecast metrics that used to sit
   here (TPR 100%, FAR 15%, "median lead 38m") are ARTIFACTS of an always-on alarm from a model with
   no skill (AUC 0.537 ~ random). Advertising "TPR 100%" on the front page contradicted our own
   validation, so they are gone. The forecast lives on its own tab, clearly flagged experimental. */
function Kpis({summary, val}){
  const mon=summary.monitor||{}, bs=mon.by_status||{};
  const v=val||{}, gm=v.ge_M||{}, gc=v.ge_C||{};
  const p=(x)=> x==null? "—" : Math.round(x*100)+"%";
  const cards=[
    {l:"Master events",v:mon.total_flares??"—",s:"total catalogued",c:"var(--blue)"},
    {l:"Confirmed",v:bs.confirmed??0,s:"soft+hard cross-confirmed",c:"var(--green)"},
    {l:"Single-instrument",v:(bs.sxr_only||0)+(bs.hxr_only||0),s:`${bs.sxr_only||0} SXR · ${bs.hxr_only||0} HXR`,c:"var(--orange)"},
    {l:"≥M completeness",v:p(gm.completeness),s:"of real GOES M/X flares detected",c:"var(--green)"},
    {l:"≥M purity",v:p(gm.purity),s:"of our ≥M detections are real",c:"var(--cyan)"},
    {l:"≥C completeness",v:p(gc.completeness),s:`purity ${p(gc.purity)}`,c:"var(--yellow)"},
    {l:"Ground truth",v:gm.n_goes!=null?(v.all&&v.all.n_goes)||"—":"—",s:"NOAA/SWPC GOES flares (complete)",c:"var(--violet)"},
  ];
  return html`<div class="kpis">${cards.map(k=>html`
    <div class="kpi" key=${k.l} style=${{"--accent":k.c}}>
      <div class="l">${k.l}</div><div class="v" style=${{color:k.c}}>${k.v}</div><div class="s">${k.s}</div>
    </div>`)}</div>`;
}

/* ============================ Catalogue ============================ */
function Catalogue({rows}){
  const [sort,setSort]=useState({k:"onset_time",dir:-1});
  const [status,setStatus]=useState("all");
  const [q,setQ]=useState("");
  const filtered = rows.filter(r=> (status==="all"||r.status===status) &&
     (!q || (r.flare_class||"").toLowerCase().includes(q.toLowerCase())));
  const sorted=[...filtered].sort((a,b)=>{ const k=sort.k; let av=a[k],bv=b[k];
    if(k==="onset_time"){av=T(av)||0;bv=T(bv)||0;} return (av>bv?1:av<bv?-1:0)*sort.dir; });
  const th=(k,label)=>html`<th onClick=${()=>setSort(s=>({k,dir:s.k===k?-s.dir:1}))}>${label}${sort.k===k?(sort.dir>0?" ▲":" ▼"):""}</th>`;
  return html`<div class="card">
    <h3>Master Flare Catalogue <span class="t">${filtered.length>1000?`showing 1000 of ${filtered.length}`:`${filtered.length} of ${rows.length}`}</span></h3>
    <div class="ctrls">
      <div class="seg">${["all","confirmed","sxr_only","hxr_only"].map(s=>html`
        <button key=${s} class=${status===s?"on":""} onClick=${()=>setStatus(s)}>${s.replace("_"," ")}</button>`)}</div>
      <input placeholder="filter class e.g. M" value=${q} onInput=${e=>setQ(e.target.value)}
        style=${{background:"var(--panel2)",color:"var(--ink)",border:"1px solid var(--line)",borderRadius:"9px",padding:"8px 11px"}}/>
    </div>
    <div class="scroll"><table><thead><tr>
      ${th("status","Status")}${th("flare_class","Class")}${th("onset_time","Onset (UTC)")}
      <th>SXR peak flux</th><th>HXR peak c/s</th><th>HXR lead</th><th>Conf.</th>
    </tr></thead><tbody>
    ${sorted.slice(0,1000).map(r=>html`<tr key=${r.event_id||r.id}>
      <td><span class="pill p-${r.status}">${(r.status||"").replace("_"," ")}</span></td>
      <td class="cls cls-${clsLetter(r.flare_class)}">${r.flare_class||"—"}</td>
      <td>${fmtTime(r.onset_time)}</td>
      <td>${fmtFlux(r.peak_sxr_flux_w_m2)}</td>
      <td>${r.peak_hxr_counts_s!=null?Math.round(r.peak_hxr_counts_s):"—"}</td>
      <td>${r.hxr_leads_sxr_minutes!=null?r.hxr_leads_sxr_minutes+" m":"—"}</td>
      <td class="faint">${r.confidence||"—"}</td>
    </tr>`)}
    </tbody></table></div></div>`;
}

/* ============================ Alerts ============================ */
function Alerts({rows, m}){
  const auc=(m&&m.test_auc!=null)?Number(m.test_auc).toFixed(3):"~0.5";
  return html`<div class="card"><h3>Forecast Alerts <span class="t" style=${{color:"#ff5d73"}}>EXPERIMENTAL · ${rows.length} episodes</span></h3>
    <div class="hint" style=${{marginBottom:"8px",color:"#ff5d73"}}>⚠ These come from a model with <b>no demonstrated skill</b> (AUC ${auc} ≈ random). Not operational — see the Forecasting tab.</div>
    ${rows.length===0? html`<div class="empty">No alert episodes.</div>` :
      html`<div class="scroll">${rows.map(a=>html`<div class="alert" key=${a.alert_id}>
        <div class="ic">⚠️</div>
        <div><div><b>P=${(a.peak_probability??0).toFixed(2)}</b> · model output (unvalidated)</div>
        <div class="ts">${fmtTime(a.start_time)} → ${fmtTime(a.end_time)}</div></div>
      </div>`)}</div>`}
  </div>`;
}

/* ============================ Validation ============================ */
function Validation({val,onRevalidate,busy}){
  if(!val||!val.ok) return html`<div class="card"><h3>Scientific Validation <span class="t">vs NOAA/SWPC GOES</span></h3>
    <div class="empty">${(val&&val.reason)||"Not run yet."}</div>
    <button onClick=${onRevalidate} disabled=${busy}>${busy?"Validating…":"Run validation"}</button></div>`;
  const tiers=[["All flares","all"],["≥ C-class","ge_C"],["≥ M-class","ge_M"]];
  return html`<div class="card">
    <h3>Scientific Validation <span class="t">independent NOAA/SWPC GOES · ${val.source} · ${val.range}</span></h3>
    <table><thead><tr><th>Subset</th><th>GOES</th><th>Ours</th><th>Completeness</th><th>Purity</th></tr></thead><tbody>
    ${tiers.map(([label,k])=>{const d=val[k]||{};return html`<tr key=${k}>
      <td>${label}</td><td>${d.n_goes??"—"}</td><td>${d.n_ours??"—"}</td>
      <td class="cls cls-C">${pct(d.completeness)}</td><td class="cls cls-B">${pct(d.purity)}</td></tr>`;})}
    </tbody></table>
    <div class="hint">Completeness = real GOES flares we detected (recall). Purity = our detections that are real (precision). ±${val.tolerance_min} min match.</div>
    <button style=${{marginTop:"10px"}} onClick=${onRevalidate} disabled=${busy}>${busy?"Validating…":"Re-validate"}</button>
  </div>`;
}

/* ============================ Analytics ============================ */
/* The distribution comes from the SERVER (GROUP BY over the whole table), never from the page of
   rows we happen to have fetched — that used to show ~60% of reality next to a KPI with the true
   COUNT(*), because the catalogue request was capped at 2000 rows. */
function Analytics({dist}){
  const order=["X","M","C","B","A","U"];
  const counts=Object.fromEntries(order.map(k=>[k,(dist&&dist.by_class&&dist.by_class[k])||0]));
  const total=(dist&&dist.total)||order.reduce((a,k)=>a+counts[k],0);
  const max=Math.max(1,...order.map(k=>counts[k]));
  return html`<div class="card"><h3>Flare Class Distribution <span class="t">${total} events (full catalogue)</span></h3>
    ${order.map(k=>html`<div key=${k} style=${{margin:"9px 0"}}>
      <div style=${{display:"flex",justifyContent:"space-between",fontSize:"12px",marginBottom:"4px"}}>
        <span class="cls cls-${k}">${k}-class</span><span class="faint">${counts[k]}</span></div>
      <div class="barwrap"><div class="bar" style=${{width:(counts[k]/max*100)+"%",background:CLS_COLOR[k]}}></div></div>
    </div>`)}
  </div>`;
}

/* ============================ App ============================ */
const NAV=[["overview","Overview","◎"],["live","Live Monitor","∿"],["nowcast","Nowcast Catalogue","▤"],
  ["forecast","Forecasting","◈"],["alerts","Alerts","⚑"],["validation","Validation","✓"],
  ["analytics","Analytics","▦"],["about","About","ⓘ"]];
const TITLES={overview:"Mission Control",live:"Live X-ray Monitor",nowcast:"Nowcast Catalogue",
  forecast:"Forecasting",alerts:"Forecast Alerts",validation:"Scientific Validation",analytics:"Analytics",about:"About"};

function App(){
  const [view,setView]=useState("overview");
  const [summary,setSummary]=useState({metrics:{},monitor:{}});
  const [cat,setCat]=useState([]); const [alerts,setAlerts]=useState([]);
  const [val,setVal]=useState(null); const [fc,setFc]=useState(null);
  const [dist,setDist]=useState(null);   // server-side class distribution (never from a page)
  const [clock,setClock]=useState(""); const [busy,setBusy]=useState(false); const [vbusy,setVbusy]=useState(false);

  /* Fetch in PARALLEL with a re-entrancy guard.
     It used to await six calls in sequence every 5s. Once the catalogue grew (3,328 rows) and the
     forecast-timeline endpoint started parsing an 8 MB CSV (~1.1s), one chain took longer than the
     5s interval, so chains piled up and the LAST calls (validation, timeline, class-distribution)
     were permanently starved — the Validation card sat on "Not run yet" while the API was happily
     returning the data. Parallel + guarded + a slower tick for the heavy stuff fixes it. */
  const inflight=useRef(false);
  const refresh=useCallback(async(heavy=false)=>{
    if(inflight.current) return;
    inflight.current=true;
    const jobs=[
      api.summary().then(setSummary).catch(()=>{}),
      api.alerts().then(setAlerts).catch(()=>{}),
      api.validation().then(setVal).catch(()=>{}),
      api.classDist().then(setDist).catch(()=>{}),
    ];
    if(heavy){
      jobs.push(api.catalogue().then(setCat).catch(()=>{}));
      jobs.push(api.forecastTimeline().then(setFc).catch(()=>{}));
    }
    try{ await Promise.allSettled(jobs); } finally { inflight.current=false; }
  },[]);
  useEffect(()=>{
    refresh(true);                                    // first load: everything
    const light=setInterval(()=>refresh(false),6000); // cheap KPIs/validation
    const heavy=setInterval(()=>refresh(true),30000); // catalogue + 8MB timeline
    return ()=>{ clearInterval(light); clearInterval(heavy); };
  },[refresh]);
  useEffect(()=>{ const id=setInterval(()=>setClock(new Date().toISOString().replace("T"," ").slice(0,19)),1000); return ()=>clearInterval(id); },[]);

  const ingest=async()=>{ setBusy(true); await api.triggerIngest(); setTimeout(()=>{setBusy(false);refresh(true);},1800); };
  const revalidate=async()=>{ setVbusy(true); await api.triggerValidate(); setTimeout(()=>{setVbusy(false);refresh(true);},2500); };

  const st=(summary.monitor||{}).status||"IDLE";
  const m=summary.metrics||{};
  const thr=m.alert_threshold!=null?m.alert_threshold:(fc&&fc.threshold);
  const lastP = fc&&fc.points&&fc.points.length ? [...fc.points].reverse().find(p=>p.proba!=null) : null;

  const forecastView = html`<div>
    <div class="card" style=${{borderLeft:"3px solid #ff5d73",marginBottom:"14px"}}>
      <h3 style=${{color:"#ff5d73"}}>⚠ Experimental — negative result, not an operational forecast</h3>
      <p class="muted" style=${{margin:"6px 0 0"}}>
        Evaluated against the <b>independent</b> NOAA/SWPC flare list, this model is <b>indistinguishable from random</b> (test AUC ${m.test_auc!=null?Number(m.test_auc).toFixed(3):"~0.5"}; Brier Skill Score ${m.skill_brier_skill_score!=null?Number(m.skill_brier_skill_score).toFixed(3):"—"} — below 0 means worse than climatology). <b>Do not use these probabilities operationally.</b><br/>
        Flares are <i>magnetic</i> events: X-ray flux shows that a flare <i>is happening</i>, not that one <i>is coming</i>. Aditya-L1 carries no magnetograph, so onset forecasting from it alone is
        fundamentally limited. Genuine skill requires magnetogram (SHARP/SDO-HMI) features — the next phase.
        The <b>nowcast</b> (Catalogue / Validation tabs) is the validated deliverable.
      </p>
    </div>
    <div class="row g-2-1">
      <div class="card"><h3>Forecast Probability Timeline <span class="t">experimental · P(≥C-class flare within 30 min) · ▾ = actual flare onset</span></h3>
        <${ForecastTimeline} data=${fc} />
        <div class="legend"><span><i style=${{background:"#9b8cff"}}></i>forecast probability (no demonstrated skill)</span>
          <span><i style=${{background:"#ff5d73"}}></i>alert threshold ${thr!=null?(thr*100).toFixed(0)+"%":""}</span>
          <span><i style=${{background:"#ffcf4d"}}></i>actual flares (C/M/X)</span></div></div>
      <div class="card"><h3>Model Probability — last archived minute <span class="t" style=${{color:"#ff5d73"}}>experimental · NOT live</span></h3>
        <${Gauge} value=${lastP?lastP.proba:0} threshold=${thr} />
        <div class="hint" style=${{textAlign:"center"}}>${lastP?fmtTime(lastP.t):"—"} · end of the archived dataset, <b>not real time</b> · <b>no demonstrated skill</b></div>
        <div style=${{marginTop:"12px",display:"grid",gridTemplateColumns:"1fr 1fr",gap:"10px"}}>
          <div class="kpi" style=${{"--accent":"var(--red)"}}><div class="l">Test AUC</div><div class="v" style=${{fontSize:"20px",color:"var(--red)"}}>${m.test_auc!=null?Number(m.test_auc).toFixed(3):"—"}</div><div class="s">0.5 = random</div></div>
          <div class="kpi" style=${{"--accent":"var(--red)"}}><div class="l">Brier skill</div><div class="v" style=${{fontSize:"20px",color:"var(--red)"}}>${m.skill_brier_skill_score!=null?Number(m.skill_brier_skill_score).toFixed(3):"—"}</div><div class="s">below 0 = worse than climatology</div></div>
          <div class="kpi" style=${{"--accent":"var(--faint)"}}><div class="l">Model TSS</div><div class="v" style=${{fontSize:"20px"}}>${m.skill_model_tss!=null?Number(m.skill_model_tss).toFixed(3):"—"}</div><div class="s">0 = no skill</div></div>
          <div class="kpi" style=${{"--accent":"var(--faint)"}}><div class="l">Labels</div><div class="v" style=${{fontSize:"13px"}}>NOAA/SWPC</div><div class="s">independent ground truth</div></div>
        </div>
        <div class="hint" style=${{marginTop:"10px"}}>TPR/FAR/lead-time are deliberately NOT shown: at this
        threshold the alarm is on most of the time, so they look excellent while the model has no skill.</div></div>
    </div></div>`;

  return html`<div class="app">
    <div class="side">
      <div class="brand"><div class="logo"></div><div><b>SURYA-KAVACH</b><span>FLARE OPS</span></div></div>
      <div class="nav">${NAV.map(([k,l,ic])=>html`<a key=${k} class=${view===k?"on":""} onClick=${()=>setView(k)}>
        <span class="ic">${ic}</span>${l}${k==="forecast"?html`<span class="tag" style=${{background:"#ff5d73"}}>EXP</span>`:null}</a>`)}</div>
      <div class="sidefoot">Aditya-L1 · SoLEXS + HEL1OS<br/>v1.0.0 · BAH 2026 · PS-15</div>
    </div>
    <div class="main">
      <div class="top">
        <div><h1>${TITLES[view]}</h1><div class="sub">SoLEXS (soft X-ray) + HEL1OS (hard X-ray) · dual-channel nowcasting & forecasting</div></div>
        <div class="right">
          <div class="clock"><small>UTC TIME</small>${clock}</div>
          <span class="chip ${st==='MONITORING'?'c-live':st==='INGESTING'?'c-on':'c-off'}">
            <span class="d ${st==='INGESTING'?'blink':''}" style=${{background:st==='MONITORING'?'var(--green)':st==='INGESTING'?'var(--blue)':'var(--faint)'}}></span>${st}</span>
          <button class="pri" disabled=${busy} onClick=${ingest}>${busy?"Ingesting…":"⟳ Ingest"}</button>
        </div>
      </div>
      <div class="content">
        ${view==="overview"&&html`<div>
          <${Kpis} summary=${summary} val=${val} />
          <div class="row g-2-1"><${LiveMonitor} /><${Alerts} rows=${alerts} m=${summary.metrics} /></div>
          <div class="row g-1-1"><${Validation} val=${val} onRevalidate=${revalidate} busy=${vbusy} /><${Analytics} dist=${dist} /></div>
        </div>`}
        ${view==="live"&&html`<${LiveMonitor} />`}
        ${view==="nowcast"&&html`<${Catalogue} rows=${cat} />`}
        ${view==="forecast"&&forecastView}
        ${view==="alerts"&&html`<${Alerts} rows=${alerts} m=${summary.metrics} />`}
        ${view==="validation"&&html`<${Validation} val=${val} onRevalidate=${revalidate} busy=${vbusy} />`}
        ${view==="analytics"&&html`<div class="row g-1-1"><${Analytics} dist=${dist} /><${Catalogue} rows=${cat} /></div>`}
        ${view==="about"&&html`<div class="card"><h3>About SURYA-KAVACH</h3>
          <p class="muted">Dual-channel (SoLEXS soft + HEL1OS hard X-ray) solar-flare <b>nowcasting</b> from ISRO
          Aditya-L1. A Poisson-significance detector with SXR/HXR temporal-coincidence fusion builds the master
          catalogue, validated 1:1 against the <b>complete</b> official NOAA/SWPC GOES list: <b>78% of M-class-and-above flares detected at 71% purity</b>. ISRO BAH 2026 — Problem Statement 15
          ("Forecasting <i>and/or</i> Nowcasting").</p>
          <p class="muted"><b>Forecasting is an honest negative result.</b> Scored on independent GOES ground truth,
          X-ray-only forecasting is indistinguishable from random (test AUC ${m.test_auc!=null?Number(m.test_auc).toFixed(3):"~0.5"}; BSS ${m.skill_brier_skill_score!=null?Number(m.skill_brier_skill_score).toFixed(3):"—"}). Flares are magnetic
          events and Aditya-L1 carries no magnetograph; genuine skill requires SHARP/SDO-HMI magnetogram features.</p>
          <p class="faint">Loaded window: ${m.time_start||"—"} → ${m.time_end||"—"} · ${(m.samples||0).toLocaleString()} samples.</p></div>`}
      </div>
    </div>
  </div>`;
}

ReactDOM.createRoot(document.getElementById("root")).render(html`<${App} />`);
