# -*- mode: python ; coding: utf-8 -*-

import os
import sys

# ---- 可移植的 tcl/tk 路径（基于运行 PyInstaller 的 Python 环境自动推导） ----
_base = sys.base_prefix
_lib_dir = os.path.join(_base, "Library", "lib")
_bin_dir = os.path.join(_base, "Library", "bin")


def _collect_tk():
    binaries, datas = [], []
    for dll in ("tcl86t.dll", "tk86t.dll", "zlib.dll"):
        p = os.path.join(_bin_dir, dll)
        if os.path.isfile(p):
            binaries.append((p, "."))
    for name in ("tcl8.6", "tk8.6"):
        d = os.path.join(_lib_dir, name)
        if os.path.isdir(d):
            datas.append((d, name))
    return binaries, datas


_tk_binaries, _tk_datas = _collect_tk()

# ---- exe 文件版本信息（右键 -> 属性 -> 详细信息可见） ----
try:
    from pyinstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )

    _VERSION = (1, 1, 0, 0)

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_VERSION,
            prodvers=_VERSION,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable('040904B0', [
                    StringStruct('CompanyName', 'DeepSeek AI'),
                    StringStruct('FileDescription', 'DeepSeek Harness Launcher'),
                    StringStruct('FileVersion', '1.1.0'),
                    StringStruct('InternalName', 'dsh-launcher'),
                    StringStruct('OriginalFilename', 'dsh-launcher.exe'),
                    StringStruct('ProductName', 'DeepSeek Harness Launcher'),
                    StringStruct('ProductVersion', '1.1.0'),
                ]),
            ]),
            VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
        ],
    )
except Exception:
    version_info = None


a = Analysis(
    ['dsh_launcher.py'],
    pathex=[],
    binaries=_tk_binaries,
    datas=_tk_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='dsh-launcher',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=version_info,
)
