"""驱动抽象层。

IDriver 定义上层（state/nav）需要的全部语义能力：窗口绑定、鼠标键盘、识别。
DmDriver 是基于大漠插件(dm.dll)的具体实现——当前唯一实现。

以后要做"主控副机"，只需再写一个 RemoteDriver，把同样的方法转发到副机执行，
state/nav 等上层代码不需要改一行，这正是这一层存在的意义。
"""
from __future__ import annotations

import ctypes
from abc import ABC, abstractmethod
from ctypes import wintypes
from typing import Optional

import win32com.client


class IDriver(ABC):
    """驱动抽象接口：上层只认这些方法，不关心背后是大漠还是别的实现。"""

    # ---------------- 生命周期 ----------------
    @abstractmethod
    def register(self) -> None:
        """初始化驱动：注册插件、加载字库、加载AI模型等一次性准备工作。"""

    @abstractmethod
    def find_windows(self) -> set[str]:
        """查找匹配配置的游戏窗口句柄集合。"""

    @abstractmethod
    def bind(self, hwnd: str) -> None:
        """绑定窗口，之后的所有操作都作用于这个窗口（后台操作，窗口可以被遮挡/最小化）。"""

    @abstractmethod
    def unbind(self) -> None:
        """解除窗口绑定。"""

    # ---------------- 鼠标 ----------------
    @abstractmethod
    def move_to(self, x: int, y: int) -> None:
        """移动鼠标到窗口内绝对坐标 (x, y)。"""

    @abstractmethod
    def move_r(self, rx: int, ry: int) -> None:
        """相对当前位置移动鼠标 (rx, ry)。"""

    @abstractmethod
    def click(self, button: str = "left") -> None:
        """点击鼠标按钮，button 为 'left' 或 'right'。"""

    @abstractmethod
    def mouse_down(self, button: str = "left") -> None: ...

    @abstractmethod
    def mouse_up(self, button: str = "left") -> None: ...

    @abstractmethod
    def wheel(self, direction: str, count: int = 1) -> None:
        """滚轮，direction 为 'up' 或 'down'。"""

    # ---------------- 键盘 ----------------
    @abstractmethod
    def key_down(self, key: str) -> None: ...

    @abstractmethod
    def key_up(self, key: str) -> None: ...

    @abstractmethod
    def key_press(self, keys, hold_ms: int = 80) -> None:
        """按下并松开一个键或一组组合键（如 ["alt", "q"]），hold_ms 控制按住时长。"""

    # ---------------- 识别 ----------------
    @abstractmethod
    def find_pic(self, x1, y1, x2, y2, pic_path, delta_color="202020", sim=0.9, direction=0):
        """在区域内找单张/多选一模板图，返回 (ret, x, y)，ret=-1 表示未找到。"""

    @abstractmethod
    def find_pic_ex(self, x1, y1, x2, y2, pic_path, delta_color="202020", sim=0.9, direction=0):
        """找所有匹配位置，返回 [(pic_id, x, y), ...]。"""

    @abstractmethod
    def get_pic_size(self, pic_path):
        """返回模板图的 (width, height)，用于换算中心点。"""

    @abstractmethod
    def ocr(self, x1, y1, x2, y2, color, sim=0.9, dict_index=0):
        """识别区域内文字，返回字符串。"""

    @abstractmethod
    def find_str(self, x1, y1, x2, y2, text, color, sim=0.9, dict_index=0):
        """在区域内查找指定文字，返回 (ret, x, y)。"""

    @abstractmethod
    def find_color(self, x1, y1, x2, y2, color, sim=0.9):
        """全图找色，返回 (ret, x, y)。"""

    @abstractmethod
    def yolo_detect(self, x1, y1, x2, y2, model_index=None, prob=0.5, iou=0.45):
        """YOLO 检测，返回 [(class_name, confidence, x, y, w, h), ...]。"""

    # ---------------- 杂项 ----------------
    @abstractmethod
    def delay(self, ms: int) -> None:
        """延时指定毫秒。"""

    @abstractmethod
    def capture(self, x1, y1, x2, y2, file_path, quality=90) -> bool:
        """截图保存到文件，用于异常现场存证。"""


class DmDriver(IDriver):
    """基于大漠插件(dm.dll)的驱动实现。

    config 结构（由 app.bootstrap 组装后传入，均为绝对路径）：
        {
            "auth": {"reg_code": str, "ver_info": str},
            "dll": {"dmreg_path": str, "dm_path": str},
            "dict_path": str,
            "dicts": [{"index": int, "name": str}, ...],
            "ai": {
                "module_path": str,
                "yolo": {"version": str, "models": [{"index": int, "path": str, "password": str}, ...]},
            },
            "game_window": {"process_name": str, "title": str, "class_name": str, "filter": int},
            "bind": {"display": str, "mouse": str, "keypad": str, "public": str, "mode": int},
        }
    """

    def __init__(self, config: dict, templates_dir: str):
        self._config = config
        self._templates_dir = templates_dir
        self._dm = None
        self._hwnd: Optional[str] = None

    # ---------------- 生命周期 ----------------
    def register(self) -> None:
        self._init_dll()
        self._dm = self._register_plugin()
        self._init_dicts()
        self._init_ai()

    def _init_dll(self) -> None:
        dll_cfg = self._config["dll"]
        dmreg = ctypes.WinDLL(dll_cfg["dmreg_path"])
        set_dll_path = dmreg.SetDllPathW
        set_dll_path.argtypes = [wintypes.LPCWSTR, wintypes.LONG]
        set_dll_path.restype = wintypes.LONG
        if set_dll_path(dll_cfg["dm_path"], 0) != 1:
            raise RuntimeError("大漠DLL路径设置失败")

    # 大漠插件 Reg() 返回码含义，方便直接定位问题而不用去查文档
    _REG_ERROR_MESSAGES = {
        -1: "无法连接网络",
        -2: "进程没有以管理员方式运行",
        0: "未知错误",
        2: "余额不足",
        3: "绑定了本机器，但是账户余额不足50元",
        4: "注册码错误",
        5: "机器或IP在黑名单列表中",
        6: "非法使用插件或系统语言不是中文简体",
        7: "帐号因为非法使用被封禁",
        8: "ver_info不在设置的附加白名单中",
        77: "机器码或IP因为非法使用被封禁",
        777: "同一个机器码注册次数超过限制",
    }

    def _register_plugin(self):
        dm = win32com.client.Dispatch("dm.dmsoft")
        auth = self._config["auth"]
        ret = dm.Reg(auth["reg_code"], auth["ver_info"])
        if ret != 1:
            msg = self._REG_ERROR_MESSAGES.get(ret, "未知错误")
            raise RuntimeError(f"大漠插件注册失败，错误码: {ret} ({msg})")
        return dm

    def _init_dicts(self) -> None:
        self._dm.EnableShareDict(1)
        self._dm.SetPath(self._config["dict_path"])
        for item in self._config.get("dicts", []):
            ret = self._dm.SetDict(item["index"], item["name"])
            if ret == 0:
                raise RuntimeError(f"字库加载失败: index={item['index']} name={item['name']}")

    def _init_ai(self) -> None:
        ai_cfg = self._config.get("ai")
        if not ai_cfg:
            return
        ret = self._dm.LoadAi(ai_cfg["module_path"])
        if ret != 1:
            raise RuntimeError(f"AI模块加载失败，错误码: {ret}")
        yolo_cfg = ai_cfg.get("yolo") or {}
        if not yolo_cfg:
            return
        self._dm.AiYoloSetVersion(yolo_cfg["version"])
        for model in yolo_cfg.get("models", []):
            ret = self._dm.AiYoloSetModel(model["index"], model["path"], model["password"])
            if ret != 1:
                raise RuntimeError(f"YOLO模型加载失败: {model['path']}")

    def find_windows(self) -> set[str]:
        w = self._config["game_window"]
        hwnd_str = self._dm.EnumWindowByProcess(
            w["process_name"], w["title"], w["class_name"], w["filter"]
        )
        if not hwnd_str:
            return set()
        return set(hwnd_str.split(","))

    def bind(self, hwnd: str) -> None:
        bind_cfg = self._config["bind"]
        ret = self._dm.BindWindowEx(
            hwnd,
            bind_cfg["display"],
            bind_cfg["mouse"],
            bind_cfg["keypad"],
            bind_cfg["public"],
            bind_cfg["mode"],
        )
        if ret != 1:
            raise RuntimeError(f"窗口绑定失败，句柄:{hwnd}，错误码:{self._dm.GetLastError()}")
        self._hwnd = hwnd
        # 绑定成功后的标准化设置
        self._dm.EnableShareDict(1)
        self._dm.EnableRealKeypad(1)
        self._dm.SetExactOcr(1)
        self._verify_window_size()

    def _verify_window_size(self) -> None:
        """核对绑定窗口的客户区分辨率。

        flags.yaml/fallback_graph.yaml/maps.yaml 里的所有像素坐标都是按固定分辨率
        标定的——不管这个窗口是本机多开的，还是远程机器上的（多机场景见
        架构设计方案.md 第10节），只要分辨率不一致，所有坐标就全错，而且是那种
        "看起来在动，但悄悄点错位置"的错，比直接报错更难排查。所以绑定后立刻校验，
        不一致就直接拒绝继续，而不是带着错误的假设往下跑。
        """
        w_cfg = self._config["game_window"]
        expected_w = w_cfg.get("expected_width")
        expected_h = w_cfg.get("expected_height")
        if not expected_w or not expected_h:
            return
        ret, width, height = self._dm.GetClientSize(self._hwnd)
        if ret != 1:
            raise RuntimeError(f"获取窗口客户区大小失败，句柄:{self._hwnd}")
        if (width, height) != (expected_w, expected_h):
            raise RuntimeError(
                f"窗口分辨率不匹配：期望 {expected_w}x{expected_h}，实际 {width}x{height}。"
                f"所有导航坐标都是按期望分辨率标定的，必须先把游戏窗口调成一致大小再绑定。"
            )

    def unbind(self) -> None:
        self._dm.UnBindWindow()
        self._hwnd = None

    # ---------------- 鼠标 ----------------
    def move_to(self, x: int, y: int) -> None:
        self._dm.MoveTo(x, y)

    def move_r(self, rx: int, ry: int) -> None:
        self._dm.MoveR(rx, ry)

    def click(self, button: str = "left") -> None:
        if button == "left":
            self._dm.LeftClick()
        else:
            self._dm.RightClick()

    def mouse_down(self, button: str = "left") -> None:
        if button == "left":
            self._dm.LeftDown()
        else:
            self._dm.RightDown()

    def mouse_up(self, button: str = "left") -> None:
        if button == "left":
            self._dm.LeftUp()
        else:
            self._dm.RightUp()

    def wheel(self, direction: str, count: int = 1) -> None:
        for _ in range(count):
            if direction == "up":
                self._dm.WheelUp()
            else:
                self._dm.WheelDown()

    # ---------------- 键盘 ----------------
    def key_down(self, key: str) -> None:
        self._dm.KeyDownChar(key.lower())

    def key_up(self, key: str) -> None:
        self._dm.KeyUpChar(key.lower())

    def key_press(self, keys, hold_ms: int = 80) -> None:
        if isinstance(keys, str):
            keys = [keys]
        for k in keys:
            self.key_down(k)
        self.delay(hold_ms)
        for k in reversed(keys):
            self.key_up(k)

    # ---------------- 识别 ----------------
    def find_pic(self, x1, y1, x2, y2, pic_path, delta_color="202020", sim=0.9, direction=0):
        self._dm.SetPath(self._templates_dir)
        return self._dm.FindPic(x1, y1, x2, y2, pic_path, delta_color, sim, direction)

    def find_pic_ex(self, x1, y1, x2, y2, pic_path, delta_color="202020", sim=0.9, direction=0):
        self._dm.SetPath(self._templates_dir)
        result = self._dm.FindPicEx(x1, y1, x2, y2, pic_path, delta_color, sim, direction)
        if not result:
            return []
        hits = []
        for item in result.split("|"):
            if item:
                pic_id, x, y = map(int, item.split(","))
                hits.append((pic_id, x, y))
        return hits

    def get_pic_size(self, pic_path):
        self._dm.SetPath(self._templates_dir)
        size = self._dm.GetPicSize(pic_path)
        w, h = map(int, size.split(","))
        return w, h

    def ocr(self, x1, y1, x2, y2, color, sim=0.9, dict_index=0):
        self._dm.UseDict(dict_index)
        return self._dm.Ocr(x1, y1, x2, y2, color, sim)

    def find_str(self, x1, y1, x2, y2, text, color, sim=0.9, dict_index=0):
        self._dm.UseDict(dict_index)
        return self._dm.FindStr(x1, y1, x2, y2, text, color, sim)

    def find_color(self, x1, y1, x2, y2, color, sim=0.9):
        return self._dm.FindColorEx(x1, y1, x2, y2, color, sim)

    def yolo_detect(self, x1, y1, x2, y2, model_index=None, prob=0.5, iou=0.45):
        if model_index is not None:
            self._dm.AiYoloUseModel(model_index)
        result = self._dm.AiYoloDetectObjects(x1, y1, x2, y2, prob, iou)
        if not result:
            return []
        objs = []
        for item in result.split("|"):
            if item:
                cls, conf, x, y, w, h = item.split(",")
                objs.append((cls, float(conf), int(x), int(y), int(w), int(h)))
        return objs

    # ---------------- 杂项 ----------------
    def delay(self, ms: int) -> None:
        self._dm.delay(ms)

    def capture(self, x1, y1, x2, y2, file_path, quality=90) -> bool:
        return self._dm.CaptureJpg(x1, y1, x2, y2, file_path, quality) == 1
