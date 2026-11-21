"""自带沙箱评测（§12）。

本包**只用标准库、要能在 Python 3.8 上跑**：worker（``syncoj-judge``）可以装在另一台
机器上、以专用账号运行，不需要服务端的那些第三方依赖。服务端也 import 这里的
:mod:`.testdata` 来校验上传的数据包 —— 两边对数据包的理解只有一份。

* :mod:`.testdata` —— 数据包（zip）的结构：测试点、分值、子任务、checker。
* :mod:`.sandbox` —— 运行不可信程序的后端：``isolate``（推荐）与 ``rlimit``（仅开发）。
* :mod:`.runner` —— 编译、逐点运行、比对、计分，产出 ``result.json`` 的内容。
* :mod:`.worker` —— 向服务端领任务、取文件、交结果的循环（命令行 ``syncoj-judge``）。
"""
