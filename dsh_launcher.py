# -*- coding: utf-8 -*-
"""
DeepSeek Harness 启动器

功能：
  - 更新源码：从 GitHub 拉取最新源码，重新安装依赖并构建
  - 指定端口启动 dsh web 服务（默认 3080）
  - 停止服务 / 取消更新

技术栈：tkinter 图形界面，subprocess 管理 git / node / corepack 子进程，
        可用 PyInstaller 打包为单文件 exe。
"""

import glob
import http.client
import json
import math
import os
import queue
import random
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import webbrowser
from tkinter import ttk, scrolledtext, filedialog, messagebox

# ---------------- 常量 ----------------
DEFAULT_REPO = r"d:\agent-workspace\deepseek-harness"
DEFAULT_NODE_DIR = r"C:\Program Files\nodejs"
SYSTEM32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
CMD_EXE = os.path.join(SYSTEM32, "cmd.exe")
TASKKILL_EXE = os.path.join(SYSTEM32, "taskkill.exe")
DEFAULT_PORT = "3080"

# 在 windowed GUI 里启动控制台子进程时，禁止弹出新的 cmd 窗口
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 日志框最多保留的行数，防止构建输出过多导致界面卡死
MAX_LOG_LINES = 4000

# git 默认的 schannel 后端在本机报 SEC_E_UNTRUSTED_ROOT，改用 openssl 后端
GIT_SSL_ARGS = ["-c", "http.sslBackend=openssl"]

# 仓库 engines 要求：^22.19.0 || >=24.0.0（23.x 不满足）
NODE_VERSION_HINT = "^22.19.0 || >=24.0.0"

# 等待服务就绪的超时（秒）与探测间隔
READY_TIMEOUT = 90
READY_INTERVAL = 0.5

# 配置持久化位置：%APPDATA%\dsh-launcher\config.json
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dsh-launcher")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

# 单实例互斥量名称
MUTEX_NAME = "Local\\dsh-launcher-singleton"

# Chrome 应用窗口（App Mode）快捷方式默认路径：优先用独立应用窗口打开 DSH 页面
CHROME_APP_LNK = (r"C:\Users\zhang\AppData\Roaming\Microsoft\Windows\Start Menu\Programs"
                  r"\Chrome 应用\DeepSeek Harness.lnk")

# Chrome 应用窗口检测辅助脚本：UIAutomation 探测已打开的 App 窗口。
# App 模式窗口类名 Chrome_WidgetWin_1，标题带应用名且不以 " - Google Chrome" 结尾
# （普通标签窗口标题会带该后缀）。输出 "refresh" = 已找到并刷新；"open" = 未找到。
CHROME_APP_REFRESH_PS1 = r'''
param([string]$appTitle)
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
  Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
  Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
} catch {
  Write-Output 'open'
  exit 0
}
$root = [System.Windows.Automation.AutomationElement]::RootElement
$cond = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ClassNameProperty, 'Chrome_WidgetWin_1')
$windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
foreach ($w in $windows) {
  try {
    $name = $w.Current.Name
    if ($name -and $name -like "*$appTitle*" -and $name -notlike '* - Google Chrome') {
      try { $w.SetFocus() } catch {}
      Start-Sleep -Milliseconds 300
      [System.Windows.Forms.SendKeys]::SendWait('^r')
      Write-Output 'refresh'
      exit 0
    }
  } catch {}
}
Write-Output 'open'
'''

# 浏览器标签去重辅助脚本：用 UIAutomation 探测已打开的浏览器标签。
# 输出 "refresh" = 已找到并刷新；"open" = 未找到，应新开标签。
BROWSER_DEDUP_PS1 = r'''
param([string]$url)
$ErrorActionPreference = 'Stop'
try {
  Add-Type -AssemblyName UIAutomationClient -ErrorAction Stop
  Add-Type -AssemblyName UIAutomationTypes -ErrorAction Stop
  Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
} catch {
  Write-Output 'open'
  exit 0
}

$target = ''
try {
  $u = [Uri]$url
  $target = "$($u.Host):$($u.Port)"
} catch {
  $target = $url
}

$root = [System.Windows.Automation.AutomationElement]::RootElement
$classes = @('Chrome_WidgetWin_1', 'MozillaWindowClass')

foreach ($cls in $classes) {
  $cond = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ClassNameProperty, $cls)
  $windows = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $cond)
  foreach ($w in $windows) {
    try {
      # 1) 当前活动标签页的地址栏 = 目标地址 -> 直接刷新
      $addrCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'AddressAndSearchBar')
      $addr = $w.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $addrCond)
      if ($addr) {
        $vp = $addr.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        $val = $vp.Current.Value
        if ($val -and $target -and $val -like "*$target*") {
          $w.SetFocus()
          Start-Sleep -Milliseconds 300
          [System.Windows.Forms.SendKeys]::SendWait('^r')
          Write-Output 'refresh'
          exit 0
        }
      }
    } catch {}

    try {
      # 2) 遍历所有标签页，按标题匹配 DeepSeek Harness -> 选中并刷新
      $tabCond = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
      $tabs = $w.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabCond)
      foreach ($t in $tabs) {
        if ($t.Current.Name -like '*DeepSeek Harness*') {
          $sel = $t.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
          $sel.Select()
          Start-Sleep -Milliseconds 200
          $t.SetFocus()
          Start-Sleep -Milliseconds 200
          [System.Windows.Forms.SendKeys]::SendWait('^r')
          Write-Output 'refresh'
          exit 0
        }
      }
    } catch {}
  }
}

Write-Output 'open'
'''


# ---------------- 工具函数 ----------------
def find_git():
    """在常见位置查找 git.exe。"""
    candidates = []
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if p:
            g = os.path.join(p, "git.exe")
            if os.path.isfile(g):
                candidates.append(g)
    candidates += [
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
    ]
    gh = os.path.join(os.path.expanduser("~"), "AppData", "Local", "GitHubDesktop")
    for d in sorted(glob.glob(os.path.join(gh, "app-*")), reverse=True):
        g = os.path.join(d, "resources", "app", "git", "cmd", "git.exe")
        if os.path.isfile(g):
            candidates.append(g)
    for g in candidates:
        if os.path.isfile(g):
            return g
    return None


def find_node():
    """先在 PATH 中查找 node.exe，找不到再回落默认目录。返回 (node_exe, node_dir)。"""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        if not p:
            continue
        exe = os.path.join(p, "node.exe")
        if os.path.isfile(exe):
            return exe, p
    exe = os.path.join(DEFAULT_NODE_DIR, "node.exe")
    if os.path.isfile(exe):
        return exe, DEFAULT_NODE_DIR
    return None, DEFAULT_NODE_DIR


def build_cmdline(argv):
    """.cmd/.bat 脚本需要用 cmd.exe 来执行。"""
    if argv and argv[0].lower().endswith((".cmd", ".bat")):
        return [CMD_EXE, "/c"] + argv
    return argv


def base_env(node_dir):
    """构造带 node 目录的子进程环境。"""
    env = os.environ.copy()
    if node_dir:
        env["PATH"] = node_dir + os.pathsep + env.get("PATH", "")
    env["COREPACK_ENABLE_DOWNLOAD_PROMPT"] = "0"
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    return env


def load_config():
    cfg = {"repo": DEFAULT_REPO, "port": DEFAULT_PORT, "auto_open": True,
           "geometry": "", "pet_enabled": True, "pet_x": None, "pet_y": None}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for k in list(cfg):
                if k in data:
                    cfg[k] = data[k]
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------- 桌面宠物 ----------------
# 透明色：宠物窗口里这个颜色的区域会变为透明（仅 Windows 有效）
PET_TRANSPARENT = "#ff00ff"

# 状态 → 光环颜色
PET_AURA_COLORS = {
    "running": "#4cc95c",   # 运行中：绿
    "starting": "#f0c040",  # 启动中：黄
    "busy": "#5b9cf5",      # 更新/重建/构建：蓝
    "error": "#ef5f5f",     # 失败/超时：红
    "idle": "#e8c98a",      # 就绪：奶油色
}

# 画布尺寸（顶部留给气泡，底部留给飘动的爱心）
PET_W, PET_H = 170, 210
PET_CX, PET_CY = 85, 130      # 宠物图片中心
PET_IMG_SIZE = 120            # 宠物图片目标显示尺寸
PET_AURA_R = 62               # 状态光环半径

# 状态 → 气泡文案池
PET_BUBBLES = {
    "idle": ["喵～", "在呢在呢", "好无聊呀…", "戳我干嘛 >_<", "要不要启动服务？"],
    "running": ["服务运行中 ♪", "我在认真干活！", "一切正常喵", "有需要就叫我"],
    "starting": ["正在启动…", "稍等哦", "马上就好！"],
    "busy": ["更新中…", "别打扰我干活！", "构建到一半啦"],
    "error": ["出错了…", "好难过 QAQ", "看看日志吧"],
}
PET_FLOAT_TEXTS = ["❤", "♪", "✨", "ฅ^•ﻌ•^ฅ"]

# 贴边收缩：距屏幕边缘多少像素内触发收缩 / 收缩后露出的边宽 / 滑动动画参数
PET_EDGE_SNAP = 28
PET_SLIVER = 36
PET_SLIDE_STEPS = 10
PET_SLIDE_INTERVAL = 18


def asset_path(name):
    """定位内置素材：PyInstaller 打包后从 _MEIPASS 读取，否则从脚本目录读取。"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "assets", name)


def make_tray_image():
    """生成托盘图标（用宠物素材缩小；失败时画一个圆脸）。"""
    try:
        from PIL import Image
        p = asset_path("pet.png")
        if os.path.isfile(p):
            return Image.open(p).convert("RGBA").resize((32, 32), Image.LANCZOS)
    except Exception:
        pass
    try:
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.ellipse([2, 8, 30, 30], fill=(240, 200, 100, 255), outline=(91, 70, 54, 255))
        d.ellipse([10, 16, 14, 20], fill=(0, 0, 0, 255))
        d.ellipse([18, 16, 22, 20], fill=(0, 0, 0, 255))
        d.arc([12, 20, 20, 26], start=0, extent=180, fill=(0, 0, 0, 255))
        return img
    except Exception:
        return None


class DesktopPet:
    """常驻桌面宠物：图片形象 + 状态光环 + 待机动画 + 点击互动。

    交互：
      - 左键单击：跳一下 + 随机气泡 + 爱心/音符飘起
      - 左键双击：打开面板
      - 左键拖动：移动（松手记住位置）
      - 右键：菜单（启动/停止/更新/重建/退出）
      - 悬停：状态气泡
    """

    def __init__(self, app):
        self.app = app
        self._drag = None
        self._tip = None
        self._face = "idle"
        self._bob = 0.0            # 待机浮动相位
        self._jump = 0             # 跳跃剩余帧
        self._jump_total = 12
        self._jump_height = 14
        self._moved = False        # 拖动是否超过阈值
        self._click_after = None   # 单击互动延时（用于区分双击）
        self._idle_counter = random.randint(180, 500)
        self._dead = False
        self._docked = None        # 贴边状态：None / left / right / top / bottom
        self._resting = None       # 收缩前的完整位置 (x, y)
        self._slide_cb = None      # 滑动动画的 after 句柄
        self._img = None
        self._heart_img = None
        self._img_item = None
        self._aura_item = None
        self._bubble_items = []
        self._float_items = []

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.attributes("-transparentcolor", PET_TRANSPARENT)
        except tk.TclError:
            pass
        self.win.configure(bg=PET_TRANSPARENT)

        self.canvas = tk.Canvas(self.win, width=PET_W, height=PET_H,
                                bg=PET_TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack()

        self._load_images()
        self._draw()

        # 交互
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._hide_tip)

        # 初始位置：上次保存的位置（需校验在屏幕内），否则右下角
        cfg = app.cfg
        x, y = cfg.get("pet_x"), cfg.get("pet_y")
        if not (isinstance(x, int) and isinstance(y, int)):
            x, y = None, None
        if x is not None and y is not None and not self._position_visible(x, y):
            self.app._append_log("[提示] 上次宠物位置在屏幕外，已恢复到默认位置。")
            x, y = None, None
        if x is None or y is None:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x, y = sw - PET_W - 24, sh - PET_H - 80
        self.win.geometry("+%d+%d" % (x, y))

        # 动画循环
        self._tick_after = self.win.after(60, self._tick)

    def _position_visible(self, x, y):
        """宠物图片区域是否大部分在屏幕内（防止位置在屏幕外导致宠物“消失”）。"""
        try:
            bx, by, bx1, by1 = self._screen_bounds()
            if bx1 - bx <= 0 or by1 - by <= 0:
                bx, by, bx1, by1 = 0, 0, self.win.winfo_screenwidth(), self.win.winfo_screenheight()
            cx, cy = x + PET_CX, y + PET_CY
            if not (bx < cx < bx1 and by < cy < by1):
                return False
            # 图片包围盒（PET_IMG_SIZE 见方，中心 PET_CX/PET_CY）至少 85% 在屏内
            half = PET_IMG_SIZE // 2
            ix0, iy0, ix1, iy1 = cx - half, cy - half, cx + half, cy + half
            vis_w = max(0, min(ix1, bx1) - max(ix0, bx))
            vis_h = max(0, min(iy1, by1) - max(iy0, by))
            return (vis_w * vis_h) >= (PET_IMG_SIZE * PET_IMG_SIZE) * 0.85
        except tk.TclError:
            return False

    # ---------- 素材 ----------
    def _load_images(self):
        def load(name, target):
            try:
                p = asset_path(name)
                if not os.path.isfile(p):
                    return None
                img = tk.PhotoImage(file=p)
                w, h = img.width(), img.height()
                if w >= target:
                    return img.subsample(max(1, w // target), max(1, h // target))
                k = max(1, target // max(w, 1))
                return img.zoom(k, k)
            except tk.TclError:
                return None
        self._img = load("pet.png", PET_IMG_SIZE)
        self._heart_img = load("heart.png", 22)

    # ---------- 绘制 ----------
    def _draw(self):
        c = self.canvas
        c.delete("all")
        aura = PET_AURA_COLORS.get(self._face, PET_AURA_COLORS["idle"])
        self._aura_item = c.create_oval(
            PET_CX - PET_AURA_R, PET_CY - PET_AURA_R,
            PET_CX + PET_AURA_R, PET_CY + PET_AURA_R,
            outline=aura, width=7)
        if self._img is not None:
            self._img_item = c.create_image(PET_CX, PET_CY, image=self._img)
        else:
            self._draw_fallback_cat()

    def _draw_fallback_cat(self):
        """无素材时的兜底手绘猫。"""
        c = self.canvas
        head = PET_AURA_COLORS.get(self._face, PET_AURA_COLORS["idle"])
        dark = "#5b4636"
        ox, oy = PET_CX - 42, PET_CY - 42
        c.create_polygon(ox + 6, oy + 14, ox + 18, oy - 10, ox + 30, oy + 6,
                         fill=head, outline=dark, width=2)
        c.create_polygon(ox + 38, oy + 6, ox + 50, oy - 10, ox + 62, oy + 14,
                         fill=head, outline=dark, width=2)
        c.create_oval(ox, oy, ox + 68, oy + 62, fill=head, outline=dark, width=2)
        c.create_oval(ox + 18, oy + 22, ox + 30, oy + 34, fill=dark)
        c.create_oval(ox + 38, oy + 22, ox + 50, oy + 34, fill=dark)
        c.create_oval(ox + 22, oy + 24, ox + 26, oy + 28, fill="#ffffff")
        c.create_oval(ox + 42, oy + 24, ox + 46, oy + 28, fill="#ffffff")
        c.create_oval(ox + 4, oy + 36, ox + 14, oy + 44, fill="#ffd9d9", outline="")
        c.create_oval(ox + 54, oy + 36, ox + 64, oy + 44, fill="#ffd9d9", outline="")
        c.create_arc(ox + 28, oy + 36, ox + 40, oy + 48, start=0, extent=180,
                     style="arc", outline=dark, width=2)

    # ---------- 状态联动 ----------
    def reflect(self, status_text):
        """根据状态栏文字切换光环颜色、宠物表情与提示气泡。"""
        if "运行中" in status_text:
            face = "running"
        elif "启动" in status_text:
            face = "starting"
        elif ("更新" in status_text or "重建" in status_text
              or "构建" in status_text or "安装" in status_text):
            face = "busy"
        elif "失败" in status_text or "超时" in status_text:
            face = "error"
        else:
            face = "idle"
        if face != self._face:
            self._face = face
            self._draw()
            if face in ("running", "error") and not self._dead:
                self._bubble(random.choice(PET_BUBBLES[face]))
                if face == "running":
                    self._float_text(random.choice(PET_FLOAT_TEXTS))
        if self._tip is not None and self._tip.winfo_exists():
            self._tip_label.configure(text=status_text)

    # ---------- 动画 ----------
    def _tick(self):
        if self._dead:
            return
        try:
            c = self.canvas
            self._bob += 0.12
            dy = int(3 * math.sin(self._bob))
            # 跳跃叠加
            if self._jump > 0:
                self._jump -= 1
                progress = 1.0 - self._jump / self._jump_total
                dy += int(math.sin(progress * math.pi) * self._jump_height)
            if self._img_item is not None:
                c.coords(self._img_item, PET_CX, PET_CY + dy)
            if self._aura_item is not None:
                c.coords(self._aura_item,
                         PET_CX - PET_AURA_R, PET_CY - PET_AURA_R + dy,
                         PET_CX + PET_AURA_R, PET_CY + PET_AURA_R + dy)
            # 飘动元素上浮
            for item in list(self._float_items):
                c.move(item, 0, -2)
                box = c.bbox(item)
                if box is None or box[1] < 2:
                    c.delete(item)
                    self._float_items.remove(item)
            # 随机待机互动（约 11-30 秒一次）
            self._idle_counter -= 1
            if self._idle_counter <= 0 and not self._drag:
                self._idle_counter = random.randint(180, 500)
                if random.random() < 0.55:
                    self._bubble(random.choice(PET_BUBBLES.get(self._face, PET_BUBBLES["idle"])))
                else:
                    self._jump_start(10)
        except tk.TclError:
            return
        self._tick_after = self.win.after(60, self._tick)

    def _jump_start(self, height=None):
        self._jump_total = 12
        self._jump = self._jump_total
        self._jump_height = height or 14

    # ---------- 互动 ----------
    def _bubble(self, text):
        """在宠物头顶显示气泡，2.6 秒后消失。"""
        if self._dead or not text:
            return
        try:
            c = self.canvas
            self._clear_bubble()
            w, h = 124, 26
            x, y = PET_CX - w / 2, 12
            items = [
                c.create_oval(x, y, x + w, y + h, fill="#ffffff",
                              outline="#e0e0e0", width=1),
                c.create_polygon(PET_CX - 9, y + h - 1, PET_CX, y + h + 10,
                                 PET_CX + 9, y + h - 1, fill="#ffffff", outline=""),
            ]
            display = text if len(text) <= 12 else text[:12] + "…"
            items.append(c.create_text(x + w / 2, y + h / 2, text=display, fill="#333333",
                                       font=("Microsoft YaHei UI", 9, "bold")))
            self._bubble_items = items
            self.win.after(2600, self._clear_bubble)
        except tk.TclError:
            pass

    def _clear_bubble(self):
        try:
            for item in self._bubble_items:
                self.canvas.delete(item)
        except tk.TclError:
            pass
        self._bubble_items = []

    def _float_text(self, text):
        """从宠物旁边飘起一个表情/爱心。"""
        if self._dead:
            return
        try:
            c = self.canvas
            if self._heart_img is not None and text == "❤":
                self._float_items.append(c.create_image(PET_CX + 40, PET_CY + 50,
                                                        image=self._heart_img))
            self._float_items.append(c.create_text(
                PET_CX + random.randint(-46, 46), PET_CY + 52, text=text,
                fill="#e05588", font=("Segoe UI Emoji", 11, "bold")))
            while len(self._float_items) > 8:
                c.delete(self._float_items.pop(0))
        except tk.TclError:
            pass

    # ---------- 鼠标事件 ----------
    def _on_press(self, event):
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())
        self._moved = False
        if self._click_after is not None:
            try:
                self.win.after_cancel(self._click_after)
            except tk.TclError:
                pass
            self._click_after = None

    def _on_drag(self, event):
        if self._drag:
            nx = event.x_root - self._drag[0]
            ny = event.y_root - self._drag[1]
            if abs(nx - self.win.winfo_x()) > 5 or abs(ny - self.win.winfo_y()) > 5:
                self._moved = True
            self.win.geometry("+%d+%d" % (nx, ny))

    def _on_release(self, _event):
        if self._drag:
            self._drag = None
            if self._moved:
                if not self._maybe_dock():
                    self.app._save_settings()  # 记住新位置（贴边时已在 _maybe_dock 内处理）
            elif self._click_after is None:
                # 单击 → 延时触发互动（若随后出现双击则取消）
                self._click_after = self.win.after(260, self._react)

    def _on_double(self, _event):
        if self._click_after is not None:
            try:
                self.win.after_cancel(self._click_after)
            except tk.TclError:
                pass
            self._click_after = None
        self.open_panel()

    def _react(self):
        """单击互动：跳一下 + 气泡 + 飘爱心。"""
        self._click_after = None
        if self._dead:
            return
        self._jump_start()
        self._float_text(random.choice(PET_FLOAT_TEXTS))
        pool = PET_BUBBLES.get(self._face, PET_BUBBLES["idle"])
        self._bubble(random.choice(pool))

    # ---------- 贴边收缩 ----------
    @staticmethod
    def _screen_bounds():
        """返回虚拟屏幕边界 (x0, y0, x1, y1)，支持多显示器；失败时按主屏近似。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            x = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            y = user32.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
            w = user32.GetSystemMetrics(78)   # SM_CXVIRTUALSCREEN
            h = user32.GetSystemMetrics(79)   # SM_CYVIRTUALSCREEN
            if w > 0 and h > 0:
                return (x, y, x + w, y + h)
        except Exception:
            pass
        return (0, 0, 0, 0)  # 调用方检测到 0 会回退主屏

    def _screen_bounds_fallback(self):
        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x0 = min(0, self.win.winfo_x())
            y0 = min(0, self.win.winfo_y())
            return (x0, y0, x0 + sw, y0 + sh)
        except tk.TclError:
            return (0, 0, 1920, 1080)

    def _slide_to(self, tx, ty, steps=PET_SLIDE_STEPS, interval=PET_SLIDE_INTERVAL):
        """把窗口平滑移动到目标位置。"""
        if self._dead:
            return
        try:
            x0, y0 = self.win.winfo_x(), self.win.winfo_y()

            def step(i):
                if self._dead:
                    return
                k = (i + 1) / steps
                self.win.geometry("+%d+%d" % (int(x0 + (tx - x0) * k),
                                              int(y0 + (ty - y0) * k)))
                if i + 1 < steps:
                    self._slide_cb = self.win.after(interval, lambda: step(i + 1))

            step(0)
        except tk.TclError:
            pass

    def _maybe_dock(self):
        """拖动松手后检测是否贴近屏幕边缘：是则滑入收缩，返回 True。"""
        try:
            x, y = self.win.winfo_x(), self.win.winfo_y()
            w, h = PET_W, PET_H
            bx, by, bx1, by1 = self._screen_bounds()
            if bx1 - bx <= 0 or by1 - by <= 0:
                bx, by, bx1, by1 = self._screen_bounds_fallback()
            right, bottom = x + w, y + h
            d = {"right": abs(right - bx1), "left": abs(x - bx),
                 "bottom": abs(bottom - by1), "top": abs(y - by)}
            edge = min(d, key=d.get)
            if d[edge] > PET_EDGE_SNAP:
                self._docked = None
                self._resting = None
                return False
            # 收缩目标：让露出的一小条正好切在宠物身体上（对准画布中心 PET_CX/PET_CY）
            if edge == "right":
                tx, ty = bx1 - PET_SLIVER - PET_CX, y
            elif edge == "left":
                tx, ty = bx - PET_CX + PET_SLIVER, y
            elif edge == "bottom":
                tx, ty = x, by1 - PET_SLIVER - PET_CY
            else:
                tx, ty = x, by - PET_CY + PET_SLIVER
            self._docked = edge
            self._resting = (x, y)
            self._slide_to(tx, ty)
            return True
        except tk.TclError:
            return False

    def _undock(self):
        """从收缩状态滑回完整位置。"""
        if not self._docked:
            return
        try:
            rx, ry = self._resting or (self.win.winfo_x(), self.win.winfo_y())
            self._slide_to(rx, ry)
        except tk.TclError:
            pass
        self._docked = None
        self._resting = None

    def _on_enter(self, event=None):
        """鼠标移入：先滑出（若贴边），再显示状态提示。"""
        if self._docked:
            self._undock()
        self._show_tip(event)

    def position(self):
        """返回应保存的位置：贴边时保存完整（滑出后）的位置；屏幕外返回 None（不落盘）。"""
        if self._docked and self._resting:
            return self._resting
        try:
            x, y = self.win.winfo_x(), self.win.winfo_y()
            if not self._position_visible(x, y):
                return None
            return (x, y)
        except tk.TclError:
            return None

    # ---------- 显示 / 隐藏 ----------
    def show(self):
        self.win.deiconify()

    def hide(self):
        self._hide_tip()
        self.win.withdraw()

    def destroy(self):
        self._dead = True
        self._hide_tip()
        try:
            self.win.after_cancel(self._tick_after)
        except (tk.TclError, AttributeError):
            pass
        if self._slide_cb is not None:
            try:
                self.win.after_cancel(self._slide_cb)
            except tk.TclError:
                pass
        self.win.destroy()

    def open_panel(self):
        self.app._show_panel()

    # ---------- 悬浮提示 ----------
    def _show_tip(self, _event=None):
        try:
            if self._tip is None or not self._tip.winfo_exists():
                self._tip = tk.Toplevel(self.app.root)
                self._tip.overrideredirect(True)
                self._tip.attributes("-topmost", True)
                self._tip.configure(bg="#3a3a3a")
                self._tip_label = tk.Label(self._tip, text="", bg="#3a3a3a",
                                           fg="#ffffff", font=("Microsoft YaHei UI", 9),
                                           padx=6, pady=2)
                self._tip_label.pack()
            self._tip_label.configure(text=self.app.status_var.get())
            # 先重新排版再测量实际宽度，把提示框水平居中到宠物中心上方
            self._tip.update_idletasks()
            tw = self._tip.winfo_reqwidth()
            th = self._tip.winfo_reqheight()
            x = self.win.winfo_x() + (PET_W - tw) // 2
            y = self.win.winfo_y() - th - 6
            # 屏幕边缘钳制，避免提示框跑到屏幕外
            sw = self.win.winfo_screenwidth()
            if x < 0:
                x = 0
            elif x + tw > sw:
                x = sw - tw
            self._tip.geometry("+%d+%d" % (x, y))
            self._tip.deiconify()
        except tk.TclError:
            pass

    def _hide_tip(self, _event=None):
        try:
            if self._tip is not None and self._tip.winfo_exists():
                self._tip.withdraw()
        except tk.TclError:
            pass

    # ---------- 右键菜单 ----------
    def _show_menu(self, event):
        app = self.app
        running = app.proc is not None and app.proc.poll() is None
        busy = app.update_active
        menu = tk.Menu(self.win, tearoff=0)
        menu.add_command(label="打开面板", command=self.open_panel)
        menu.add_separator()
        menu.add_command(label="启动服务", command=app._on_start,
                         state="disabled" if (running or busy) else "normal")
        menu.add_command(label="重启服务", command=app._on_restart,
                         state="normal" if (running and not busy) else "disabled")
        menu.add_command(label="停止服务", command=app._on_stop,
                         state="normal" if running else "disabled")
        menu.add_separator()
        if busy:
            menu.add_command(label="取消更新", command=app._on_cancel)
        else:
            menu.add_command(label="更新源码", command=app._on_update)
            menu.add_command(label="重建前端", command=app._on_rebuild,
                             state="disabled" if running else "normal")
        menu.add_separator()
        menu.add_command(label="退出程序", command=app._quit_app)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()


# ---------------- 主窗口 ----------------
class LauncherApp:
    def __init__(self, root, node_exe=None, node_dir=None):
        self.root = root
        root.title("DeepSeek Harness 启动器")
        root.geometry("760x600")
        root.minsize(640, 480)

        self.node_exe = node_exe
        self.node_dir = node_dir or (os.path.dirname(node_exe) if node_exe else DEFAULT_NODE_DIR)
        self.corepack_exe = os.path.join(self.node_dir, "corepack.cmd")
        if not os.path.isfile(self.corepack_exe):
            self.corepack_exe = os.path.join(DEFAULT_NODE_DIR, "corepack.cmd")

        self.proc = None            # 正在运行的 dsh web 子进程
        self.service_ready = threading.Event()  # 服务是否已就绪（收到 "dsh web: http://..." 就绪行）
        self.ready_url = None       # 服务端报告的就绪 URL
        self.manual_stop = False    # 是否由用户手动停止（用于区分“停止/启动失败”）
        self.update_active = False  # 更新是否进行中
        self.cancel_event = threading.Event()
        self.current_proc = None    # 更新流程当前正在运行的子进程
        self.closed = False
        self.log_queue = queue.Queue()
        self.tray = None            # 系统托盘图标（pystray）

        self.cfg = load_config()
        self._build_ui()
        self._load_defaults()
        # 桌面宠物：面板关闭后程序不退出，由宠物常驻托盘区域
        self.pet = DesktopPet(self)
        if not self.pet_enabled_var.get():
            self.pet.hide()
        self.status_var.trace_add("write", self._on_status_change)
        self.pet_enabled_var.trace_add("write", self._on_pet_toggle)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(100, self._poll_log)
        self._setup_tray()

    # ---------- 界面 ----------
    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#666666")
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 9))

        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=12, pady=4)
        ttk.Label(header, text="DeepSeek Harness 启动器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="基于源码运行 dsh web 服务", style="Hint.TLabel").pack(anchor="w")

        # 源码目录
        repo_frame = ttk.LabelFrame(self.root, text="源码目录")
        repo_frame.pack(fill="x", padx=12, pady=4)
        self.repo_var = tk.StringVar()
        ttk.Entry(repo_frame, textvariable=self.repo_var).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        ttk.Button(repo_frame, text="浏览…", width=8, command=self._browse_repo).pack(
            side="left", padx=(0, 8))

        # 端口
        port_frame = ttk.LabelFrame(self.root, text="服务端口")
        port_frame.pack(fill="x", padx=12, pady=4)
        ttk.Label(port_frame, text="端口：").pack(side="left", padx=(8, 4), pady=8)
        self.port_var = tk.StringVar(value=DEFAULT_PORT)
        ttk.Entry(port_frame, textvariable=self.port_var, width=12).pack(side="left", pady=8)
        ttk.Label(port_frame, text="默认 3080", style="Hint.TLabel").pack(side="left", padx=8)

        # Chrome 应用窗口（启动后优先用独立应用窗口打开，留空则用默认浏览器）
        app_frame = ttk.LabelFrame(self.root, text="打开方式")
        app_frame.pack(fill="x", padx=12, pady=4)
        self.chrome_app_var = tk.StringVar(value=CHROME_APP_LNK)
        ttk.Entry(app_frame, textvariable=self.chrome_app_var).pack(
            side="left", fill="x", expand=True, padx=(8, 4), pady=8)
        ttk.Button(app_frame, text="浏览…", width=8, command=self._browse_chrome_app).pack(
            side="left", padx=(0, 4))
        ttk.Button(app_frame, text="恢复默认", width=9,
                   command=self._reset_chrome_app).pack(side="left", padx=(0, 8))
        ttk.Label(app_frame, text="Chrome 应用窗口(.lnk)；留空则用默认浏览器打开",
                  style="Hint.TLabel").pack(side="left")

        # 选项
        opt_frame = ttk.LabelFrame(self.root, text="选项")
        opt_frame.pack(fill="x", padx=12, pady=4)
        self.auto_open_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="启动后自动打开浏览器",
                        variable=self.auto_open_var).pack(side="left", padx=8, pady=6)
        self.pet_enabled_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opt_frame, text="启用桌面宠物",
                        variable=self.pet_enabled_var).pack(side="left", padx=8, pady=6)

        # 按钮
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=12, pady=8)
        self.update_btn = ttk.Button(btn_frame, text="更新源码", command=self._on_update)
        self.update_btn.pack(side="left", padx=(0, 8))
        self.rebuild_btn = ttk.Button(btn_frame, text="重建前端", command=self._on_rebuild)
        self.rebuild_btn.pack(side="left", padx=(0, 8))
        self.start_btn = ttk.Button(btn_frame, text="启动", command=self._on_start)
        self.start_btn.pack(side="left", padx=(0, 8))
        self.restart_btn = ttk.Button(btn_frame, text="重启", command=self._on_restart,
                                      state="disabled")
        self.restart_btn.pack(side="left", padx=(0, 8))
        self.stop_btn = ttk.Button(btn_frame, text="停止", command=self._on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn = ttk.Button(btn_frame, text="取消更新", command=self._on_cancel,
                                     state="disabled")
        self.cancel_btn.pack(side="left")

        # 日志
        log_frame = ttk.LabelFrame(self.root, text="")
        log_frame.pack(fill="both", expand=True, padx=12, pady=4)
        log_head = ttk.Frame(log_frame)
        log_head.pack(fill="x", padx=6, pady=(4, 0))
        ttk.Label(log_head, text="运行日志", style="Hint.TLabel").pack(side="left")
        ttk.Button(log_head, text="清空", width=6, command=self._clear_log).pack(side="right")
        self.log_text = scrolledtext.ScrolledText(
            log_frame, wrap="word", state="disabled", font=("Consolas", 9), height=10)
        self.log_text.pack(fill="both", expand=True, padx=6, pady=(2, 6))

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self.root, textvariable=self.status_var, style="Status.TLabel").pack(
            fill="x", padx=12, pady=(0, 6))

    def _load_defaults(self):
        cfg = self.cfg
        self.repo_var.set(cfg.get("repo") or DEFAULT_REPO)
        self.port_var.set(str(cfg.get("port") or DEFAULT_PORT))
        self.auto_open_var.set(bool(cfg.get("auto_open", True)))
        self.pet_enabled_var.set(bool(cfg.get("pet_enabled", True)))
        # Chrome 应用窗口路径：默认内置，未配置时也用默认值（为空才表示回退浏览器）
        self.chrome_app_var.set(cfg.get("chrome_app") if cfg.get("chrome_app") else CHROME_APP_LNK)
        if cfg.get("geometry"):
            try:
                self.root.geometry(cfg["geometry"])
            except tk.TclError:
                pass
        self._append_log("DeepSeek Harness 启动器已就绪。")
        if not self.node_exe:
            self._append_log("[警告] 未找到 Node.js（默认路径 %s），启动将失败。" % DEFAULT_NODE_DIR)
        else:
            threading.Thread(target=self._node_info_worker, daemon=True).start()
        if not os.path.isfile(self.corepack_exe):
            self._append_log("[警告] 未找到 corepack.cmd（%s），更新功能不可用。" % self.corepack_exe)
        git = find_git()
        if git:
            self._append_log("已找到 git：%s" % git)
        else:
            self._append_log("[警告] 未找到 git，更新功能不可用。")

    # ---------- 日志（线程安全） ----------
    def _append_log(self, text):
        stamp = time.strftime("%H:%M:%S")
        for line in str(text).splitlines():
            if line:
                self.log_queue.put("[%s] %s" % (stamp, line))

    def _clear_log(self):
        try:
            self.log_text.configure(state="normal")
            self.log_text.delete("1.0", "end")
            self.log_text.configure(state="disabled")
        except tk.TclError:
            pass

    def _poll_log(self):
        batch = []
        for _ in range(200):  # 每轮最多取 200 行，批量插入，避免一次处理过多卡住界面
            try:
                batch.append(self.log_queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", "\n".join(batch) + "\n")
            # 超出上限时裁剪最早的内容，防止文本无限增长拖慢渲染
            total = int(self.log_text.index("end-1c").split(".")[0])
            if total > MAX_LOG_LINES:
                self.log_text.delete("1.0", "%d.0" % (total - MAX_LOG_LINES + 1))
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        if not self.closed:
            self.root.after(100, self._poll_log)

    def _ui(self, fn, *args):
        """在工作线程中把 UI 更新调度到主线程执行，避免 tkinter 跨线程崩溃。"""
        def _run():
            try:
                fn(*args)
            except Exception:
                self.log_queue.put("[UI错误] %s" % traceback.format_exc().strip())
        try:
            self.root.after(0, _run)
        except Exception:
            pass

    def _ask_on_main(self, title, message, kind="yesno"):
        """从工作线程弹对话框并阻塞等待结果；窗口已关闭时返回 None。"""
        result_q = queue.Queue()

        def ask():
            try:
                if kind == "yesnocancel":
                    result_q.put(messagebox.askyesnocancel(title, message))
                else:
                    result_q.put(messagebox.askyesno(title, message))
            except Exception:
                result_q.put(None)
        try:
            self.root.after(0, ask)
        except Exception:
            return None
        try:
            return result_q.get(timeout=300)
        except queue.Empty:
            return None

    # ---------- 子进程 ----------
    def _popen(self, argv, cwd):
        cmd = build_cmdline(argv)
        return subprocess.Popen(
            cmd, cwd=cwd, env=base_env(self.node_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )

    def _run_capture(self, argv, cwd=None, timeout=30):
        """执行命令并捕获输出，返回 (code, stdout, stderr)。"""
        cmd = build_cmdline(argv)
        try:
            proc = subprocess.run(
                cmd, cwd=cwd, env=base_env(self.node_dir),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout, creationflags=CREATE_NO_WINDOW,
            )
            return proc.returncode, proc.stdout or "", proc.stderr or ""
        except Exception as exc:
            return 1, "", str(exc)

    def _taskkill(self, pid):
        try:
            subprocess.run([TASKKILL_EXE, "/PID", str(pid), "/T", "/F"],
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           creationflags=CREATE_NO_WINDOW)
        except Exception as exc:
            self._append_log("[错误] 停止进程失败：%s" % exc)

    def _kill_process(self, pid):
        """结束进程及其子进程树（/T /F），确保端口能真正释放。"""
        try:
            r = subprocess.run([TASKKILL_EXE, "/F", "/T", "/PID", str(pid)],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or "").strip() or (r.stderr or "").strip()
            if out and "SUCCESS" not in out and "成功" not in out:
                self._append_log("[提示] 结束 PID %d：%s" % (pid, out.splitlines()[-1] if out else ""))
        except Exception as exc:
            self._append_log("[错误] 结束进程失败：%s" % exc)

    @staticmethod
    def _port_open(host, port):
        """仅探测 TCP 端口是否在监听（用于启动前的占用检查）。"""
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            return False

    def _port_occupier_pids(self, port):
        """返回所有监听该端口的进程 PID 列表；无占用或查询失败返回 []。"""
        pids = []
        try:
            r = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                               capture_output=True, text=True,
                               timeout=15, creationflags=CREATE_NO_WINDOW)
            needle = ":%d " % port
            for line in (r.stdout or "").splitlines():
                if "LISTENING" in line and needle in line:
                    parts = line.split()
                    if parts and parts[-1].isdigit():
                        p = int(parts[-1])
                        if p not in pids:
                            pids.append(p)
        except Exception:
            pass
        return pids

    def _port_occupier(self, port):
        """返回占用端口的进程信息 (pid, name, path)；未占用或查询失败返回 None。"""
        pids = self._port_occupier_pids(port)
        if not pids:
            return None
        pid = pids[0]
        name, path = "未知进程", ""
        try:
            ps = ('powershell -NoProfile -ExecutionPolicy Bypass -Command '
                  '"Get-Process -Id %d -ErrorAction SilentlyContinue | '
                  'ForEach-Object { $_.ProcessName + \'|\' + $_.Path }"' % pid)
            r = subprocess.run(ps, capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=10, creationflags=CREATE_NO_WINDOW)
            out = (r.stdout or "").strip()
            if "|" in out:
                name, _, path = out.partition("|")
        except Exception:
            pass
        return pid, name or "未知进程", path or ""

    @staticmethod
    def _wait_port_free(host, port, timeout=5):
        """等待端口释放，最多 timeout 秒；释放返回 True，超时返回 False。"""
        end = time.time() + timeout
        while time.time() < end:
            if not LauncherApp._port_open(host, port):
                return True
            time.sleep(0.3)
        return False

    @staticmethod
    def _http_ready(host, port, timeout=1):
        """HTTP 探测：能拿到服务端响应才算就绪（TCP 可连不代表 HTTP 已可用）。"""
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            try:
                conn.request("GET", "/")
                conn.getresponse()
                return True
            finally:
                conn.close()
        except Exception:
            return False

    @staticmethod
    def _parse_node_version(ver):
        m = re.search(r"(\d+)\.(\d+)", ver or "")
        if not m:
            return 0, 0
        return int(m.group(1)), int(m.group(2))

    @staticmethod
    def _node_version_ok(major, minor):
        if major == 22:
            return minor >= 19
        return major >= 24

    # ---------- 事件处理 ----------
    def _browse_repo(self):
        path = filedialog.askdirectory(initialdir=self.repo_var.get() or DEFAULT_REPO,
                                       title="选择 deepseek-harness 源码目录")
        if path:
            self.repo_var.set(path)

    def _browse_chrome_app(self):
        path = filedialog.askopenfilename(
            initialdir=os.path.dirname(CHROME_APP_LNK),
            filetypes=[("快捷方式", "*.lnk"), ("所有文件", "*.*")],
            title="选择 Chrome 应用窗口快捷方式")
        if path:
            self.chrome_app_var.set(path)

    def _reset_chrome_app(self):
        self.chrome_app_var.set(CHROME_APP_LNK)

    def _validate_port(self):
        port = self.port_var.get().strip()
        if not port.isdigit():
            messagebox.showwarning("端口无效", "端口必须是数字。")
            return None
        p = int(port)
        if not (1 <= p <= 65535):
            messagebox.showwarning("端口无效", "端口需在 1 - 65535 之间。")
            return None
        return p

    def _repo_dir(self):
        repo = self.repo_var.get().strip()
        if not repo:
            messagebox.showwarning("缺少目录", "请先填写源码目录。")
            return None
        if not os.path.isdir(os.path.join(repo, "apps", "cli")):
            messagebox.showwarning("目录无效", "所选目录不像是 deepseek-harness 源码（缺少 apps/cli）。")
            return None
        return repo

    def _set_running(self, running):
        """统一刷新按钮可用状态。running：服务进程是否在运行。"""
        busy = running or self.update_active
        self.start_btn.configure(state="disabled" if busy else "normal")
        self.restart_btn.configure(state="normal" if running else "disabled")
        self.stop_btn.configure(state="normal" if running else "disabled")
        self.update_btn.configure(state="disabled" if self.update_active else "normal")
        self.rebuild_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if self.update_active else "disabled")

    # ---------- 启动 ----------
    def _on_start(self):
        if self.update_active:
            messagebox.showinfo("正在更新", "更新进行中，请先等待完成或点击“取消更新”。")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("已在运行", "服务已在运行中，请先停止。")
            return
        port = self._validate_port()
        repo = self._repo_dir()
        if port is None or repo is None:
            return
        if self._port_open("127.0.0.1", port):
            pids = self._port_occupier_pids(port)
            info = self._port_occupier(port)
            if info:
                pid, name, path = info
                msg = ("端口 %d 已被以下进程占用：\n\n"
                       "  进程：%s (PID %d)\n"
                       "  路径：%s\n\n"
                       "是否结束该进程并启动服务？\n"
                       "（选择“否”则取消启动）" % (port, name, pid, path or "未知"))
            else:
                pid_list = "、".join(str(p) for p in pids) if pids else "未知"
                msg = ("端口 %d 已被其他程序占用（无法识别进程名）。\n\n"
                       "将结束占用该端口的所有进程（PID：%s）后启动服务。\n"
                       "是否继续？\n"
                       "（选择“否”则取消启动）" % (port, pid_list))
            if not messagebox.askyesno("端口已占用", msg):
                return
            # 结束所有占用该端口的进程（尽量连带子进程树），避免旧 node 残留
            for pid in pids:
                self._append_log("正在结束占用进程 PID %d…" % pid)
                self._kill_process(pid)
            if not self._wait_port_free("127.0.0.1", port, timeout=5):
                self._append_log("[错误] 端口 %d 仍未释放，取消启动。" % port)
                return
        self._save_settings()
        self._set_running(True)
        self.status_var.set("正在启动（端口 %d）…" % port)
        auto_open = bool(self.auto_open_var.get())
        threading.Thread(target=self._start_worker, args=(repo, port, auto_open),
                         daemon=True).start()

    def _start_worker(self, repo, port, auto_open):
        if not self.node_exe:
            self._append_log("[错误] 未找到 node.exe，无法启动。")
            self._ui(self.status_var.set, "启动失败")
            self._ui(self._set_running, False)
            return
        if not os.path.isfile(os.path.join(repo, "apps", "cli", "src", "bin.ts")):
            self._append_log("[错误] 缺少 apps/cli/src/bin.ts，源码可能不完整。")
            self._ui(self.status_var.set, "启动失败")
            self._ui(self._set_running, False)
            return
        if not os.path.isdir(os.path.join(repo, "node_modules", "tsx")):
            self._append_log("[错误] 未找到 node_modules/tsx，请先执行“更新源码”安装依赖。")
            self._ui(self.status_var.set, "启动失败")
            self._ui(self._set_running, False)
            return
        if not os.path.isfile(os.path.join(repo, "apps", "web", "dist", "index.html")):
            self._append_log("[警告] 未找到 apps/web/dist/index.html（前端资源未构建），"
                             "页面可能无法加载插件，可先点击“重建前端”。")
        cmd = [self.node_exe, "--import", "tsx/esm", "apps/cli/src/bin.ts",
               "web", "--port", str(port)]
        self._append_log("> " + " ".join(cmd))
        self.manual_stop = False
        self.service_ready.clear()
        self.ready_url = None
        try:
            proc = self._popen(cmd, repo)
        except Exception as exc:
            self._append_log("[错误] 启动失败：%s" % exc)
            self._ui(self.status_var.set, "启动失败")
            self._ui(self._set_running, False)
            return
        self.proc = proc
        # 后台线程持续读取输出，进程退出时收尾
        threading.Thread(target=self._read_stdout, args=(proc,), daemon=True).start()

        # 等待就绪：服务端只在插件树完全收敛后才打印 "dsh web: http://..." 就绪行。
        # 在此之前端口虽可访问，但核心服务可能尚未挂载完，过早打开页面会看到插件加载失败。
        deadline = time.time() + READY_TIMEOUT
        while time.time() < deadline and proc.poll() is None and not self.service_ready.is_set():
            time.sleep(READY_INTERVAL)
        if self.service_ready.is_set():
            url = self.ready_url or ("http://127.0.0.1:%d" % port)
            self._append_log("服务已就绪：%s" % url)
            self._ui(self.status_var.set, "运行中（端口 %d）" % port)
            if auto_open:
                self._open_browser(url)
        elif proc.poll() is not None:
            self._append_log("[提示] 服务进程提前退出，请查看上方日志。")
            self._ui(self.status_var.set, "启动失败")
        elif self._http_ready("127.0.0.1", port):
            # 超时未收到就绪行，但端口已可访问：不再自动打开浏览器，避免竞态
            self._append_log("[警告] 未收到服务端就绪行（dsh web: ...），但端口已可访问，"
                             "请稍后手动刷新页面。")
            self._ui(self.status_var.set, "运行中（端口 %d）" % port)
        else:
            self._append_log("[提示] 等待服务就绪超时（%s 秒），请检查日志。" % READY_TIMEOUT)
            self._ui(self.status_var.set, "启动超时")

    # ---------- 打开浏览器（避免重复标签） ----------
    def _open_browser(self, url):
        """服务就绪后打开页面：优先 Chrome 应用窗口（已打开则刷新），
        应用不可用（未配置/文件不存在/启动失败）时回退默认浏览器标签。"""
        # 1) Chrome 应用窗口
        lnk = self.chrome_app_var.get().strip()
        if lnk and os.path.isfile(lnk):
            try:
                if self._try_refresh_chrome_app():
                    self._append_log("已在 Chrome 应用窗口中打开，已刷新页面。")
                    return
            except Exception as exc:
                self._append_log("[提示] Chrome 应用窗口检测失败：%s" % exc)
            try:
                os.startfile(lnk)
                self._append_log("已打开 Chrome 应用窗口（DeepSeek Harness）。")
                return
            except Exception as exc:
                self._append_log("[提示] 打开 Chrome 应用窗口失败（%s），改用浏览器打开。" % exc)
        # 2) 默认浏览器
        try:
            if self._try_refresh_browser_tab(url):
                self._append_log("已在浏览器中找到该页面，已刷新标签。")
                return
        except Exception as exc:
            self._append_log("[提示] 浏览器标签检测失败，改用常规方式打开：%s" % exc)
        try:
            webbrowser.open(url)
        except Exception as exc:
            self._append_log("[提示] 自动打开浏览器失败：%s" % exc)

    def _try_refresh_chrome_app(self):
        """探测已打开的 Chrome 应用窗口并刷新；找到返回 True，否则 False。
        应用名从快捷方式文件名推断（如 DeepSeek Harness.lnk -> DeepSeek Harness）。"""
        lnk = self.chrome_app_var.get().strip()
        title = os.path.splitext(os.path.basename(lnk))[0] or "DeepSeek Harness"
        ps1 = os.path.join(tempfile.gettempdir(), "dsh_chrome_app_refresh.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(CHROME_APP_REFRESH_PS1)
        powershell = os.path.join(SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
        proc = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1, "-appTitle", title],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=CREATE_NO_WINDOW)
        return (proc.stdout or "").strip() == "refresh"

    def _try_refresh_browser_tab(self, url):
        """用 UIAutomation 探测浏览器：找到目标标签则刷新并返回 True。"""
        ps1 = os.path.join(tempfile.gettempdir(), "dsh_browser_dedup.ps1")
        with open(ps1, "w", encoding="utf-8-sig") as f:
            f.write(BROWSER_DEDUP_PS1)
        powershell = os.path.join(SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe")
        proc = subprocess.run(
            [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", ps1, "-url", url],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=15, creationflags=CREATE_NO_WINDOW)
        out = (proc.stdout or "").strip()
        return out == "refresh"

    def _read_stdout(self, proc):
        try:
            for line in proc.stdout:
                self._append_log(line.rstrip("\n"))
                # 服务端在插件树完全收敛后打印就绪行：dsh web: http://127.0.0.1:3080
                m = re.search(r"dsh web:\s*(https?://\S+)", line)
                if m and not self.service_ready.is_set():
                    self.ready_url = m.group(1)
                    self.service_ready.set()
        except Exception:
            pass
        proc.wait()
        if self.proc is proc:
            self.proc = None
        code = proc.returncode
        self._append_log("服务进程已退出（退出码 %s）。" % code)
        if self.closed:
            return
        final = "已停止" if (self.service_ready.is_set() or self.manual_stop) else "启动失败"
        self._ui(self.status_var.set, final)
        self._ui(self._set_running, False)

    # ---------- 停止 ----------
    def _stop_service(self):
        proc = self.proc
        if not proc or proc.poll() is not None:
            return
        self.manual_stop = True
        self._append_log("正在停止服务（PID %d）…" % proc.pid)
        self._taskkill(proc.pid)

    def _on_stop(self):
        if self.proc and self.proc.poll() is None:
            self._stop_service()
        self.status_var.set("已停止")
        self._set_running(False)

    def _on_restart(self):
        """重启服务：先停止当前服务，等端口释放后按相同配置重新启动。"""
        if self.update_active:
            messagebox.showinfo("正在更新", "更新进行中，请先等待完成或点击“取消更新”。")
            return
        if not (self.proc and self.proc.poll() is None):
            messagebox.showinfo("服务未运行", "服务当前未运行，请先点击“启动”。")
            return
        port = self._validate_port()
        repo = self._repo_dir()
        if port is None or repo is None:
            return
        self._append_log("正在重启服务（端口 %d）…" % port)
        self.status_var.set("正在重启（端口 %d）…" % port)
        self._set_running(True)  # 重启期间禁用按钮，避免重复操作
        auto_open = bool(self.auto_open_var.get())
        threading.Thread(target=self._restart_worker, args=(repo, port, auto_open),
                         daemon=True).start()

    def _restart_worker(self, repo, port, auto_open):
        """重启工作线程：停止旧进程 -> 等端口释放 -> 复用启动流程。"""
        try:
            self._stop_service()
        except Exception:
            pass
        # 等待旧进程退出且端口释放（最多 8 秒）
        deadline = time.time() + 8
        while time.time() < deadline:
            proc = self.proc
            stopped = (proc is None or proc.poll() is not None)
            if stopped and not self._port_open("127.0.0.1", port):
                break
            time.sleep(0.3)
        if self._port_open("127.0.0.1", port):
            self._append_log("[错误] 服务已停止但端口 %d 未释放，取消重启。" % port)
            self._ui(self.status_var.set, "重启失败")
            self._ui(self._set_running, False)
            return
        self._append_log("旧服务已停止，正在启动新实例…")
        self._start_worker(repo, port, auto_open)

    # ---------- 更新 ----------
    def _on_update(self):
        if self.update_active:
            return
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("停止服务",
                                       "服务正在运行，更新前将先停止服务。确定继续吗？"):
                return
            self._stop_service()
        repo = self._repo_dir()
        if repo is None:
            return
        git = find_git()
        if not git:
            messagebox.showwarning("缺少 git", "未找到 git，无法更新。")
            return
        self.cancel_event = threading.Event()
        self.update_active = True
        running = self.proc is not None and self.proc.poll() is None
        self._set_running(running)
        self.status_var.set("正在更新源码…")
        threading.Thread(target=self._update_worker, args=(repo, git), daemon=True).start()

    def _on_cancel(self):
        if not self.update_active:
            return
        self._append_log("正在取消更新…")
        self.cancel_event.set()
        p = self.current_proc
        if p and p.poll() is None:
            self._taskkill(p.pid)

    def _on_rebuild(self):
        """重建前端（不拉代码）：重新安装依赖并构建，用于修复前端资源与源码不匹配。"""
        if self.update_active:
            return
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("停止服务",
                                       "服务正在运行，构建前将先停止服务。确定继续吗？"):
                return
            self._stop_service()
        repo = self._repo_dir()
        if repo is None:
            return
        self.cancel_event = threading.Event()
        self.update_active = True
        running = self.proc is not None and self.proc.poll() is None
        self._set_running(running)
        self.status_var.set("正在重建前端…")
        threading.Thread(target=self._rebuild_worker, args=(repo,), daemon=True).start()

    def _rebuild_worker(self, repo):
        self._append_log("===== 开始重建前端 =====")
        try:
            if not os.path.isdir(os.path.join(repo, "node_modules", "tsx")):
                self._append_log("未找到 node_modules/tsx，先安装依赖…")
                code = self._run_step([self.corepack_exe, "pnpm", "install"], repo)
                if self.cancel_event.is_set():
                    self._append_log("===== 已取消 =====")
                    self._ui(self.status_var.set, "已取消")
                    return
                if code != 0:
                    self._append_log("[错误] 安装依赖失败（退出码 %s）。" % code)
                    self._ui(self.status_var.set, "构建失败")
                    return
            steps = [
                ("构建库", [self.corepack_exe, "pnpm", "run", "build:lib"]),
                ("构建 Web 前端", [self.corepack_exe, "pnpm", "--filter",
                                 "@deepseek-ai/dsh-web-frontend", "run", "build"]),
            ]
            ok = True
            for name, argv in steps:
                if self.cancel_event.is_set():
                    ok = False
                    break
                self._append_log("----- %s -----" % name)
                code = self._run_step(argv, repo)
                if self.cancel_event.is_set():
                    ok = False
                    break
                if code != 0:
                    self._append_log("[错误] %s 失败（退出码 %s），构建中止。" % (name, code))
                    ok = False
                    break
            if ok:
                self._append_log("===== 前端重建完成，可启动 =====")
                self._ui(self.status_var.set, "构建完成，可启动")
            elif self.cancel_event.is_set():
                self._append_log("===== 已取消 =====")
                self._ui(self.status_var.set, "已取消")
            else:
                self._append_log("===== 构建失败 =====")
                self._ui(self.status_var.set, "构建失败")
        finally:
            self.update_active = False
            running = self.proc is not None and self.proc.poll() is None
            self._ui(self._set_running, running)

    def _update_worker(self, repo, git):
        self._append_log("===== 开始更新源码 =====")
        try:
            # 若服务仍在停止中，等它真正退出，避免文件锁冲突
            proc = self.proc
            if proc and proc.poll() is None:
                self._append_log("等待服务进程退出…")
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self._append_log("[错误] 服务进程未能及时退出，更新中止。")
                    self._ui(self.status_var.set, "更新失败")
                    return

            # 探测远程默认分支（避免硬编码 master）
            # ls-remote --symref 输出形如 "ref: refs/heads/master\tHEAD"，需按空白切分
            branch = "master"
            code, out, _ = self._run_capture(
                [git] + GIT_SSL_ARGS + ["ls-remote", "--symref", "origin", "HEAD"],
                repo, timeout=60)
            if code == 0:
                for line in out.splitlines():
                    if line.startswith("ref:"):
                        parts = line.split()
                        if len(parts) >= 2:
                            ref = parts[1]  # 例如 refs/heads/master
                            b = ref.split("/")[-1].strip()
                            if b and b != "HEAD" and not any(c in b for c in (" ", "\t")):
                                branch = b
                                break
            else:
                # 探测失败（如网络问题）时回退到本地当前分支
                code, out, _ = self._run_capture(
                    [git] + GIT_SSL_ARGS + ["rev-parse", "--abbrev-ref", "HEAD"],
                    repo, timeout=15)
                if code == 0 and out.strip() and out.strip() != "HEAD":
                    branch = out.strip()
            self._append_log("远程默认分支：%s" % branch)

            # 先拉取远程最新（只读操作，不碰工作区），再做版本校验
            if self.cancel_event.is_set():
                return
            self._append_log("----- 拉取远程最新（%s） -----" % branch)
            code = self._run_step([git] + GIT_SSL_ARGS + ["fetch", "--depth", "1",
                                                          "origin", branch], repo)
            if self.cancel_event.is_set():
                return
            if code != 0:
                self._append_log("[错误] 拉取远程最新失败（退出码 %s），更新中止。" % code)
                self._ui(self.status_var.set, "更新失败")
                return

            # 版本校验：本地 HEAD 与远程 FETCH_HEAD 一致则说明已是最新
            code, out, _ = self._run_capture([git] + GIT_SSL_ARGS + ["rev-parse", "HEAD"],
                                             repo, timeout=15)
            local_head = out.strip() if code == 0 else ""
            code, out, _ = self._run_capture([git] + GIT_SSL_ARGS + ["rev-parse", "FETCH_HEAD"],
                                             repo, timeout=15)
            remote_head = out.strip() if code == 0 else ""
            deps_ok = os.path.isdir(os.path.join(repo, "node_modules", "tsx"))
            if local_head and local_head == remote_head:
                if deps_ok:
                    self._append_log("已是最新版本（%s），无需更新。" % local_head[:12])
                    self._ui(self.status_var.set, "已是最新版本")
                    return
                self._append_log("代码已是最新（%s），但依赖未安装，继续安装依赖并构建。"
                                 % local_head[:12])
                steps = [
                    ("安装依赖", [self.corepack_exe, "pnpm", "install"]),
                    ("构建库", [self.corepack_exe, "pnpm", "run", "build:lib"]),
                    ("构建 Web 前端", [self.corepack_exe, "pnpm", "--filter",
                                     "@deepseek-ai/dsh-web-frontend", "run", "build"]),
                ]
            else:
                # 代码有更新，会执行 reset：先处理本地未提交改动
                code, out, _ = self._run_capture([git] + GIT_SSL_ARGS + ["status",
                                                                         "--porcelain"],
                                                 repo, timeout=30)
                if code == 0 and out.strip():
                    preview = "\n".join(out.strip().splitlines()[:10])
                    choice = self._ask_on_main(
                        "检测到未提交改动",
                        "源码目录有未提交改动：\n\n%s\n\n"
                        "[是] 放弃这些改动，继续更新\n"
                        "[否] git stash 暂存改动（更新后可用 git stash pop 恢复）\n"
                        "[取消] 中止更新" % preview, kind="yesnocancel")
                    if choice is None:
                        self._append_log("[提示] 用户中止，未做任何改动。")
                        self._ui(self.status_var.set, "已中止")
                        return
                    if choice is False:
                        code, _, err = self._run_capture(
                            [git] + GIT_SSL_ARGS + ["stash", "push", "-u", "-m",
                                                    "dsh-launcher auto stash"],
                            repo, timeout=60)
                        if code == 0:
                            self._append_log("已暂存改动，更新后可执行 git stash pop 恢复。")
                        else:
                            self._append_log("[警告] git stash 失败：%s" % err.strip())
                steps = [
                    ("重置到远程最新", [git] + GIT_SSL_ARGS + ["reset", "--hard", "FETCH_HEAD"]),
                    ("安装依赖", [self.corepack_exe, "pnpm", "install"]),
                    ("构建库", [self.corepack_exe, "pnpm", "run", "build:lib"]),
                    ("构建 Web 前端", [self.corepack_exe, "pnpm", "--filter",
                                     "@deepseek-ai/dsh-web-frontend", "run", "build"]),
                ]
            ok = True
            for name, argv in steps:
                if self.cancel_event.is_set():
                    ok = False
                    break
                self._append_log("----- %s -----" % name)
                code = self._run_step(argv, repo)
                if self.cancel_event.is_set():
                    ok = False
                    break
                if code != 0:
                    self._append_log("[错误] %s 失败（退出码 %s），更新中止。" % (name, code))
                    ok = False
                    break
            if ok:
                self._append_log("===== 更新完成 =====")
                self._ui(self.status_var.set, "更新完成，可启动")
            elif self.cancel_event.is_set():
                self._append_log("===== 更新已取消 =====")
                self._ui(self.status_var.set, "更新已取消")
            else:
                self._append_log("===== 更新失败 =====")
                self._ui(self.status_var.set, "更新失败")
        finally:
            self.update_active = False
            running = self.proc is not None and self.proc.poll() is None
            self._ui(self._set_running, running)

    def _run_step(self, argv, cwd):
        cmd = build_cmdline(argv)
        try:
            proc = self._popen(cmd, cwd)
        except Exception as exc:
            self._append_log("[错误] 无法执行 %s：%s" % (argv[0], exc))
            return 1
        self.current_proc = proc
        try:
            for line in proc.stdout:
                self._append_log(line.rstrip("\n"))
        except Exception:
            pass
        proc.wait()
        self.current_proc = None
        return proc.returncode

    # ---------- 版本检查 / 设置 ----------
    def _node_info_worker(self):
        code, out, _ = self._run_capture([self.node_exe, "-v"], timeout=10)
        if code != 0:
            self._append_log("[警告] 无法获取 Node.js 版本：%s" % (out.strip() or "未知错误"))
            return
        ver = out.strip()
        self._append_log("Node.js 版本：%s" % ver)
        major, minor = self._parse_node_version(ver)
        if not self._node_version_ok(major, minor):
            self._append_log("[警告] Node.js 版本不满足仓库要求（%s），启动或构建可能失败。"
                             % NODE_VERSION_HINT)

    def _save_settings(self):
        cfg = self.cfg
        cfg["repo"] = self.repo_var.get().strip()
        cfg["port"] = self.port_var.get().strip()
        cfg["auto_open"] = bool(self.auto_open_var.get())
        cfg["pet_enabled"] = bool(self.pet_enabled_var.get())
        cfg["chrome_app"] = self.chrome_app_var.get().strip()
        if hasattr(self, "pet"):
            try:
                pos = self.pet.position()
                if pos is not None:
                    cfg["pet_x"], cfg["pet_y"] = pos
            except tk.TclError:
                pass
        try:
            cfg["geometry"] = self.root.geometry()
        except tk.TclError:
            pass
        save_config(cfg)

    # ---------- 状态联动（宠物表情） ----------
    def _on_status_change(self, *_args):
        try:
            self.pet.reflect(self.status_var.get())
        except Exception:
            pass

    def _on_pet_toggle(self, *_args):
        """勾选/取消「启用桌面宠物」时立即显示/隐藏宠物。"""
        try:
            if self.pet_enabled_var.get():
                self.pet.show()
            else:
                self.pet.hide()
        except Exception:
            pass

    # ---------- 面板显示 / 关闭 / 退出 ----------
    def _show_panel(self):
        try:
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def _setup_tray(self):
        """创建系统托盘图标：左键恢复宠物（默认项），右键菜单含启停/退出。"""
        try:
            from pystray import Icon, Menu, MenuItem
            img = make_tray_image()
            if img is None:
                self._append_log("[提示] 托盘图标生成失败，跳过。")
                return
            menu = Menu(
                MenuItem("显示宠物", lambda: self._ui(self._tray_show_pet), default=True),
                MenuItem("打开面板", lambda: self._ui(self._show_panel)),
                MenuItem("启动服务", lambda: self._ui(self._on_start)),
                MenuItem("重启服务", lambda: self._ui(self._on_restart)),
                MenuItem("停止服务", lambda: self._ui(self._on_stop)),
                MenuItem("退出程序", lambda: self._ui(self._quit_app)),
            )
            icon = Icon("dsh-launcher", img, "DeepSeek Harness 启动器", menu)
            icon.run_detached()
            self.tray = icon
            self._append_log("系统托盘图标已就绪（左键恢复宠物）。")
        except Exception as exc:
            self._append_log("[提示] 托盘图标初始化失败：%s" % exc)

    def _tray_show_pet(self):
        """从托盘恢复宠物：位置无效则重置到右下角并显示。"""
        try:
            if not self.pet_enabled_var.get():
                self.pet_enabled_var.set(True)
            self.pet._undock()
            pos = self.pet.position()
            if pos is None or not self.pet._position_visible(pos[0], pos[1]):
                sw = self.root.winfo_screenwidth()
                sh = self.root.winfo_screenheight()
                self.pet.win.geometry("+%d+%d" % (sw - PET_W - 24, sh - PET_H - 80))
            self.pet.show()
            self._save_settings()
        except Exception:
            pass

    def _on_close(self):
        """关闭面板：启用宠物时隐藏到宠物（服务继续运行），否则完全退出。"""
        if self.pet_enabled_var.get():
            if self.update_active:
                if not messagebox.askyesno("隐藏",
                                           "更新仍在进行，隐藏到宠物后更新将继续。确定隐藏吗？"):
                    return
            if self.proc and self.proc.poll() is None:
                self._append_log("面板已隐藏，服务仍在运行（可右键桌面宠物/托盘图标操作）。")
            self._save_settings()
            self.root.withdraw()
            return
        self._quit_app()

    def _quit_app(self):
        """真正退出程序：中断更新、停止服务、保存配置并销毁窗口。"""
        if self.update_active:
            if not messagebox.askyesno("退出", "更新仍在进行，退出将中断更新。确定退出吗？"):
                return
            self.cancel_event.set()
            p = self.current_proc
            if p and p.poll() is None:
                self._taskkill(p.pid)
        if self.proc and self.proc.poll() is None:
            if messagebox.askyesno("退出", "服务仍在运行，确定要退出并停止服务吗？"):
                self._stop_service()
            else:
                return
        self.closed = True
        self._save_settings()
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        try:
            self.pet.destroy()
        except Exception:
            pass
        self.root.destroy()


def _ensure_single_instance():
    """单实例保护：已有一个启动器运行时提示并退出。"""
    try:
        import ctypes
        ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            root = tk.Tk()
            root.withdraw()
            messagebox.showwarning("dsh-launcher", "启动器已有一个实例在运行。")
            root.destroy()
            return False
    except Exception:
        pass
    return True


def main():
    if not _ensure_single_instance():
        return
    root = tk.Tk()
    node_exe, node_dir = find_node()
    LauncherApp(root, node_exe=node_exe, node_dir=node_dir)
    root.mainloop()


if __name__ == "__main__":
    main()
