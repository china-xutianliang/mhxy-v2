"""全局中断器：验证码/战斗/断线等横切异常的统一拦截点。

nav 层每执行一跳都会调用一次 guard()——以后要接具体的识别/处理逻辑，
只需要改这一个文件，state/nav 等上层代码不用动。

当前阶段（骨架搭建）先留空实现，不阻塞导航层本身的开发和测试。
"""
from __future__ import annotations

from core.driver import IDriver


class Interrupt:
    def guard(self, drv: IDriver) -> None:
        """检查并处理验证码/战斗/断线等打断情况。

        TODO:
          - 验证码：识图命中验证码模板 -> 自动识别/人工介入 -> 处理完成后返回
          - 战斗：识别到战斗UI -> 挂起导航，等待战斗结束（或按配置自动应战）
          - 断线：识别到登录界面 -> 重新登录、重新绑定窗口
        """
        return
