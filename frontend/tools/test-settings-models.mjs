/** Real settings/catalog/store code, deterministic IPC; never touch actual user files/audio. */
import assert from 'node:assert/strict';
import {installMiniDom} from './mini-dom.mjs';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
const dom=installMiniDom();const api=await importShared('tools/test-settings-models-entry.ts',{bundle:true});
let count=0;const test=(name,fn)=>{fn();count++;console.log('PASS '+name);};
let config={stt_provider:'local',asr_model_id:'qwen',translate_tier:'cloud',translate_model:'fixture',lang_pair:'auto-zh'};
let models=[{id:'qwen',name:'Qwen',task:'asr',installed:true},{id:'sensevoice',name:'SenseVoice Small · INT8',task:'asr',installed:false}];
const commands=[],pending=[];let hold=false,fail=false,rejectOperation=false,page;
dom.window.voxsub={backend:{command:async(command,args)=>{
 commands.push({command,args});
 if(rejectOperation&&command===api.CMD.uninstallModel)return {ok:false,error:"fixture operation failure"};
 if(command===api.CMD.listModels){if(hold)return new Promise(resolve=>pending.push(resolve));if(fail)return {ok:false,error:'test model scan failed'};return {ok:true,data:{models:models.map(m=>({...m}))}};}
 if(command===api.CMD.getConfig)return {ok:true,data:{...config}};
 if(command===api.CMD.setConfig){config={...config,...args.updates};return {ok:true,data:{...config}};}
 if(command===api.CMD.translateTiers)return {ok:true,data:{source:'auto',target:'zh',selected:'cloud',effective:'cloud',tiers:[]}};
 if(command===api.CMD.releaseNotes)return {ok:true,data:{notes:[]}};
 if(command===api.CMD.listAudioDevices)return {ok:true,data:{microphones:[],loopbacks:[]}};
 if(command===api.CMD.listCaptureTargets)return {ok:true,data:{targets:[]}};
 return {ok:true,data:{}};
}}};api.markBackendReady();
const settle=()=>dom.flushAsync(14);
const picker=()=>page.element.querySelector('[data-model-task="asr"]');
const choice=()=>picker().querySelector('select');
const complete=(resolve,data=models)=>resolve({ok:true,data:{models:data.map(m=>({...m}))}});
try{
 const select=api.buildSelect('a',[['a','Old']],()=>{throw Error('programmatic update must not fire change');});dom.mount(select);select.focus();api.replaceSelectOptions(select,'b',[['b','<img src=x>']],'Choose');
 test('shared option replacement retains control/focus, selects exact value and treats labels as text',()=>{assert.equal(dom.document.activeElement,select);assert.equal(select.value,'b');assert.equal(select.querySelector('img'),null);assert.equal(select.querySelector('option').textContent,'<img src=x>');});
 api.replaceSelectOptions(select,'missing',[['b','B']],'Unavailable');test('shared options never substitute another model for an unavailable selection',()=>{assert.equal(select.value,'');assert.equal(select.querySelector('option').disabled,true);});
 await api.loadConfig();models[1].installed=true;const reads=commands.filter(c=>c.command===api.CMD.listModels).length;
 page=api.buildSettings();dom.mount(page.element);await settle();
 test('opening settings after an installation refreshes cached catalog and exposes SenseVoice',()=>{assert.ok(commands.filter(c=>c.command===api.CMD.listModels).length>reads);assert.match(picker().textContent,/SenseVoice/);assert.equal(choice().value,'qwen');});
 test('opening refresh does not write config, download or reload it',()=>{assert.equal(commands.filter(c=>c.command===api.CMD.getConfig).length,1);assert.equal(commands.filter(c=>[api.CMD.setConfig,api.CMD.installModel].includes(c.command)).length,0);});
 const input=page.element.querySelector('input'),pane=page.element.querySelector('.settings__panes'),originalSelect=choice();input.value='unsaved draft';input.focus();
 hold=true;dom.window.dispatchEvent(new Event('voxsub:models'));await settle();complete(pending.shift());await settle();
 test('catalog refresh only updates model controls, preserving pane, input text and focus',()=>{assert.equal(page.element.querySelector('.settings__panes'),pane);assert.equal(choice(),originalSelect);assert.equal(page.element.querySelector('input'),input);assert.equal(input.value,'unsaved draft');assert.equal(dom.document.activeElement,input);});
 dom.window.dispatchEvent(new Event('voxsub:models'));await settle();dom.window.dispatchEvent(new Event('voxsub:models'));await settle();const older=pending.shift(),newer=pending.shift();complete(newer,[{id:'fresh',name:'Fresh model',task:'asr',installed:true}]);await settle();complete(older);await settle();
 test('out-of-order scan cannot restore an older model list or falsify saved choice',()=>{assert.match(picker().textContent,/Fresh model/);assert.ok(!picker().textContent.includes('SenseVoice'));assert.equal(choice().value,'');assert.equal(config.asr_model_id,'qwen');});
 hold=false;fail=true;dom.window.dispatchEvent(new Event('voxsub:models'));await settle();
 test('scan failure is explicitly unavailable rather than empty installed-model success',()=>{assert.match(picker().textContent,/模型列表读取失败/);assert.equal(choice().disabled,true);assert.equal(picker().querySelector('button').hidden,false);});
 fail=false;picker().querySelector('button').click();await settle();
 test('retry recovers exact choices and removes stale error',()=>{assert.match(picker().textContent,/SenseVoice/);assert.ok(!picker().textContent.includes('模型列表读取失败'));assert.equal(choice().disabled,false);});
 choice().value='sensevoice';choice().dispatchEvent(dom.makeEvent('change'));await settle();
 test('explicit selection writes only asr_model_id',()=>{const writes=commands.filter(c=>c.command===api.CMD.setConfig);assert.equal(writes.length,1);assert.deepEqual(writes[0].args.updates,{asr_model_id:'sensevoice'});});
 hold=true;dom.window.dispatchEvent(new Event('voxsub:models'));await settle();const oldReply=pending.shift();const old=page,oldRetry=old.element.querySelector(".model-picker .btn");page.dispose();page=api.buildSettings();dom.mount(page.element);await settle();complete(pending.shift(),[{id:'new-page',name:'New page model',task:'asr',installed:true}]);await settle();complete(oldReply);await settle();
 test('disposed page reply cannot overwrite a newer page catalog',()=>{assert.match(picker().textContent,/New page model/);assert.ok(!picker().textContent.includes('SenseVoice'));});
 const activePicker=picker();old.element.querySelector('.settings__tab').click();
 test('disposed navigation cannot clear the active page model bindings',()=>assert.equal(picker(),activePicker));
 hold=false;models=[];dom.window.dispatchEvent(new Event('voxsub:models'));await settle();
 test('uninstall last available model leaves an honest disabled picker and no automatic config save',()=>{assert.equal(choice().disabled,true);assert.match(picker().textContent,/还没有下载/);assert.equal(commands.filter(c=>c.command===api.CMD.setConfig).length,1);});
 let notifications=0;const listener=()=>notifications++;dom.window.addEventListener('voxsub:models',listener);
 dom.document.documentElement.dataset.modelsRoot='C:/models';
 const event={type:'download',modelId:'sensevoice',modelsRoot:'C:/models',token:'one',revision:10,status:'done',completed:100,total:100,stage:'done'};
 api.store.applyEvent(event);api.store.applyEvent({...event,revision:9});api.store.applyEvent({...event,revision:11,modelsRoot:'D:/other'});await settle();
 test('only an accepted completed download invalidates installed models; stale/foreign events do not',()=>assert.equal(notifications,1));
 for(const command of [api.CMD.uninstallModel,api.CMD.importModels,api.CMD.installModel])await api.callWithOutcome(command,{});await settle();
 test('successful uninstall/import/install request paths signal catalog invalidation',()=>assert.equal(notifications,4));
 rejectOperation=true;await api.callWithOutcome(api.CMD.uninstallModel,{});await settle();rejectOperation=false;
 test('failed operation does not pretend the installed catalog changed',()=>assert.equal(notifications,4));
 dom.window.removeEventListener('voxsub:models',listener);
 page.dispose();const before=commands.length;dom.window.dispatchEvent(new Event('voxsub:models'));await settle();
 test('disposed settings remove refresh listeners and stale retry controls cannot issue requests',()=>{oldRetry.click();assert.equal(commands.length,before);});
 api.setLanguage('en');page=api.buildSettings();dom.mount(page.element);await settle();
 test('empty list and refresh states have English UI text without changing model names',()=>assert.match(page.element.textContent,/No local speech|haven.t downloaded|No local recognition|downloaded a local/i));
 console.log('Settings models: '+count+' checks passed');
}finally{for(const resolve of pending)complete(resolve);page?.dispose();await settle();dom.restore();cleanupShared();}
