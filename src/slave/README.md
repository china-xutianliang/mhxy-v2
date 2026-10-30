# 梦幻西游 副机共享助手（独立项目）

> 第一层已定稿：本助手的行为属于 `第一层_远程画面监控_规范.md` 的副机契约（B1~B7），修改前先看该规范第 8 节。
> 2026-10-28 的"Failed to connect to TightVNC Server"已在副机上修复，**以副机上的修复版为准**，同步回本目录后请在下面补充根因与修法。


这个文件夹（主项目里的 `src/slave/`）**只在副机上用**，和主机项目的其他代码没有任何依赖，自带所需的 `common/`。可以整个拷到副机上单独开发、调试、打包。

## 它是干什么的
主机（A）通过 TightVNC 查看器远程看副机（B）。副机上的游戏窗口客户区是 **800x600**。
本助手（`game_share.py`，Tk 图形界面）做一件事：
1. 每秒找一次游戏窗口（类名 `WSGAME`，进程 `mhmain.exe`，或标题含“梦幻”且客户区 800x600），算出它在**副机桌面**上的位置；
2. 在**副机本机**调用 `tvnserver.exe -controlservice -sharerect 800x600+X+Y`（服务方式）或 `-controlapp`（程序方式），让 TightVNC 服务端**只共享这块矩形**；
3. 窗口挪动或每 30 秒复查一次，必要时重新下发；关闭窗口/点“停止共享”会恢复共享整桌面。

主机收到的画面就恰好是 800x600 的游戏，坐标就是游戏坐标，也快得多（整桌面 2560x1440 太慢）。

## 硬约束（不要改）
- 副机上**严禁**运行大漠(dm.dll)、注入/hook、虚拟驱动（账号安全）。本项目只用标准库 + ctypes + tkinter，不要引入第三方运行时依赖。
- 不保存、不打印任何 VNC 密码。管理密码文件 `C:\ProgramData\mhxy\tvn_pass.txt` 仅在用户明确设置时才写，且限制为管理员可读。
- 必须 **TightVNC 2.x（推荐 2.8.90）**。TightVNC 4.0 已删除 `-sharerect`，助手会检测并拒绝下发。
- 助手需要管理员身份（启动时自动 UAC 提权）。

## 目录
```
game_share.py        主程序（界面 + 找窗口 + 下发 + 诊断）
cursor_log.py        E3 只读指针记录器（只调用 GetCursorPos，输出 JSON）
common/winapi.py     Win32 薄封装（ctypes）
common/wincap.py     截图（窗口检查器页签用）
common/inspector.py  “窗口检查器”页签
tests/test_game_share.py  单元测试（不需要 TightVNC）
dist/game_share.exe  最近一次打包的 exe（Python 3.11 + PyInstaller）
build.bat            打包脚本
```
运行：`python game_share.py`（`--console` 无界面守护；`--once --dry-run` 只打印不下发；`--smoke` 界面自检）。
测试：`python -m unittest discover -s tests`
E3 指针记录：`python cursor_log.py --seconds 30`（只读，不移动鼠标；记录期间不要动副机鼠标）。

## 当前待解决问题（请优先处理）
**现象**：点“开始共享”后一直失败，界面红条“下发失败，稍后自动重试”，状态“失败（退出码 -1）”，副机屏幕上堆积 `TightVNC Control Interface` 弹窗 “Failed to connect to TightVNC Server.”。凌晨同样环境是成功的，早上起不行。

**已知事实**（从副机界面“诊断：TightVNC”读到）：
- 助手是管理员身份；选用的 tvnserver = `I:\TightVNC\tvnserver.exe`（TightVNC 2.8.x）。
- 服务 `tvnserver` 状态 RUNNING（pid 在会话 0，命令行 `-service`）；另有会话 1 的 `-desktopserver` 进程（正常）。
- 控制管道存在：`TightVNC_Service_Control`、`TVN_log_pipe_public_name`。
- 主机能连 5900，握手正常，安全类型为无认证(1)+Tight(16)，说明服务端本身工作，**VNC 无密码**。
- **副机没有设置 TightVNC 管理（控制）密码**，但助手之前每次都带 `-passfile C:\PROGRA~3\mhxy	vn_pass.txt`（界面显示“管理密码：已设置”，是旧文件）。
- 超时的控制命令会留下卡死的 `tvnserver.exe -controlservice -slave` 进程和弹窗，弹窗阻塞导致 `subprocess.run` 超时返回 -1。

**已做的修改（最新 exe 在 dist，尚未在副机验证）**：
- `apply_rect()`：先**不带** passfile 下发，失败再带；服务/程序两种模式都试；记住上次成功组合；超时后 `kill_stuck_control()` 清理卡死的控制进程（只杀命令行含 controlservice/controlapp 的，不碰 `-service`/`-desktopserver`）。
- 新增“诊断：TightVNC”按钮（`diagnose_tvn()`）：服务状态、tvnserver 进程、控制管道；下发失败时自动运行一次。

**如果新版仍失败，可以排查的方向**：
1. 在管理员 cmd 里手动跑：`I:\TightVNC\tvnserver.exe -controlservice -sharerect 800x600+X+Y`，看是否仍弹 “Failed to connect”，以及弹窗前是否有密码提示；
2. 服务端设置里 “Administration / 控制接口” 是否启用了需要密码的控制认证（`UseControlAuthentication`），注册表在 `HKLM\SOFTWARE\TightVNC\Server`；
3. 控制管道的 ACL / 服务是否以 SYSTEM 运行、会话隔离导致管理员进程连不上管道；
4. 不走命令行：直接读写注册表 `HKLM\SOFTWARE\TightVNC\Server` 的 `ShareRect`(?) 相关键并 `tvnserver -controlservice -reload`（需确认 2.8.x 的键名）；
5. 换用 `-controlapp`（程序方式）或把服务端改成以应用方式运行测试；
6. 复现时务必先关闭旧弹窗并清理 `-controlservice -slave` 残留进程。

## 开发注意
- PowerShell 5.1：含中文的字符串用 here-string 或写成 .py 文件；不要在非管理员终端直接 `--smoke` 打包后的 exe（会触发 UAC 并另起管理员进程）。
- 打包：`build.bat`（需要 `pip install pyinstaller`，使用 32 位或 64 位 Python 均可，副机是 64 位 Windows 11）。
