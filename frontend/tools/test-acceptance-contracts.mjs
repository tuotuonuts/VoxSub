#!/usr/bin/env node
/** Read-only structural contracts, not runtime acceptance. No Electron/config imports. */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import ts from 'typescript';

const read = path => readFileSync(new URL('../' + path, import.meta.url), 'utf8');
const printer = ts.createPrinter({ removeComments: true });
const parse = text => ts.createSourceFile('contract.ts', text, ts.ScriptTarget.Latest, true);
const print = node => printer.printNode(ts.EmitHint.Unspecified, node, node.getSourceFile()).replace(/\s+/g, '');
const expression = text => print(parse(`(${text})`).statements[0].expression.expression);
function nodes(root, predicate) {
  const found = [];
  function visit(node) { if (predicate(node)) found.push(node); ts.forEachChild(node, visit); }
  if (root) visit(root);
  return found;
}
const calls = (root, callee) => nodes(root, n => ts.isCallExpression(n) && print(n.expression) === expression(callee));
const call = (root, callee, args) => calls(root, callee).find(n =>
  args.every((arg, i) => arg === null || (n.arguments[i] && print(n.arguments[i]) === expression(arg))));
const fn = (root, name) => nodes(root, n => ts.isFunctionDeclaration(n) && n.name?.text === name)[0];
const event = (root, control, name) => call(root, 'on', [control, JSON.stringify(name)])?.arguments[2];

export function inspectFrontend(readSource = read) {
  const results = [];
  const check = (name, ok) => results.push({ name, ok: Boolean(ok) });
  const load = path => {
    const tree = parse(readSource(path));
    check(`${path}: parses`, tree.parseDiagnostics.length === 0);
    return tree;
  };
  const main = load('src/main/main.ts');
  const preload = load('src/main/preload.ts');
  const overlay = load('src/renderer/overlay.ts');
  const renderer = load('src/renderer/index.ts');
  const language = load('src/renderer/language-selection.ts');
  const capabilities = load('src/renderer/language-capabilities.ts');
  const settings = load('src/renderer/views/settings.ts');
  const opacityHandler = call(main, 'ipcMain.handle', ['"overlay:set-opacity"'])?.arguments[1];
  check('main: opacity handler clamps and broadcasts',
    call(opacityHandler, 'Number.isFinite', ['value']) &&
    call(opacityHandler, 'Math.min', ['1', 'Math.max(0.2, value)']) &&
    call(opacityHandler, 'sendToWindow', ['overlayWindow', '"overlay:opacity"', 'overlayOpacity']));
  const property = name => nodes(preload, n => ts.isPropertyAssignment(n) && n.name.getText() === name)[0];
  check('preload: setOpacity invokes exact channel/value',
    call(property('setOpacity'), 'ipcRenderer.invoke', ['"overlay:set-opacity"', 'value']));
  check('preload: onOpacityChanged subscribes exact channel',
    call(property('onOpacityChanged'), 'subscribe', ['"overlay:opacity"', 'handler']));
  const appearance = fn(settings, 'appearanceTab');
  check('settings: appearance input previews opacity',
    call(event(appearance, 'opacity', 'input'), 'window.voxsub?.overlay.setOpacity', ['Number(opacity.value) / 100']));
  check('settings: appearance change persists overlay_opacity',
    call(event(appearance, 'opacity', 'change'), 'saveConfig', ['{ overlay_opacity: Number(opacity.value) / 100 }']));
  const restore = fn(overlay, 'restoreOpacity');
  check('overlay: restore reads config key and applies/syncs value',
    call(restore, 'window.voxsub?.backend.command', ['"get_config"', 'null']) &&
    nodes(restore, n => ts.isElementAccessExpression(n) && n.argumentExpression && print(n.argumentExpression) === '"overlay_opacity"').length &&
    call(restore, 'applyVisuals', []) && call(restore, 'window.voxsub?.overlay.setOpacity', ['opacity']));
  check('overlay: boot and backend ready both restore',
    call(fn(overlay, 'boot'), 'restoreOpacity', []) &&
    nodes(fn(overlay, 'wireBackend'), n => ts.isIfStatement(n) && print(n.expression) === expression('raw.type === "ready"') && call(n.thenStatement, 'restoreOpacity', [])).length);
  check('overlay: receiver updates visuals and CSS uses opacity',
    call(call(overlay, 'window.voxsub?.overlay.onOpacityChanged', [])?.arguments[0], 'applyVisuals', []) &&
    calls(fn(overlay, 'applyVisuals'), 'root.setProperty').some(n => print(n.arguments[0]) === '"--overlay-opacity"' && print(n.arguments[1]) === 'String(opacity)'));
  check('index: imports language controls from real module',
    nodes(renderer, n => ts.isImportDeclaration(n) && n.moduleSpecifier.text === './language-capabilities' &&
      nodes(n, c => ts.isImportSpecifier(c) && c.name.text === 'buildLanguageControls').length).length &&
    call(renderer, 'buildLanguageControls', []));
  check('controls: shared wrapper delegates source/target to persistence',
    call(fn(capabilities, 'savePair'), 'persistLanguagePair', ['source', 'target']));
  for (const control of ['sourceSelect', 'targetSelect']) {
    check('controls: ' + control + ' change uses acknowledged persistence',
      call(event(fn(capabilities, 'buildLanguageControls'), control, 'change'), 'savePair', []));
  }
  const persist = fn(language, 'persistLanguagePair');
  check('language-selection: set_langs uses source/target object', call(persist, 'call', ['"set_langs"', '{ source, target }']));
  check('language-selection: set_config persists lang_pair', call(persist, 'call', ['"set_config"', '{ updates: { lang_pair: `${source}-${target}` } }']));
  return results;
}

function inspectPython() {
  // Parse Python AST without importing any application code or touching config/models.
  const script = String.raw`
import ast, json, pathlib, sys
root = pathlib.Path(sys.argv[1])
def tree(path): return ast.parse((root / path).read_text(encoding='utf-8'))
def function(t, name): return next(n for n in ast.walk(t) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
def calls(t, name): return [n for n in ast.walk(t) if isinstance(n, ast.Call) and ast.unparse(n.func) == name]
def has(t, name, *args): return any([ast.unparse(a) for a in n.args] == list(args) for n in calls(t, name))
session = tree('frontend/backend/handlers/session.py')
ipc = tree('frontend/backend/ipc_server.py')
factory = function(tree('voxsub/translate/factory.py'), 'resolve_tier')
pipeline = tree('voxsub/pipeline.py')
loader = function(pipeline, '_load_translator_for_pair')
usable = function(pipeline, '_usable_for_pair')
checks = [
 ('backend: set_langs reads exact source/target payload', has(session, 'pipeline.apply_language_pair', "str(args.get('source', 'auto'))", "str(args.get('target', 'zh'))")),
 ('backend: config restoration applies parsed language pair', bool(calls(ipc, 'pipeline.set_langs')) and has(ipc, 'pair.partition', "'-'")),
 ('factory: resolver checks selected and candidate language support', has(factory, 'tier_supports', 'tier', 'src_lang', 'dst_lang', 'config') and has(factory, 'tier_supports', 'candidate', 'src_lang', 'dst_lang', 'config')),
 ('pipeline: candidate loader checks actual translator support', has(loader, '_candidate_tiers', 'kind') and bool(calls(loader, '_load_translator')) and has(loader, '_usable_for_pair', 'translator', 'src_lang', 'dst_lang')),
 ('pipeline: availability/support and no-candidate fallback exist', bool(calls(usable, 'ready')) and has(usable, 'supports', 'src_lang', 'dst_lang') and bool(calls(loader, '_NoopTranslator')) and bool(calls(loader, '_close_quietly'))),
]
print(json.dumps([dict(name=name, ok=ok) for name, ok in checks]))
`;
  const result = spawnSync(process.env.PYTHON || 'python', ['-c', script, fileURLToPath(new URL('../../', import.meta.url))], { encoding: 'utf8' });
  if (result.error || result.status !== 0) throw new Error(result.error?.message || result.stderr);
  return JSON.parse(result.stdout);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const results = [...inspectFrontend(), ...inspectPython()];
    for (const { name, ok } of results) console.log(`${ok ? 'PASS' : 'FAIL'} ${name}`);
    const passed = results.filter(r => r.ok).length;
    console.log(`\n${passed}/${results.length} structural acceptance contracts passed (not runtime proof)`);
    process.exitCode = passed === results.length ? 0 : 1;
  } catch (error) {
    console.error('FAIL acceptance contract inspection:', error.message);
    process.exitCode = 1;
  }
}
