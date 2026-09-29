import assert from 'node:assert/strict';
import { installMiniDom } from './mini-dom.mjs';
import { importShared } from './esbuild-ts.mjs';

// Renderer production assembly on MiniDOM, not Chromium/Electron or real audio.
export async function testRecordingWorkspace() {
  const dom = installMiniDom();
  const { window } = dom;
  const listeners = new Set();
  const state = (enabled = false, extra = {}) => ({ running: false, paused: false, mode: 'a',
    recordingEnabled: enabled, recordingActive: false, recordingSupported: true,
    recordingCanChange: true, ...extra });
  const emit = event => { for (const fn of [...listeners]) fn(event); };
  // A task boundary drains promise continuations without timer sleeps/race guesses.
  const settle = () => new Promise(resolve => setImmediate(resolve));
  let page, off;
  let read = async () => ({ ok: true, data: state() });
  let set = async () => { throw Error('unexpected set_recording'); };
  let session = async command => { throw Error(`unexpected session command: ${command}`); };
  const commands = [];
  const pages = [];
  const results = [];
  const deferred = () => { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; };
  try {
    const { buildWorkspace, connectBackend, refreshSessionState, store, CMD, setLanguage, tr } = await importShared('tools/test-recording-workspace-entry.ts', { bundle: true });
    window.voxsub = { backend: {
      start: async () => ({ ok: true }),
      onEvent: fn => { listeners.add(fn); return () => listeners.delete(fn); },
      command: (command, args) => {
        commands.push({ command, args });
        if (command === CMD.state) return read();
        if (command === CMD.setRecording) return set(args);
        if ([CMD.start, CMD.pause, CMD.resume, CMD.stop].includes(command)) return session(command);
        if (command === CMD.lastRecording) return Promise.resolve({ ok: true, data: { path: null } });
        throw Error(`unexpected command: ${command}`);
      },
    } };
    off = connectBackend();
    emit({ type: 'ready', version: 'recording-test', session: state() });
    await settle();
    page = buildWorkspace();
    dom.mount(page.element);
    await settle();
    const input = page.element.querySelector('input[type="checkbox"]');
    const dot = page.element.querySelector('.rec-dot');
    assert.equal(input.checked, false);
    assert.equal(input.disabled, false);
    emit({ type: 'state', ...state(true, { recordingActive: true, recordingCanChange: false, running: true }) });
    assert.equal(store.get().recordingState.recordingActive, true, 'backend event reached production store');
    assert.equal(dot.hidden, false, 'backend active event must reach production workspace DOM');
    assert.equal(input.checked, true);
    assert.equal(input.disabled, true);
    emit({ type: 'state', ...state(true) });
    assert.equal(dot.hidden, true);
    assert.equal(input.disabled, false);
    console.log('PASS MiniDOM production workspace: live active/inactive/canChange event chain');
    page.dispose();
    results.push({ name: 'live active/inactive/canChange event chain', ok: true });
    const send = value => emit({ type: 'state', ...value });
    const ready = session => emit({ type: 'ready', version: 'recording-test', session });
    const build = () => {
      const p = buildWorkspace(); pages.push(p); dom.mount(p.element);
      return { ...p, input: p.element.querySelector('input[type="checkbox"]'),
        dot: p.element.querySelector('.rec-dot'), hint: p.element.querySelector('.rec-hint') };
    };
    const test = async (name, fn) => {
      try {
        read = async () => ({ ok: true, data: state() });
        set = async () => { throw Error('unexpected set_recording'); };
        session = async command => { throw Error(`unexpected session command: ${command}`); };
        ready(state()); await settle();
        await fn();
        results.push({ name, ok: true }); console.log(`PASS MiniDOM production workspace: ${name}`);
      } catch (error) {
        results.push({ name, ok: false }); console.error(`FAIL MiniDOM production workspace: ${name}\n${error.stack}`);
      } finally {
        for (const p of pages.splice(0)) p.dispose();
        dom.document.body.replaceChildren();
        setLanguage('zh');
      }
    };
    await test('initial backend event snapshot is rendered synchronously', async () => {
      send(state(true, { recordingActive: true, recordingCanChange: false }));
      const held = deferred(); read = () => held.promise;
      const p = build();
      assert.equal(p.input.checked, true);
      assert.equal(p.input.indeterminate, false, 'initial store snapshot is known before any query returns');
      assert.equal(p.dot.hidden, false);
      assert.equal(p.input.disabled, true);
    });
    await test('ready session snapshot survives parsing and initial assembly', async () => {
      send(state());
      const held = deferred(); read = () => held.promise;
      ready(state(true, { recordingActive: true, recordingCanChange: false }));
      assert.equal(store.get().recordingState?.recordingActive, true, 'ready session recording fields retained');
      const p = build();
      assert.equal(p.input.checked, true);
      assert.equal(p.dot.hidden, false);
    });
    await test('ready resync query restores recording into store and page', async () => {
      send(state());
      const held = deferred(); read = () => held.promise;
      ready(undefined);
      const p = build();
      held.resolve({ ok: true, data: state(true, { recordingActive: true }) });
      await settle();
      assert.equal(store.get().recordingState?.recordingActive, true, 'state response updates shared recording snapshot');
      assert.equal(p.dot.hidden, false);
      assert.equal(p.input.checked, true);
    });
    await test('backend reason updates clears and stays plain text in both languages', async () => {
      const p = build(); await settle();
      for (const language of ['zh', 'en']) {
        setLanguage(language);
        const reason = '<b>Recorder resources are closing</b>';
        send(state(true, { recordingCanChange: false, recordingReason: reason }));
        assert.equal(store.get().recordingState?.recordingReason, reason);
        assert.equal(p.hint.textContent, reason, 'backend reason is data, not translated or HTML');
        assert.equal(p.hint.querySelector('b'), null);
        send(state(true, { recordingReason: 'Recovered' }));
        assert.equal(p.hint.textContent, 'Recovered');
        send(state());
        assert.equal(p.hint.textContent, tr('仅生成字幕，不保存麦克风音频'));
        send(state(false, { recordingReason: { invalid: true } }));
        assert.equal(p.hint.textContent, tr('仅生成字幕，不保存麦克风音频'));
      }
    });
    const change = p => { p.input.checked = !p.input.checked; p.input.dispatchEvent(dom.makeEvent('change')); };
    const safeUnknown = p => {
      assert.equal(p.input.indeterminate, true);
      assert.equal(p.input.disabled, true);
      assert.equal(p.input.checked, false);
      assert.equal(p.dot.hidden, true);
    };
    await test('malformed state events clear old authority and recover', async () => {
      const p = build(); await settle();
      for (const invalid of [{}, { recordingEnabled: true }, state(true, { recordingActive: 'yes' })]) {
        send(state(true, { recordingActive: true }));
        send({ running: false, paused: false, mode: 'a', ...invalid });
        assert.equal(store.get().recordingState, null);
        safeUnknown(p);
      }
      send(state()); assert.equal(p.input.disabled, false);
    });
    await test('mode switches pause and resume consume only backend active capability', async () => {
      const p = build(); await settle();
      send(state(true, { running: true, recordingActive: true, recordingCanChange: false }));
      assert.equal(p.dot.hidden, false);
      send(state(true, { running: true, paused: true, recordingActive: false, recordingCanChange: false }));
      assert.equal(p.dot.hidden, true); assert.equal(p.input.disabled, true);
      send(state(true, { running: true, recordingActive: true, recordingCanChange: false }));
      assert.equal(p.dot.hidden, false);
      for (const mode of ['b', 'c', 'd']) {
        send(state(true, { mode, recordingSupported: false, recordingCanChange: false }));
        assert.equal(p.input.disabled, true); assert.equal(p.dot.hidden, true);
        assert.equal(p.hint.textContent, tr('录音仅在麦克风同传模式可用'));
      }
      send(state()); assert.equal(p.input.disabled, false); assert.equal(p.input.checked, false);
    });
    await test('disconnect invalidates snapshot and in-flight acknowledgement until ready', async () => {
      const p = build(); await settle();
      const ack = deferred(); set = () => ack.promise;
      change(p); await settle();
      emit({ type: 'disconnected', reason: 'test transport exit' });
      assert.equal(store.get().recordingState, null, 'disconnect must clear stale authoritative snapshot');
      safeUnknown(p);
      assert.equal(p.hint.textContent, tr('后端已断开，无法确认录音保存状态'));
      send(state(true, { recordingActive: true }));
      ack.resolve({ ok: true, data: state(true) }); await settle();
      safeUnknown(p);
      ready(state(false, { recordingReason: 'Reconnected' }));
      assert.equal(p.input.checked, false); assert.equal(p.input.disabled, false);
      assert.equal(p.hint.textContent, 'Reconnected');
      await settle();
    });
    await test('ready without recording snapshot clears old state until resync arrives', async () => {
      const p = build(); await settle();
      send(state(true, { recordingActive: true }));
      const query = deferred(); read = () => query.promise;
      ready(undefined);
      assert.equal(store.get().recordingState, null);
      safeUnknown(p);
      query.resolve({ ok: true, data: state() }); await settle();
      assert.equal(p.input.indeterminate, false); assert.equal(p.input.disabled, false);
    });
    await test('late resync reply cannot overwrite newer state event or reconnect', async () => {
      const p = build(); await settle();
      const oldRead = deferred(); read = () => oldRead.promise;
      ready(state()); await settle();
      send(state(true, { recordingActive: true, recordingCanChange: false }));
      oldRead.resolve({ ok: true, data: state() }); await settle();
      assert.equal(store.get().recordingState.recordingActive, true);
      assert.equal(p.dot.hidden, false); assert.equal(p.input.disabled, true);
      const previousConnection = deferred(); read = () => previousConnection.promise;
      ready(state()); await settle();
      emit({ type: 'disconnected' });
      read = async () => ({ ok: true, data: state(true, { recordingActive: true }) });
      ready(state(true)); await settle();
      previousConnection.resolve({ ok: true, data: state() }); await settle();
      assert.equal(p.input.checked, true); assert.equal(p.dot.hidden, false);
    });
    await test('refused timeout and unknown replies remain safe across page rebuild', async () => {
      for (const response of [
        { ok: false, code: 'busy', error: 'recorder closing' },
        { ok: false, timedOut: true, delivery: 'unknown' },
        { ok: true, data: {} },
      ]) {
        send(state());
        const p = build(); await settle();
        set = async () => response;
        change(p); await settle();
        safeUnknown(p);
        assert.equal(store.get().recordingState, null, 'uncertain command must not leave reusable stale snapshot');
        p.dispose();
        const held = deferred(); read = () => held.promise;
        const next = build(); safeUnknown(next); next.dispose();
      }
    });
    await test('accepted command snapshot survives rebuild without optimistic UI', async () => {
      send(state());
      const p = build(); await settle();
      const ack = deferred(); set = () => ack.promise;
      change(p);
      assert.equal(p.input.checked, false, 'click is intent, not confirmation');
      assert.equal(p.input.getAttribute('aria-busy'), 'true');
      await settle(); ack.resolve({ ok: true, data: state(true) }); await settle();
      assert.equal(p.input.checked, true);
      assert.equal(store.get().recordingState.recordingEnabled, true);
      p.dispose();
      const next = build(); assert.equal(next.input.checked, true);
    });
    const subscriptions = new Set();
    const originalSubscribe = store.subscribe.bind(store);
    store.subscribe = fn => {
      const unsubscribe = originalSubscribe(fn); subscriptions.add(fn);
      return () => { subscriptions.delete(fn); unsubscribe(); };
    };
    const snapshot = p => ({ checked: p.input.checked, disabled: p.input.disabled,
      indeterminate: p.input.indeterminate, hidden: p.dot.hidden, hint: p.hint.textContent });
    await test('dispose removes recording listeners and old DOM stays untouched', async () => {
      send(state()); const p = build(); await settle();
      assert.equal(subscriptions.size, 1);
      p.dispose(); p.dispose();
      const before = snapshot(p);
      const beforeCommands = commands.length;
      assert.equal(subscriptions.size, 0);
      assert.equal(p.input.listenerCount('change'), 0, 'recording DOM listener must be released');
      send(state(true, { recordingActive: true }));
      assert.deepEqual(snapshot(p), before);
      p.input.dispatchEvent(dom.makeEvent('change')); await settle();
      assert.equal(commands.length, beforeCommands, 'disposed input must not submit commands');
      assert.equal(dom.timers.live.size, 0);
    });
    await test('replacement before old dispose isolates owners and pending responses', async () => {
      send(state()); const old = build(); await settle();
      const ack = deferred(); set = () => ack.promise;
      change(old); await settle();
      const replacement = build(); await settle();
      const oldView = snapshot(old);
      old.dispose();
      send(state(true, { recordingActive: true, recordingCanChange: false }));
      assert.equal(replacement.dot.hidden, false, 'old dispose must not disconnect the replacement controller');
      assert.equal(replacement.input.disabled, true);
      assert.equal(subscriptions.size, 1, 'only the current workspace owns a store listener');
      ack.resolve({ ok: true, data: state() }); await settle();
      assert.equal(replacement.dot.hidden, false);
      assert.equal(store.get().recordingState.recordingActive, true);
      assert.deepEqual(snapshot(old), oldView, 'superseded DOM is no longer writable');
    });
    await test('repeated rebuild and state bursts do not multiply subscriptions', async () => {
      const windowBefore = dom.residualListeners().live;
      for (let i = 0; i < 20; i++) {
        send(state()); const p = build(); await settle();
        assert.equal(subscriptions.size, 1);
        for (let j = 0; j < 30; j++) send(state(j % 2 === 0));
        assert.equal(subscriptions.size, 1);
        assert.equal(listeners.size, 1, 'workspaces reuse the existing backend/store connection');
        p.dispose(); assert.equal(subscriptions.size, 0);
        assert.equal(dom.timers.live.size, 0);
      }
      assert.deepEqual(dom.residualListeners().live, windowBefore);
    });
    await test('backend event wins over in-flight click acknowledgement and unrelated patches', async () => {
      send(state()); const p = build(); await settle();
      const ack = deferred(); set = () => ack.promise;
      const count = commands.length;
      change(p); await settle();
      assert.deepEqual(commands.slice(count), [{ command: CMD.setRecording, args: { enabled: true } }]);
      send(state(false, { recordingCanChange: false, recordingReason: 'Closing' }));
      store.pushLog({ ts: '', level: 'INFO', message: 'unrelated' });
      ack.resolve({ ok: true, data: state(true) }); await settle();
      assert.equal(p.input.checked, false); assert.equal(p.input.disabled, true);
      assert.equal(p.hint.textContent, 'Closing');
      assert.equal(store.get().recordingState.recordingEnabled, false);
      send(state(true)); assert.equal(p.input.checked, true);
    });
    await test('state event between click and queued dispatch cancels obsolete intent', async () => {
      send(state()); const p = build(); await settle();
      let sets = 0;
      set = async () => { sets++; return { ok: true, data: state(true) }; };
      change(p);
      send(state(false, { recordingCanChange: false }));
      await settle();
      assert.equal(sets, 0, 'do not dispatch a queued click after backend revokes admission');
      assert.equal(p.input.checked, false); assert.equal(p.input.disabled, true);
    });
    await test('rapid clicks serialize latest intent through real workspace transport', async () => {
      send(state()); const p = build(); await settle();
      const a = deferred(), b = deferred(), requests = [];
      set = args => { requests.push(args.enabled); return requests.length === 1 ? a.promise : b.promise; };
      change(p); await settle(); change(p);
      assert.deepEqual(requests, [true]);
      a.resolve({ ok: true, data: state(true) }); await settle();
      assert.deepEqual(requests, [true, false]);
      b.resolve({ ok: true, data: state() }); await settle();
      assert.equal(p.input.checked, false); assert.equal(p.input.getAttribute('aria-busy'), 'false');
    });
    await test('timeout notification invalidates recording but unrelated timeouts do not', async () => {
      send(state()); const p = build(); await settle();
      emit({ type: 'request-timeout', command: 'list_models' });
      assert.equal(p.input.disabled, false);
      const ack = deferred(); set = () => ack.promise;
      change(p); await settle();
      emit({ type: 'request-timeout', command: CMD.setRecording });
      safeUnknown(p);
      assert.equal(store.get().recordingState, null);
      ack.resolve({ ok: true, data: state(true) }); await settle();
      safeUnknown(p);
      send(state()); assert.equal(p.input.disabled, false);
    });
    await test('initial page read from a previous ready cannot resurrect stale recording', async () => {
      const oldRead = deferred(), currentRead = deferred();
      read = () => oldRead.promise;
      ready(undefined);
      const p = build(); await settle();
      read = () => currentRead.promise;
      ready(undefined); await settle();
      oldRead.resolve({ ok: true, data: state(true, { recordingActive: true }) }); await settle();
      safeUnknown(p);
      currentRead.resolve({ ok: true, data: state() }); await settle();
      assert.equal(p.input.disabled, false); assert.equal(p.input.checked, false);
    });
    await test('pending initial read cannot write a disposed page or its replacement', async () => {
      const held = deferred(); read = () => held.promise;
      ready(undefined); const old = build(); await settle();
      old.dispose(); const before = snapshot(old);
      send(state()); const replacement = build(); await settle();
      held.resolve({ ok: true, data: state(true, { recordingActive: true }) }); await settle();
      assert.deepEqual(snapshot(old), before);
      assert.equal(replacement.input.checked, false); assert.equal(replacement.dot.hidden, true);
    });
    await test('resync transport rejection is unknown rather than an unhandled ready failure', async () => {
      send(state(true, { recordingActive: true })); const p = build(); await settle();
      read = async () => { throw Error('transport closed'); };
      await assert.doesNotReject(() => refreshSessionState());
      safeUnknown(p);
      assert.equal(store.get().recordingState, null);
    });
    for (const [label, response, enabled] of [
      ['success', { ok: true, data: state(true) }, true],
      ['refused snapshot', { ok: true, data: state(false) }, false],
      ['refused command', { ok: false, code: 'busy', error: 'closing' }, null],
      ['unknown timeout', { ok: false, timedOut: true, delivery: 'unknown' }, null],
      ['malformed success', { ok: true, data: {} }, null],
      ['transport rejection', null, null],
    ]) {
      await test(`sent recording ${label} settles shared authority after dispose/rebuild without events`, async () => {
        const ack = deferred(), sent = deferred();
        set = args => { assert.equal(args.enabled, true); sent.resolve(); return ack.promise; };
        const old = build(); change(old); await sent.promise;
        old.dispose(); const before = snapshot(old);
        let reads = 0; read = async () => { reads++; return { ok: true, data: state() }; };
        const next = build();
        assert.equal(subscriptions.size, 1);
        // The previous known snapshot must not admit another write while one is sent.
        assert.equal(next.input.disabled, true, 'replacement cannot reuse pre-command authority');
        const sentCount = commands.filter(c => c.command === CMD.setRecording).length;
        change(next); await settle();
        assert.equal(commands.filter(c => c.command === CMD.setRecording).length, sentCount, 'replacement cannot dispatch a concurrent recording write');
        if (response) ack.resolve(response); else ack.reject(Error('transport closed'));
        await settle();
        assert.deepEqual(snapshot(old), before, 'old DOM never receives the completion');
        assert.equal(reads, 0, 'do not race an in-flight write with an unsequenced page read');
        if (enabled === null) { safeUnknown(next); assert.equal(store.get().recordingState, null); }
        else {
          assert.equal(next.input.checked, enabled);
          assert.equal(next.input.disabled, false);
          assert.equal(next.input.indeterminate, false);
          assert.equal(store.get().recordingState.recordingEnabled, enabled);
        }
      });
    }
    for (const superseder of ['state', 'reconnect']) {
      await test(`newer ${superseder} supersedes recording reply after dispose/rebuild`, async () => {
        const ack = deferred(), sent = deferred();
        set = () => { sent.resolve(); return ack.promise; };
        const old = build(); change(old); await sent.promise;
        old.dispose(); const before = snapshot(old); const next = build();
        const latest = state(false, { recordingCanChange: false, recordingReason: 'New authority' });
        if (superseder === 'state') send(latest);
        else { emit({ type: 'disconnected' }); read = async () => ({ ok: true, data: latest }); ready(latest); await settle(); }
        const expected = snapshot(next);
        ack.resolve({ ok: true, data: state(true) }); await settle();
        assert.deepEqual(snapshot(next), expected);
        assert.equal(store.get().recordingState.recordingReason, 'New authority');
        assert.deepEqual(snapshot(old), before);
      });
    }
    const sessionClick = (p, command) => p.element.querySelector(
      command === CMD.pause || command === CMD.resume ? 'button.btn--ghost' : 'button.btn--primary',
    ).dispatchEvent(dom.makeEvent('click'));
    for (const command of [CMD.start, CMD.pause, CMD.resume, CMD.stop]) {
      const initial = state(false, { running: command !== CMD.start, paused: command === CMD.resume });
      const reply = state(true, { running: command !== CMD.stop, paused: command === CMD.pause,
        recordingCanChange: command === CMD.stop, recordingReason: `${command} acknowledged` });
      await test(`${command} valid reply reaches replacement without a state event`, async () => {
        send(initial);
        const ack = deferred(), sent = deferred();
        session = actual => { assert.equal(actual, command); sent.resolve(); return ack.promise; };
        const old = build(); sessionClick(old, command); await sent.promise;
        old.dispose(); const before = snapshot(old); const next = build();
        ack.resolve({ ok: true, data: reply }); await settle();
        assert.equal(store.get().running, reply.running);
        assert.equal(store.get().paused, reply.paused);
        assert.equal(next.input.checked, true);
        assert.equal(next.input.disabled, !reply.recordingCanChange);
        assert.equal(next.hint.textContent, reply.recordingReason);
        assert.deepEqual(snapshot(old), before);
      });
      for (const superseder of ['state', 'reconnect', 'newer command']) {
        await test(`${command} stale reply cannot overwrite ${superseder} in replacement`, async () => {
          send(initial);
          const ack = deferred(), sent = deferred();
          session = actual => { assert.equal(actual, command); sent.resolve(); return ack.promise; };
          const old = build(); sessionClick(old, command); await sent.promise;
          old.dispose(); const before = snapshot(old); const next = build();
          const latest = state(false, { recordingReason: 'Latest authority' });
          if (superseder === 'state') send(latest);
          else if (superseder === 'reconnect') {
            emit({ type: 'disconnected' }); read = async () => ({ ok: true, data: latest }); ready(latest); await settle();
          } else {
            const newer = deferred(), newerSent = deferred();
            session = actual => { assert.equal(actual, command); newerSent.resolve(); return newer.promise; };
            sessionClick(next, command); await newerSent.promise;
            newer.resolve({ ok: true, data: latest }); await settle();
          }
          const expected = snapshot(next);
          ack.resolve({ ok: true, data: reply }); await settle();
          assert.deepEqual(snapshot(next), expected);
          assert.equal(store.get().recordingState.recordingReason, 'Latest authority');
          assert.equal(store.get().running, false);
          assert.equal(store.get().paused, false);
          assert.deepEqual(snapshot(old), before);
        });
      }
    }
    const failures = results.filter(r => !r.ok);
    console.log(`Recording workspace MiniDOM: ${results.length - failures.length} passed / ${failures.length} failed / ${results.length} total`);
    assert.equal(failures.length, 0, failures.map(r => r.name).join('; '));
  } finally {
    page?.dispose();
    off?.();
    dom.restore();
  }
}
