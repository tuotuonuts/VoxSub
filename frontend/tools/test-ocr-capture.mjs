/** Real capture module with isolated Electron/filesystem fixtures: no native window or desktop access. */
import assert from "node:assert/strict";
import vm from "node:vm";
import {readFileSync} from "node:fs";
import path from "node:path";
import ts from "typescript";
import {ROOT, importShared, cleanupShared} from "./esbuild-ts.mjs";
const geometry=await importShared("src/shared/ocr-geometry.ts");
const windows=[], writes=[];
let rejectLoad=false, sources=[];
class Window {
 constructor(){this.dead=false;this.events=new Map();this.calls=[];windows.push(this);}
 once(name,fn){this.events.set(name,fn);}
 isDestroyed(){return this.dead;}
 destroy(){this.dead=true;this.calls.push("destroy");this.events.get("closed")?.();}
 setAlwaysOnTop(){} setIgnoreMouseEvents(){} setVisibleOnAllWorkspaces(){}
 showInactive(){this.calls.push("showInactive");} setContentProtection(){this.calls.push("protect");}
 loadFile(){return rejectLoad?Promise.reject(new Error("missing renderer")):Promise.resolve();}
}
const display={id:7,bounds:{x:-100,y:0,width:100,height:100},scaleFactor:2};
const electron={BrowserWindow:Window,screen:{getAllDisplays:()=>[display]},desktopCapturer:{getSources:async()=>sources}};
const context=vm.createContext({exports:{},__dirname:path.join(ROOT,"src/main"),setTimeout,console,
 require(name){if(name==="electron")return electron;if(name==="../shared/ocr-geometry")return geometry;
 if(name==="node:fs")return {mkdirSync(){},writeFileSync(file,bytes){writes.push({file,bytes});}};
 if(name==="node:os")return {tmpdir:()=>"D:/synthetic-temp"};if(name==="node:path")return path;
 if(name==="node:crypto")return {randomUUID:()=>"synthetic-id"};throw Error("Unexpected import "+name);}});
vm.runInContext(ts.transpileModule(readFileSync(path.join(ROOT,"src/main/capture.ts"),"utf8"),
 {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,context);
let checks=0;const check=(label,fn)=>{fn();checks++;console.log("PASS "+label);};
try{
 const api=context.exports, area={x:-90,y:10,width:20,height:20};
 sources=[{display_id:"unrelated",thumbnail:{}}];
 const missing=await api.captureRegion(area);
 check("missing display never falls back to a different screen",()=>{assert.equal(missing,null);assert.equal(writes.length,0);});
 let crop;
 sources=[{display_id:"7",thumbnail:{getSize:()=>({width:50,height:50}),crop:r=>{crop=r;return {toPNG:()=>Buffer.from("synthetic")};}}}];
 const result=await api.captureRegion(area);
 check("capture uses actual thumbnail size, not requested physical size",()=>{assert.deepEqual({...crop},{x:5,y:5,width:10,height:10});assert.equal(result.scaleFactor,2);assert.equal(writes.length,1);});
 const overlay=api.createOverlayForArea(area);overlay.events.get("ready-to-show")();
 check("overlay displays without focus before content protection",()=>assert.deepEqual(overlay.calls,["showInactive","protect"]));
 rejectLoad=true;const failed=api.createOverlayForArea(area);await Promise.resolve();await Promise.resolve();
 check("renderer load rejection destroys failed overlay",()=>assert.equal(failed.dead,true));
 failed.events.get("ready-to-show")();
 check("late readiness cannot show a destroyed overlay",()=>assert.deepEqual(failed.calls,["destroy"]));
 console.log(`OCR capture: ${checks} checks passed (fixtures, not native acceptance)`);
}finally{cleanupShared();}
