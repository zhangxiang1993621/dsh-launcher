# DeepSeek Harness 启动器

Windows 桌面启动器，用于从源码运行 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（`dsh`）的 Web 服务。

技术栈：Python + tkinter，`subprocess` 管理 git / node / corepack 子进程，可用 PyInstaller 打包为单文件 exe。

## 功能

- **更新源码**：从 GitHub 拉取最新源码（自动探测远程默认分支），重新安装依赖并构建；本地有未提交改动时可选「放弃 / git stash 保留 / 中止」；代码已是最新版本时自动跳过
- **启动服务**：默认端口 3080，等待服务端就绪信号（`dsh web: http://...`）后才打开浏览器，避免插件加载竞态
- **停止服务**：`taskkill /T /F` 结束整棵进程树
- **重建前端**：代码不变时重新构建前端资源（修复 dist 与源码不匹配导致的插件加载失败）
- **桌面宠物**：关闭面板后程序驻留桌面小宠物，右键可启动/停止/更新/打开面板，表情随服务状态变色
- 配置持久化（`%APPDATA%\dsh-launcher\config.json`）、单实例保护、端口占用预检、Node 版本校验

## 运行

```bash
# 直接运行源码
python dsh_launcher.py
```

或使用打包好的 `dist\dsh-launcher.exe`（windowed，无控制台窗口）。

## 打包

```bash
# 一键打包（推荐）：自动定位 PyInstaller，产物 dist\dsh-launcher.exe
build.bat

# 或手动执行
pyinstaller --noconfirm dsh-launcher.spec
```

spec 已基于当前 Python 环境自动推导 tcl/tk 路径（可移植），exe 带 1.1.0 版本信息，`assets/`（桌面宠物素材）会一并打包。

## 依赖环境

- Windows
- Python 3.x（含 tkinter）
- Node.js（默认 `C:\Program Files\nodejs`，也支持从 PATH 自动探测）
- Git
- deepseek-harness 源码目录（默认 `d:\agent-workspace\deepseek-harness`）
