"""给**任何页面**用的运行时元信息。**不鉴权。**

为什么单独一个 `meta` 端点而不是塞进登录响应里：选手页与装机页都不登录，而它们
同样需要这些值（尤其是显示时区 —— 选手页上的倒计时按它走字）。挂在登录接口上等于
"没登录的人只能自己猜"，而这份数据的性质本来就是"全站共用的常量"。

只放**不含任何考场数据**的东西：这里能被任何能打开这个端口的人取到。
"""

from __future__ import annotations

from fastapi import APIRouter

from ..schemas import MetaOut
from ..timeutil import display_label, display_offset_minutes

__all__ = ["router"]

router = APIRouter(prefix="/api/v1", tags=["meta"])


@router.get("/meta", response_model=MetaOut)
def get_meta() -> MetaOut:
    """全站共用的运行时元信息。

    ``display_utc_offset_minutes`` 是**服务端与前端必须一致**的那个数：界面上
    所有时间字符串都按它渲染。让前端自己猜（例如拿浏览器时区）的后果是教师的
    笔记本不在东八区时，整页时间都差几个小时 —— 而页面上完全看不出来。
    """
    return MetaOut(
        display_utc_offset_minutes=display_offset_minutes(),
        display_timezone=display_label(),
    )
