# mhxy

梦幻西游多机自动化。主控机 A 用大漠（32 位）后台绑定 TightVNC 查看器的画面子窗口，控制副机 B 上的游戏；副机不运行任何大漠、注入或虚拟驱动程序。

设计文档：`架构设计方案.md`（总体）、`控制层方案.md`（控制层 v2、Spike 与实施计划）、**`第一层_远程画面监控_规范.md`（第一层已定稿冻结，后续开发的基线）**。

## 运行

- 必须使用 32 位 Python，测试台需要管理员权限（大漠注册）。
- 旧测试台 `src\bench\app.py` 已退役（816×616 边带方案，与第一层 v1.6 不兼容，连接/启动/绑定入口会拒绝执行），请用下面的后台鼠键测试工具。
- 后台鼠键测试工具：双击 `dist\kmtool\kmtool.exe`（32 位、请求管理员），或 `python src\kmtool\app.py`；使用说明见 `dist\kmtool\使用说明.txt`。查看器画面恒为 800×600 物理像素，输入/抓图尺度 1.0（第一层 v1.6）。
- 单元测试：`python -m unittest discover -s tests`
- 主机侧“远程控制管理台”（看所有设备的画面）：`python src\manager\app.py`，或双击 `dist\manager\mhxy_manager.exe`（v1.6）。界面依赖 `customtkinter` 和 `pillow`（`pip install customtkinter pillow`）。设备端 TightVNC 必须是 2.x（4.0 没有 `-sharerect`）。
- 主机侧“地图素材采集器”（只读录制地图、路径和备注）：双击 `dist\recorder\mhxy_recorder.exe`。它只抓标准 800×600 查看器画面，不发鼠标键盘；使用说明见 `dist\recorder\使用说明.md`，重新打包运行 `build\recorder\build.bat`。
- 副机侧（副机共享助手 `game_share`）是 `src/slave/` 下的独立文件夹（自带 common，与其他代码互不依赖，整个拷到副机使用/开发）。主机管理程序只通过 TightVNC 与它配合。

## 注意

- `resources/config/driver.yaml` 含大漠注册码，不要外传，且已被 gitignore。首次克隆后复制 `resources/config/driver.example.yaml` 为 `driver.yaml`，填入自己的 `reg_code` / `ver_info`。
- VNC 密码不写入任何文件，由程序运行时输入。
