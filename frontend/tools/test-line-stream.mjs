import assert from 'node:assert/strict';
import {importShared,cleanupShared} from './esbuild-ts.mjs';
const {BoundedLineStream}=await importShared('src/main/line-stream.ts',{bundle:true});
let count=0;const check=(name,fn)=>{fn();count++;console.log('PASS '+name);};
try{
 check('split NDJSON, CRLF and multiple lines preserve exact boundaries',()=>{const lines=[];const s=new BoundedLineStream(128,l=>lines.push(l),()=>assert.fail('overflow'));s.push('{"ok":');s.push('true}\r');s.push('\nINFO [');s.push('model] ready\nlast');s.flush();assert.deepEqual(lines,['{"ok":true}','INFO [model] ready','last']);});
 check('byte budget counts UTF-8, not UTF-16 characters',()=>{const lines=[];let over=0;const s=new BoundedLineStream(6,l=>lines.push(l),()=>over++);s.push('你好\n');s.push('你好吗');s.push('discarded');s.push('\nok\n');assert.deepEqual(lines,['你好','ok']);assert.equal(over,1);});
 check('oversized non-terminated lines release buffers and recover at newline',()=>{let over=0;const lines=[];const s=new BoundedLineStream(16,l=>lines.push(l),()=>over++);for(let i=0;i<1000;i++)s.push('xxxx');assert.equal(over,1);assert.equal(s.parts.length,0);s.push('\n{}\n');assert.deepEqual(lines,['{}']);});
 check('EOF flush is idempotent and never emits a truncated oversized line',()=>{let over=0;const lines=[];const s=new BoundedLineStream(4,l=>lines.push(l),()=>over++);s.push('12345');s.flush();s.flush();assert.equal(over,1);assert.deepEqual(lines,[]);s.push('safe');s.flush();assert.deepEqual(lines,['safe']);});
 check('log level prefix split across chunks remains one log event',()=>{const lines=[];const s=new BoundedLineStream(128,l=>lines.push(l),()=>{});s.push('ERR');s.push('OR [voxsub] timeout\n');assert.deepEqual(lines,['ERROR [voxsub] timeout']);});
 check('invalid limits are rejected',()=>{for(const n of [0,-1,NaN,Infinity,.5])assert.throws(()=>new BoundedLineStream(n,()=>{},()=>{}));});
 console.log(`${count} bounded stdio checks passed`);
}finally{cleanupShared();}
