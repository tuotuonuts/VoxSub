"""Create catalog entries without creating a circular import at module load time."""
from voxsub.speech_assets import SPEECH_ASSETS, runtime_files


def speech_catalog():
    from voxsub.model_catalog import ModelSpec, ModelSource, RemoteFile
    entries = []
    for key, name, languages, description, ram in (
        ("granite", "Granite 4.0 1B Speech", "英语 ↔ 法德西葡日；英语 → 中意", "把音视频直接翻译成文字。支持 NVIDIA 显卡或处理器；可选仅译文或双语字幕，双语模式分两次生成。", 8),
        ("index", "Index-Echo S2TT 2B", "中文 → 英语 / 日语 / 西班牙语", "把中文音视频生成双语字幕和句子时间戳。支持 NVIDIA 显卡或处理器；官方提示 BF16 约需 10GB 显存并留有余量。", 10),
    ):
        manifest = SPEECH_ASSETS[key]
        # Runtime implementation is vendored; never download and execute Python code.
        files = runtime_files(key)
        sources = tuple(ModelSource(source, label, f'{host}/{manifest["repo"]}',
                        f'{host}/{manifest["repo"]}/resolve/{manifest["revision"]}/{files[0]["file"]}',
                        tuple(RemoteFile(f'{host}/{manifest["repo"]}/resolve/{manifest["revision"]}/{f["file"]}',
                                         f["file"], f["size"], f["sha256"]) for f in files))
                        for source, label, host in (("global", "Hugging Face 全球源", "https://huggingface.co"),
                                                    ("china", "HF Mirror 中国镜像", "https://hf-mirror.com")))
        entries.append(ModelSpec(
            id="speech-granite-4-1b" if key == "granite" else "speech-index-echo-2b",
            task="speech", name=name, vendor="IBM" if key == "granite" else "IndexTeam",
            release="2026", description=description, runtime=f"speech-{key}", quality_score=80,
            languages=languages, license="Apache-2.0", download_bytes=sum(f["size"] for f in files),
            installed_bytes=sum(f["size"] for f in files), install_rel=f"speech/{key}",
            required_paths=tuple(f["file"] for f in files), sources=sources,
            min_ram_gb=16, working_ram_gb=ram, compute_cost=90, gpu_supported=True,
            tags=("文件专用", "NVIDIA 显卡加速", languages),
            official_repo=(f'https://huggingface.co/{manifest["repo"]}' if key == 'granite' else 'https://github.com/bilibili/Index-Translate')))
    entries.append(seamless_entry())
    return tuple(entries)


def seamless_entry():
    from voxsub.model_catalog import ModelSpec, ModelSource, RemoteFile
    from voxsub.seamless_assets import SEAMLESS_ASSETS as manifest
    files = manifest['files']
    sources = tuple(ModelSource(source, label, f'{host}/{manifest["repo"]}',
        f'{host}/{manifest["repo"]}/resolve/{manifest["revision"]}/tokenizer.model',
        tuple(RemoteFile(f'{host}/{manifest["repo"]}/resolve/{manifest["revision"]}/{f["file"]}', f['file'], f['size'], f['sha256']) for f in files))
        for source, label, host in (("global", "Hugging Face 全球源", "https://huggingface.co"), ("china", "HF Mirror 中国镜像", "https://hf-mirror.com")))
    return ModelSpec(id='speech-seamless-streaming', task='speech', name='SeamlessStreaming', vendor='Meta', release='2023',
        description='面向多语言实时翻译的模型。官方运行环境为 Linux／WSL；本应用提供权重下载和官方说明，不在 Windows 内直接启动。仅限非商业用途。',
        runtime='seamless-external', quality_score=80, languages='多语言语音 → 多语言文字', license='CC-BY-NC-4.0',
        download_bytes=sum(f['size'] for f in files), installed_bytes=sum(f['size'] for f in files), install_rel='speech/seamless-streaming',
        required_paths=tuple(f['file'] for f in files), sources=sources, min_ram_gb=16, working_ram_gb=10, compute_cost=95,
        gpu_supported=True, tags=('流式翻译', 'Linux／WSL 外部运行', '仅非商业使用'),
        official_repo='https://github.com/facebookresearch/seamless_communication', external_runtime='Linux／WSL 外部运行',
        usage_url='https://github.com/facebookresearch/seamless_communication/blob/main/src/seamless_communication/cli/streaming/README.md')
