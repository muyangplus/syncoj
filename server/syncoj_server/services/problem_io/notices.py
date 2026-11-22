"""导入导出的警告 / 错误代码与中文消息，集中在这里。

消息是**后果陈述**（「子任务依赖已丢弃，得分可能偏高」），不是解释；界面原样显示。
模板用 ``str.format`` 占位符，``notice(code, **params)`` 填参数。

格式模块需要新代码时，在自己的模块里 ``register({...})``，不必改这个文件。
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from .bundle import Notice

__all__ = ["MESSAGES", "ProblemIoError", "notice", "register"]

MESSAGES: Dict[str, str] = {
    # ---- 整包级错误（ProblemIoError） ----
    "bad_zip": "不是合法的 zip",
    "unknown_format": "无法识别的格式",
    "plain_testdata": "单题数据包：在题目「设置」中上传",
    "cdf_binary": "旧版二进制 .cdf，请在 LemonLime 中重新保存",
    "cdf_without_data": "缺少 data/ 目录，请把整个比赛目录打包为 zip",
    "unsafe_path": "不安全的路径：{path}",
    "too_many_members": "文件数超过 {limit} 个",
    "name_too_long": "文件名超过 255 字节：{path}",
    "archive_too_large": "解压后超过 {limit}",
    "zip_bomb": "{path} 压缩比异常",
    "member_too_large": "{path} 超过 {limit}",
    "member_corrupt": "{path} 的压缩数据已损坏",
    "too_many_problems": "题目超过 {limit} 道",
    "no_problems": "包里没有题目",
    "format_unimplemented": "暂不支持{action} {format}",
    "bad_manifest": "{path} 无法解析：{reason}",
    "version_unsupported": "{path} 的版本 {version} 不受支持",
    "yaml_unsupported": "{path} 第 {line} 行：不支持的 YAML 写法",
    "yaml_invalid": "{path} 第 {line} 行：{reason}",
    "xml_unsafe": "XML 含 DOCTYPE / ENTITY，已拒绝",
    "no_exportable": "没有可导出的题目",
    # ---- 单题错误（这道题只能跳过） ----
    "missing_file": "缺少文件 {path}",
    "missing_answer": "测试点 {name} 缺少答案",
    "no_cases": "没有测试点",
    "sha_mismatch": "{path} 的 sha256 与清单不符",
    "testdata_invalid": "数据包不对：{reason}",
    "ident_invalid": "题目标识 {ident} 不合法",
    "io_name_invalid": "文件名 {name} 不合法",
    "interactive_unsupported": "交互题无法导入",
    "communication_unsupported": "通信题无法导入",
    "answers_only_unsupported": "提交答案题无法导入",
    "problem_type_unsupported": "题目类型 {type} 无法导入",
    "lemon_spj_unsupported": "Lemon 旧式 spj 无法导入",
    "checker_missing_source": "checker {path} 没有源码",
    "checker_language": "checker 语言 {language} 无法导入",
    "checker_protocol_unsupported": "{checker} 协议的 checker 无法导入",
    "checker_unknown": "内置 checker {name} 无法导入",
    "grader_unsupported": "带 grader 的题无法导入",
    "qduoj_spj_unsupported": "两参数 spj（无标准答案）无法导入",
    "too_many_cases": "测试点 {count} 个，超过 {limit} 个",
    # ---- 警告 ----
    "ident_renamed": "标识「{source}」改为 {ident}",
    "limit_clamped": "时限/内存超出范围，已改为 {time} ms / {memory} MB",
    "score_truncated": "小数分值已向下取整",
    "scores_missing": "未给分值，按 100 分平分",
    "flags_dropped": "编译参数 {flags} 已丢弃",
    "stack_limit_dropped": "栈限制 {mb} MB 已丢弃",
    "judge_mode_mismatch": "两处评测方式不一致，按 {mode} 导入",
    "checker_not_testlib": "checker 未使用 testlib，退出码 0 以外均判 WA",
    "extra_files_dropped": "附加文件 {files} 已丢弃",
    "acm_as_bundle": "ACM 题改为整体捆绑，任一测试点错误得 0 分",
    "subtask_scores_uneven": "子任务 {group} 内分值不一致，取最小值 {score}",
    "subtask_average_split": "平均分子任务已拆成独立测试点",
    "ergodic_as_default": "「遇错即停」已改为逐点计分，非满分时得分可能偏高",
    "languages_mapped": "编译参数取 {flags}",
    "statement_dropped": "题面、标签等未导入",
    "dependency_dropped": "子任务依赖已丢弃，得分可能偏高",
    "limits_collapsed": "各测试点时限/内存不同，已统一为 {time} ms / {memory} MB",
    "limits_defaulted": "时限/内存未知，已设为 1000 ms / 256 MB",
    "mixed_io": "输入与输出方式不一致，按文件 IO 导入",
    "strict_compare_relaxed": "原为严格逐行比较，现忽略行末空白",
    "diff_compare_relaxed": "原为 diff 比较，现忽略行末空白",
    "real_checker_generated": "已生成实数比较 checker（误差 {eps}）",
    "builtin_checker_approx": "内置 checker {name} 改用默认比较",
    "max_as_bundle": "max 子任务按捆绑计分，得分可能偏低",
    "mul_as_bundle": "mul 子任务按捆绑计分，得分可能偏低",
    "wrapped_hustoj_spj": "HUSTOJ spj 已套外壳，参数顺序已对调",
    "tpj_order_uncertain": "tpj 按 testlib 参数顺序导入，需人工核对",
    "testlib_injected": "已补入 testlib.h",
    "testlib_missing": "缺少 testlib.h，checker 编译会失败",
    "problem_yaml_ignored": "problem.yaml 无法解析，标题与 pid 未导入",
    "file_patterns_dropped": "代码路径模式不合法，已改用默认模式",
    # ---- 导出警告 ----
    "no_testdata": "无数据包，已跳过",
    "checker_needs_build": "checker 需在 LemonLime 机器上编译为 {path}",
    "checker_extra_headers": "{files} 未导出",
    "io_names_lost": "文件名 {input} / {output} 未导出，按 {ident}.in / {ident}.out",
    "pid_omitted": "pid {ident} 未导出",
    "hydro_total_not_100": "满分 {total}，Hydro 不换算成 100",
    "hydro_zero_score_filled": "{count} 组 0 分测试点在 Hydro 中共得 {score} 分，满分变为 100",
    "hydro_case_limit": "测试点 {count} 个，Hydro 默认评测机会拒绝",
    "hydro_total_time": "测试点时限合计 {seconds} s，超过 60 s，Hydro 默认评测机会拒绝",
}


class _Missing(dict):
    def __missing__(self, key: str) -> str:
        return "{%s}" % key


def notice(code: str, message: Optional[str] = None, **params: object) -> Notice:
    """按代码构造一条提示。``message`` 给了就直接用（仍要求代码已登记）。"""
    if code not in MESSAGES:
        raise KeyError("未登记的提示代码：%s" % code)
    text = message if message is not None else MESSAGES[code].format_map(_Missing(params))
    return Notice(code=code, message=text)


def register(messages: Mapping[str, str]) -> None:
    """格式模块登记自己的新代码。与已有代码同名但消息不同 → 报错（防止两个模块抢同一个码）。"""
    for code, text in messages.items():
        existing = MESSAGES.get(code)
        if existing is not None and existing != text:
            raise ValueError("提示代码 %s 已登记为另一条消息" % code)
        MESSAGES[code] = text


class ProblemIoError(Exception):
    """整包级失败：无法识别、二进制 .cdf、不安全路径、zip 炸弹、格式未实现……

    接口层把它翻成 400 ``validation_error``（``details.code_detail`` = 代码）；
    ``too_many_problems`` 翻成 413。
    """

    def __init__(self, code: str, **params: object) -> None:
        self.notice = notice(code, **params)
        super().__init__(self.notice.message)

    @property
    def code(self) -> str:
        return self.notice.code
