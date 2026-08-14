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
import os
import queue
import re
import socket
import subprocess
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

# 状态 → 脸的颜色
PET_FACE_COLORS = {
    "running": "#9fe8a0",   # 运行中：绿
    "starting": "#ffe9a8",  # 启动中：黄
    "busy": "#bcd6ff",      # 更新/重建/构建：蓝
    "error": "#ffc9c9",     # 失败/超时：红
    "idle": "#f6e3b4",      # 就绪：奶油色
}

PET_SIZE = 84


class DesktopPet:
    """常驻桌面的小宠物：双击/右键打开面板，右键菜单可启动、停止、更新、退出。"""

    def __init__(self, app):
        self.app = app
        self._drag = None
        self._tip = None
        self._face = "idle"

        self.win = tk.Toplevel(app.root)
        self.win.overrideredirect(True)
        try:
            self.win.attributes("-topmost", True)
            self.win.attributes("-transparentcolor", PET_TRANSPARENT)
        except tk.TclError:
            pass
        self.win.configure(bg=PET_TRANSPARENT)

        self.canvas = tk.Canvas(self.win, width=PET_SIZE, height=PET_SIZE,
                                bg=PET_TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack()
        self._draw()

        # 交互
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", lambda e: self.open_panel())
        self.canvas.bind("<Button-3>", self._show_menu)
        self.canvas.bind("<Enter>", self._show_tip)
        self.canvas.bind("<Leave>", self._hide_tip)

        # 初始位置：上次保存的位置，或屏幕右下角
        cfg = app.cfg
        x, y = cfg.get("pet_x"), cfg.get("pet_y")
        if isinstance(x, int) and isinstance(y, int):
            self.win.geometry("+%d+%d" % (x, y))
        else:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            self.win.geometry("+%d+%d" % (sw - PET_SIZE - 24, sh - PET_SIZE - 120))

    # ---------- 绘制 ----------
    def _draw(self):
        self.canvas.delete("all")
        c = self.canvas
        head = PET_FACE_COLORS.get(self._face, PET_FACE_COLORS["idle"])
        dark = "#5b4636"
        # 耳朵（猫）
        c.create_polygon(14, 30, 26, 6, 38, 22, fill=head, outline=dark, width=2)
        c.create_polygon(46, 22, 58, 6, 70, 30, fill=head, outline=dark, width=2)
        # 头
        c.create_oval(8, 16, 76, 78, fill=head, outline=dark, width=2)
        # 眼睛
        c.create_oval(26, 38, 38, 50, fill=dark)
        c.create_oval(46, 38, 58, 50, fill=dark)
        c.create_oval(30, 40, 34, 44, fill="#ffffff")
        c.create_oval(50, 40, 54, 44, fill="#ffffff")
        # 腮红
        c.create_oval(12, 52, 22, 60, fill="#ffd9d9", outline="")
        c.create_oval(62, 52, 72, 60, fill="#ffd9d9", outline="")
        # 嘴
        c.create_arc(36, 52, 48, 64, start=0, extent=180, style="arc", outline=dark, width=2)

    def reflect(self, status_text):
        """根据状态栏文字切换脸的颜色与悬浮提示。"""
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
        if self._tip is not None and self._tip.winfo_exists():
            self._tip_label.configure(text=status_text)

    # ---------- 显示 / 隐藏 ----------
    def show(self):
        self.win.deiconify()

    def hide(self):
        self._hide_tip()
        self.win.withdraw()

    def destroy(self):
        self._hide_tip()
        self.win.destroy()

    def open_panel(self):
        self.app._show_panel()

    # ---------- 拖动 ----------
    def _on_press(self, event):
        self._drag = (event.x_root - self.win.winfo_x(),
                      event.y_root - self.win.winfo_y())

    def _on_drag(self, event):
        if self._drag:
            self.win.geometry("+%d+%d" % (event.x_root - self._drag[0],
                                          event.y_root - self._drag[1]))

    def _on_release(self, _event):
        if self._drag:
            self._drag = None
            self.app._save_settings()  # 记住新位置

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
            x = self.win.winfo_x() + PET_SIZE // 2
            y = self.win.winfo_y() - 26
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

    @staticmethod
    def _port_open(host, port):
        """仅探测 TCP 端口是否在监听（用于启动前的占用检查）。"""
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
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
            if not messagebox.askyesno("端口已占用",
                                       "端口 %d 似乎已被其他程序占用，仍要启动吗？" % port):
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
                try:
                    webbrowser.open(url)
                except Exception as exc:
                    self._append_log("[提示] 自动打开浏览器失败：%s" % exc)
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
        if hasattr(self, "pet"):
            try:
                cfg["pet_x"] = self.pet.win.winfo_x()
                cfg["pet_y"] = self.pet.win.winfo_y()
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

    def _on_close(self):
        """关闭面板：启用宠物时隐藏到宠物（服务继续运行），否则完全退出。"""
        if self.pet_enabled_var.get():
            if self.update_active:
                if not messagebox.askyesno("隐藏",
                                           "更新仍在进行，隐藏到宠物后更新将继续。确定隐藏吗？"):
                    return
            if self.proc and self.proc.poll() is None:
                self._append_log("面板已隐藏，服务仍在运行（可右键桌面宠物操作）。")
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
