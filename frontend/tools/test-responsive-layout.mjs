/** Quiet offscreen Electron acceptance; no production main/preload/backend or real audio. */
import {spawnSync} from 'node:child_process';
import {mkdtempSync,writeFileSync,readFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join,resolve} from 'node:path';
import {createRequire} from 'node:module';
import {buildSync} from 'esbuild';
import {ROOT} from './esbuild-ts.mjs';
const out=process.env.VOXSUB_ACCEPTANCE_DIR?resolve(process.env.VOXSUB_ACCEPTANCE_DIR):mkdtempSync(join(tmpdir(),'voxsub-responsive-'));
const bundle=join(out,'responsive-entry.js');
const realModel=process.env.VOXSUB_ACCEPTANCE_MODEL?JSON.parse(readFileSync(process.env.VOXSUB_ACCEPTANCE_MODEL,'utf8')):null;
if(realModel&&(!realModel.installed||realModel.id!=='asr-sensevoice-small-int8'))throw Error('Acceptance model is not an installed SenseVoice');
if(realModel)console.log('Using actual installed model-list row: '+realModel.id);
buildSync({define:{__ACCEPTANCE_MODEL__:JSON.stringify(realModel)},entryPoints:[join(ROOT,'tools/responsive-layout-entry.ts')],bundle:true,format:'iife',platform:'browser',outfile:bundle});
const require=createRequire(import.meta.url),env={...process.env,VOXSUB_LAYOUT_ROOT:ROOT,VOXSUB_LAYOUT_OUT:out};delete env.ELECTRON_RUN_AS_NODE;
const result=spawnSync(require('electron'),[join(ROOT,'tools/test-responsive-layout.cjs')],{env,windowsHide:true,encoding:'utf8',timeout:180000,maxBuffer:8*1024*1024});
writeFileSync(join(out,'electron-layout.log'),(result.stdout??'')+(result.stderr??''));
console.log(result.stdout??'');if(result.error)console.error(result.error);if(result.status!==0)console.error(result.stderr??'');
console.log('Evidence: '+out);process.exitCode=result.status??1;
