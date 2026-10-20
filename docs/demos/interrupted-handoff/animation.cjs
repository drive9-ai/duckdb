const result = require('./results.json');
if (!result.complete || result.fresh_reader.sha256 !== result.fresh_reader.expected_sha256)
  throw new Error('A complete, checksum-verified result is required');
const W=1920,H=1080,DUR=34;
const takeover=result.takeover_seconds.toFixed(2), rejection=(result.stale_commit_rejection_seconds*1000).toFixed(2);
const count=result.fresh_reader.rows.toLocaleString('en-US');
const before=result.before_partition.rows.toLocaleString('en-US');
const failed=result.attempts.filter(a=>a.result==='locked').length;
const BEATS=[
  [0,6,'What if the old writer returns?'],
  [6,13,'A loses its network. B waits.'],
  [13,20,`B takes over in ${takeover} s.`],
  [20,27,'A reconnects. COMMIT is rejected.'],
  [27,34,"Connectivity returns. Ownership doesn't."]
];
const C={bg:'#080e17',panel:'#111c2b',edge:'#293a4f',white:'#edf3fa',muted:'#9eafc3',blue:'#65b5ff',amber:'#e9ba72',green:'#8ad2ac'};
const clamp=x=>Math.max(0,Math.min(1,x));
const smooth=x=>{x=clamp(x);return x*x*(3-2*x)};
function frame(ctx,t){
  t=Math.max(0,Math.min(DUR-.001,t));
  ctx.resetTransform();ctx.globalAlpha=1;ctx.lineCap='round';ctx.lineJoin='round';ctx.setLineDash([]);
  ctx.fillStyle=C.bg;ctx.fillRect(0,0,W,H);
  const boxes=[];
  function text(value,x,y,size=38,color=C.white,weight=500,align='left',mono=false,id=value){
    ctx.font=`${weight} ${size}px "${mono?'JetBrains Mono':'Inter'}"`;
    ctx.fillStyle=color;ctx.textAlign=align;ctx.textBaseline='alphabetic';
    const width=ctx.measureText(value).width;
    const x0=align==='center'?x-width/2:align==='right'?x-width:x;
    boxes.push({id,text:value,x0,y0:y-size,x1:x0+width,y1:y+size*.22});
    ctx.fillText(value,x,y);
  }
  function box(x,y,w,h,fill=C.panel,stroke=C.edge,r=24){
    ctx.beginPath();ctx.roundRect(x,y,w,h,r);ctx.fillStyle=fill;ctx.fill();
    ctx.strokeStyle=stroke;ctx.lineWidth=2;ctx.stroke();
  }
  function line(x0,y0,x1,y1,color,width=4,dash=[]){
    ctx.beginPath();ctx.setLineDash(dash);ctx.moveTo(x0,y0);ctx.lineTo(x1,y1);
    ctx.strokeStyle=color;ctx.lineWidth=width;ctx.stroke();ctx.setLineDash([]);
  }
  function badge(value,x,y,color){
    ctx.font='600 28px Inter';const width=ctx.measureText(value).width+40;
    box(x,y,width,46,C.panel,color,12);text(value,x+20,y+32,28,color,600);
  }
  const phase=t<6?0:t<13?1:t<20?2:t<27?3:4;
  text('DUCKDB × TiDB FILE SYSTEM',96,70,30,C.muted,600);
  text('REAL TEST · MEASURED EVENT REPLAY',1824,70,25,C.muted,500,'right');
  text(BEATS[phase][2],96,166,64,C.white,600);
  const progress=clamp((t-BEATS[phase][0])/(BEATS[phase][1]-BEATS[phase][0]));
  for(let i=0;i<5;i++){
    line(96+i*350,207,416+i*350,207,C.edge,5);
    if(i<phase)line(96+i*350,207,416+i*350,207,C.blue,5);
    if(i===phase)line(96+i*350,207,96+i*350+320*progress,207,C.blue,5);
  }
  const ax=96,bx=1056,y=260,cw=768,ch=348;
  box(ax,y,cw,ch);box(bx,y,cw,ch);
  text('Writer A',ax+36,y+60,42,C.white,600);
  text('Worker B',bx+36,y+60,42,C.white,600);
  const astates=[
    ['LIVE · WRITING','COMMIT → acknowledged',`${before} rows • WAL pending`,'1 more row uncommitted'],
    ['LIVE · DISCONNECTED','Process still running','Metadata + S3 routes cut','Transaction still open'],
    ['LIVE · EXPIRED','Old session unusable','Network still disconnected','No close. No process pause.'],
    ['RECONNECTED','INSERT …; COMMIT;','ERROR: session expired',`Rejected in ${rejection} ms · exit 1`],
    ['FAILED · EXIT 1','Old session stays invalid','Must close and reopen','No stale rows published']
  ];
  const bstates=[
    ['WAITING','ATTACH database','Writer lock held by A',''],
    ['LOCKED OUT','ATTACH → locked','Waiting for expiry + cleanup',`${failed} failed attempts`],
    ['CURRENT WRITER','ATTACH → recovered WAL',`${before} rows recovered`,''],
    ['CURRENT WRITER','Lock stays with B',`${count} rows unchanged`,''],
    ['COMMITTED + CLOSED','Fresh reader → checksum OK',`${count} rows • 0 stale rows`,'All ordered rows verified']
  ];
  if(phase===2 && t>=16)bstates[2]=['CURRENT WRITER','COMMIT → acknowledged',`${count} committed rows`,'WAL recovery verified'];
  const a=astates[phase],b=bstates[phase];
  for(const [x,state,color] of [[ax,a,phase===0?C.blue:C.amber],[bx,b,phase===4?C.green:phase>=2?C.blue:C.muted]]){
    text(state[0],x+36,y+120,29,color,600);
    text(state[1],x+36,y+191,36,C.white,500,'left',true);
    text(state[2],x+36,y+251,35,phase===3&&x===ax?C.amber:C.muted);
    text(state[3],x+36,y+308,28,C.muted);
  }
  // The same two routes persist throughout. They stand for client access to
  // logical shared storage; they do not depict S3 bytes proxied by the FS API.
  const disconnected=t>=6&&t<20;
  const aColor=disconnected?C.amber:phase===0?C.blue:C.muted;
  line(480,610,750,716,aColor,4,disconnected?[12,12]:[]);
  line(1440,610,1170,716,phase>=2?C.blue:C.edge,4);
  if(disconnected){
    line(588,635,620,670,C.amber,6);line(588,670,620,635,C.amber,6);
  }
  if(t>=20&&t<21){
    const p=smooth(t-20);line(480,610,480+270*p,610+106*p,C.blue,5);
  }
  if(phase<4){
    const p=smooth((t-13)/.7),x=530+960*p;
    badge('WRITE LOCK',x,272,C.blue);
  }
  const rows=t<16?before:count;
  box(480,716,960,190,C.panel,phase===4?C.green:C.edge,24);
  text('SHARED DATABASE',960,763,29,C.muted,600,'center');
  text(`${rows} committed rows`,960,826,48,phase===4?C.green:C.white,600,'center');
  text(phase===4?'SHA-256 matches the independent expected data':'TiDB metadata + object storage',960,875,29,C.muted,500,'center');
  const note=[
    'Acknowledged commit is durable in the WAL.',
    'Both routes cut. The old process stays alive.',
    'Expiry + cleanup allows the next writer to open.',
    'Working network ≠ valid write ownership.',
    `${count} committed rows. Zero abandoned or stale rows.`
  ][phase];
  text(note,960,969,36,C.white,500,'center');
  text('EXPERIMENTAL · ONE LOCAL RUN · ~60 s SESSION · 1 s CLEANUP SETTING',960,1031,25,C.muted,500,'center');
  return boxes;
}
module.exports={W,H,DUR,BEATS,frame};
