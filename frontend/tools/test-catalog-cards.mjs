#!/usr/bin/env node
/** Actual shared components and production catalog handlers; no windows, links opened or downloads. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { transformSync } from 'esbuild';
import { installMiniDom } from './mini-dom.mjs';
import { importShared,cleanupShared,ROOT } from './esbuild-ts.mjs';
const dom=installMiniDom();const {document,window}=dom;
const [{buildBadge},{buildRating},{buildRepositoryLink},{repositoryTarget},catalog]=await importShared([
  'src/renderer/ui/badge.ts','src/renderer/ui/rating.ts','src/renderer/ui/repository-link.ts',
  'src/shared/repository-target.ts','tools/test-catalog-entry.ts',
],{bundle:true});
let count=0;const test=(name,fn)=>{fn();count++;console.log('PASS '+name);};
const moon={ id:'asr-moonshine-tiny-en-v2',name:'Moonshine Tiny EN · 轻量离线',task:'asr',quality:77,sizeLabel:'28 MB',sizeBytes:29858559,installedBytes:0,installed:false,builtin:false,
  runtime:'sherpa-moonshine-v2',license:'MIT',languages:'英语',description:'把英语语音转成文字，体积小、占用少，适合日常短句和轻薄本；说完一句后出结果，不支持中文。',
  gpuSupported:false,igpuSupported:false,npuSupported:false,minRamGb:4,tags:['轻量省资源','短句识别','离线使用'],officialRepo:'https://github.com/moonshine-ai/moonshine',recommendation:{level:'recommended',loadPercent:10,reason:'CPU 预计负载 10%'}};
let items=[moon];const opened=[],commands=[],listeners=[];
window.voxsub={dialog:{openExternal:async url=>{opened.push(url);return true;}},backend:{start:async()=>({ok:true}),onEvent:fn=>{listeners.push(fn);return()=>{};},command:async(command,args)=>{
  commands.push({command,args});if(command===catalog.CMD.listModels)return {ok:true,data:{models:items,modelsRoot:'fixture:/models',lookupRoots:[],diagnostics:[]}};
  if(command===catalog.CMD.ocrCacheDir)return {ok:true,data:{path:'fixture:/cache'}};
  if(command===catalog.CMD.state)return {ok:true,data:{running:false,paused:false,mode:'a'}};
  throw Error('Unexpected command '+command);
}}};
const disconnect=catalog.connectBackend();for(const l of listeners)l({type:'ready'});await dom.flushAsync(8);
let handle;
try{
 test('common badge semantic tone and plain text prevent HTML interpretation',()=>{const b=buildBadge('<test>','attention','why');assert.equal(b.className,'badge badge--attention');assert.equal(b.textContent,'<test>');assert.equal(b.getAttribute('title'),'why');});
 test('rating has visible capability label, five dots and accessible numeric explanation',()=>{const r=buildRating(77,'能力','同类参考');assert.equal(r.querySelector('.rating__label').textContent,'能力');assert.equal(r.querySelectorAll('.pip').length,5);assert.equal(r.querySelectorAll('.is-on').length,4);assert.match(r.getAttribute('aria-label'),/4\/5/);assert.match(r.getAttribute("title"),/4\/5/);});
 test('rating bounds and invalid values cannot imply a full rating',()=>{assert.equal(buildRating(120,'能力','hint').querySelectorAll('.is-on').length,5);assert.match(buildRating(NaN,'能力','hint').getAttribute('aria-label'),/—\/5/);assert.equal(buildRating(-1,'能力','hint').querySelectorAll('.is-on').length,0);});
 test('repository target allows exactly upstream HTTPS roots, rejects malicious/mirror/download URLs',()=>{
  for(const v of ['javascript:alert(1)','https://github.com.evil.test/org/repo','https://hf-mirror.com/org/repo','https://github.com/org/repo/releases/download/x','https://user@github.com/org/repo','http://github.com/org/repo','https://github.com/org/repo?x=1','https://github.com/org/repo#test'])assert.equal(repositoryTarget(v),null,v);
  assert.equal(repositoryTarget(moon.officialRepo).provider,'github');assert.equal(repositoryTarget('https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3').provider,'huggingface');
 });
 test('provider icons are local and accessible; navigation is delegated and prevented',()=>{const opened=[];const a=buildRepositoryLink(moon.officialRepo,'官方仓库',u=>opened.push(u));assert.ok(a.querySelector('.repository-link__github'));assert.match(a.querySelector('span').style.getPropertyValue('--repository-icon'),/data:image\/svg\+xml/);assert.match(a.getAttribute('aria-label'),/GitHub/);const event=dom.makeEvent('click');a.dispatchEvent(event);assert.equal(event.defaultPrevented,true);assert.deepEqual(opened,[moon.officialRepo]);assert.equal(buildRepositoryLink('ftp://example.com','仓库',()=>{}),null);const hf=buildRepositoryLink('https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3','仓库',()=>{});assert.equal(hf.textContent,'🤗');assert.equal(hf.getAttribute('data-provider'),'huggingface');});
 handle=catalog.buildModelCatalog();document.body.append(handle.element);await dom.flushAsync(12);
 const cell=()=>handle.element.querySelector('.cell');
 test('Moonshine title, task and model size remain exactly unchanged',()=>{assert.equal(cell().querySelector('.cell__name').textContent,moon.name);assert.equal(cell().querySelector('.cell__task').textContent,'识别');assert.equal(cell().querySelector('.cell__size').textContent,'28 MB');assert.equal(cell().querySelector('.cell__no'),null);});
 test('plain summary and tags replace runtime, license and unsupported hardware strings',()=>{assert.equal(cell().querySelector('.cell__desc').textContent,moon.description);assert.equal(cell().querySelector('.cell__facts').textContent,'英语轻量省资源短句识别离线使用');for(const s of ['sherpa-moonshine-v2','MIT','不支持 NPU','验证'])assert.ok(!cell().textContent.includes(s));assert.equal(cell().querySelector('.rating__label').textContent,'能力');});
 test('model official repository link passes verified URL to opener without changing the model',()=>{cell().querySelector('.repository-link').click();assert.deepEqual(opened,[moon.officialRepo]);assert.ok(!commands.some(c=>[catalog.CMD.installModel,catalog.CMD.uninstallModel,catalog.CMD.setAsrModel].includes(c.command)));});
 const tiers=[['basic','基础款','neutral'],['recommended','推荐','positive'],['elevated','中高负载','attention'],['heavy','高负载','danger'],['insufficient','配置不足','neutral'],['unknown','待评估','neutral']];
 for(const [level,label,tone] of tiers){items=[{...moon,recommendation:{level,loadPercent:40,reason:'fixture reason'}}];await catalog.loadModels();test('production device rating '+level,()=>{const b=cell().querySelector('.cell__recommendation');assert.equal(b.textContent,label);assert.ok(b.classList.contains('badge--'+tone));assert.match(b.getAttribute("title"),/本机配置估算/);});}
 items=[{...moon,installed:true,installedBytes:44}];await catalog.loadModels();
 test('downloaded has whole-card state plus explicit downloaded badge; actions remain',()=>{assert.ok(cell().classList.contains('is-installed'));assert.equal(cell().querySelector('.cell__installed').textContent,'✓ 已下载');assert.deepEqual([...cell().querySelectorAll('.cell__action')].map(b=>b.textContent),['使用','卸载']);});
 document.documentElement.dataset.activeModels=moon.id;await catalog.loadModels();test('selected downloaded model still clearly reports downloaded and in-use',()=>{assert.equal(cell().querySelector('.cell__state').textContent,'使用中');assert.ok(cell().querySelector('.cell__installed'));});
 items=[{...moon,officialRepo:'https://hf-mirror.com/a/b',recommendation:undefined}];await catalog.loadModels();test('missing device evidence or invalid upstream never fabricates positive recommendations or links',()=>{assert.equal(cell().querySelector('.cell__recommendation').textContent,'待评估');assert.equal(cell().querySelector('.repository-link'),null);});
 items=[{...moon,id:'speech-seamless-streaming',task:'speech',name:'SeamlessStreaming',installed:true,externalRuntime:'Linux / WSL',usageUrl:'https://github.com/facebookresearch/seamless_communication/blob/main/src/seamless_communication/cli/streaming/README.md'}];await catalog.loadModels();
 test('external speech model remains downloadable but has guide instead of in-app use',()=>{
   assert.deepEqual([...cell().querySelectorAll('.cell__action')].map(b=>b.textContent),['卸载']);
   const guide=[...cell().querySelectorAll('button')].find(b=>b.textContent==='官方运行说明');assert.ok(guide);guide.click();assert.equal(opened.at(-1),items[0].usageUrl);
 });
 const i18n=fs.readFileSync(ROOT+'/src/renderer/i18n.ts','utf8').replace(/^import .*;$/m,'');const tr=vm.runInNewContext(transformSync(i18n+'\ntr;',{loader:'ts',format:'cjs'}).code,{module:{exports:{}},store:{get:()=>({lang:'en',theme:'dark'})}});
 test('all new visible summary and tags resolve in English',()=>{for(const text of [moon.description,...moon.tags,...tiers.map(t=>t[1]),'能力','已下载','模型官方仓库','使用'])assert.notEqual(tr(text),text,text);});
 test('new public components have no store, business, translation or IPC dependencies',()=>{
  for(const name of ['badge','rating','repository-link']){
   const source=fs.readFileSync(ROOT+'/src/renderer/ui/'+name+'.ts','utf8');
   assert.doesNotMatch(source,/window\.voxsub|from\s+["'][^"']*(?:store|protocol|views|i18n)["']/);
  }
 });
 test('orange and downloaded CSS use theme tokens, accessible focus and full borders',()=>{const css=fs.readFileSync(ROOT+'/src/renderer/app.css','utf8');assert.match(css,/\.badge--attention\s*\{\s*color: var\(--attention\)/);assert.match(css,/\.cell\.is-installed\s*\{\s*border-color: var\(--accent\)/);assert.match(css,/\.repository-link:focus-visible/);});
 console.log(`Catalog cards: ${count} checks passed`);
}finally{handle?.dispose();disconnect();dom.restore();cleanupShared();}
