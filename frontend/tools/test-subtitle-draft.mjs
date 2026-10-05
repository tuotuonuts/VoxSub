import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import vm from 'node:vm';
import { transformSync } from 'esbuild';
import { importShared, cleanupShared, ROOT } from './esbuild-ts.mjs';
import { installMiniDom } from './mini-dom.mjs';
const { mergePartialDraft } = await importShared('src/shared/subtitle-draft.ts');
const examples = [
 [null,'Hello',{source:'Hello',translation:''}],
 [{source:'Hello world',translation:'你好'},'Hello world again',{source:'Hello world again',translation:'你好'}],
 [{source:'The door is red',translation:'门是红色的'},'The door is blue',{source:'The door is blue',translation:'门是红色的'}],
 [{source:'Hello world',translation:'你好'},'A different sentence',{source:'A different sentence',translation:''}],
 [{source:'你好世界',translation:'Hello'},'你好世界再见',{source:'你好世界再见',translation:'Hello'}],
 [{source:'Hello world',translation:'你好'},'Hello',{source:'Hello',translation:''}],
 [{source:'你好世界',translation:'Hello'},'再见世界',{source:'再见世界',translation:''}],
];
for (const [current, source, expected] of examples) assert.deepEqual(mergePartialDraft(current,source),expected);
console.log('PASS shared partial merge: progression, correction, rollback, unrelated source and CJK');
const dom = installMiniDom();
try {
 const {store} = await importShared('src/renderer/store.ts',{bundle:true});
 store.applyEvent({type:'draft',source:'Hello world',translation:'你好'});
 store.applyEvent({type:'partial',text:'Hello world again'});
 assert.equal(store.get().draft.translation,'你好');
 store.applyEvent({type:'partial',text:'Different next sentence'});
 assert.equal(store.get().draft.translation,'');
 store.applyEvent({type:'draft',source:'Different next sentence',translation:'另一句话'});
 store.applyEvent({type:'utterance',source:'Different next sentence',translation:'另一句话'});
 assert.equal(store.get().draft,null);
 assert.equal(store.get().subtitles.at(-1).translation,'另一句话');
 store.applyEvent({type:'partial',text:'A new sentence'});
 assert.equal(store.get().draft.translation,'');
 console.log('PASS production store: readable preview, unrelated source isolation and final commit');
} finally { dom.restore(); }
const source=readFileSync(join(ROOT,'src/renderer/overlay.ts'),'utf8');
const a=source.indexOf('function handleBackendEvent('),b=source.indexOf('/** 后端事件 → 字幕。 */',a);
assert.ok(a>0&&b>a);
const js=transformSync(source.slice(a,b)+'\nglobalThis.feed = handleBackendEvent;',{loader:'ts'}).code;
const state={draft:{src:'',dst:''},history:[],historyIndex:0,browsing:false,paintCount:0,
 mergePartialDraft,paint:()=>state.paintCount++,pushHistory:item=>state.history.push(item),isBrowsing:()=>state.browsing};
vm.runInNewContext(js,state);
state.feed({type:'draft',source:'Hello world',translation:'你好'});
state.feed({type:'partial',text:'Hello world again'});
assert.equal(state.draft.dst,'你好');
state.feed({type:'partial',text:'Another sentence'});
assert.equal(state.draft.dst,'');
state.feed({type:'draft',source:'Another sentence',translation:'另一句话'});
state.feed({type:'utterance',source:'Another sentence',translation:'另一句话'});
assert.equal(state.history.at(-1).dst,'另一句话');
assert.equal(state.draft.dst,'');
state.browsing=true;const painted=state.paintCount;
state.feed({type:'partial',text:'Live source'});
assert.equal(state.paintCount,painted);
state.feed({type:'session',action:'stop'});
assert.equal(state.draft.src,'');assert.equal(state.history.length,1);
state.feed({type:'session',action:'start'});
assert.equal(state.history.length,0);
console.log('PASS production overlay receiver: previews, final/history, browsing and session boundaries');
const settings=readFileSync(join(ROOT,'src/renderer/views/settings.ts'),'utf8');
assert.equal((settings.match(/boolField\("asr_live_draft_enabled"/g)??[]).length,1);
assert.ok(settings.indexOf('boolField("asr_live_draft_enabled"')<settings.indexOf('const contextCard'));
console.log('PASS existing settings controls reused, draft switch independent of context-only card');
cleanupShared();
