const fs=require('fs'),path=require('path'),crypto=require('crypto'),{spawn}=require('child_process');
const {once}=require('events'),{createRequire}=require('module');
let canvasLib;
try{canvasLib=require('@napi-rs/canvas')}catch(error){
  if(!process.env.CANVAS_NODE_MODULES)throw error;
  canvasLib=createRequire(path.join(process.env.CANVAS_NODE_MODULES,'_loader.cjs'))('@napi-rs/canvas');
}
const {createCanvas,GlobalFonts}=canvasLib;
for(const [file,name] of [['Inter.ttf','Inter'],['JetBrainsMono.ttf','JetBrains Mono']])
  if(!GlobalFonts.registerFromPath(path.join(__dirname,'fonts',file),name))throw new Error('Font unavailable: '+file);
const {W,H,DUR,BEATS,frame}=require('./animation.cjs');
const canvas=createCanvas(W,H),ctx=canvas.getContext('2d'),out=__dirname;
fs.mkdirSync(path.join(out,'qa'),{recursive:true});
const times=[0,3,5.9,6,6.2,9,12.9,13,13.2,13.6,15,16,18,20,20.2,20.8,22,26.8,27,30,33.9];
const hash=t=>{frame(ctx,t);return crypto.createHash('sha256').update(canvas.toBuffer('image/png')).digest('hex')};
const hashes=times.map(t=>[t,hash(t)]);
for(const [t,expected] of hashes.slice().reverse())if(hash(t)!==expected)throw new Error('Non-deterministic frame '+t);
let minMargin=Infinity;
const visible=new Map(),spans=[];
for(let n=0;n<=DUR*20;n++){
  const t=n/20,boxes=n===DUR*20?[]:frame(ctx,t),seen=new Set();
  for(const box of boxes){
    if(box.x0<0||box.y0<0||box.x1>W||box.y1>H)throw new Error('Clipped text '+JSON.stringify(box));
    minMargin=Math.min(minMargin,box.x0,W-box.x1,box.y0,H-box.y1);
    if(!box.text)continue;
    seen.add(box.text);if(!visible.has(box.text))visible.set(box.text,t);
  }
  for(const [text,start] of visible)if(!seen.has(text)){
    const duration=t-start,minimum=text.replace(/\s/g,'').length/15+1.5;
    spans.push({text,start,duration,minimum});
    if(duration+.05<minimum)throw new Error('Text too brief: '+JSON.stringify(spans.at(-1)));
    visible.delete(text);
  }
}
for(const [a,b,title] of BEATS)
  if(b-a<title.replace(/\s/g,'').length/15+1.5)throw new Error('Title too brief: '+title);
for(const t of times){frame(ctx,t);fs.writeFileSync(path.join(out,'qa',`frame-${t}.png`),canvas.toBuffer('image/png'))}
frame(ctx,23);fs.writeFileSync(path.join(out,'poster.png'),canvas.toBuffer('image/png'));
function stamp(t){const s=Math.floor(t);return `00:${String(Math.floor(s/60)).padStart(2,'0')}:${String(s%60).padStart(2,'0')},000`}
fs.writeFileSync(path.join(out,'captions.srt'),BEATS.map((b,i)=>`${i+1}\n${stamp(b[0])} --> ${stamp(b[1])}\n${b[2]}\n`).join('\n'));
fs.writeFileSync(path.join(out,'qa/validation.json'),JSON.stringify({dimensions:[W,H],seconds:DUR,fps:24,determinism:hashes,
  titleReadingTimes:'passed',allTextReadingTimes:spans,textBounds:'passed',minimumMargin:minMargin,audio:'intentionally silent measured-event replay'},null,2));
if(process.argv.includes('--stills')){console.log('Stills, bounds, title timing and determinism passed.');process.exit(0)}
async function main(){
  const ff=spawn('ffmpeg',['-hide_banner','-loglevel','warning','-y','-f','rawvideo','-pixel_format','rgba','-video_size',`${W}x${H}`,
    '-framerate','24','-i','pipe:0','-an','-c:v','libx264','-preset','fast','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',
    '-colorspace','bt709','-color_primaries','bt709','-color_trc','bt709',path.join(out,'interrupted-handoff.mp4')],{stdio:['pipe','inherit','inherit']});
  let failed;const done=new Promise((resolve,reject)=>{ff.on('error',reject);ff.on('close',c=>c===0?resolve():reject(new Error('ffmpeg '+c)));ff.stdin.on('error',e=>{failed=e})});
  for(let n=0;n<DUR*24;n++){
    if(failed)throw failed;
    frame(ctx,n/24);const pixels=ctx.getImageData(0,0,W,H);
    if(!ff.stdin.write(Buffer.from(pixels.data.buffer)))await once(ff.stdin,'drain');
    if(n%144===0)console.log(`Rendered ${n/24}s / ${DUR}s`);
  }
  ff.stdin.end();await done;console.log('MP4 complete.');
}
main().catch(error=>{console.error(error);process.exit(1)});
