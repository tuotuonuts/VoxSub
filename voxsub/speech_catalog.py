"""Create catalog entries without creating a circular import at module load time."""
from voxsub.speech_assets import SPEECH_ASSETS, runtime_files


def speech_catalog():
    from voxsub.model_catalog import ModelSpec, ModelSource, RemoteFile
    entries = []
    for key, name, languages, description, ram in (
        ("granite", "Granite 4.0 1B Speech", "英语 → 中文", "把英文音视频直接翻译成中文，可同时生成英文原文。文件专用；原文与译文分两次生成，处理速度较慢。", 8),
        ("index", "Index-Echo S2TT 2B", "中文 → 英语 / 日语 / 西班牙语", "把中文音视频直接生成双语字幕，并提供句子时间戳。文件专用；处理速度较慢。", 10),
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
            min_ram_gb=16, working_ram_gb=ram, compute_cost=90, gpu_supported=False,
            tags=("文件专用", "单模型翻译", languages),
            official_repo=(f'https://huggingface.co/{manifest["repo"]}' if key == 'granite' else 'https://github.com/bilibili/Index-Translate')))
    return tuple(entries)
