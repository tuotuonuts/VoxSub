/** Hidden GPU/software Chromium regression. Never captures the desktop or starts a backend. */
const { app, BrowserWindow } = require('electron');
const { buildSync } = require('esbuild');
const { join } = require('node:path');
const { writeFileSync } = require('node:fs');
const assert = require('node:assert/strict');
const Module = require('node:module');
const compiled = buildSync({entryPoints:[join(__dirname, '../src/main/overlay-surface.ts')], bundle:true, platform:'node', format:'cjs', write:false}).outputFiles[0].text;
const m = new Module(__filename, module); m._compile(compiled, __filename);
const { OverlaySurface } = m.exports;
const software = process.argv.includes('--software');
app.setPath('userData', process.env.VOXSUB_ALPHA_PROFILE);
app.commandLine.appendSwitch('mute-audio');
if (software) app.disableHardwareAcceleration();
app.on('window-all-closed', () => {});
let current, violations = 0;
const records = [];
const pause = () => new Promise(resolve => setTimeout(resolve, 120));
setTimeout(() => { console.error('overlay alpha fixture timed out'); app.exit(1); }, 45000).unref();
app.whenReady().then(async () => {
  try {
    const surface = new OverlaySurface();
    for (const offscreen of [false, true]) {
      current = new BrowserWindow({width:860, height:140, frame:false, transparent:true, backgroundColor:'#00000000', show:false, focusable:false, skipTaskbar:true, hasShadow:false,
        webPreferences:{offscreen, contextIsolation:true, nodeIntegration:false, sandbox:true, backgroundThrottling:false}});
      current.on('show', () => { violations++; current.hide(); });
      current.on('focus', () => violations++);
      await current.loadFile(join(__dirname, '../dist/renderer/overlay.html')); await pause(); await pause();
      for (const opacity of [.2, .5, .92, 1]) {
        await current.webContents.executeJavaScript(`document.documentElement.style.setProperty('--overlay-opacity', '${opacity}')`);
        // Exercise real native calls, including the unsafe-to-plain recovery transition.
        for (const enabled of [false, true, false]) {
          surface.invalidate(current);
          surface.apply(current, enabled);
          await pause();
          const image = await current.webContents.capturePage(), size = image.getSize(), pixels = image.toBitmap();
          const alpha = (x,y) => pixels[(y*size.width+x)*4+3];
          const center = alpha(Math.floor(size.width/2), Math.floor(size.height/2));
          const dom = await current.webContents.executeJavaScript(`({shell:getComputedStyle(document.querySelector('.shell')).backgroundColor, body:getComputedStyle(document.body).backgroundColor, textOpacity:getComputedStyle(document.querySelector('#src')).opacity})`);
          assert.ok(alpha(0,0) <= 2, 'window corner must not acquire an opaque background');
          assert.ok(Math.abs(center - opacity*255) <= 2, `background alpha ${center} disagrees with setting ${opacity}`);
          assert.equal(dom.body, 'rgba(0, 0, 0, 0)'); assert.equal(dom.textOpacity, '1');
          assert.equal(current.isVisible(), false); assert.equal(current.isFocused(), false);
          records.push({offscreen, opacity, enabled, cornerAlpha:alpha(0,0), centerAlpha:center, dom});
        }
      }
      current.destroy(); current = null;
    }
    assert.equal(violations, 0);
    const result = {status:'PASS', rendererMode:software?'software':'default-GPU', records, violations, visibleDesktop:'NOT_RUN',
      boundary:'Hidden Chromium RGBA only. Native acrylic desktop composition, multi-monitor DPI and interactive resize are NOT_RUN.'};
    if (process.env.VOXSUB_ALPHA_REPORT) writeFileSync(process.env.VOXSUB_ALPHA_REPORT, JSON.stringify(result, null, 2));
    console.log(JSON.stringify({status:result.status, rendererMode:result.rendererMode, checks:records.length, violations, visibleDesktop:result.visibleDesktop}));
    app.exit(0);
  } catch (error) { console.error({error, completed:records.length, last:records.at(-1)}); if(current && !current.isDestroyed())current.destroy(); app.exit(1); }
});
