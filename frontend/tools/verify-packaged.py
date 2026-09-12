#!/usr/bin/env python
"""验证**打包版**能否真正工作 —— 这是"能交付"的唯一证据。

为什么必须单独验：源码能跑 ≠ 打包能用。打包会改变三件事：
  1. Python 变成 frozen，`__file__` 与 `sys._MEIPASS` 行为不同
  2. 后端变成 sidecar 可执行文件，不再有 venv
  3. asar 打包后前端路径变化

任何一处没处理好，表现都是"装了但用不了"。所以打包后必须真跑一遍。

做法：以前台方式启动打包版（避开托管环境的后台 tty 问题），
通过 CDP 连上去检查界面与后端连接是否正常。

用法：python tools/verify-packaged.py [安装目录]
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_DIR = Path(r"D:/OneDrive/app_dve/Release/electron/win-unpacked")
PORT = 9333

passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"PASS  {name}" + (f"  — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"FAIL  {name}" + (f"  — {detail}" if detail else ""))


def cdp_call(port: int, path: str, timeout: float = 3.0):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def evaluate(ws_url: str, expression: str, timeout: float = 20.0):
    """在页面里求值。用最简 WebSocket 客户端，避免额外依赖。"""
    import base64
    import os
    import socket
    import struct

    # 解析 ws://host:port/path
    assert ws_url.startswith("ws://")
    rest = ws_url[5:]
    host_port, _, path = rest.partition("/")
    host, _, port_str = host_port.partition(":")
    port = int(port_str or 80)

    sock = socket.create_connection((host, port), timeout=timeout)
    key = base64.b64encode(os.urandom(16)).decode()
    handshake = (
        f"GET /{path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(handshake.encode())
    sock.recv(4096)  # 握响应

    def send_frame(payload: bytes) -> None:
        header = bytearray([0x81])
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        sock.sendall(bytes(header) + masked)

    def recv_frame() -> bytes:
        first = sock.recv(2)
        if len(first) < 2:
            return b""
        length = first[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", sock.recv(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", sock.recv(8))[0]
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                break
            data += chunk
        return data

    message = json.dumps({
        "id": 1, "method": "Runtime.evaluate",
        "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
    }).encode()
    send_frame(message)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = recv_frame()
        if not raw:
            break
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        if payload.get("id") == 1:
            result = payload.get("result", {})
            if result.get("exceptionDetails"):
                raise RuntimeError(result["exceptionDetails"].get("text", "求值出错"))
            return result.get("result", {}).get("value")
    raise TimeoutError("等待求值结果超时")


def main() -> int:
    install_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_DIR

    print("=" * 70)
    print("打包版可用性验证")
    print(f"目录: {install_dir}")
    print("=" * 70)

    exe = install_dir / "VoxSub.exe"
    if not exe.is_file():
        print(f"FAIL 找不到 {exe}")
        return 1
    check("主程序存在", True, f"{exe.stat().st_size / 1048576:.0f} MB")

    sidecar = install_dir / "resources" / "backend" / "VoxSubBackend.exe"
    check("sidecar 已随包", sidecar.is_file(),
          f"{sidecar.stat().st_size / 1048576:.1f} MB" if sidecar.is_file() else "缺失")

    asar = install_dir / "resources" / "app.asar"
    check("前端已打包为 asar", asar.is_file(),
          f"{asar.stat().st_size / 1024:.0f} KB" if asar.is_file() else "缺失")

    # 启动（前台子进程，避免托管环境的 stdin 问题）
    #
    # 注意：不能把 stdout/stderr 指向 DEVNULL 就以为没问题 ——
    # 打包版启动慢（要解压 asar、spawn sidecar），而 CDP 端口要等
    # Chromium 初始化完才监听。之前用 DEVNULL + 30 秒超时导致误判"起不来"，
    # 实际是等待不足。这里把输出留存到文件，并把等待放宽到 60 秒。
    # 启动前必须清掉已在运行的实例。
    #
    # 为什么：打包版与源码版共用同一个 userData 目录，而主进程有单实例锁
    # （app.requestSingleInstanceLock）。已有实例在跑时，新实例会拿到
    # "非首个实例"的分支直接退出 —— 表现就是"启动后 30 秒无响应"，
    # 极易误判成打包坏了。这是实测踩到的。
    print("\n清理已在运行的实例…")
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-Process VoxSub,VoxSubBackend,electron -ErrorAction SilentlyContinue "
         "| Where-Object { $_.Path -like '*VoxSub*' -or $_.Path -like '*voxsub*' } "
         "| Stop-Process -Force"],
        capture_output=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    time.sleep(3)

    print("\n启动打包版…")
    log_path = Path(tempfile.gettempdir()) / "voxsub-packaged-run.log"
    log_file = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen(
        [str(exe), f"--remote-debugging-port={PORT}"],
        cwd=str(install_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**__import__("os").environ, "VOXSUB_HEADLESS": "1"},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    try:
        # 等 CDP 起来（打包版首次启动要解压 + 初始化，给足时间）
        targets = None
        for _ in range(60):
            if proc.poll() is not None:
                check("打包版启动", False, f"进程已退出（code {proc.returncode}）")
                tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
                if tail.strip():
                    print("\n--- 启动输出 ---")
                    print(tail.strip())
                return 1
            time.sleep(1)
            try:
                targets = cdp_call(PORT, "/json/list")
                if targets:
                    break
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                continue

        if not targets:
            check("打包版启动并暴露调试端口", False, "60 秒内无响应")
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-600:]
            if tail.strip():
                print("\n--- 启动输出 ---")
                print(tail.strip())
            return 1
        check("打包版启动并暴露调试端口", True, f"{len(targets)} 个页面")

        page = next((t for t in targets if t.get("type") == "page"
                     and "语幕" in (t.get("title") or "")), None)
        check("主窗已创建", page is not None,
              str([t.get("title") for t in targets])[:60])
        if page is None:
            return 1

        ws = page["webSocketDebuggerUrl"]

        # 界面是否渲染出结构
        rendered = evaluate(ws, """JSON.stringify({
            modes: document.querySelectorAll('.mode-cell').length,
            buttons: [...document.querySelectorAll('.topbar__actions button')].map(b => b.textContent),
            workspace: !!document.querySelector('.workspace')
        })""")
        info = json.loads(rendered)
        check("界面已渲染", info["modes"] == 4, f"{info['modes']} 个模式")
        check("顶栏就位", len(info["buttons"]) >= 4, "/".join(info["buttons"]))

        # 关键：后端是否连上（打包版走 sidecar，不是 venv）
        # 等后端 ready
        backend_ok = False
        for _ in range(25):
            try:
                state = evaluate(ws, """(async () => {
                    const r = await window.voxsub.backend.command('ping', null);
                    return JSON.stringify(r);
                })()""", timeout=15)
                payload = json.loads(state)
                if payload.get("ok"):
                    backend_ok = True
                    check("sidecar 后端连通", True,
                          f"版本 {payload.get('data', {}).get('version')}")
                    break
            except (RuntimeError, TimeoutError):
                pass
            time.sleep(1)

        if not backend_ok:
            check("sidecar 后端连通", False, "25 秒内未响应 ping")

        # 模型列表：确认 frozen 状态下路径解析正确
        try:
            models = evaluate(ws, """(async () => {
                const r = await window.voxsub.backend.command('list_models', {models_root: null});
                return JSON.stringify({ok: r.ok, n: r.data ? r.data.models.length : 0,
                                       root: r.data ? r.data.modelsRoot : null});
            })()""", timeout=40)
            info = json.loads(models)
            check("模型路径解析正确（frozen 模式）", info["ok"] and info["n"] > 0,
                  f"{info['n']} 个模型 @ {info['root']}")
        except (RuntimeError, TimeoutError) as error:
            check("模型路径解析正确（frozen 模式）", False, str(error)[:60])

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    print()
    print("=" * 70)
    print(f"{passed} 通过 / {failed} 失败 / 共 {passed + failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
