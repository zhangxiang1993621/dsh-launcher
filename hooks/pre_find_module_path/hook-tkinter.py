# -*- mode: python ; coding: utf-8 -*-
# 覆盖 PyInstaller 默认 pre_find_module_path/hook-tkinter.py：
# 默认实现会初始化 tcltk_info 并缓存 Tcl/Tk 探测结果（TclTkInfo: initializing
# cached Tcl/Tk info...），该缓存不稳定且与 dsh-launcher.spec 手动收集重复。
# Tcl/Tk 数据统一由 spec 的 _collect_tk() 处理。

def pre_find_module_path(api):
    pass
