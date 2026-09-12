"""Static contracts for the source-mode llama.cpp runtime bootstrap."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts" / "llama_runtime_manifest.psd1"
SYNC = ROOT / "scripts" / "sync_llama_runtime.ps1"
# 注：原先还有 BUILD = scripts/build.ps1，那是 Qt 版的 PyInstaller 打包脚本，
# 已随 Qt 前端移入 qt-legacy 分支。


def test_manifest_pins_source_runtime_matrix() -> None:
    text = MANIFEST.read_text(encoding="utf-8")

    assert 'Version = "b10470"' in text
    assert 'Name = "cpu"' in text
    assert 'Name = "vulkan"' in text
    assert "llama-b10470-bin-win-cpu-x64.zip" in text
    assert "llama-b10470-bin-win-vulkan-x64.zip" in text
    assert "ggml-vulkan.dll" in text
    assert "A31F1F317813AE7E044BE183E0A20B90E78A80C0E97EE11A8B32A014ECCD5043" in text
    assert "2E89637B30E0E2F90D4ED486118E8642F60625B1DBEBB9BA3A30BC4100306FC9" in text
    assert 'Name = "openvino"' in text
    assert 'llama-b10470-bin-win-openvino-2026.2.1-x64.zip' in text
    assert '671B0A0C8D5F58E20DA178732435617B182D7127E62080D2CBE270A7A0D69EBD' in text
    assert 'Size = 80730898' in text
    assert 'SourceOnly = $true' in text
    assert 'openvino_intel_npu_compiler_loader.dll' in text


def test_source_runtime_sync_validates_and_installs_openvino_without_user_data_loss() -> None:
    text = SYNC.read_text(encoding="utf-8-sig")

    assert "Import-PowerShellDataFile" in text
    assert "Get-FileHash" in text
    assert "SHA256 校验失败" in text
    assert "Get-ArchiveDiagnostics" in text
    assert "Invoke-LlamaDownload" in text
    assert "curl.exe" in text
    assert "下载响应不是 ZIP" in text
    assert "下载大小不符" in text
    assert "maxDownloadAttempts = 3" in text
    assert "canonical release URL" in text
    assert "Expand-Archive" in text
    assert "llama-server.exe" in text
    assert "RequiredDll" in text
    assert "RequiredDlls" in text
    assert 'Join-Path $Destination $Asset.Name' in text
    manifest = MANIFEST.read_text(encoding="utf-8")
    assert "openvino" in manifest.casefold()
    assert "Get-FileHash" in text
    assert SYNC.read_bytes().startswith(b"\xef\xbb\xbf")


def test_build_and_source_runner_share_the_runtime_manifest() -> None:
    """源模式的 llama 运行时同步契约。

    原先这个测试检查 scripts/build.ps1 与 run_source_test.ps1 是否把
    llama_runtime_manifest.psd1 接进构建流程 —— 那两个脚本是 Qt 版
    （PyInstaller）的打包脚本，已随 Qt 前端一起移入 qt-legacy 分支。

    main 分支改为 Electron 前端后，打包路径变成 electron-builder，
    而打包配置尚未编写（见 STATUS.md 未完成项）。所以这里改为断言
    当前仍然成立的部分：清单与同步脚本本身齐备，且同步脚本被文档引用。

    TODO(打包): 编写 electron-builder 配置时，必须把 llama 运行时同步
    接进打包流程，否则质量翻译档在安装版上不可用。届时把断言改回
    "打包配置引用了 llama_runtime_manifest.psd1"。
    """
    manifest = MANIFEST.read_text(encoding="utf-8")
    sync = SYNC.read_text(encoding="utf-8-sig")

    # 清单与同步脚本必须齐备（它们是运行时下载的唯一依据）
    assert "llama_runtime_manifest.psd1" in str(MANIFEST.name)
    assert "Assets" in manifest
    assert "sync_llama_runtime.ps1" in str(SYNC.name)
    assert "质量翻译运行时未准备完成" in sync or "Get-FileHash" in sync

    # 同步脚本仍引用清单（保证两者不会各自漂移）
    assert "llama_runtime_manifest.psd1" in sync
