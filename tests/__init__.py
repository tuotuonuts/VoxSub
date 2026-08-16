"""语幕 VoxSub 测试包。

存在理由: pytest 默认 importmode=prepend 下, 含 __init__.py 的测试包
会向上回溯到第一个非包目录 (项目根) 插入 sys.path, 使 `import voxsub` 可用。
"""