# -*- mode: python ; coding: utf-8 -*-

import os
import sys

# ---- 可移植的 tcl/tk 路径（基于运行 PyInstaller 的 Python 环境自动推导） ----
_base = sys.base_prefix
_lib_dir = os.path.join(_base, "Library", "lib")
_bin_dir = os.path.join(_base, "Library", "bin")


def _collect_tk():
    """收集 Tcl/Tk 数据到 PyInstaller rthook 期望的 _tcl_data/_tk_data。

    注意：不能用 'tcl8.6'/'tk8.6' 作为目标目录名——pyi_rth__tkinter.py
    固定从 sys._MEIPASS/_tcl_data 和 _tk_data 读取（TCL_ROOTNAME/TK_ROOTNAME）。
    PyInstaller 自带 tcltk_info 的收集依赖环境缓存，不稳定，这里手动兜底。
    """
    binaries, datas = [], []
    for dll in ("tcl86t.dll", "tk86t.dll", "zlib.dll"):
        p = os.path.join(_bin_dir, dll)
        if os.path.isfile(p):
            binaries.append((p, "."))
    # tcl 脚本目录 -> _tcl_data；tk 脚本目录 -> _tk_data
    # 注意：PyInstaller datas 元组 (src, dest_dir) 会自动在 dest_dir 后追加 src 的
    # basename，所以 dest 只需给到目录级（子目录用 rel 的 dirname）。
    tcl_d = os.path.join(_lib_dir, "tcl8.6")
    if os.path.isdir(tcl_d):
        for root, dirs, files in os.walk(tcl_d):
            dirs[:] = [d for d in dirs if d not in ("demos", "nmake", "pkgconfig", "cmake")]
            for f in files:
                if f.endswith((".lib", "tclConfig.sh")):
                    continue
                src = os.path.join(root, f)
                rel = os.path.relpath(src, tcl_d)
                dest = os.path.join("_tcl_data", os.path.dirname(rel))
                datas.append((src, dest))
    tk_d = os.path.join(_lib_dir, "tk8.6")
    if os.path.isdir(tk_d):
        for root, dirs, files in os.walk(tk_d):
            dirs[:] = [d for d in dirs if d not in ("demos", "nmake", "pkgconfig", "cmake")]
            for f in files:
                if f.endswith((".lib", "tkConfig.sh")):
                    continue
                src = os.path.join(root, f)
                rel = os.path.relpath(src, tk_d)
                dest = os.path.join("_tk_data", os.path.dirname(rel))
                datas.append((src, dest))
    return binaries, datas


_tk_binaries, _tk_datas = _collect_tk()


def _collect_conda_dlls():
    """收集 conda 环境的系统 DLL（PyInstaller 依赖分析常漏掉 Library\\bin 下的文件）。

    ffi.dll 是 _ctypes 的依赖，缺失会导致 import ctypes 失败（托盘图标/单实例失效）；
    ssl/crypto/expat 是 _ssl/_hashlib/pyexpat 的依赖，一并补齐避免隐性崩溃。
    """
    out = []
    for dll in ("ffi.dll", "libcrypto-3-x64.dll", "libssl-3-x64.dll", "libexpat.dll"):
        p = os.path.join(_bin_dir, dll)
        if os.path.isfile(p):
            out.append((p, "."))
    return out


_extra_binaries = _collect_conda_dlls()

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
# exe 图标：用宠物图片生成的 pet.ico（多尺寸，见 assets/pet.ico）
_icon_path = os.path.join(_assets_dir, 'pet.ico')

a = Analysis(
    ['dsh_launcher.py'],
    pathex=[],
    binaries=_tk_binaries + _extra_binaries,
    datas=_tk_datas + [(_assets_dir, 'assets')],
    hiddenimports=['pystray', 'PIL'],
    hookspath=[os.path.join(SPECPATH, 'hooks')],
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
    icon=_icon_path if os.path.isfile(_icon_path) else None,
    version=version_info,
)
