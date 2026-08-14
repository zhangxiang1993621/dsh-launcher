# -*- mode: python ; coding: utf-8 -*-
# 覆盖 PyInstaller 默认 hook-_tkinter.py：
# 默认实现依赖 tcltk_info 的环境缓存收集 _tcl_data/_tk_data，缓存不稳定会导致
# 打包后 rthook 找不到 Tcl 数据目录。Tcl/Tk 脚本数据已由 dsh-launcher.spec 的
# _collect_tk() 手动收集到 _tcl_data/_tk_data，这里不再重复收集。

def hook(hook_api):
    pass
