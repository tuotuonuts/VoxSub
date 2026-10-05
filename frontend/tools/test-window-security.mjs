import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
import {securityContents,loadFixturePage,senderEvent} from './electron-security-fixture.mjs';
const {protectWindow,isTrustedDocument,guardedIpc}=await importShared('src/main/window-security.ts',{bundle:true});
let count=0;const check=(name,fn)=>{fn();count++;console.log('PASS '+name);};
const events=new Map(),permissions={};let popup,dead=false;
const contents=securityContents({on:(n,f)=>events.set(n,f),once:(n,f)=>events.set(n,f),setWindowOpenHandler:f=>popup=f,
 session:{setPermissionCheckHandler:f=>permissions.check=f,setPermissionRequestHandler:f=>permissions.request=f}});
const owner={webContents:contents,isDestroyed:()=>dead};const file=resolve('fixture UI/index.html');
protectWindow(owner,file);loadFixturePage(owner,file);
const handlers=new Map();let called=0;
guardedIpc({handle:(n,f)=>handlers.set(n,f)}).handle('backend:command',(_e,value)=>{called++;return value;});
const invoke=event=>handlers.get('backend:command')(event,'ok');
try{
 check('owned exact local top frame may invoke IPC',()=>assert.equal(invoke(senderEvent(contents)),'ok'));
 check('same WebContents iframe is not the top-frame owner',()=>assert.throws(()=>invoke({sender:contents,senderFrame:{url:contents.mainFrame.url}}),/Untrusted/));
 check('foreign WebContents cannot spoof a trusted document URL',()=>{
  const other=securityContents();other.mainFrame.url=contents.mainFrame.url;assert.throws(()=>invoke(senderEvent(other)),/Untrusted/);
 });
 check('remote navigation invalidates privileges',()=>{
  contents.mainFrame.url='https://example.invalid/';assert.throws(()=>invoke(senderEvent(contents)),/Untrusted/);loadFixturePage(owner,file);
 });
 check('file URLs use exact paths, not vulnerable prefix matching',()=>{
  contents.mainFrame.url+='.untrusted';assert.throws(()=>invoke(senderEvent(contents)),/Untrusted/);loadFixturePage(owner,file);
 });
 check('same-document anchors retain permission, query parameters do not',()=>{
  contents.mainFrame.url+='#settings';assert.equal(invoke(senderEvent(contents)),'ok');
  loadFixturePage(owner,file);contents.mainFrame.url+='?url=remote';assert.throws(()=>invoke(senderEvent(contents)),/Untrusted/);loadFixturePage(owner,file);
 });
 check('navigation, redirects, subframes and webviews are blocked',()=>{
  for(const name of ['will-navigate','will-frame-navigate','will-redirect','will-attach-webview']){let prevented=false;events.get(name)({preventDefault(){prevented=true;}});assert.equal(prevented,true);}
  assert.deepEqual(popup({url:'https://example.invalid/'}),{action:'deny'});
 });
 check('device and clipboard read permission denied; local text copy retained',()=>{
  for(const permission of ['media','display-capture','geolocation','clipboard-read'])assert.equal(permissions.check(contents,permission),false);
  assert.equal(permissions.check(contents,'clipboard-sanitized-write'),true);
  let result;permissions.request(contents,'media',v=>result=v);assert.equal(result,false);
 });
 check('clipboard writes denied after navigation',()=>{contents.mainFrame.url='https://example.invalid/';assert.equal(permissions.check(contents,'clipboard-sanitized-write'),false);loadFixturePage(owner,file);});
 check('scoped selector is trusted for its callback but not privileged IPC',()=>{
  const other={webContents:securityContents(),isDestroyed:()=>false};protectWindow(other,file,false);loadFixturePage(other,file);
  assert.equal(isTrustedDocument(senderEvent(other.webContents),other.webContents),true);
  assert.equal(isTrustedDocument(senderEvent(contents),other.webContents),false);
  assert.throws(()=>invoke(senderEvent(other.webContents)),/Untrusted/);
 });
 check('closed owners cannot use retained frames',()=>{dead=true;assert.throws(()=>invoke(senderEvent(contents)),/Untrusted/);});
 check('blocked IPC never reached handler',()=>assert.equal(called,2));
 console.log(`${count} window security checks passed`);
}finally{cleanupShared();}
