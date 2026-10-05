/** Actual shared page frame, synthetic DOM only: no native window/backend. */
import assert from "node:assert/strict";
import {installMiniDom} from "./mini-dom.mjs";
import {importShared,cleanupShared} from "./esbuild-ts.mjs";
const dom=installMiniDom();let count=0;
const check=(label,fn)=>{fn();count++;console.log("PASS "+label);};
try{
 const {buildPageFrame}=await importShared("src/renderer/ui/page-frame.ts",{bundle:true});
 const content=document.createElement("section");content.textContent="Settings content";let backs=0;
 const frame=buildPageFrame("Settings",content,()=>backs++);
 check("header and scroll body are separate siblings and preserve original content node",()=>{
  assert.equal(frame.className,"page");assert.equal(frame.children[0].className,"page__bar");assert.equal(frame.children[1].className,"page__content");assert.equal(frame.children[1].children[0],content);
 });
 check("back reuses common button and invokes caller exactly once",()=>{const button=frame.querySelector("button");assert.ok(button.classList.contains("btn"));assert.ok(button.classList.contains("page__back"));button.click();assert.equal(backs,1);assert.equal(button.getAttribute("type"),"button");});
 check("narrow-window back stays accessible and title is plain text",()=>{const button=frame.querySelector("button");assert.equal(button.getAttribute("aria-label"),"返回");assert.equal(frame.querySelector(".page__back-icon").getAttribute("aria-hidden"),"true");const f=buildPageFrame("<img src=x>",document.createElement("div"),()=>{});assert.equal(f.querySelector(".page__title").textContent,"<img src=x>");assert.equal(f.querySelector(".page__title").children.length,0);});
 check("separate frames do not share content or callbacks",()=>{let second=0;const f=buildPageFrame("Other",document.createElement("article"),()=>second++);f.querySelector("button").click();assert.equal(second,1);assert.equal(backs,1);assert.equal(frame.children[1].children[0],content);});
 console.log(`Page frame: ${count} checks passed`);
}finally{cleanupShared();dom.restore();}
