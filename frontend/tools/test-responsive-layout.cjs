const {app,BrowserWindow}=require('electron');
const fs=require('node:fs'),path=require('node:path'),{pathToFileURL}=require('node:url');
const out=process.env.VOXSUB_LAYOUT_OUT,root=process.env.VOXSUB_LAYOUT_ROOT;
app.disableHardwareAcceleration();app.setPath('userData',path.join(out,'electron-profile'));app.on('window-all-closed',()=>{});
let samples=0,violations=0;const results=[],errors=[];
const monitor=setInterval(()=>{samples++;if(BrowserWindow.getAllWindows().some(w=>w.isVisible()||w.isFocused()))violations++;},20);
const wait=(ms=50)=>new Promise(r=>setTimeout(r,ms));
app.whenReady().then(async()=>{
 let win;
 try{
  const css=pathToFileURL(path.join(root,'src/renderer/app.css')).href;
  const script=pathToFileURL(path.join(out,'responsive-entry.js')).href;
  const file=path.join(out,'responsive.html');
  fs.writeFileSync(file,`<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="${css}"><script defer src="${script}"></script></head><body><div id="app"></div></body></html>`);
  const matrix=[[1240,820,1],[1366,768,1.25],[1280,720,1.5],[1280,720,2],[1920,1080,1],[2560,1440,1.5],[3440,1440,1.25],[1024,768,1],[540,960,1],[640,480,1],[360,640,1],[320,240,1]];
  for(const lang of ['zh','en'])for(const theme of ['dark','light']){
   win=new BrowserWindow({width:1240,height:820,show:false,frame:false,webPreferences:{offscreen:true,backgroundThrottling:false,contextIsolation:true,nodeIntegration:false,sandbox:true}});
   win.webContents.on('console-message',(_event,_level,message)=>{if(message.includes('Uncaught'))errors.push(message);});
   await win.loadFile(file,{query:{lang}});await wait(180);await win.webContents.executeJavaScript(`if(document.documentElement.dataset.theme!=="${theme}")document.querySelector(".topbar__actions .btn:last-child").click()`);await wait(80);if(await win.webContents.executeJavaScript(`document.documentElement.dataset.theme`)!==theme)throw Error("Theme fixture did not apply "+theme);
   for(const [width,height,zoom] of matrix){
    win.setSize(width,height);win.webContents.setZoomFactor(zoom);await wait();
    for(const page of ['main-a','main-b','main-c','main-d','catalog','settings','diagnostics']){
     await win.webContents.executeJavaScript(`(()=>{const t=window.layoutTest;document.querySelector('.page__back')?.click();if('${page}'.startsWith('main-')){const mode='${page}'.slice(-1);document.querySelectorAll('.mode-cell').forEach(button=>{if(button.dataset.mode===mode)button.click();});}else t.open('${page}');})()`);
     await wait(35);
     const measurement=await win.webContents.executeJavaScript(`(()=>{
      const layer=document.querySelector('.page-layer');const host=layer&&!layer.hidden?layer:document.querySelector('.shell');
      const overflow=host.scrollWidth>host.clientWidth+2;
      if('${page}'.startsWith('main-')&&window.layoutTest.store.get().mode!=='${page}'.slice(-1))throw Error('Wrong workspace mode ${page}');
      const offenders=[...host.querySelectorAll('*')].filter(el=>{const r=el.getBoundingClientRect();const style=getComputedStyle(el);return r.width>0&&style.position!=='fixed'&&(r.right>innerWidth+2||r.left< -2);}).slice(0,8).map(el=>el.className||el.tagName);
      let headerClearOfChrome=true;
      if(layer&&!layer.hidden){const original=layer.scrollTop;layer.scrollTop=layer.scrollHeight;const button=layer.querySelector('.page__back');const chrome=Math.max(34,parseFloat(getComputedStyle(document.body,'::before').height)||34);headerClearOfChrome=button.getBoundingClientRect().top>=chrome-1;layer.scrollTop=original;}
      return {viewport:[innerWidth,innerHeight],hostWidth:host.clientWidth,scrollWidth:host.scrollWidth,overflow,offenders,headerClearOfChrome};
     })()`);
     results.push({lang,theme,width,height,zoom,page,...measurement});
     if(measurement.overflow||measurement.offenders.length||!measurement.headerClearOfChrome)console.log('FAIL '+JSON.stringify(results.at(-1)));
     if(lang==='zh'&&theme==='dark'&&[[640,480],[3440,1440]].some(([w,h])=>w===width&&h===height)&&['main-a','main-d','catalog'].includes(page))fs.writeFileSync(path.join(out,page+'-'+width+'.png'),(await win.webContents.capturePage()).toPNG());
    }
   }
   win.setSize(640,480);win.webContents.setZoomFactor(1);await win.webContents.executeJavaScript(`window.layoutTest.open('settings')`);await wait();
   for(let tab=0;tab<8;tab++){
    await win.webContents.executeJavaScript(`document.querySelectorAll('.settings__nav .settings__tab')[${tab}].click()`);await wait();
    const m=await win.webContents.executeJavaScript(`(()=>{const h=document.querySelector('.page-layer');return {overflow:h.scrollWidth>h.clientWidth+2,offenders:[],viewport:[innerWidth,innerHeight]};})()`);results.push({lang,theme,width:640,height:480,zoom:1,page:'settings-tab-'+tab,...m});
   }
   for(let tab=0;tab<3;tab++){
    if(tab===0)await win.webContents.executeJavaScript(`window.layoutTest.open('diagnostics')`);
    await win.webContents.executeJavaScript(`document.querySelectorAll('.settings__nav .settings__tab')[${tab}].click()`);await wait();
    if(tab===1)await win.webContents.executeJavaScript(`window.layoutTest.store.pushLog({ts:'2026-10-05T00:00:00+00:00',level:'ERROR',message:'Long diagnostic error: '+ 'X'.repeat(300)});window.layoutTest.refreshLogView()`);
    const m=await win.webContents.executeJavaScript(`(()=>{const h=document.querySelector('.page-layer');return {overflow:h.scrollWidth>h.clientWidth+2,offenders:[],viewport:[innerWidth,innerHeight]};})()`);results.push({lang,theme,width:640,height:480,zoom:1,page:'diagnostic-tab-'+tab,...m});
   }
   // Actual Chromium settings control: failure-free list updates must preserve unrelated drafts/focus.
   const picker=await win.webContents.executeJavaScript(`(async()=>{window.layoutTest.open('settings');await new Promise(r=>setTimeout(r,80));const select=document.querySelector('[data-model-task="asr"] select');window.layoutTest.setInstalled(false);await new Promise(r=>setTimeout(r,80));const disabled=select.disabled;window.layoutTest.setInstalled(true);await new Promise(r=>setTimeout(r,80));return {sameNode:select===document.querySelector('[data-model-task="asr"] select'),disabledWhenUninstalled:disabled,selectedWhenRestored:select.value,found:select.textContent.includes('SenseVoice')};})()`);
   if(!picker.sameNode||!picker.disabledWhenUninstalled||!picker.found||picker.selectedWhenRestored!=='asr-sensevoice-small-int8')throw Error('Chromium model picker '+JSON.stringify(picker));
   fs.writeFileSync(path.join(out,'model-picker-'+lang+'-'+theme+'.json'),JSON.stringify(picker,null,2));
   const draft=await win.webContents.executeJavaScript(`(async()=>{document.querySelectorAll('.settings__nav .settings__tab')[3].click();await new Promise(r=>setTimeout(r,50));const input=document.querySelector('.settings input[type="number"]');input.value='0.42';input.dispatchEvent(new Event('change'));input.focus();window.layoutTest.setInstalled(false);await new Promise(r=>setTimeout(r,80));window.layoutTest.setInstalled(true);await new Promise(r=>setTimeout(r,80));return {sameNode:input===document.querySelector('.settings input[type="number"]'),value:input.value,focused:document.activeElement===input};})()`);
   if(!draft.sameNode||draft.value!=='0.42'||!draft.focused)throw Error('Chromium tuning draft '+JSON.stringify(draft));
   fs.writeFileSync(path.join(out,'tuning-draft-'+lang+'-'+theme+'.json'),JSON.stringify(draft,null,2));
   await win.webContents.executeJavaScript(`document.querySelectorAll('.settings__nav .settings__tab')[0].click()`);await wait(50);

   fs.writeFileSync(path.join(out,'settings-'+lang+'-'+theme+'.png'),(await win.webContents.capturePage()).toPNG());win.destroy();win=null;
  }
  const failed=results.filter(r=>r.overflow||r.offenders.length||r.headerClearOfChrome===false);clearInterval(monitor);
  const summary={checks:results.length,failures:failed.length,samples,visibilityOrFocusViolations:violations,rendererErrors:errors.length};
  fs.writeFileSync(path.join(out,'layout-results.json'),JSON.stringify({summary,results,errors},null,2));
  console.log(JSON.stringify(summary));app.exit(failed.length||violations||errors.length?1:0);
 }catch(error){console.error(error);clearInterval(monitor);win?.destroy();app.exit(2);}
});
