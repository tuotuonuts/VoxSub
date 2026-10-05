/** Hidden native Windows acceptance. Does not load production main/backend or show any window. */
const {app,BrowserWindow}=require('electron');
const {buildSync}=require('esbuild');
const {spawnSync}=require('node:child_process');
const path=require('node:path');
const fs=require('node:fs');
const os=require('node:os');
const assert=require('node:assert/strict');
const Module=require('node:module');
const compiled=buildSync({entryPoints:[path.join(__dirname,'../src/main/overlay-surface.ts')],bundle:true,platform:'node',format:'cjs',write:false}).outputFiles[0].text;
const m=new Module(__filename,module);m._compile(compiled,__filename);const {OverlaySurface}=m.exports;
const profile=fs.mkdtempSync(path.join(os.tmpdir(),'voxsub-region-'));
app.setPath('userData',profile);
app.disableHardwareAcceleration();
app.on('window-all-closed',()=>{}); // Keep the fixture alive across owner replacement.
setTimeout(()=>{console.error('native acceptance timed out');app.exit(1);},45000).unref();
app.commandLine.appendSwitch('mute-audio');
let current;const records=[];
const pause=()=>new Promise(r=>setTimeout(r,80));
function probe(win,points){
 const bytes=win.getNativeWindowHandle(),hwnd=bytes.length===8?bytes.readBigUInt64LE().toString():String(bytes.readUInt32LE());
 const env={...process.env};delete env.PYTHONPATH;delete env.PYTHONHOME;delete env.NODE_OPTIONS;
 const result=spawnSync(process.env.VOXSUB_TEST_PYTHON,[path.join(__dirname,'probe-overlay-region.py'),JSON.stringify({hwnd,points})],{env,windowsHide:true,encoding:'utf8',timeout:10000});
 assert.equal(result.status,0,result.stderr);const value=JSON.parse(result.stdout);assert.equal(value.visible,false);assert.equal(value.foregroundIsFixture,false);return value;
}
app.whenReady().then(async()=>{
 try {
  const surface=new OverlaySurface();
  for(let owner=0;owner<2;owner++){
   current=new BrowserWindow({width:860,height:140,frame:false,transparent:true,show:false,focusable:false,hasShadow:false,resizable:true,webPreferences:{offscreen:true,backgroundThrottling:false,nodeIntegration:false,contextIsolation:true}});
   current.on('show',()=>{throw Error('Fixture must never become visible');});
   await current.loadFile(path.join(__dirname,'../dist/renderer/overlay.html'));
   const baseline=probe(current,[[0,0]]);records.push({owner,phase:'before',...baseline});
   for(const [width,height,zoom] of [[860,140,1],[640,100,1],[860,140,1.25],[860,140,1.5],[860,140,2]]){
    current.setSize(width,height);current.webContents.setZoomFactor(zoom);await pause();
    assert.equal(surface.apply(current,true),true);await pause();
    const inset=Math.ceil(6*zoom),points=[[0,height/2],[inset,inset],[width/2,height/2],[inset+16*zoom,inset+1],[width-1,height/2],[width/2,height-1]];
    const observed=probe(current,points);
    assert.ok(observed.kind>1);assert.deepEqual(observed.points,[false,false,true,true,false,false]);assert.equal(current.isResizable(),true);
    // Chromium pixels are a separate check, not evidence of visible DWM compositing.
    const capture=await current.webContents.capturePage();
    const image={...capture.getSize(),data:capture.toBitmap()};
    assert.equal(image.data.length,image.width*image.height*4);
    const alpha=(x,y)=>image.data[(y*image.width+x)*4+3];
    assert.equal(alpha(0,0),0);assert.ok(alpha(Math.floor(image.width/2),Math.floor(image.height/2))>0);
    records.push({owner,width,height,zoom,...observed,chromium:{width:image.width,height:image.height,cornerAlpha:alpha(0,0),centerAlpha:alpha(Math.floor(image.width/2),Math.floor(image.height/2))}});
   }
   assert.equal(surface.apply(current,false),false);await pause();const disabled=probe(current,[[0,0]]);assert.equal(disabled.kind,0);records.push({owner,phase:'disabled',...disabled});current.destroy();current=null;
  }
  const output={status:'PASS',rendererMode:'software-offscreen',records,visibleDwm:'NOT_RUN',desktopScreenshot:'NOT_RUN',interactiveResize:'NOT_RUN',boundary:'Hidden native HWND region + offscreen Chromium only; no audio, backend, desktop capture or focus.'};
  if(process.env.VOXSUB_TEST_REPORT)fs.writeFileSync(process.env.VOXSUB_TEST_REPORT,JSON.stringify(output,null,2));console.log(JSON.stringify(output));app.exit(0);
 }catch(error){console.error(error);if(process.env.VOXSUB_TEST_REPORT)fs.writeFileSync(process.env.VOXSUB_TEST_REPORT,JSON.stringify({status:'FAIL',error:String(error),records},null,2));app.exit(1);}
 finally{if(current&&!current.isDestroyed())current.destroy();}
});
