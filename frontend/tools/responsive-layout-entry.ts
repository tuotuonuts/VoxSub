/** Real renderer with a read-only in-memory backend fixture, for offscreen layout checks. */
import "../src/renderer/index";
import { store } from "../src/renderer/store";
import { setLanguage } from "../src/renderer/i18n";
import { CMD } from "../src/renderer/protocol";
import { refreshLogView } from "../src/renderer/views/diagnostics";
const language = new URLSearchParams(location.search).get("lang") === "en" ? "en" : "zh";
setLanguage(language);
let installed = true;
let catalogDelay: ((result: unknown) => void) | null = null;
const listeners = new Set<(event: unknown) => void>();
let openPage: (page: string) => void = () => {};
declare const __ACCEPTANCE_MODEL__: Record<string, unknown> | null;
const baseSensevoice = {id:"asr-sensevoice-small-int8",name:"SenseVoice Small · INT8",task:"asr",quality:88,sizeLabel:"155.5 MB",sizeBytes:163002883,installedBytes:239549735,builtin:false,runtime:"sherpa-sensevoice",license:"Apache-2.0",languages:"中文 / 粤语 / 英语 / 日语 / 韩语",description:"识别多种语言的语音，适合离线字幕。",tags:["多语言","离线识别"],minRamGb:4,gpuSupported:false,igpuSupported:false,npuSupported:false,officialRepo:"https://github.com/k2-fsa/sherpa-onnx",recommendation:{level:"recommended",loadPercent:25,reason:"fixture"},download:null};
const sensevoice = {...baseSensevoice, ...__ACCEPTANCE_MODEL__};
const catalog=()=>({models:[{...sensevoice,installed}, {...sensevoice,id:"translation",name:"Offline translation",task:"translate",installed:true}],modelsRoot:"D:/Fixture/Models",lookupRoots:[],diagnostics:[]});
const config={stt_provider:"local",asr_model_id:sensevoice.id,translate_model_id:"translation",translate_tier:"fast",lang_pair:"auto-zh",models_root:"D:/Fixture/Models",models_root_mode:"custom",asr_hotwords:"unsaved fixture text"};
const api = {
 backend: {onEvent(fn:(event:unknown)=>void){listeners.add(fn);return()=>listeners.delete(fn);},async start(){queueMicrotask(()=>listeners.forEach(fn=>fn({type:"ready",version:"test"})));return {ok:true};},async command(command:string){
  let data: unknown = {};
  if(command===CMD.listModels){if(catalogDelay) return await new Promise(resolve=>{catalogDelay=resolve;});data=catalog();}
  if(command===CMD.getConfig)data={...config};
  if(command===CMD.state)data={running:false,paused:false,mode:store.get().mode};
  if(command===CMD.languageCapabilities)data={sources:["auto","zh","en","ja","ko"],targets:{auto:["zh","en"],zh:["en","zh"],en:["zh","en"],ja:["zh","en"],ko:["zh","en"]},compatible:true,reason:""};
  if(command===CMD.translateTiers)data={source:"auto",target:"zh",selected:"fast",effective:"fast",tiers:[]};
  if(command===CMD.releaseNotes)data={notes:[]};
  if(command===CMD.listAudioDevices)data={microphones:[],loopbacks:[]};
  if(command===CMD.listCaptureTargets)data={targets:[]};
  if(command===CMD.detectLegacy)data={legacy:{found:false},needsDecision:false};
  if(command===CMD.recentLogs)data={text:"",lines:0,run_id:"layout"};
  if(command===CMD.runSelfCheck)data={results:[{check:"configuration",status:"warn",detail:"Fixture check detail: " + "VeryLongHardwareOrPathName".repeat(5),suggestion:"Review configuration"}]};
  if(command===CMD.hardwareProfile)data={cpu:"Intel Core i5-13600KF",ramGb:32,devices:[]};
  if(command===CMD.listDevices)data={devices:[]};
  if(command===CMD.ocrCacheDir)data={path:"D:/Fixture/Cache/"+"LongFolderName".repeat(12)};
  if(command===CMD.asrTuningMeta)data=null;
  return {ok:true,data};
 }},
 app:{onOpenPage(fn:(page:string)=>void){openPage=fn;},onBlockingTask(){},async setBusy(){return {ok:true};}},
 overlay:{on(){},async toggleVisible(){return false;},async getGlass(){return {enabled:false,strength:50,active:false,supported:true};},async setGlass(){return {enabled:false,strength:50,active:false,supported:true};}},
 dialog:{},
};
Object.assign(window,{voxsub:api,layoutTest:{store,open:(page:string)=>openPage(page),refreshLogView,
 setInstalled(value:boolean){installed=value;window.dispatchEvent(new Event("voxsub:models"));},
 deferCatalog(){catalogDelay=()=>{};},
 releaseCatalog(){const resolve=catalogDelay;catalogDelay=null;resolve?.({ok:true,data:catalog()});},
}});
