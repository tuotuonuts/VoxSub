#!/usr/bin/env node
/** Search logic, IME-safe shared controls and actual catalog handlers. No windows or downloads. */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { installMiniDom } from './mini-dom.mjs';
import { importShared, cleanupShared, ROOT } from './esbuild-ts.mjs';
const dom = installMiniDom();
const [logic, {buildSearchField}, catalog] = await importShared([
 'src/shared/model-search.ts', 'src/renderer/ui/search-field.ts', 'tools/test-catalog-entry.ts',
], {bundle:true});
let count=0;
const test=(name,fn)=>{fn();count++;console.log('PASS '+name);};
const {createSearchDocument:doc,searchModels:search,normalizeSearch:normalize}=logic;
const docs=[doc('name',{names:['Moonshine Tiny EN'],tags:['英语','轻量省资源'],descriptions:['离线使用']}),
 doc('tag',{names:['Other'],tags:['Moonshine','英语'],descriptions:['English offline']}),
 doc('description',{names:['Third'],tags:['中文'],descriptions:['Moonshine']}),
 doc('tie',{names:['Moonshine Tiny EN'],tags:['英语'],descriptions:[]})];
const ids=q=>search(docs,q);
let handle,disconnect;
try {
 test('name outranks tag and description; ties preserve catalog order',()=>assert.deepEqual(ids('Moonshine'),['name','tie','tag','description']));
 test('partial name and case-insensitive search',()=>assert.deepEqual(ids('MOONSH'),['name','tie','tag','description']));
 test('full width, accents and punctuation normalize',()=>assert.equal(normalize('ＭＯＯＮ Café-EN'), 'moon cafe en'));
 test('punctuation-insensitive model ID',()=>assert.deepEqual(search([doc('id',{names:['asr-moonshine-tiny-en-v2'],tags:[]})],'moonshine-tiny'),['id']));
 test('adjacent-letter transposition tolerated',()=>assert.deepEqual(ids('moonshnie'),['name','tie','tag','description']));
 test('single missing letter tolerated',()=>assert.deepEqual(ids('moonshne'),['name','tie','tag','description']));
 test('short words never use ambiguous typo correction',()=>assert.deepEqual(ids('em'),[]));
 test('Chinese partial matches but not Chinese typos',()=>{assert.deepEqual(ids('轻量'),['name']);assert.deepEqual(ids('轻凉'),[]);});
 test('AND keywords cross name and tag fields',()=>assert.deepEqual(ids('Moonshine 英语'),['name','tie','tag']));
 test('AND keywords can cross tags',()=>assert.deepEqual(ids('英语 轻量'),['name']));
 test('a missing keyword excludes the result',()=>assert.deepEqual(ids('Moonshine 德语'),[]));
 test('quoted phrase stays together and accepts duplicate keywords',()=>assert.deepEqual(ids('"Tiny EN" tiny'),['name','tie']));
 test('name scope excludes tag and description',()=>assert.deepEqual(search(docs,'Moonshine','name'),['name','tie']));
 test('tag scope excludes name and description',()=>assert.deepEqual(search(docs,'Moonshine','tags'),['tag']));
 test('empty and punctuation-only query preserve input order',()=>{assert.deepEqual(ids(''),docs.map(d=>d.value));assert.deepEqual(ids('[]*'),docs.map(d=>d.value));});
 test('query is bounded, malicious-looking text is literal',()=>{assert.deepEqual(ids('x'.repeat(10000)),[]);assert.deepEqual(ids('<img onerror=alert(1)>'),[]);});
 test('search never mutates cached documents',()=>{const before=JSON.stringify(docs);ids('moonshnie 英语');assert.equal(JSON.stringify(docs),before);});
 const seen=[];const field=buildSearchField('',{label:'搜索',placeholder:'名字/标签',clear:'清空'},q=>seen.push(q));
 dom.document.body.append(field.element);
 const emit=(type,init)=>field.input.dispatchEvent(dom.makeEvent(type,init));
 test('accessible local search with no automatic focus',()=>{assert.equal(field.input.getAttribute('aria-label'),'搜索');assert.equal(field.input.getAttribute('maxlength'),'160');assert.ok(field.element.querySelector('button').hidden);assert.notEqual(dom.document.activeElement,field.input);});
 test('input immediately emits once and silent setValue does not emit',()=>{field.input.value='moon';emit('input');emit('input');field.setValue('英语');assert.deepEqual(seen,['moon']);});
 test('IME defers partial input and emits final text only once',()=>{emit('compositionstart');field.input.value='英';emit('input',{isComposing:true});assert.deepEqual(seen,['moon']);field.input.value='英语';emit('compositionend');emit('input');assert.deepEqual(seen,['moon']);field.input.value='中文';emit('compositionstart');emit('input');emit('compositionend');emit('input');assert.deepEqual(seen,['moon','中文']);});
 test('Escape while composing does not clear or prevent IME cancellation',()=>{emit('compositionstart');const e=dom.makeEvent('keydown',{key:'Escape',isComposing:true});let stopped=false;e.stopPropagation=()=>{stopped=true;};field.input.dispatchEvent(e);assert.ok(stopped);assert.equal(e.defaultPrevented,false);assert.equal(field.input.value,'中文');emit('compositionend');});
 test('Escape clears text, prevents page escape and restores input focus',()=>{const e=dom.makeEvent('keydown',{key:'Escape'});let stopped=false;e.stopPropagation=()=>{stopped=true;};field.input.dispatchEvent(e);assert.ok(e.defaultPrevented&&stopped);assert.equal(field.input.value,'');assert.equal(dom.document.activeElement,field.input);assert.equal(seen.at(-1),'');});
 test('clear button updates visibility and query',()=>{field.input.value='tiny';emit('input');const clear=field.element.querySelector('button');assert.equal(clear.hidden,false);clear.click();assert.equal(clear.hidden,true);assert.equal(seen.at(-1),'');});
 test('disposed controls cannot emit or focus',()=>{field.dispose();const old=seen.length;field.input.value='old';emit('input');field.element.querySelector('button').click();assert.equal(seen.length,old);});
 const base={quality:70,sizeLabel:'28 MB',sizeBytes:10,installedBytes:0,builtin:false,runtime:'x',license:'MIT',gpuSupported:false,igpuSupported:false,npuSupported:false,minRamGb:4};
 let items=[{...base,id:'moon',name:'Moonshine Tiny',task:'asr',languages:'英语',tags:['轻量省资源','离线使用'],description:'把英语语音转成文字，体积小、占用少，适合日常短句和轻薄本；说完一句后出结果，不支持中文。',installed:true},
 {...base,id:'other',name:'Other',task:'translate',languages:'中文',tags:['离线使用'],description:'翻译',installed:false}];
 const commands=[],listeners=[];
 dom.window.voxsub={backend:{start:async()=>({ok:true}),onEvent:fn=>{listeners.push(fn);return()=>{};},command:async(command,args)=>{commands.push({command,args});if(command===catalog.CMD.listModels)return {ok:true,data:{models:items,modelsRoot:'fixture:/models',lookupRoots:[],diagnostics:[]}};if(command===catalog.CMD.ocrCacheDir)return {ok:true,data:{path:'fixture:/cache'}};if(command===catalog.CMD.state)return {ok:true,data:{running:false,paused:false,mode:'a'}};throw Error('Unexpected '+command);}}};
 disconnect=catalog.connectBackend();listeners.forEach(l=>l({type:'ready'}));await dom.flushAsync(8);
 handle=catalog.buildModelCatalog();dom.mount(handle.element);await dom.flushAsync(10);
 const page=()=>handle.element;
 const input=()=>page().querySelector('input[type="search"]');
 const setQuery=q=>{input().value=q;input().dispatchEvent(dom.makeEvent('input'));};
 const shown=()=>[...page().querySelectorAll('.cell')].map(c=>c.getAttribute('data-id'));
 const scope=v=>{const s=page().querySelector('[data-search-scope]');s.value=v;s.dispatchEvent(dom.makeEvent('change'));};
 const toggle=v=>{const s=page().querySelector('[data-installed-filter]');s.checked=v;s.dispatchEvent(dom.makeEvent('change'));};
 const task=label=>[...page().querySelectorAll('.filter-chip')].find(b=>b.textContent===label).click();
 test('production page indexes names and fuzzy search',()=>{setQuery('moonshnie');assert.deepEqual(shown(),['moon']);assert.equal(page().querySelector('.catalog__count').textContent,'1 / 2 项');});
 test('production tags and English aliases work across interface language',()=>{setQuery('英语 轻量');assert.deepEqual(shown(),['moon']);setQuery('English');assert.deepEqual(shown(),['moon']);});
 test('production scope distinguishes names from tags',()=>{scope('name');assert.deepEqual(shown(),[]);scope('tags');assert.deepEqual(shown(),['moon']);scope('all');});
 test('production description is searchable',()=>{setQuery('轻薄本');assert.deepEqual(shown(),['moon']);});
 test('downloaded filter intersects search and use filters',()=>{setQuery('');toggle(true);assert.deepEqual(shown(),['moon']);task('翻译');assert.deepEqual(shown(),[]);task('识别');assert.deepEqual(shown(),['moon']);toggle(false);});
 test('repeated use filter switches replace visible active bar correctly',()=>{task('翻译');task('识别');task('全部');assert.deepEqual(shown(),['moon','other']);const active=page().querySelector('.filter-chip.is-active');assert.equal(active.textContent,'全部');});
 test('empty result reset preserves download source and sends no commands',()=>{const s=page().querySelector('[data-download-source]');s.value='china';s.dispatchEvent(dom.makeEvent('change'));setQuery('notfound');toggle(true);scope('tags');const before=commands.length;page().querySelector('.empty-state button').click();assert.deepEqual(shown(),['moon','other']);assert.equal(input().value,'');assert.equal(s.value,'china');assert.equal(commands.length,before);});
 setQuery('Moonshine');const beforeRefresh=commands.length;await catalog.loadModels();
 test('refresh preserves search and its cached documents; only normal list/cache IPC',()=>{assert.deepEqual(shown(),['moon']);assert.equal(input().value,'Moonshine');assert.deepEqual(commands.slice(beforeRefresh).map(c=>c.command),[catalog.CMD.listModels,catalog.CMD.ocrCacheDir]);});
 const old=handle,oldInput=input(),oldScope=page().querySelector('[data-search-scope]'),oldTask=page().querySelector('.filter-chip');
 handle=catalog.buildModelCatalog();dom.mount(handle.element);await dom.flushAsync(10);old.dispose();
 test('old page events and delayed disposal cannot alter new page search',()=>{oldInput.value='Other';oldInput.dispatchEvent(dom.makeEvent('input'));oldScope.value='tags';oldScope.dispatchEvent(dom.makeEvent('change'));oldTask.click();assert.equal(input().value,'Moonshine');assert.deepEqual(shown(),['moon']);});
 test('search never writes config, logs query or requests backend search',()=>{assert.ok(commands.every(c=>[catalog.CMD.listModels,catalog.CMD.ocrCacheDir,catalog.CMD.state].includes(c.command)));assert.ok(!JSON.stringify(catalog.store.get().logs).includes('moonshnie'));});
 test('new reusable UI components never import store, IPC or business pages',()=>{for(const name of ['search-field','empty-state'])assert.doesNotMatch(fs.readFileSync(ROOT+'/src/renderer/ui/'+name+'.ts','utf8'),/window\.voxsub|from\s+["'][^"']*(?:store|protocol|views|i18n)["']/);});
 console.log(`Catalog search: ${count} checks passed`);
} finally {handle?.dispose();disconnect?.();dom.restore();cleanupShared();}
