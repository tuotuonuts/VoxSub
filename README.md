# 语幕 VoxSub

Windows 10/11 大众实时翻译软件：麦克风对话、会议/网课系统声音、本地视频音频 → 实时双语字幕。默认全本地离线，可选云端高质量翻译。

## 需求/规划/设计

- [REQUIREMENTS.md](REQUIREMENTS.md) — 需求与范围
- [PLAN.md](PLAN.md) — 技术选型与里程碑
- [DESIGN.md](DESIGN.md) — 架构与接口契约
- [TODO.txt](TODO.txt) — 修改追踪（时间戳分段）

## 开发环境

```bash
uv venv                 # 创建虚拟环境 (.venv)
uv pip install -r requirements.txt
```

Python 3.11+。开发中，暂无可运行产物。

## 目录结构

```
voxsub/     主包（模块见 DESIGN.md）
tests/      pytest 测试
scripts/    构建/工具脚本
models/     运行时模型缓存（gitignore）
```