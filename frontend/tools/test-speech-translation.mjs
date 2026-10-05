import assert from 'node:assert/strict';
import { installMiniDom } from './mini-dom.mjs';
import { importShared, cleanupShared } from './esbuild-ts.mjs';
const dom = installMiniDom();
const api = await importShared('tools/test-speech-translation-entry.ts', {bundle:true});
let count = 0, page, picker;
const check = (name, fn) => { fn(); count++; console.log('PASS '+name); };
const choices = [{id:'g',name:'Granite',installed:true,runtimeAvailable:true}, {id:'i',name:'Index',installed:false}];
const writes=[];
try {
  const form = api.buildFileTranslationForm(u=>writes.push(u));dom.mount(form.element);
  form.update({file_translation_mode:'dual',speech_model_id:'g'},choices,false);
  check('dual route uses understandable labels and hides the speech model picker',()=>{
    assert.match(form.element.textContent,/双模型/);assert.match(form.element.textContent,/原|设置/);
    assert.equal(form.element.querySelectorAll('select')[1].closest('.field').hidden,true);
  });
  form.update({file_translation_mode:'single',speech_model_id:'g'},choices,false);
  check('single route exposes only installed speech models and honest CPU/timing limits',()=>{
    const model=form.element.querySelectorAll('select')[1];assert.equal(model.value,'g');assert.ok(!model.textContent.includes('Index'));
    assert.match(form.element.textContent,/不启动独立翻译器/);assert.match(form.element.textContent,/分两次生成/);
  });
  const route=form.element.querySelector('select');route.value='dual';route.dispatchEvent(new Event('change'));
  check('route changes do not overwrite ASR, translation, cloud, recording or language configuration',()=>assert.deepEqual(writes,[{file_translation_mode:'dual'}]));
  form.update({file_translation_mode:'single',speech_model_id:'i'},choices,false);
  check('missing selection is never silently replaced by a downloaded model',()=>assert.equal(form.element.querySelectorAll('select')[1].value,''));
  form.update({file_translation_mode:'single',speech_model_id:'g'},choices,true);
  check('in-flight/running task disables both selectors',()=>assert.ok([...form.element.querySelectorAll('select')].every(s=>s.disabled)));
  form.update({file_translation_mode:'single',speech_model_id:'g'},[{...choices[0],runtimeAvailable:false}],false);
  check('missing runtime is disclosed rather than claiming the downloaded model can run',()=>assert.match(form.element.textContent,/缺少语音翻译运行组件/));
  form.update({file_translation_mode:'single',speech_model_id:'g',speech_device:'cuda',speech_output:'translation'},[...choices,{id:'external',name:'Seamless',installed:true,externalRuntime:'WSL'}],false);
  check('GPU and translation-only choices save only their fields; external runtime cannot be selected',()=>{
    const selects=form.element.querySelectorAll('select');
    assert.equal(selects[2].value,'cuda');assert.equal(selects[3].value,'translation');
    assert.ok(!selects[1].textContent.includes('Seamless'));
    selects[2].value='cpu';selects[2].dispatchEvent(new Event('change'));
    assert.deepEqual(writes.at(-1),{speech_device:'cpu'});
    selects[3].value='bilingual';selects[3].dispatchEvent(new Event('change'));
    assert.deepEqual(writes.at(-1),{speech_output:'bilingual'});
  });
  api.setLanguage('en');const english=api.buildFileTranslationForm(()=>{});english.update({file_translation_mode:'single',speech_model_id:'g'},choices,false);
  check('shared control has English UI without translating model names',()=>{assert.match(english.element.textContent,/one model/);assert.match(english.element.textContent,/Granite/);});
  api.setLanguage('zh');
  api.markBackendReady();let config={file_translation_mode:'dual',speech_model_id:'g'};let invalid=false;
  dom.window.voxsub={backend:{command:async(name,args)=>{
    if(name==='get_config')return {ok:true,data:{...config}};
    if(name==='list_models')return {ok:true,data:invalid?{}:{models:choices.map(c=>({...c,task:'speech'}))}};
    if(name==='set_config'){config={...config,...args.updates};return {ok:true,data:config};}
    return {ok:true,data:{}};
  }},dialog:{saveSession:async()=>null}};
  picker=api.buildFileTranslationPicker();dom.mount(picker.element);await dom.flushAsync(20);
  check('connected file picker starts from backend config',()=>assert.equal(picker.element.querySelector('select').value,'dual'));
  picker.dispose();invalid=true;picker=api.buildFileTranslationPicker();dom.mount(picker.element);await dom.flushAsync(20);
  check('malformed model list produces a disabled error state, not an unhandled rejection',()=>{assert.match(picker.element.textContent,/无法读取/);assert.ok(picker.element.querySelector('select').disabled);});picker.dispose();
  api.store.patch({subtitles:[],sessionStartedAt:0,mode:'c',running:false});
  api.store.applyEvent({type:'utterance',source:'Repeat',translation:'重复',startMs:100,endMs:400});
  api.store.applyEvent({type:'utterance',source:'Repeat',translation:'重复',startMs:500,endMs:800});
  check('identical text at different media timestamps is not deduplicated',()=>{assert.equal(api.store.get().subtitles.length,2);assert.equal(api.store.get().subtitles[1].tsMs,500);});
  let exported;dom.window.voxsub.dialog.saveSession=async payload=>{exported=payload;return null;};
  page=api.buildWorkspace();dom.mount(page.element);await dom.flushAsync(20);
  [...page.element.querySelectorAll('button')].find(b=>b.textContent==='导出会话').click();await dom.flushAsync(10);
  check('manual file export preserves model end timestamps instead of wall-clock durations',()=>assert.deepEqual(exported.lines.map(l=>[l.startMs,l.endMs]),[[100,400],[500,800]]));
  console.log(`Speech UI: ${count} checks passed (MiniDOM, no desktop/audio)`);
} finally {picker?.dispose();page?.dispose();await dom.flushAsync(20);dom.restore();cleanupShared();}
