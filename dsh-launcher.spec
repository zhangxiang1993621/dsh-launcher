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

# ---- 版本与构建号：输出 dsh-launcher-1.1.0-<构建号>.exe，构建号每次打包自增 ----
_VERSION = (1, 1, 0, 0)
_VERSION_STR = "1.1.0"


def _next_build_number():
    """从 %APPDATA%\dsh-launcher\build_counter 读取并自增构建号。"""
    try:
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dsh-launcher")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, "build_counter")
        n = 0
        try:
            with open(p, "r", encoding="utf-8") as f:
                n = int(f.read().strip() or "0")
        except (OSError, ValueError):
            n = 0
        n += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(n))
        return n
    except Exception:
        return 0


_BUILD_NO = _next_build_number()
_PKG_NAME = "dsh-launcher-%s-%d" % (_VERSION_STR, _BUILD_NO)

# ---- exe 文件版本信息（右键 -> 属性 -> 详细信息可见） ----
try:
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )

    _FILEVERS = (_VERSION[0], _VERSION[1], _VERSION[2], _BUILD_NO)

    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_FILEVERS,
            prodvers=_FILEVERS,
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
                    StringStruct('FileVersion', '%s.%d' % (_VERSION_STR, _BUILD_NO)),
                    StringStruct('InternalName', 'dsh-launcher'),
                    StringStruct('OriginalFilename', _PKG_NAME + '.exe'),
                    StringStruct('ProductName', 'DeepSeek Harness Launcher'),
                    StringStruct('ProductVersion', '%s.%d' % (_VERSION_STR, _BUILD_NO)),
                ]),
            ]),
            VarFileInfo([VarStruct('Translation', [0x0409, 1200])]),
        ],
    )
except Exception:
    version_info = None


# 宠物素材（pet.png / heart.png / sad.png / ASSETS.md），打包进 exe 的 assets/ 目录
_assets_dir = os.path.join(SPECPATH, 'assets')

a = Analysis(
    ['dsh_launcher.py'],
    pathex=[],
    binaries=_tk_binaries,
    datas=_tk_datas + [(_assets_dir, 'assets')],
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
    name=_PKG_NAME,
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
