const result=require('./results.json');
if(!result.complete||!result.session_guard_advertised||result.heartbeat_ms!==1000||
  result.maintenance_interval!=='1s'||result.poll_interval_seconds!==.2)
  throw new Error('A complete result with the measured configuration is required');
for(const stage of ['before_partition','b_recovered','b_before_reconnect','b_after_reconnect','fresh_reader'])
  if(result[stage].sha256!==result[stage].expected_sha256)throw new Error('Checksum mismatch');
const event=name=>result.events.find(e=>e.event===name);
const sequence=['network_cut','b_acquired','wal_recovered','b_committed','network_restored','a_stale_commit_rejected','fresh_reader_verified','passed'];
if(sequence.some(name=>!event(name))||sequence.some((name,i)=>i>0&&event(name).elapsed_seconds<event(sequence[i-1]).elapsed_seconds))
  throw new Error('Missing or out-of-order evidence');
if(!event('network_cut').writer_a_alive||!event('network_restored').both_routes_reachable||
  event('fresh_reader_verified').forbidden_rows!==0||!result.stale_commit_error.includes('session failed or expired'))
  throw new Error('Missing liveness, reconnection or rejection evidence');
const W=1920,H=1080,DUR=28;
const metrics={takeover:result.takeover_seconds.toFixed(2),rejection:(result.stale_commit_rejection_seconds*1000).toFixed(2),
  recovered:result.b_recovered.rows.toLocaleString('en-US'),rows:result.fresh_reader.rows.toLocaleString('en-US')};
const BEATS=[
  [0,6,'One database. One writer.'],
  [6,13,'Cut A’s network. Keep A running.'],
  [13,20,'B takes over and recovers the WAL.'],
  [20,28,'A reconnects. Its COMMIT is rejected.']
];
const C={bg:'#080e17',panel:'#111c2b',edge:'#2a3b50',white:'#edf3fa',muted:'#a7b6c9',blue:'#65b5ff',amber:'#e9ba72',green:'#8ad2ac'};
const clamp=x=>Math.max(0,Math.min(1,x));
const smooth=x=>{x=clamp(x);return x*x*(3-2*x)};
function frame(ctx,t){
  t=Math.max(0,Math.min(DUR-.001,t));
  ctx.resetTransform();ctx.globalAlpha=1;ctx.lineCap='round';ctx.lineJoin='round';ctx.setLineDash([]);
  ctx.fillStyle=C.bg;ctx.fillRect(0,0,W,H);
  const bounds=[];
  function text(value,x,y,size=38,color=C.white,weight=500,align='left'){
    ctx.font=`${weight} ${size}px "Inter"`;
    ctx.fillStyle=color;ctx.textAlign=align;ctx.textBaseline='alphabetic';
    const width=ctx.measureText(value).width,x0=align==='center'?x-width/2:align==='right'?x-width:x;
    bounds.push({text:value,x0,y0:y-size,x1:x0+width,y1:y+size*.22});ctx.fillText(value,x,y);
  }
  function box(x,y,w,h,stroke=C.edge,r=24){
    ctx.beginPath();ctx.roundRect(x,y,w,h,r);ctx.fillStyle=C.panel;ctx.fill();
    ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();
  }
  function line(x0,y0,x1,y1,color,width=4,dash=[]){
    ctx.beginPath();ctx.setLineDash(dash);ctx.moveTo(x0,y0);ctx.lineTo(x1,y1);
    ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([]);
  }
  const phase=t<6?0:t<13?1:t<20?2:3;
  text('DUCKDB × TiDB FS · DISCONNECT / RECONNECT',96,66,28,C.muted,600);
  text('ONE LOCAL RUN · MEASURED REPLAY',1824,66,28,C.muted,500,'right');
  text(`${metrics.takeover} s`,96,215,110,C.blue,600);
  text('Takeover after disconnect',100,278,38,C.white);
  text(`${metrics.rejection} ms`,1084,215,110,C.amber,600);
  text('Stale COMMIT rejection',1088,278,38,C.white);
  text('1 s heartbeat · ~5 s session lifetime',100,329,30,C.muted);
  text('After both network routes recovered',1088,329,30,C.muted);
  line(96,362,1824,362,C.edge,2);
  text(BEATS[phase][2],96,432,62,C.white,600);
  const a={x:96,y:500,w:704,h:230},b={x:1120,y:500,w:704,h:230};
  box(a.x,a.y,a.w,a.h,phase===0?C.blue:phase===3?C.amber:C.edge);
  box(b.x,b.y,b.w,b.h,phase>=2?C.blue:C.edge);
  text('Writer A',a.x+36,557,43,C.white,600);
  text('Worker B',b.x+36,557,43,C.white,600);
  const aState=[['Writing','Acknowledged commit in WAL'],['Disconnected','Metadata + S3 cut'],['Expired session','Process still alive'],['COMMIT rejected','Network restored']][phase];
  const bState=[['Waiting','A holds the write lock'],['Waiting','Until expiry + cleanup'],['Current writer',`${metrics.recovered} rows recovered`],['Current writer','Zero stale rows published']][phase];
  text(aState[0],a.x+36,632,49,phase===0?C.blue:C.amber,600);
  text(aState[1],a.x+36,692,36,C.muted);
  text(bState[0],b.x+36,632,49,phase>=2?C.blue:C.muted,600);
  text(bState[1],b.x+36,692,36,C.muted);
  // Logical client access to shared metadata + object storage, not an S3 proxy.
  const cut=phase===1||phase===2;
  line(448,732,768,830,cut?C.amber:phase===0?C.blue:C.muted,4,cut?[12,12]:[]);
  line(1472,732,1152,830,phase>=2?C.blue:C.edge,4);
  if(cut){line(574,754,606,790,C.amber,5);line(574,790,606,754,C.amber,5)}
  if(t>=20&&t<20.6){const p=smooth((t-20)/.6);line(448,732,448+320*p,732+98*p,C.muted,5)}
  const ownerX=448+(1472-448)*smooth((t-13)/.6);
  box(ownerX-126,465,252,44,C.blue,12);
  text('WRITE LOCK',ownerX,496,27,C.blue,600,'center');
  box(640,830,640,128,phase===3?C.green:C.edge);
  text('Shared database',960,879,38,C.white,600,'center');
  text(phase===3?`${metrics.rows} rows verified`:'Same database + WAL',960,929,38,phase===3?C.green:C.muted,500,'center');
  text('1 s cleanup · 200 ms retries · advisory locks',960,1030,32,C.muted,500,'center');
  return bounds;
}
module.exports={W,H,DUR,BEATS,frame,metrics};
