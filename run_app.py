#!/usr/bin/env python
"""语幕应用打包入口: python run_app.py 或 PyInstaller 均以此为入口。

为什么单独入口: python -m voxsub.ui.app 在 PyInstaller 环境下
模块解析行为不同, 单一入口文件最稳。
"""
import sys


def main() -> int:
    from voxsub.ui.app import main as ui_main
    return ui_main()


if __name__ == "__main__":
    sys.exit(main())