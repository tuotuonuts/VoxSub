"""QwenQualityTranslator 生命周期与并发初始化测试 (缺陷 #7)。

测试纪律
--------
* 被测逻辑真跑: 存活判定、旧实例摘除、重建、终止、close() 幂等 —— 全部走真实代码路径。
* 只替换外部边界:
    - OS 进程边界: ``subprocess.Popen`` (替身, 绝不真的拉起 llama-server);
    - HTTP 边界: ``urllib.request.urlopen`` (健康探针) 与 ``chat_completion``;
    - 系统探测边界: detect_hardware / discover_llama_runtimes / select_llama_runtime
      固定为确定性 CPU 运行时, 避免测试依赖本机设备。
* 时序控制只用 threading.Event / Barrier, 不用 sleep 碰运气。

竞态复现 (缺陷 #7a/#7b)
-----------------------
旧 ``_ensure`` 的判定在锁外, 摘除动作却是无条件 ``self.close()``::

    if <陈旧>:            # 锁外快速路径
        self.close()      # 锁外! 拿锁后清状态 + 终止"当前"实例
        with self._lock:  # 阻塞点
            if <仍然陈旧>: self._spawn()

线程 A 持锁 spawn 期间, 线程 B 早已判定陈旧并在 ``self.close()`` 上等锁; A 一建好
实例并释放锁, B 的 close() 就落在这条**新实例**上: 后建的健康实例被误关、状态被清空,
最终开出第二个子进程。见 ``test_concurrent_ensure_does_not_retire_fresh_instance``。
"""
from __future__ import annotations

import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voxsub.hardware import HardwareProfile, LlamaRuntime  # noqa: E402
from voxsub.translate import qwen as qwen_module  # noqa: E402
from voxsub.translate.base import TranslationError  # noqa: E402
from voxsub.translate.qwen import QwenQualityTranslator  # noqa: E402


# ---------------------------------------------------------------------------
# OS 进程边界替身
# ---------------------------------------------------------------------------
class ScriptedStream:
    """llama-server stdout 替身: 按脚本产出行, 耗尽时置位事件。"""

    def __init__(self, lines=(), drained=None) -> None:
        self._lines = list(lines)
        self._drained = drained

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0)
        if self._drained is not None:
            self._drained.set()
        raise StopIteration


class FakeProcess:
    """subprocess.Popen 替身: 只实现 qwen.py 真正使用的边界方法。

    ``drain_event`` 同时作为 stdout 耗尽信号与 "返回退出码前先等日志排空" 的闸门:
    真实进程的 stdout 是由独立线程读取的, 若在它读到崩溃原因之前就 poll 出退出码,
    调用方会拿到不完整的 ``_server_output_tail``。测试里用事件对齐这个先后关系,
    而不是靠 sleep 碰运气。
    """

    def __init__(self, pid: int = 4242, *, exit_code: int | None = None,
                 stdout_lines=(), drain_event=None) -> None:
        self.pid = pid
        self.returncode = exit_code
        self.stdout = ScriptedStream(stdout_lines, drain_event)
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self._alive = exit_code is None
        self._drain_event = drain_event

    def poll(self):  # noqa: A003 - 对齐 subprocess.Popen.poll 语义
        if not self._alive and self._drain_event is not None:
            self._drain_event.wait(timeout=5.0)
        return None if self._alive else (
            self.returncode if self.returncode is not None else 1)

    def wait(self, timeout=None) -> int:
        self.wait_calls += 1
        return self.returncode if self.returncode is not None else 0

    def terminate(self) -> None:
        self.terminate_calls += 1
        self._alive = False
        if self.returncode is None:
            self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.terminate()

    @property
    def alive(self) -> bool:
        return self._alive


class PidlessProcess(FakeProcess):
    """异常替身: 进程句柄存在但 ``pid`` 不可用 (激活阶段失败注入)。"""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # 真实场景对应"句柄可用但属性读取失败"; 移除实例属性后访问 pid 抛 AttributeError。
        del self.__dict__["pid"]


class Launch:
    """一次被捕获的进程启动。"""

    def __init__(self, command: list[str], environment: dict, proc: FakeProcess) -> None:
        self.command = command
        self.environment = environment
        self.proc = proc

    @property
    def port(self) -> int:
        return int(self.command[self.command.index("--port") + 1])


class ProcessBoundary:
    """``subprocess.Popen`` 替身工厂: 记录每次启动, 按计划返回假进程。"""

    def __init__(self, plan=()) -> None:
        self.launches: list[Launch] = []
        self._plan = list(plan)

    def __call__(self, command, **kwargs) -> FakeProcess:
        spec = self._plan.pop(0) if self._plan else {}
        if isinstance(spec, FakeProcess):
            proc = spec
        else:
            factory = spec.get("factory", FakeProcess)
            proc = factory(**spec.get("kwargs", {}))
        self.launches.append(Launch(list(command), dict(kwargs), proc))
        hook = spec.get("on_start")
        if hook is not None:
            hook(self.launches[-1])
        return proc


class _SubprocessBoundary:
    """只替换 ``Popen``, 保留 qwen.py 使用的其余 subprocess 常量与异常。"""

    PIPE = subprocess.PIPE
    STDOUT = subprocess.STDOUT
    TimeoutExpired = subprocess.TimeoutExpired
    CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def __init__(self, popen) -> None:
        self.Popen = popen


# ---------------------------------------------------------------------------
# 网络边界替身
# ---------------------------------------------------------------------------
class _FakeHTTPResponse:
    def __init__(self, status: int = 200, payload: bytes = b"{}") -> None:
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def read(self, *_args) -> bytes:
        return self._payload


def install_http_boundary(monkeypatch, *, status: int = 200, calls=None) -> None:
    """替换健康探针的 HTTP 边界 (urllib.request.urlopen)。"""

    def fake_urlopen(url, timeout=None, **_kwargs):
        if calls is not None:
            calls.append(str(url))
        return _FakeHTTPResponse(status)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


# ---------------------------------------------------------------------------
# 被测对象工厂
# ---------------------------------------------------------------------------
def make_translator(tmp_path: Path, monkeypatch, *,
                    translator_cls=QwenQualityTranslator,
                    model_bytes: bytes = b"x" * 600_000) -> QwenQualityTranslator:
    """构造携带真实模型/可执行文件路径的 translator (硬件探测由替身固定)。"""
    tools = tmp_path / "tools" / "llama"
    tools.mkdir(parents=True, exist_ok=True)
    (tools / "llama-server.exe").write_bytes(b"MZ fake llama-server")
    monkeypatch.setattr(qwen_module, "_default_tools_dir", lambda: tools)
    model = tmp_path / "model.gguf"
    model.write_bytes(model_bytes)
    return translator_cls(model_path=model, n_ctx=64, n_threads=1)


def install_cpu_runtime(monkeypatch, translator: QwenQualityTranslator) -> LlamaRuntime:
    """把硬件/运行时选择边界固定为单 CPU 运行时 (不依赖本机设备)。"""
    profile = HardwareProfile("test cpu", 4, 8, 16.0)
    runtime = LlamaRuntime(Path(translator._server_exe), "cpu", "CPU")
    monkeypatch.setattr(qwen_module, "detect_hardware", lambda: profile)
    monkeypatch.setattr(qwen_module, "discover_llama_runtimes", lambda: [runtime])
    monkeypatch.setattr(
        qwen_module, "select_llama_runtime", lambda *_a, **_kw: runtime)
    return runtime


def install_boundaries(monkeypatch, *, plan=(), http_status: int = 200,
                       http_calls=None) -> ProcessBoundary:
    """一次性装好 OS 进程边界与 HTTP 边界。"""
    boundary = ProcessBoundary(plan)
    monkeypatch.setattr(qwen_module, "subprocess", _SubprocessBoundary(boundary))
    install_http_boundary(monkeypatch, status=http_status, calls=http_calls)
    return boundary


def prepare(tmp_path, monkeypatch, *, plan=(), translator_cls=QwenQualityTranslator):
    """常见组合: 真实路径 + CPU 运行时替身 + OS/HTTP 边界替身。"""
    translator = make_translator(tmp_path, monkeypatch, translator_cls=translator_cls)
    install_cpu_runtime(monkeypatch, translator)
    boundary = install_boundaries(monkeypatch, plan=plan)
    return translator, boundary


class ProbeTranslator(QwenQualityTranslator):
    """把 ``_endpoint`` 读取变成可观测点, 用于精确编排线程时序。

    只是属性读探针 (读到的值原样返回, 不阻塞、不改变任何同步语义);
    生命周期方法仍然完全走父类的真实实现。
    """

    probe_thread_id: int | None = None
    probe_read: threading.Event | None = None

    @property
    def _endpoint(self) -> str | None:
        value = self.__dict__.get("_endpoint_value")
        event = self.__dict__.get("probe_read")
        if event is not None and threading.get_ident() == self.__dict__.get("probe_thread_id"):
            event.set()
        return value

    @_endpoint.setter
    def _endpoint(self, value: str | None) -> None:
        self.__dict__["_endpoint_value"] = value


def _endpoint_port(endpoint: str) -> int:
    authority = endpoint.split("//", 1)[1].split("/", 1)[0]
    return int(authority.rsplit(":", 1)[1])


def _join(threads, timeout: float = 10.0) -> None:
    for thread in threads:
        thread.join(timeout=timeout)


# ---------------------------------------------------------------------------
# 缺陷 #7 主回归: 并发 _ensure 不得摘除后建的健康实例
# ---------------------------------------------------------------------------
def test_concurrent_ensure_does_not_retire_fresh_instance(tmp_path, monkeypatch) -> None:
    """两个线程并发 _ensure: 早先线程的摘除不得落到后建的健康实例上。

    时序 (全部 Event 编排):
      1. 线程 A 看到陈旧实例 -> 持锁 spawn; 假 Popen 在锁内等 ``release_spawn``。
      2. 线程 B 在 A 仍持锁时完成陈旧判定 (读探针 ``probe_read`` 置位, 说明 B 已经
         在被审查的检查点上, 且此时 A 尚未激活新实例)。
      3. 主线程才放行 A 的 spawn -> B 的 close() 落到 A 刚建好的实例上。

    旧实现 (检查/摘除分离在锁外) 必现: 第一个实例被误关 + 第二次 spawn -> 断言失败。
    修复后 B 在 ``_lifecycle_lock`` 上等待, 复用 A 建好的实例 -> 只 spawn 一次。
    """
    release_spawn = threading.Event()
    spawn_started = threading.Event()

    def blocked_popen(_launch: Launch) -> None:
        spawn_started.set()
        assert release_spawn.wait(timeout=10.0), "spawn 未被放行"

    translator, boundary = prepare(
        tmp_path, monkeypatch,
        plan=[{"on_start": blocked_popen}, {}],   # A 的 spawn(锁内阻塞) / 旧实现里 B 的 spawn
        translator_cls=ProbeTranslator)

    stale = FakeProcess(pid=1, exit_code=1)
    translator._port = 49001
    translator._endpoint = "http://127.0.0.1:49001/v1/chat/completions"
    translator._proc = stale

    probe_read = threading.Event()
    translator.probe_read = probe_read
    results: dict[str, str] = {}
    errors: list[tuple[str, BaseException]] = []

    def worker(name: str) -> None:
        try:
            results[name] = translator._ensure()
        except BaseException as exc:  # noqa: BLE001 - 需要看到真实异常类型
            errors.append((name, exc))

    winner = threading.Thread(target=worker, args=("winner",), name="ensure-winner")
    winner.start()
    assert spawn_started.wait(timeout=10.0), "第一个线程没有进入 spawn"

    def loser_worker() -> None:
        translator.probe_thread_id = threading.get_ident()
        worker("loser")

    loser = threading.Thread(target=loser_worker, name="ensure-loser")
    loser.start()
    # 旧实现: 败者线程在胜者持锁期间读到陈旧状态 -> 探针立即置位 (确定性的)。
    # 修复后: 败者在 _lifecycle_lock 上等待, 不可能读到陈旧状态 -> 只能等超时,
    #         随后复用胜者已建好的实例 (这正是期望行为)。
    probe_read.wait(timeout=1.0)
    release_spawn.set()
    _join((winner, loser))
    assert not winner.is_alive() and not loser.is_alive(), "并发 _ensure 未在超时内返回"

    assert len(boundary.launches) == 1, (
        f"并发初始化只能启动一个子进程, 实际 {len(boundary.launches)} 次: "
        f"{[launch.port for launch in boundary.launches]}")
    assert boundary.launches[0].proc.terminate_calls == 0, "后建的健康实例被并发线程误关"
    assert errors == [], f"并发 _ensure 抛错: {errors}"
    assert results["winner"] == results["loser"] == translator._endpoint
    assert translator._proc is boundary.launches[0].proc
    assert translator._proc.poll() is None, "返回的实例应仍然存活"


def test_concurrent_cold_start_spawns_single_live_process(tmp_path, monkeypatch) -> None:
    """8 线程并发冷启动: 只有一个子进程、一个端口、一条 endpoint。"""
    translator, boundary = prepare(tmp_path, monkeypatch)

    barrier = threading.Barrier(8)
    endpoints: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        barrier.wait(timeout=10.0)
        try:
            endpoints.append(translator._ensure())
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    _join(threads)

    assert errors == [], f"并发 _ensure 抛错: {errors}"
    assert len(endpoints) == 8
    assert len(boundary.launches) == 1, f"应只启动一个子进程, 实际 {len(boundary.launches)}"
    assert set(endpoints) == {translator._endpoint}
    assert translator._proc is boundary.launches[0].proc
    assert translator._port == _endpoint_port(translator._endpoint) == boundary.launches[0].port
    assert boundary.launches[0].proc.terminate_calls == 0


def test_ensure_retires_dead_instance_and_rebuilds(tmp_path, monkeypatch) -> None:
    """陈旧 (已退出) 实例被摘除并终止, 新实例状态自洽 (进程/端口/endpoint 同代)。"""
    translator, boundary = prepare(tmp_path, monkeypatch)
    stale = FakeProcess(pid=11, exit_code=3)
    translator._port = 49002
    translator._endpoint = "http://127.0.0.1:49002/v1/chat/completions"
    translator._proc = stale

    endpoint = translator._ensure()

    assert len(boundary.launches) == 1
    assert stale.terminate_calls == 1, "陈旧实例必须被终止"
    assert endpoint == translator._endpoint
    assert translator._proc is boundary.launches[0].proc
    assert translator._port == boundary.launches[0].port == _endpoint_port(endpoint)
    assert translator._proc.poll() is None


# ---------------------------------------------------------------------------
# close() 幂等 + 只终止自己拥有的子进程
# ---------------------------------------------------------------------------
def test_close_is_idempotent_and_only_terminates_owned_process(tmp_path, monkeypatch) -> None:
    """close() 幂等; 只终止本实例的句柄, 不按进程名/全局状态宽杀。"""
    translator, boundary = prepare(tmp_path, monkeypatch)
    translator._ensure()
    owned = boundary.launches[0].proc

    translator.close()
    translator.close()
    translator.close()

    assert owned.terminate_calls == 1, "重复 close() 不得重复终止同一进程"
    assert owned.wait_calls == 1, "终止后不应再次等待"
    assert translator._proc is None and translator._port is None
    assert translator._endpoint is None

    foreign = FakeProcess(pid=77)
    translator.close()
    assert foreign.terminate_calls == 0
    assert foreign.alive


def test_close_generation_guard_skips_newer_instance(tmp_path, monkeypatch) -> None:
    """带 epoch 的 close(): 只关闭自己观测到的实例, 不误关后来者。"""
    translator, boundary = prepare(tmp_path, monkeypatch)

    first_endpoint, first_generation = translator._ensure_instance()
    first_proc = boundary.launches[0].proc

    # 模拟另一个线程: 摘除并重建 -> 新实例 (新 epoch)
    translator.close()
    new_endpoint, new_generation = translator._ensure_instance()
    new_proc = boundary.launches[1].proc
    assert new_generation != first_generation
    assert first_proc.terminate_calls == 1

    translator.close(generation=first_generation)   # 过期调用 -> 必须无副作用
    assert new_proc.terminate_calls == 0, "过期 close() 误关了后来者的实例"
    assert translator._proc is new_proc and translator._endpoint == new_endpoint
    assert translator._ensure_instance()[0] == new_endpoint

    translator.close(generation=new_generation)     # 当前 epoch -> 正常关闭
    assert new_proc.terminate_calls == 1
    assert translator._proc is None and translator._port is None
    assert translator._endpoint is None
    assert first_endpoint != new_endpoint


# ---------------------------------------------------------------------------
# 初始化半成功: 不得残留状态 / 进程句柄
# ---------------------------------------------------------------------------
def test_failed_startup_leaves_no_residue_and_no_orphan(tmp_path, monkeypatch) -> None:
    """启动即退出的子进程: 报错、状态清空、进程被终止 (不留孤儿/残留端口)。"""
    translator, boundary = prepare(tmp_path, monkeypatch, plan=[{
        "kwargs": {"exit_code": 1, "stdout_lines": ["llama: failed to load model"]},
    }])

    with pytest.raises(TranslationError):
        translator._ensure()

    assert len(boundary.launches) == 1
    assert boundary.launches[0].proc.terminate_calls == 1, "失败进程必须被回收"
    assert translator._proc is None, "失败后不得残留进程句柄"
    assert translator._port is None, "失败后不得残留端口"
    assert translator._endpoint is None, "失败后不得残留 endpoint"


def test_activation_failure_rolls_back_state(tmp_path, monkeypatch) -> None:
    """激活阶段抛错: 进程句柄/端口/endpoint 必须原子回滚, 且进程被回收。"""
    translator, boundary = prepare(tmp_path, monkeypatch, plan=[{
        "factory": PidlessProcess,
    }])

    with pytest.raises(Exception):
        translator._ensure()

    assert len(boundary.launches) == 1
    assert boundary.launches[0].proc.terminate_calls == 1, "半成功启动的进程必须被回收"
    assert translator._proc is None, "激活失败后不得残留进程句柄"
    assert translator._port is None, "激活失败后不得残留端口"
    assert translator._endpoint is None, "激活失败后不得残留 endpoint"

    translator.close()  # 回滚后 close() 仍须幂等无副作用
    assert boundary.launches[0].proc.terminate_calls == 1
