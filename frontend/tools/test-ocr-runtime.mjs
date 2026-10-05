/** Production OCR scheduler/geometry/overlay/workspace, synthetic IPC/DOM only. */
import assert from "node:assert/strict";
import {importShared, cleanupShared} from "./esbuild-ts.mjs";
import {installMiniDom} from "./mini-dom.mjs";
let checks = 0;
const check = (label, fn) => {fn(); checks++; console.log("PASS " + label);};
const deferred = () => {let resolve; const promise = new Promise(r => resolve=r); return {promise, resolve};};
const g = await importShared("src/shared/ocr-geometry.ts");
const {localFileUrl} = await importShared("src/shared/file-url.ts");
check("negative-coordinate virtual desktop and dominant-screen clipping", () => {
  const displays = [{bounds:{x:-1920,y:0,width:1920,height:1080}}, {bounds:{x:0,y:0,width:2560,height:1440}}];
  assert.deepEqual(g.virtualBounds(displays.map(d=>d.bounds)),{x:-1920,y:0,width:4480,height:1440});
  assert.equal(g.bestDisplay({x:-100,y:20,width:400,height:200},displays),displays[1]);
  assert.equal(g.intersect({x:1,y:1,width:NaN,height:1},displays[1].bounds),null);
});
check("actual thumbnail dimensions, clipping and 125/150/200 percent DIP mapping", () => {
  const bounds={x:-1920,y:0,width:1920,height:1080};
  assert.deepEqual(g.thumbnailRect({x:-1910,y:20,width:100,height:50},bounds,{width:960,height:540}),{x:5,y:10,width:50,height:25});
  for (const scale of [1,1.25,1.5,2]) assert.deepEqual(g.overlayRect([20*scale,10*scale,120*scale,40*scale],{width:200*scale,height:100*scale},{width:200,height:100}),{x:20,y:10,width:100,height:30});
  assert.equal(g.overlayRect([NaN,0,1,2],{width:100,height:100},{width:100,height:100}),null);
  assert.equal(g.overlayRect([110,0,120,2],{width:100,height:100},{width:100,height:100}),null);
});
check("local image URLs escape query/hash/percent and preserve UNC",()=>{
  assert.equal(localFileUrl("D:/my #image?100%.png"),"file:///D:/my%20%23image%3F100%25.png");
  assert.equal(localFileUrl("//server/share/a b.png"),"file://server/share/a%20b.png");
});
const {LiveOcrSession} = await importShared("src/main/ocr-live.ts",{bundle:true});
const area={x:0,y:0,width:200,height:100}, langs={source:"en",target:"zh"};
const shot={path:"fixture.png",area,scaleFactor:2};
let capture=deferred(), reply=deferred(), captured=0, requests=[], renders=[], removed=[], statuses=[], failed=0, now=0;
const deps={capture:async()=>{captured++;return capture.promise;}, command:async(name,args)=>{requests.push({name,args});return reply.promise;},
  translated:p=>renders.push(p), remove:p=>removed.push(p), status:s=>statuses.push(s), failed:()=>failed++};
const session=new LiveOcrSession(deps,()=>now);
session.start(area,langs);const first=session.tick();await session.tick();
check("one in-flight capture; stopping/restarting never resets its lock",()=>{assert.equal(captured,1);session.stop();session.start(area,langs);});
await session.tick();capture.resolve(shot);await first;
check("stale capture is deleted and never recognized",()=>{assert.equal(captured,1);assert.equal(requests.length,0);assert.deepEqual(removed,["fixture.png"]);});
capture=deferred();const second=session.tick();capture.resolve(shot);await Promise.resolve();await Promise.resolve();
session.configure({source:"ja",target:"en"});reply.resolve({ok:true,data:{width:400,height:200,lines:[{translation:"旧译文",box:[0,0,20,20]}]}});await second;
check("language/config changes fence late backend results",()=>assert.equal(renders.length,0));
capture=deferred();reply=deferred();const third=session.tick();capture.resolve(shot);await Promise.resolve();await Promise.resolve();
reply.resolve({ok:true,data:{width:400,height:200,lines:[{translation:"Hello",box:[0,0,40,20]},{translation:"",box:[40,0,80,20]}],failedLines:1}});await third;
check("only translations reach overlay, image dimensions travel with payload",()=>{assert.equal(renders[0].lines.length,1);assert.equal(renders[0].width,400);assert.equal(statuses.at(-1),"partial");assert.equal(requests.at(-1).args.source,"ja");});
const count=captured;await session.tick();check("partial errors back off instead of hammering cloud/model",()=>assert.equal(captured,count));
now=1001;capture=deferred();reply=deferred();const fourth=session.tick();capture.resolve(shot);await Promise.resolve();await Promise.resolve();reply.resolve({ok:false});await fourth;
check("failed backend frame reports failure and still removes screenshot",()=>{assert.equal(failed,1);assert.equal(removed.length,4);});
session.stop();
// Execute actual overlay receiver in a fake DOM, with a controlled timer.
const dom=installMiniDom();const callbacks={};const timers=new Map();let sequence=0;
const realSet=globalThis.setTimeout,realClear=globalThis.clearTimeout;
try {
 dom.window.innerWidth=200;dom.window.innerHeight=100;
 const blocks=dom.document.createElement("div");blocks.id="blocks";dom.mount(blocks);
 dom.window.voxsub={ocrOverlay:{onRegionReady:f=>callbacks.region=f,onTranslated:f=>callbacks.translated=f,onFrameFailed:f=>callbacks.failed=f}};
 globalThis.setTimeout=(fn,ms)=>{const id=++sequence;timers.set(id,{fn,ms});return id;};globalThis.clearTimeout=id=>timers.delete(id);
 await importShared("src/renderer/ocr-overlay.ts",{bundle:true});
 callbacks.translated({width:400,height:200,lines:[{text:"Hi",box:[40,20,240,80]}]});
 check("production overlay maps high-DPI boxes and bounds text",()=>{const block=blocks.querySelector(".block");assert.equal(block.style.left,"20px");assert.equal(block.style.width,"100px");assert.equal(block.style.height,"30px");assert.equal(timers.size,1);});
 callbacks.failed();callbacks.translated({width:400,height:200,lines:[]});
 check("empty/repeated failures do not renew obsolete translations",()=>{assert.equal(sequence,1);assert.ok(blocks.querySelector(".block").classList.contains("is-stale"));});
 [...timers.values()][0].fn();
 check("2.5-second expiry clears actual rendered nodes",()=>{assert.equal(blocks.querySelectorAll(".block").length,0);assert.equal(timers.size,0);});
 callbacks.translated({width:400,height:200,lines:[{text:"New",box:[0,0,60,20]}]});callbacks.region();
 check("region reset cancels expiry and clears nodes",()=>{assert.equal(timers.size,0);assert.equal(blocks.querySelectorAll(".block").length,0);});
} finally {globalThis.setTimeout=realSet;globalThis.clearTimeout=realClear;dom.restore();}
const pageDom=installMiniDom();let page;const pending=[];let path="first.png",liveCallback,unsubscribed=0,stops=0;
try {
 const api=await importShared("tools/test-ocr-workspace-entry.ts",{bundle:true});
 pageDom.window.voxsub={backend:{command:(name,args)=>{
  if(name===api.CMD.ocrRecognize)return new Promise(resolve=>pending.push({resolve,args}));
  if(name===api.CMD.renderOcrImage)return Promise.resolve({ok:true,data:{path:"D:/translated #.png"}});
  return Promise.resolve({ok:true,data:{}});
 }},dialog:{pickImage:async()=>path},ocr:{onLiveStatus:f=>{liveCallback=f;return()=>unsubscribed++;},startLiveRegion:async()=>area,stopLiveRegion:async()=>{stops++;return true;},updateLanguages:async()=>true}};
 api.markBackendReady();page=api.buildOcrWorkspace();pageDom.mount(page.element);
 const clickUpload=()=>[...page.element.querySelectorAll("button")].find(b=>b.textContent==="上传图片并翻译").click();
 clickUpload();await pageDom.flushAsync(8);path="second.png";clickUpload();await pageDom.flushAsync(8);
 const result=text=>({ok:true,data:{text,translation:text+"译文",sourcePath:"D:/"+text+"#.png",lines:[{text,translation:text+"译文",box:[1,1,30,20]}]}});
 pending[1].resolve(result("Second"));await pageDom.flushAsync(12);pending[0].resolve(result("First"));await pageDom.flushAsync(12);
 check("real workspace keeps latest same-page request, not late old image",()=>{assert.ok(page.element.textContent.includes("Second"));assert.ok(!page.element.textContent.includes("First"));assert.ok(page.element.querySelector("img").src.includes("%23"));});
 const toggle=page.element.querySelector('input[type="checkbox"]');
 toggle.checked=false;toggle.dispatchEvent(pageDom.makeEvent("change"));
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="上传图片并识别").click();await pageDom.flushAsync(8);
 check("recognition-only uses common toggle and sends translate=false",()=>assert.equal(pending[2].args.translate,false));
 const only=result("Extracted");only.data.translation="";only.data.lines[0].translation="";pending[2].resolve(only);await pageDom.flushAsync(12);
 check("recognition-only shows original, disables translated export, offers retry",()=>{assert.ok(page.element.textContent.includes("未调用翻译模型"));assert.ok([...page.element.querySelectorAll("button")].find(b=>b.textContent==="导出译后图片").disabled);assert.ok(![...page.element.querySelectorAll("button")].find(b=>b.textContent==="重新处理当前图片").disabled);});
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="实时区域").click();
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="选择区域并开始").click();await pageDom.flushAsync(12);
 liveCallback({state:"no-text"});
 check("real live page receives meaningful no-text state",()=>assert.ok(page.element.textContent.includes("没有检测到文字")));
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="截图翻译").click();
 check("rebuilt screenshot tab preserves recognition-only labels and toggle",()=>{assert.ok([...page.element.querySelectorAll("button")].some(b=>b.textContent==="上传图片并识别"));assert.equal(page.element.querySelector('input[type="checkbox"]').checked,false);});
 const originalPicker=window.voxsub.dialog.pickImage;
 window.voxsub.dialog.pickImage=async()=>{throw new Error("native capture failure");};
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="上传图片并识别").click();await pageDom.flushAsync(8);
 check("native picker rejection is handled without false completion",()=>assert.ok(page.element.textContent.includes("图片或框选操作失败")));
 window.voxsub.dialog.pickImage=originalPicker;
 const finalToggle=page.element.querySelector('input[type="checkbox"]');finalToggle.checked=true;finalToggle.dispatchEvent(pageDom.makeEvent("change"));
 [...page.element.querySelectorAll("button")].find(b=>b.textContent==="重新处理当前图片").click();await pageDom.flushAsync(8);
 const skipped=result("Skipped");skipped.data.translation="";skipped.data.lines[0].translation="";skipped.data.failedLines=0;skipped.data.untranslatedLines=1;
 pending[3].resolve(skipped);await pageDom.flushAsync(8);
 check("language-filtered empty translation is not a failed translation or fake translated image",()=>{assert.ok(page.element.textContent.includes("没有可显示的译文"));assert.ok(!page.element.textContent.includes("部分翻译失败"));assert.ok([...page.element.querySelectorAll("button")].find(b=>b.textContent==="导出译后图片").disabled);});

 page.dispose();await pageDom.flushAsync(8);
 check("page disposal stops live OCR and unsubscribes status",()=>{assert.equal(unsubscribed,1);assert.ok(stops>=1);});
} finally {page?.dispose();pageDom.restore();cleanupShared();}
console.log(`OCR runtime: ${checks} checks passed`);
