import assert from 'node:assert/strict';
import {installMiniDom} from './mini-dom.mjs';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
const dom=installMiniDom();
const api=await importShared('tools/test-hardware-profile-entry.ts',{bundle:true});
const {hardwareRows,buildKeyValueRow,buildDiagnostics,CMD,setLanguage,tr}=api;
const group=items=>({status:'ok',items});
const categories={cpu:group([{Name:'Intel Core i5-13600KF'}]),motherboard:group([{Manufacturer:'MSI',Product:'MAG B760M MORTAR WIFI II (MS-7E13)'}]),
 memory:group([1,2].map(n=>({Manufacturer:'Vendor',PartNumber:'DDR5-DIMM-'+n,Capacity:16*2**30,ConfiguredClockSpeed:6000,Speed:4800,SMBIOSMemoryType:34}))),
 gpus:group([{Name:'NVIDIA GeForce RTX 4060',vramGb:8,DriverVersion:'32.0.15.0'},{Name:'Intel Graphics',vramGb:null}]),
 monitors:group([{Name:'24M1N3200Z',Manufacturer:'PHL',ProductCode:'C263',SizeInches:23.8}]),
 disks:group([{Model:'KIOXIA EXCERIA G2 SSD',Size:2e12},{Model:'SOYO SSD',Size:1.024e12},{Model:'TOSHIBA EXTERNAL USB',Size:2e12},{Model:'WDC SSD',Size:5e11}]),
 sound:group([{Name:'NVIDIA High Definition Audio'},{Name:'Realtek High Definition Audio'},{Name:'USB Audio Device'}]),
 network:group([{Name:'Realtek Gaming 2.5GbE Family Controller'},{Name:'Intel Wi-Fi 6E AX211 160MHz'}]),
 os:group([{Caption:'Windows 11 Pro',Version:'10.0.26100',BuildNumber:'26100'}]),bios:group([{Manufacturer:'AMI',SMBIOSBIOSVersion:'1.20'}]),drivers:group([])};
let profile={cpu:'13th Gen Intel Core i5-13600KF',physicalCores:14,logicalCores:20,ramGb:31.8,gpu:'NVIDIA GeForce RTX 4060',vramGb:8,gpuProvider:'CUDA',npu:'',inventory:{checkedAt:'2026-10-05T07:00:00Z',source:'Windows CIM',categories}};
let checks=0;const test=(label,fn)=>{fn();checks++;console.log('PASS '+label);};
const rows=()=>hardwareRows(profile,tr);const values=label=>rows().find(r=>r.label===label).values;
let pending=null;let calls=[];const listeners=[];dom.window.voxsub={backend:{start:async()=>({ok:true}),onEvent:fn=>{listeners.push(fn);return()=>{};},command:async(command,args)=>{calls.push({command,args});if(command===CMD.hardwareProfile)return pending??{ok:true,data:profile};if(command===CMD.listDevices)return {ok:true,data:{devices:[]}};return {ok:true,data:{}};}}};
const off=api.connectBackend();listeners.forEach(fn=>fn({type:"ready",version:"test",session:{mode:"a",running:false}}));await dom.flushAsync(8);
try {
 test('CPU model is human-readable with cores and threads',()=>assert.match(values('处理器')[0],/i5-13600KF.*14 核.*20 线程/));
 test('board product is retained without inventing a chipset',()=>{assert.match(values('主板')[0],/MSI.*MAG B760M/);assert.ok(!values('主板')[0].includes('芯片组'));});
 test('memory contains both modules and installed capacity, configured rate and DDR type',()=>{assert.equal(values('内存')[0],'32 GB（16 GB + 16 GB）');assert.match(values('内存')[1],/DDR5-DIMM-1.*16 GB.*DDR5.*6000 MT\/s/);assert.equal(values('内存').length,3);});
 test('all GPUs remain visible and unknown VRAM is never zero or WMI guessed',()=>{assert.match(values('显卡')[0],/RTX 4060.*8 GB.*驱动/);assert.match(values('显卡')[1],/Intel Graphics.*显存未确认/);});
 test('monitor reports EDID model and approximate size',()=>assert.match(values('显示器')[0],/24M1N3200Z.*PHL \/ C263.*约 23.8 英寸/));
 test('all physical disks, sound and network models are retained',()=>{assert.equal(values('磁盘').length,4);assert.match(values('磁盘')[0],/2000 GB/);assert.match(values('磁盘')[1],/1024 GB/);assert.equal(values('声卡').length,3);assert.equal(values('网卡').length,2);});
 test('unavailable and not detected are different',()=>{const p={...profile,inventory:{...profile.inventory,categories:{...categories,monitors:{status:'unavailable',items:[]},sound:{status:'not_detected',items:[]}}}};const r=hardwareRows(p,tr);assert.equal(r.find(x=>x.label==='显示器').values[0],'未能读取');assert.equal(r.find(x=>x.label==='声卡').values[0],'未检测到');});
 test('legacy backend and CPUID never fabricate a CPU model or DIMM details',()=>{const {inventory,...p}=profile;p.cpu='Intel64 Family 6 Model 183 Stepping 1, GenuineIntel';const r=hardwareRows(p,tr);assert.ok(!r[0].values[0].includes('Family'));assert.match(r.find(x=>x.label==='内存').values[0],/内存条信息未检查/);});
 test('unreported RAM brand and rated speed remain honest',()=>{const p={...profile,inventory:{...profile.inventory,categories:{...categories,memory:group([{Manufacturer:'0x859B',PartNumber:'DIMM',Speed:4800}])}}};assert.match(hardwareRows(p,tr).find(x=>x.label==='内存').values[0],/品牌未提供.*容量未提供.*标称/);});
 test('shared multiline key-value primitive keeps separate lines and treats markup as text',()=>{const r=buildKeyValueRow('磁盘',['<script>unsafe</script>','SSD']);assert.equal(r.querySelectorAll('li').length,2);assert.equal(r.querySelector('script'),null);assert.equal(r.querySelector('li').textContent,'<script>unsafe</script>');});
 setLanguage('en');test('English labels translate while real product names remain unchanged',()=>{assert.match(values('Processor')[0],/Intel Core i5-13600KF.*14 cores/);assert.match(values('Displays')[0],/About 23.8 inches/);});setLanguage('zh');
 const page=buildDiagnostics();dom.mount(page.element);page.element.querySelectorAll('.settings__tab')[2].click();await dom.flushAsync(14);
 test('actual diagnostics page uses shared rows and exposes all models without model loading',()=>{assert.match(page.element.textContent,/MAG B760M.*DDR5-DIMM-1.*RTX 4060.*24M1N3200Z.*KIOXIA.*USB Audio Device.*AX211/s);assert.ok(page.element.querySelector('.kv__list'));assert.ok(!calls.some(c=>['start','install_model','set_config'].includes(c.command)));});page.dispose();
 let resolve;pending=new Promise(r=>resolve=r);const old=buildDiagnostics();dom.mount(old.element);old.element.querySelectorAll('.settings__tab')[2].click();await dom.flushAsync(5);old.dispose();const latest=buildDiagnostics();dom.mount(latest.element);resolve({ok:true,data:profile});await dom.flushAsync(12);
 test('late hardware reply after page disposal cannot mutate a newer page',()=>assert.equal(latest.element.querySelector('.device-wrap'),null));latest.dispose();
 console.log('Hardware profile: '+checks+' checks passed');
} finally {off();dom.restore();cleanupShared();}
