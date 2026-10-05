"""Quiet real HTTP, durable session and crash/restart regressions (synthetic bytes only)."""
from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from voxsub.downloader import DownloadCancelled, fetch_file
from voxsub.model_catalog import ModelMarketplace, ModelSource, RemoteFile, get_model
from voxsub.model_downloads import ModelDownloads
from voxsub.models import ModelManager


@pytest.fixture
def server():
    class Handler(http.server.BaseHTTPRequestHandler):
        payload = bytes(range(256)) * (32 * 1024)
        etag = '"v1"'
        delay = .002
        chunk_size = 64 * 1024
        ignore_range = False
        bad_range = False
        requests = []

        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.send_response(200); self.end_headers()

        def do_GET(self):
            cls = type(self)
            rng = self.headers.get("Range", "")
            cls.requests.append((self.path, rng, self.headers.get("If-Range", "")))
            resume = bool(rng) and not cls.ignore_range
            if self.headers.get("If-Range") and self.headers["If-Range"] != cls.etag:
                resume = False
            start = int(rng.split("=")[1].split("-")[0]) if resume else 0
            if start >= len(cls.payload):
                self.send_response(416); self.end_headers(); return
            body = cls.payload[start:]
            self.send_response(206 if resume else 200)
            self.send_header("Content-Length", str(len(body)))
            if cls.etag:
                self.send_header("ETag", cls.etag)
            if resume:
                offset = start + 1 if cls.bad_range else start
                self.send_header("Content-Range", f"bytes {offset}-{len(cls.payload)-1}/{len(cls.payload)}")
            self.end_headers()
            try:
                for i in range(0, len(body), cls.chunk_size):
                    self.wfile.write(body[i:i+cls.chunk_size]); self.wfile.flush()
                    time.sleep(cls.delay)
            except (OSError, ConnectionError):
                pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", Handler
    finally:
        httpd.shutdown(); httpd.server_close(); thread.join(3)


def spec_for(url, data, *, sha=True, archive=False):
    return replace(get_model("asr-moonshine-tiny-en-v2"), id="test-resumable", install_rel="test/model",
                   asset_name="weights.tar.bz2" if archive else "model.bin", archive=archive,
                   required_paths=("model.bin",), required_patterns=(), legacy_install_rels=(),
                   download_bytes=len(data), sha256=hashlib.sha256(data).hexdigest() if sha else "",
                   sources=(ModelSource("global", "海外", url+"/global", url+"/global"),
                            ModelSource("china", "中国大陆", url+"/china", url+"/china")))


def partial(manager, model, data):
    state = manager.prepare(model, "china")
    manager.pause(model, state["token"])
    p = manager.folder / (model.asset_name + ".part")
    p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
    return state


def test_queued_pause_is_durable_and_never_starts_network(tmp_path, server):
    url, h = server; m = spec_for(url, h.payload)
    d = ModelDownloads(ModelMarketplace(tmp_path)); s = d.prepare(m, "china")
    assert d.pause(m, s["token"])["status"] == "paused"
    assert d.run(m, s["token"], lambda: False) is None
    recovered = ModelDownloads(ModelMarketplace(tmp_path)).snapshot(m)
    assert recovered["status"] == "paused" and recovered["source"] == "china"
    assert h.requests == []


@pytest.mark.parametrize("preference", ["global", "china", "auto"])
def test_real_pause_resume_and_selected_source(tmp_path, server, preference):
    url, h = server; m = spec_for(url, h.payload); reports = []
    d = ModelDownloads(ModelMarketplace(tmp_path), reports.append)
    s = d.prepare(m, preference)
    def emit(state):
        reports.append(state)
        if state["status"] == "downloading" and state["completed"] > 0:
            d.pause(m, s["token"])
    d.emit = emit
    paused = d.run(m, s["token"], lambda: False)
    assert paused["status"] == "paused" and 0 < paused["completed"] < len(h.payload)
    before = (d.folder / "model.bin.part").stat().st_size
    assert not d.market.is_installed(m)
    assert any(r["status"] == "pausing" for r in reports)
    d.emit = reports.append
    s2 = d.prepare(m, preference)
    assert d.run(m, s2["token"], lambda: False)["status"] == "done"
    assert d.market.model_file(m).read_bytes() == h.payload
    assert h.requests[-1][1:] == (f"bytes={before}-", '"v1"')
    if preference != "auto": assert h.requests[-1][0] == '/'+preference
    assert len({r["revision"] for r in reports}) == len(reports)
    assert d.snapshot(m) is None


def test_process_killed_mid_download_recovers_paused_and_resumes(tmp_path, server):
    url, h = server; h.delay = .015
    config = {"root":str(tmp_path),"url":url,"size":len(h.payload),"sha":hashlib.sha256(h.payload).hexdigest()}
    # Spawn only our own headless Python, with isolated model root and no audio imports.
    code = '''import json,sys
from dataclasses import replace
from voxsub.model_catalog import ModelMarketplace,ModelSource,get_model
from voxsub.model_downloads import ModelDownloads
c=json.loads(sys.argv[1]);m=replace(get_model("asr-moonshine-tiny-en-v2"),id="test-resumable",install_rel="test/model",asset_name="model.bin",archive=False,required_paths=("model.bin",),required_patterns=(),legacy_install_rels=(),download_bytes=c["size"],sha256=c["sha"],sources=(ModelSource("china","中国大陆",c["url"]+"/china",c["url"]+"/china"),))
d=ModelDownloads(ModelMarketplace(c["root"]));s=d.prepare(m,"china");d.run(m,s["token"],lambda:False)
'''
    env=os.environ.copy();env.pop("PYTHONPATH",None);env.pop("PYTHONHOME",None)
    child=subprocess.Popen([sys.executable,"-c",code,json.dumps(config)],cwd=Path(__file__).resolve().parents[1],
                           env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    part=tmp_path/".downloads/model.bin.part"
    try:
        for _ in range(500):
            if part.exists() and part.stat().st_size > 0: break
            assert child.poll() is None
            time.sleep(.01)
        else: pytest.fail("isolated download did not write a part")
        child.kill();child.wait(5)
    finally:
        if child.poll() is None: child.kill();child.wait(5)
    m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path));before=part.stat().st_size
    requests=len(h.requests);s=d.snapshot(m)
    assert s["status"] == "paused" and s["completed"] == before
    time.sleep(.05);assert len(h.requests)==requests  # Restore is not auto-resume.
    h.delay=0;s=d.prepare(m,"china");d.run(m,s["token"],lambda:False)
    assert h.requests[-1][1]==f"bytes={before}-"
    assert d.market.model_file(m).read_bytes()==h.payload


def test_delete_only_own_partial_and_never_installed_files(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path))
    s=partial(d,m,h.payload[:1024]);installed=d.market.model_dir(m)/"keep.txt"
    installed.parent.mkdir(parents=True);installed.write_bytes(b"installed")
    other=d.folder/"other.part";other.write_bytes(b"other")
    before=d.snapshot(m)["revision"]
    assert d.delete(m,s["token"])["status"]=="deleted"
    assert installed.read_bytes()==b"installed" and other.read_bytes()==b"other"
    assert not (d.folder/"model.bin.part").exists()
    assert ModelDownloads(ModelMarketplace(tmp_path)).snapshot(m) is None
    assert d.prepare(m,"global")["revision"]>before


def test_active_or_stale_delete_and_duplicate_prepare_are_rejected(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,"china")
    with pytest.raises(RuntimeError):d.prepare(m,"global")
    with pytest.raises(RuntimeError):d.delete(m,s["token"])
    with pytest.raises(RuntimeError):d.pause(m,"stale")
    d.pause(m,s["token"]);s2=d.prepare(m,"global")
    assert d.run(m,s["token"],lambda:False) is None
    assert d.snapshot(m)["token"]==s2["token"] and h.requests==[]


def test_old_or_corrupt_record_adopts_only_trusted_partial_paths(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path))
    part=tmp_path/".downloads/model.bin.part";part.parent.mkdir();part.write_bytes(b"prefix")
    record=tmp_path/".downloads/.states/test-resumable.json";record.parent.mkdir()
    record.write_text('{"modelId":"test-resumable","status":[],"path":"../../outside"}')
    s=d.snapshot(m);assert s["status"]=="paused" and s["completed"]==6


@pytest.mark.parametrize("ignore_range", [False, True])
def test_etag_change_or_ignored_range_restarts_without_mixed_bytes(tmp_path, server, ignore_range):
    url,h=server;m=spec_for(url,h.payload,sha=False);d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,"china")
    d.emit=lambda st:d.pause(m,s["token"]) if st["status"]=="downloading" and st["completed"]>0 else None
    d.run(m,s["token"],lambda:False)
    old=len(h.requests);h.payload=b"Z"*len(h.payload);h.etag='"v2"';h.ignore_range=ignore_range;h.delay=0
    d.emit=lambda state:None;s=d.prepare(m,"china");d.run(m,s["token"],lambda:False)
    assert d.market.model_file(m).read_bytes()==h.payload and len(h.requests)>old


def test_bad_content_range_never_promotes_or_appends(tmp_path, server):
    url,h=server;h.bad_range=True;dest=tmp_path/"x.bin";part=tmp_path/"x.bin.part";part.write_bytes(h.payload[:1024])
    assert not fetch_file(url,dest,expected_sha=hashlib.sha256(h.payload).hexdigest(),expected_size=len(h.payload),safe_resume=True)
    assert not dest.exists() and part.read_bytes()==h.payload[:1024]


def test_no_validator_and_no_sha_safely_restarts_legacy_part(tmp_path, server):
    url,h=server;h.etag='';h.delay=0;dest=tmp_path/"x.bin";dest.with_name("x.bin.part").write_bytes(b"garbage")
    assert fetch_file(url,dest,expected_size=len(h.payload),safe_resume=True)
    assert h.requests[0][1]=='' and dest.read_bytes()==h.payload


def test_network_failure_preserves_paused_record(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path))
    def fail(*args,**kw):raise OSError("offline")
    d.market.install=fail;s=d.prepare(m,"china")
    with pytest.raises(OSError):d.run(m,s["token"],lambda:False)
    s2=ModelDownloads(ModelMarketplace(tmp_path)).snapshot(m)
    assert s2["status"]=="paused" and 'offline' in s2["error"]


def test_multifile_resume_reuses_completed_assets_and_partial_prefix(tmp_path, server):
    url,h=server;h.delay=0
    files=tuple(RemoteFile(url+f'/file{i}',f'{i}.bin',len(h.payload),hashlib.sha256(h.payload).hexdigest()) for i in range(2))
    m=replace(spec_for(url,h.payload),asset_name='',sha256='',download_bytes=2*len(h.payload),required_paths=('0.bin','1.bin'),sources=(ModelSource('china','中国大陆','',url+'/probe',files=files),))
    d=ModelDownloads(ModelMarketplace(tmp_path));p=d.folder/m.id;p.mkdir(parents=True)
    (p/'0.bin').write_bytes(h.payload);(p/'1.bin.part').write_bytes(h.payload[:1024])
    assert d.snapshot(m)['completed']==len(h.payload)+1024
    s=d.prepare(m,'china');d.run(m,s['token'],lambda:False)
    assert len(h.requests)==1 and h.requests[0][0]=='/file1' and h.requests[0][1]=='bytes=1024-'
    assert d.market.is_installed(m)


def test_archive_pause_retains_completed_archive_and_publishes_atomically(tmp_path, server):
    url,h=server;body=b'fake weights';out=io.BytesIO()
    with tarfile.open(fileobj=out,mode='w:bz2') as tf:
        info=tarfile.TarInfo('package/model.bin');info.size=len(body);tf.addfile(info,io.BytesIO(body))
    h.payload=out.getvalue();m=spec_for(url,h.payload,archive=True);d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,'china')
    d.emit=lambda state:d.pause(m,s['token']) if state['stage']=='校验并安装' else None
    # _report throttles events; force one for the extraction-boundary test.
    d.last_report[m.id]=-1e10
    def emit(state):
        if state['status']=='downloading':d.last_report[m.id]=-1e10
        if state['stage']=='校验并安装':d.pause(m,s['token'])
    d.emit=emit
    assert d.run(m,s['token'],lambda:False)['status']=='paused'
    assert (d.folder/m.asset_name).exists() and not d.market.model_dir(m).exists()
    d.emit=lambda state:None;s=d.prepare(m,'china');d.run(m,s['token'],lambda:False)
    assert d.market.model_file(m).read_bytes()==body
    assert len(h.requests)==1


def test_internal_download_artifacts_excluded_from_model_integrity_manifest(tmp_path):
    (tmp_path/'.downloads/.states').mkdir(parents=True)
    (tmp_path/'.downloads/.states/x.json').write_text('{}')
    (tmp_path/'model.part.resume.json').write_text('{}')
    (tmp_path/'tokens.txt').write_text('test')
    manager=ModelManager(tmp_path);manager.scan();manifest=manager.load_manifest()
    assert set(manifest['files'])=={'tokens.txt'}

def test_real_ipc_pause_bypasses_busy_worker_and_never_initializes_pipeline(tmp_path, server, monkeypatch):
    backend=Path(__file__).resolve().parents[1]/'frontend/backend';monkeypatch.syspath_prepend(str(backend))
    import ipc_loop,job_runner
    from ipc_server import BackendService
    import handlers.models as handlers
    url,h=server;m=spec_for(url,h.payload);market=ModelMarketplace(tmp_path)
    started=threading.Event()
    def blocking_install(model,**options):
        started.set()
        for _ in range(500):
            if options['cancelled']():raise DownloadCancelled('paused')
            time.sleep(.005)
        pytest.fail('pause was queued behind the download')
    market.install=blocking_install
    service=BackendService();service._marketplace=lambda args:market;service._spec=lambda mid:m
    service.ensure_pipeline=lambda:pytest.fail('download controls must not initialize inference')
    emitted=[];runner=job_runner.JobRunner();loop=ipc_loop.IpcLoop(service,runner,emitted.append)
    monkeypatch.setattr(handlers,'_event',lambda kind,**fields:loop._event(kind,**fields))
    service.bind_job_runner(runner);runner.start()
    def request(i,cmd,args):loop.handle_line(json.dumps({'id':i,'command':cmd,'args':args}))
    try:
        request(1,'prepare_model_download',{'model_id':m.id,'models_root':str(tmp_path),'source':'china'})
        reply=next(e for e in emitted if e.get('id')==1);assert reply['ok'];token=reply['data']['download']['token']
        request(2,'install_model',{'model_id':m.id,'models_root':str(tmp_path),'token':token})
        assert started.wait(2)
        start=time.monotonic();request(3,'pause_model_download',{'model_id':m.id,'models_root':str(tmp_path),'token':token})
        assert time.monotonic()-start<.5
        reply=next(e for e in emitted if e.get('id')==3);assert reply['ok'] and reply['data']['download']['status']=='pausing'
        for _ in range(500):
            if any(e.get('id')==2 for e in emitted):break
            time.sleep(.005)
        final=next(e for e in emitted if e.get('id')==2)
        assert final['ok'] and final['data']['download']['status']=='paused'
        assert not loop._contract_warned and service._pipeline is None
    finally:runner.stop(timeout=3)


def test_delete_also_removes_only_own_interrupted_extraction(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path));s=partial(d,m,b'partial')
    own=tmp_path/'.installing'/m.id;other=tmp_path/'.installing'/'other-model'
    own.mkdir(parents=True);other.mkdir();(own/'temp').write_bytes(b'partial');(other/'keep').write_bytes(b'other')
    d.delete(m,s['token'])
    assert not own.exists() and (other/'keep').read_bytes()==b'other'


def test_download_part_link_is_rejected_without_touching_target(tmp_path, server):
    url,h=server;m=spec_for(url,h.payload);folder=tmp_path/'.downloads';folder.mkdir()
    outside=tmp_path/'outside.bin';outside.write_bytes(b'keep')
    try:(folder/'model.bin.part').symlink_to(outside)
    except OSError:pytest.skip('Windows symlink permission unavailable')
    d=ModelDownloads(ModelMarketplace(tmp_path))
    with pytest.raises(ValueError):d.prepare(m,'china')
    assert outside.read_bytes()==b'keep' and h.requests==[]

def test_multifile_pause_at_commit_boundary_keeps_staged_files(tmp_path, server):
    url,h=server;h.delay=0
    item=RemoteFile(url+'/file','model.bin',len(h.payload),hashlib.sha256(h.payload).hexdigest())
    m=replace(spec_for(url,h.payload),asset_name='',sources=(ModelSource('china','中国大陆','',url+'/probe',files=(item,)),))
    d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,'china')
    def emit(state):
        if state['status']=='downloading':d.last_report[m.id]=-1e10
        if state['stage']=='校验并安装':d.pause(m,s['token'])
    d.emit=emit
    assert d.run(m,s['token'],lambda:False)['status']=='paused'
    assert not d.market.model_dir(m).exists()
    assert (d.folder/m.id/'model.bin').read_bytes()==h.payload
    d.emit=lambda state:None;s=d.prepare(m,'china');d.run(m,s['token'],lambda:False)
    assert d.market.is_installed(m) and len(h.requests)==1


def test_ipc_coordinator_writer_does_not_reuse_legacy_lookup_root(tmp_path, server, monkeypatch):
    backend=Path(__file__).resolve().parents[1]/'frontend/backend';monkeypatch.syspath_prepend(str(backend))
    from ipc_server import BackendService
    import handlers.models as handlers
    monkeypatch.setattr(handlers,'_event',lambda *a,**kw:None)
    url,h=server;m=spec_for(url,h.payload)
    market=ModelMarketplace(tmp_path);market._uses_default_root=True
    market._lookup_roots=(tmp_path,tmp_path/'legacy')
    service=BackendService();service._marketplace=lambda args:market
    writer=service._downloads_for({}).market
    assert writer._lookup_roots==(tmp_path.resolve(),)


def test_corrupt_download_entry_is_reported_without_hiding_other_cards(tmp_path, monkeypatch):
    backend=Path(__file__).resolve().parents[1]/'frontend/backend';monkeypatch.syspath_prepend(str(backend))
    from ipc_server import BackendService
    from voxsub.model_catalog import CATALOG
    service=BackendService();market=ModelMarketplace(tmp_path);service._marketplace=lambda args:market
    downloads=service._downloads_for({});real=downloads.snapshot
    downloads.snapshot=lambda model:(_ for _ in ()).throw(ValueError('unsafe partial path')) if model.id==CATALOG[0].id else real(model)
    result=service._cmd_list_models({})
    assert len(result['models'])==len(CATALOG)
    assert any('下载状态读取失败' in line for line in result['diagnostics'])

def test_partial_can_switch_china_to_global_when_hash_guarantees_identity(tmp_path, server):
    url,h=server;h.delay=0;m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path))
    s=partial(d,m,h.payload[:1024]);s=d.prepare(m,'global');d.run(m,s['token'],lambda:False)
    assert h.requests[0][0]=='/global' and h.requests[0][1]=='bytes=1024-'
    assert d.market.model_file(m).read_bytes()==h.payload

def test_pause_on_slow_link_does_not_wait_for_a_full_megabyte(tmp_path, server):
    url,h=server;h.chunk_size=1024;h.delay=.01
    m=spec_for(url,h.payload);d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,'china');results=[]
    thread=threading.Thread(target=lambda:results.append(d.run(m,s['token'],lambda:False)),daemon=True)
    thread.start();part=d.folder/'model.bin.part'
    try:
        for _ in range(300):
            if part.exists() and part.stat().st_size>0:break
            time.sleep(.005)
        else:pytest.fail('slow-link fixture did not begin writing available bytes')
        start=time.monotonic();d.pause(m,s['token']);thread.join(.75)
        assert not thread.is_alive() and time.monotonic()-start<.75
        assert results[0]['status']=='paused' and 0<part.stat().st_size<len(h.payload)
    finally:
        h.delay=0
        d.pause(m,s['token'])
        thread.join(3)

def test_auto_resume_prefers_previous_origin_instead_of_reprobing_mirrors(tmp_path, server, monkeypatch):
    url,h=server;m=spec_for(url,h.payload,sha=False);d=ModelDownloads(ModelMarketplace(tmp_path));s=d.prepare(m,'china')
    d.emit=lambda state:d.pause(m,s['token']) if state['status']=='downloading' and state['completed']>0 else None
    d.run(m,s['token'],lambda:False)
    def unexpected_probe(source):pytest.fail('resuming auto must not switch away from a proven origin')
    monkeypatch.setattr(d.market,'_probe',unexpected_probe)
    d.emit=lambda state:None;s=d.prepare(m,'auto');d.run(m,s['token'],lambda:False)
    assert h.requests[-1][0]=='/china' and h.requests[-1][1].startswith('bytes=')
    assert d.market.model_file(m).read_bytes()==h.payload


def test_list_returns_canonical_root_without_rewriting_configuration(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / 'frontend/backend'))
    import handlers.models as handlers
    monkeypatch.setattr(handlers, '_profile_for_cards', lambda failures: None)
    service = handlers.ModelsHandlers()
    service._lock = threading.RLock()
    service._model_downloads = {}
    market = ModelMarketplace(Path('relative-models'))
    service._marketplace = lambda args: market
    result = service._cmd_list_models({})
    assert result['modelsRoot'] == str((tmp_path / 'relative-models').resolve())
    assert market.models_dir == Path('relative-models')


def test_archive_rejects_redirected_staging_before_extraction(tmp_path, monkeypatch):
    model = spec_for('http://unused', b'fixture', archive=True)
    market = ModelMarketplace(tmp_path)
    staging = tmp_path / '.installing' / model.id
    outside = tmp_path / 'unrelated'; outside.mkdir()
    keep = outside / 'keep'; keep.write_bytes(b'unchanged')
    resolve = Path.resolve
    def redirected(path, *args, **kwargs):
        return outside if path == staging else resolve(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'resolve', redirected)
    monkeypatch.setattr(market, '_extract_archive', lambda *args: pytest.fail('unsafe extraction'))
    with pytest.raises(RuntimeError, match='临时目录'):
        market._install_archive(model, tmp_path / 'fake.tar', market.model_dir(model))
    assert keep.read_bytes() == b'unchanged'
    assert not staging.exists()


@pytest.mark.parametrize("suffix", ["", ".part", ".part.resume.json"])
@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("multifile", [False, True])
def test_download_link_validation_covers_progress_and_cached_sessions(tmp_path, server, suffix, cached, multifile):
    url, handler = server
    model = spec_for(url, handler.payload)
    if multifile:
        item = RemoteFile(url + "/file", "nested/model.bin", len(handler.payload))
        model = replace(model, asset_name="", sources=(ModelSource("china", "中国", "", url, (item,)),))
    downloads = ModelDownloads(ModelMarketplace(tmp_path))
    if cached:
        state = downloads.prepare(model, "china")
        downloads.pause(model, state["token"])
    relative = f"{model.id}/nested/model.bin" if multifile else "model.bin"
    link = downloads.folder / (relative + suffix)
    link.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-kept.bin"
    outside.write_bytes(b"keep")
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Windows symlink permission unavailable")
    with pytest.raises(ValueError):
        downloads.prepare(model, "china")
    with pytest.raises(ValueError):
        downloads.snapshot(model)
    assert outside.read_bytes() == b"keep" and handler.requests == []


def test_prepare_validates_asset_paths_even_without_symlink_privilege(tmp_path, monkeypatch):
    model = spec_for("http://unused.invalid", b"keep")
    downloads = ModelDownloads(ModelMarketplace(tmp_path))
    original = downloads._path
    seen = []
    def checked(relative):
        seen.append(relative)
        if relative.endswith(".part"):
            raise ValueError("simulated reparse-point rejection")
        return original(relative)
    monkeypatch.setattr(downloads, "_path", checked)
    with pytest.raises(ValueError):
        downloads.prepare(model, "china")
    assert "model.bin.part" in seen and not downloads.states
