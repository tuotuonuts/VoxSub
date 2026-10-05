#!/usr/bin/env node
/** Actual catalog actions and store reducers; no windows, dialogs, HTTP or model changes. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {installMiniDom} from './mini-dom.mjs';
import {importShared,cleanupShared,ROOT} from './esbuild-ts.mjs';
const dom=installMiniDom();
const [logic,catalog]=await importShared(['src/shared/model-download-state.ts','tools/test-catalog-entry.ts'],{bundle:true});
const {mergeDownload,visibleDownload,downloadsForRoot}=logic;
let count=0;const test=(name,fn)=>{fn();count++;console.log('PASS '+name);};
const state=(revision,status='downloading',token='t1')=>({modelId:'model',modelsRoot:'C:/fixture/models',token,revision,completed:50,total:100,status,stage:status==='paused'?'已暂停':'正在下载',source:'china',error:''});
const listeners=[],commands=[],installRequests=[],prepareRequests=[];
const base={id:'model',name:'Fixture',task:'asr',quality:70,sizeLabel:'100 B',sizeBytes:100,installedBytes:0,installed:false,builtin:false,runtime:'x',license:'MIT',languages:'英语',tags:[],description:'fixture',minRamGb:4,gpuSupported:false,igpuSupported:false,npuSupported:false};
let live=state(1,'paused'),installed=false,confirm=false,delayPrepare=false,handle,disconnect,unsubscribe;
const send=s=>{live=s;for(const l of listeners)l({type:'download',...s});};
dom.document.documentElement.dataset.modelsRoot='C:/fixture/alias';
dom.window.confirm=()=>confirm;
dom.window.voxsub={backend:{start:async()=>({ok:true}),onEvent:fn=>{listeners.push(fn);return()=>{};},command:async(command,args)=>{
 commands.push({command,args});
 if(command===catalog.CMD.state)return {ok:true,data:{running:false,paused:false,mode:'a'}};
 if(command===catalog.CMD.listModels)return {ok:true,data:{models:[{...base,installed,download:visibleDownload(live)??null}],modelsRoot:'C:/fixture/models',lookupRoots:[],diagnostics:[]}};
 if(command===catalog.CMD.ocrCacheDir)return {ok:true,data:{path:'C:/cache'}};
 if(command===catalog.CMD.prepareModelDownload){const next=state(live.revision+1,'queued','t'+(prepareRequests.length+2));next.source=args.source;prepareRequests.push(args);const finish=()=>{send(next);return {ok:true,data:{model_id:'model',download:next}};};if(delayPrepare)return new Promise(resolve=>prepareRequests.at(-1).resolve=()=>resolve(finish()));return finish();}
 if(command===catalog.CMD.installModel)return new Promise(resolve=>installRequests.push({args,resolve}));
 if(command===catalog.CMD.pauseModelDownload){const next={...live,revision:live.revision+1,status:'pausing',stage:'正在暂停'};send(next);return {ok:true,data:{model_id:'model',download:next}};}
 if(command===catalog.CMD.deleteModelDownload){assert.equal(args.confirm,true);const next={...live,revision:live.revision+1,status:'deleted',completed:0};send(next);return {ok:true,data:{model_id:'model',download:next}};}
 throw Error('Unexpected '+command);
}}};
const buttons=()=>[...handle.element.querySelectorAll('.cell__action')];
const labels=()=>buttons().map(b=>b.textContent);
const press=label=>buttons().find(b=>b.textContent===label).click();
try{
 test('stale progress cannot undo a newer pause',()=>{const current={model:state(5,'paused')};assert.equal(mergeDownload(current,'model',state(4),'C:/fixture/models'),current);});
 test('late deleted/done events cannot override a newer generation',()=>{const current={model:state(8,'downloading','t2')};assert.equal(mergeDownload(current,'model',state(7,'deleted'),'C:/fixture/models'),current);});
 test('same Windows root matches case and separator differences',()=>assert.ok(mergeDownload({},'model',state(1),'c:\\FIXTURE\\models\\').model));
 test('foreign roots never change current model state',()=>{const current={};assert.equal(mergeDownload(current,'model',state(1),'D:/other'),current);assert.deepEqual(downloadsForRoot({model:state(1)},'D:/other'),{});});
 test('terminal snapshots are retained as race tombstones but have no visible progress',()=>{assert.equal(visibleDownload(state(3,'done')),undefined);assert.equal(visibleDownload(state(4,'deleted')),undefined);assert.ok(visibleDownload({completed:1,total:2,stage:'legacy'}));});
 disconnect=catalog.connectBackend();listeners.forEach(l=>l({type:'ready'}));await dom.flushAsync(8);
 handle=catalog.buildModelCatalog();dom.mount(handle.element);await dom.flushAsync(12);
 unsubscribe=catalog.store.subscribe(()=>catalog.refreshDownloads());
 test('list canonicalizes runtime root aliases without writing saved config',()=>{assert.equal(dom.document.documentElement.dataset.modelsRoot,'C:/fixture/models');assert.ok(commands.every(c=>c.command!==catalog.CMD.setConfig));});
 test('restored paused download shows progress and Continue/Delete, no automatic install',()=>{assert.deepEqual(labels(),['继续','删除']);assert.ok(handle.element.querySelector('.progress__track.is-paused'));assert.equal(installRequests.length,0);assert.match(handle.element.querySelector('.cell__progress-label').textContent,/已暂停.*50%/);});
 const failureText='GitHub 全球源：HTTP 404：下载地址不存在。请更换下载源 <img src=x>';send({...live,revision:live.revision+1,error:failureText});await dom.flushAsync(4);
 test('paused card shows actionable backend cause using the shared field without hiding Continue/Delete',()=>{assert.deepEqual(labels(),['继续','删除']);assert.equal(handle.element.querySelector('.field__label').textContent,'下载原因');assert.equal(handle.element.querySelector('.field__hint').textContent,failureText);assert.equal(handle.element.querySelector('.field__hint').children.length,0);assert.equal(handle.element.querySelector('img[src="x"]'),null);});
 send({...live,revision:live.revision+1,error:''});await dom.flushAsync(4);
 test('clearing a failure removes stale reason and preserves the visible shared progress track',()=>{assert.equal(handle.element.querySelector('.field__hint'),null);assert.equal(handle.element.querySelector('.progress__track').parentElement,handle.element.querySelector('.cell__progress'));});
 send({...live,revision:live.revision+1,total:200,completed:100});await dom.flushAsync(4);
 test('uncompressed source progress uses its transfer total rather than the model card archive size',()=>{assert.match(handle.element.querySelector('.cell__progress-label').textContent,/50%/);assert.equal(handle.element.querySelector('.progress__fill').style.width,'50%');});
 send({...live,revision:live.revision+1,total:100,completed:50});await dom.flushAsync(4);
 delayPrepare=true;press('继续');press('暂停');await dom.flushAsync(5);
 test('preparation disables pause and repeated clicks do not submit duplicates',()=>{assert.equal(prepareRequests.length,1);assert.equal(buttons()[0].disabled,true);assert.equal(installRequests.length,0);});
 prepareRequests[0].resolve();await dom.flushAsync(8);delayPrepare=false;
 test('Continue uses durable selected source and token to start the worker',()=>{assert.equal(prepareRequests[0].source,'china');assert.equal(installRequests[0].args.token,live.token);assert.deepEqual(labels(),['暂停']);});
 press('暂停');await dom.flushAsync(8);
 test('pause request stays Pausing and disables actions until writer stops',()=>{assert.deepEqual(labels(),['正在暂停']);assert.equal(buttons()[0].disabled,true);assert.equal(commands.filter(c=>c.command===catalog.CMD.pauseModelDownload).length,1);});
 const firstPaused={...live,status:'paused',revision:live.revision+1,stage:'已暂停'};send(firstPaused);await dom.flushAsync(4);
 test('writer confirmation enables Continue/Delete',()=>assert.deepEqual(labels(),['继续','删除']));
 const region=handle.element.querySelector('[data-download-source]');region.value='global';region.dispatchEvent(dom.makeEvent('change'));
 press('继续');await dom.flushAsync(8);const newestToken=live.token;
 test('explicit source change during pause overrides the saved preference',()=>assert.equal(prepareRequests.at(-1).source,'global'));
 installRequests[0].resolve({ok:true,data:{model_id:'model',download:firstPaused}});await dom.flushAsync(12);
 test('late old install reply cannot erase a resumed task',()=>{assert.equal(catalog.store.get().downloads.model.token,newestToken);assert.deepEqual(labels(),['暂停']);});
 send({...firstPaused,revision:1});await dom.flushAsync(4);
 test('late old progress event cannot replace resumed buttons',()=>assert.deepEqual(labels(),['暂停']));
 send({...live,revision:live.revision+10,status:'paused',stage:'已暂停'});await dom.flushAsync(4);
 press('删除');await dom.flushAsync(4);
 test('declining delete sends no destructive command',()=>assert.equal(commands.filter(c=>c.command===catalog.CMD.deleteModelDownload).length,0));
 confirm=true;press('删除');await dom.flushAsync(12);
 test('confirmed delete sends only partial-download deletion and returns Download button',()=>{assert.deepEqual(labels(),['下载']);assert.equal(commands.filter(c=>c.command===catalog.CMD.deleteModelDownload).length,1);assert.equal(commands.filter(c=>c.command===catalog.CMD.uninstallModel).length,0);assert.equal(handle.element.querySelector('.cell__progress'),null);});
 press('下载');await dom.flushAsync(8);const done={...live,revision:live.revision+1,status:'done',completed:100};installed=true;send(done);await dom.flushAsync(12);
 test('completion event reconciles Use/Uninstall even when the long request reply is missing',()=>{assert.deepEqual(labels(),['使用','卸载']);assert.equal(handle.element.querySelector('.cell__progress'),null);});
 installed=false;live=state(100,'paused','final');await catalog.loadModels();const old=handle;const oldContinue=buttons()[0];handle=catalog.buildModelCatalog();dom.mount(handle.element);await dom.flushAsync(8);old.dispose();const before=commands.length;oldContinue.click();
 test('stale download controls on a disposed page do not send commands',()=>assert.equal(commands.length,before));
 test('download UI reuses shared buttons/progress and has no bespoke styling primitive',()=>{const text=fs.readFileSync(ROOT+'/src/renderer/views/catalog.ts','utf8');assert.match(text,/buildProgressBar/);assert.match(text,/const button = buildButton/);assert.match(text,/runAfterConfirm/);});
 test('column layout preserves a visible shared progress track',()=>assert.match(fs.readFileSync(ROOT+'/src/renderer/app.css','utf8'),/\.cell__progress > \.progress__track\s*\{[^}]*flex: 0 0 0\.2308rem/s));
 console.log(`Model downloads: ${count} checks passed`);
}finally{for(const request of installRequests)request.resolve({ok:false,error:'test teardown'});unsubscribe?.();handle?.dispose();disconnect?.();await dom.flushAsync(6);dom.restore();cleanupShared();}
