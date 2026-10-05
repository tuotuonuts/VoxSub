/** Launch only a private hidden fixture, never the user's app/session. */
import { spawn } from 'node:child_process';
import { createRequire } from 'node:module';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
const require = createRequire(import.meta.url);
const profile = mkdtempSync(join(tmpdir(), 'voxsub-alpha-'));
const env = {...process.env, VOXSUB_ALPHA_PROFILE:profile};
delete env.ELECTRON_RUN_AS_NODE; delete env.NODE_OPTIONS; delete env.PYTHONHOME; delete env.PYTHONPATH;
const child = spawn(require('electron'), [join(dirname(fileURLToPath(import.meta.url)), 'test-overlay-alpha-native.cjs'), ...process.argv.slice(2)],
  {env, windowsHide:true, stdio:['ignore','pipe','pipe']});
child.stdout.pipe(process.stdout); child.stderr.pipe(process.stderr);
child.on('error', error => { console.error(error); process.exitCode = 1; });
child.on('exit', code => {
  // This path comes only from this process's mkdtempSync, not from app/user input.
  try { rmSync(profile, {recursive:true, force:true}); } catch { /* A renderer may still hold a temporary cache handle. */ }
  process.exitCode = code ?? 1;
});
