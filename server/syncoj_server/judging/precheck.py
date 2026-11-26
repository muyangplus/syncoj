"""编译前的选手源码文本预检（§12）：违规写法直接拦截，疑似混淆给预警。

只用标准库、兼容 Python 3.8（评测机 ``syncoj-judge`` 也用）。

**先规范化再匹配**，按 C 翻译阶段：

1. 换行统一为 ``\\n``；三字符序列（``??=`` → ``#`` 等）替换；
2. 反斜杠续行拼接（反斜杠与换行之间允许空白，同 GCC），行号仍记原始行；
3. 去注释（块注释换成一个空格，可跨行并进指令），跳过字符串、字符字面量与 C++ 原始字符串
   ``R"x(...)x"``；原始字符串内部按标准撤销 1、2 两步，这里也直接从原文取；
4. 二合字母（``%:`` → ``#``、``%:%:`` → ``##``、``<: :> <% %>``）替换；代码里的 ``\\uXXXX`` 解码。

字面量内容在规范化文本里换成占位符（``"\\x01"``），另存一份供文件名、编码串等规则单独检查 ——
字符串、注释里出现 ``system(`` 不会误报。

规则 code 稳定，可在题目配置里逐条关闭（:class:`PrecheckConfig`）。
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = [
    "BLOCK_RULES",
    "WARN_RULES",
    "MODES",
    "Finding",
    "PrecheckConfig",
    "PrecheckReport",
    "check_source",
    "RISK_EVENT_CODES",
    "LEVEL_RISK",
    "LEVEL_WARNING",
    "level_of",
    "count_flags",
    "count_flags_json",
]

#: 拦截项：code → 说明（文档的规则表与这里一致）
BLOCK_RULES: Dict[str, str] = {
    "pragma": "#pragma / _Pragma / __pragma",
    "attribute_optimize": "__attribute__ / [[gnu::]] 里的 optimize、target",
    "inline_asm": "内联汇编 asm / __asm__ / __asm",
    "syscall": "执行命令、建进程、网络、动态加载等系统调用",
    "forbidden_header": "禁用头文件；#include 绝对路径、上级目录、系统目录、宏",
    "file_access": "打开题目输入输出以外的文件或路径",
    "bidi": "Unicode 双向控制字符（Trojan Source）",
}
#: 预警项
WARN_RULES: Dict[str, str] = {
    "macro_heavy": "宏定义过多，或宏重定义关键字 / 常用标准名",
    "token_paste": "宏里用 ## 拼接记号",
    "trigraph_digraph": "使用三字符序列或二合字母",
    "line_splice": "续行拆开标识符",
    "long_line": "单行极长或平均行长异常",
    "big_table": "大型字面量表，疑似打表",
    "encoded_blob": "长 base64 / 十六进制串",
    "non_ascii_ident": "标识符含非 ASCII 字符",
    "huge_source": "源码过大",
    "file_dynamic": "打开文件的文件名不是字面量",
}
MODES = ("off", "warn", "block")

#: 评测时运行选手程序后发现的风险事件（``runner`` 写进结果的 ``risk_events``）：code → 说明
RISK_EVENT_CODES: Dict[str, str] = {
    "workdir_symlink": "工作目录里出现符号链接、FIFO、设备文件或硬链接",
    "tamper_checker": "工作目录里出现 checker 同名文件，或程序自身被改写",
    "escaped_process": "运行结束后仍有逃出进程组的进程（已清掉）",
    "precheck_blocked": "源码预检拦截",
}

#: 分级（界面上的颜色只按这里给的 ``level``，前端不维护 code 表）：
#: ``risk`` = 风险事件全部 + 拦截类规则（题目配置成 ``warn`` 时它们出现在预警里，级别不降）；
#: ``warning`` = 疑似违规（预警类规则）。不认识的 code 按 ``warning``。
LEVEL_RISK, LEVEL_WARNING = "risk", "warning"


def level_of(code: str) -> str:
    """一条预检结果 / 风险事件的级别。"""
    if code in RISK_EVENT_CODES or code in BLOCK_RULES:
        return LEVEL_RISK
    return LEVEL_WARNING


def count_flags(document: object) -> Tuple[int, int]:
    """结果文档（``result.json``）里的 ``(疑似违规条数, 风险条数)``。

    风险 = ``risk_events`` 全部 + ``precheck_warnings`` 里拦截类 code；疑似违规 = 其余预警。
    形状不对的项跳过；不是自带评测的结果（没有这两个键）为 ``(0, 0)``。
    """
    if not isinstance(document, dict):
        return 0, 0
    warnings = risks = 0
    for item in document.get("precheck_warnings") or []:
        if isinstance(item, dict) and item.get("code"):
            if level_of(str(item["code"])) == LEVEL_RISK:
                risks += 1
            else:
                warnings += 1
    for item in document.get("risk_events") or []:
        if isinstance(item, dict) and item.get("code"):
            risks += 1
    return warnings, risks


def count_flags_json(raw: Optional[str]) -> Tuple[int, int]:
    """同 :func:`count_flags`，入参是结果文档的 JSON 原文；解析不了为 ``(0, 0)``。"""
    if not raw or ('"precheck_warnings"' not in raw and '"risk_events"' not in raw):
        return 0, 0
    import json

    try:
        return count_flags(json.loads(raw))
    except ValueError:
        return 0, 0

#: 每条规则最多报几处（拦截信息要能读完）
MAX_FINDINGS_PER_CODE = 20
EXCERPT_LIMIT = 80
#: 平均行长只在非空行不少于这么多时才看（几行的小程序不算）
AVG_LINE_MIN_LINES = 10


@dataclass
class Finding:
    code: str
    line: int
    message: str
    excerpt: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {"code": self.code, "line": self.line, "message": self.message, "excerpt": self.excerpt}


@dataclass
class PrecheckConfig:
    """``mode``：``off`` 不查；``warn`` 拦截项降为预警；``block``（默认）拦截项拦截。"""

    mode: str = "block"
    disabled_codes: FrozenSet[str] = frozenset()
    max_defines: int = 30
    max_line_length: int = 500
    max_avg_line_length: int = 200
    max_literal_bytes: int = 64 * 1024
    max_initializer_items: int = 5000
    min_blob_length: int = 200
    max_source_bytes: int = 256 * 1024

    @classmethod
    def from_json(cls, raw: object) -> "PrecheckConfig":
        """题目配置里的 ``precheck: {mode, disabled}``；不对抛 :class:`ValueError`（信息给教师看）。"""
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise ValueError("precheck 必须是对象")
        unknown = sorted(set(raw) - {"mode", "disabled"})
        if unknown:
            raise ValueError("precheck 里有不认识的字段：%s" % ", ".join(unknown))
        mode = raw.get("mode", "block")
        if mode not in MODES:
            raise ValueError("precheck.mode 只能是 off / warn / block")
        disabled = raw.get("disabled", [])
        if not isinstance(disabled, list) or not all(isinstance(code, str) for code in disabled):
            raise ValueError("precheck.disabled 必须是规则 code 的数组")
        bad = sorted(set(disabled) - set(BLOCK_RULES) - set(WARN_RULES))
        if bad:
            raise ValueError("precheck.disabled 里有不存在的规则：%s" % ", ".join(bad))
        return cls(mode=str(mode), disabled_codes=frozenset(disabled))


@dataclass
class PrecheckReport:
    blocked: List[Finding] = field(default_factory=list)
    warnings: List[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocked

    def blocked_message(self) -> str:
        """记进编译信息的文字：每个拦截项一行，下一行缩进给出原文片段。"""
        lines = []
        for item in self.blocked:
            lines.append("源码检查未通过：%s（第 %d 行）" % (item.message, item.line))
            if item.excerpt:
                lines.append("    " + item.excerpt)
        return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #

TRIGRAPHS = {"=": "#", "/": "\\", "'": "^", "(": "[", ")": "]", "!": "|", "<": "{", ">": "}", "-": "~"}
_SPLICE_TAIL = re.compile(r"[ \t\f\v]*\n")
_RAW_DELIM = re.compile(r"([^\s()\\]{0,16})\(")
RAW_PREFIXES = ("R", "LR", "uR", "UR", "u8R")
FILLER = "\x01"
_IDENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_PPNUM_CHARS = _IDENT_CHARS | frozenset("'.")
BIDI_CHARS = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)]
    + ["‎", "‏", "؜"]
)


@dataclass
class _Literal:
    kind: str  # string / char
    value: str
    line: int
    pos: int  # 在规范化代码里开引号的位置


class _Source:
    """一份源码规范化后的样子。``code[i]`` 来自原文第 ``lines[i]`` 行。"""

    def __init__(self, text: str, language: str) -> None:
        self.text = text
        self.language = language
        self.src_lines = text.split("\n")
        self.trigraph_lines: List[int] = []
        self.digraph_lines: List[int] = []
        self.splice_ident_lines: List[int] = []
        self.non_ascii_lines: List[int] = []
        self.literals: List[_Literal] = []
        self.code = ""
        self.lines: List[int] = []
        self._lex(*self._logical())
        self.literal_at = {lit.pos: lit for lit in self.literals}

    # 第 1、2 阶段：三字符序列 + 续行
    def _logical(self):
        src = self.text
        chars: List[str] = []
        starts: List[int] = []
        lines: List[int] = []
        spliced: Set[int] = set()
        i, n, line, pending = 0, len(src), 1, False
        while i < n:
            c = src[i]
            j = i + 1
            tri = False
            if c == "?" and i + 2 < n and src[i + 1] == "?" and src[i + 2] in TRIGRAPHS:
                c, j, tri = TRIGRAPHS[src[i + 2]], i + 3, True
            if tri:
                self.trigraph_lines.append(line)
            if c == "\\":
                match = _SPLICE_TAIL.match(src, j)
                if match:
                    line += 1
                    i = match.end()
                    pending = True
                    continue
            if pending:
                spliced.add(len(chars))
                pending = False
            chars.append(c)
            starts.append(i)
            lines.append(line)
            if c == "\n":
                line += 1
            i = j
        return chars, starts, lines, spliced

    # 第 3、4 阶段：注释、字面量、二合字母
    def _lex(self, chars: List[str], starts: List[int], lines: List[int], spliced: Set[int]) -> None:
        out: List[str] = []
        out_lines: List[int] = []
        src = self.text
        n = len(chars)
        cpp = self.language != "c"

        def emit(text: str, line: int) -> None:
            for ch in text:
                out.append(ch)
                out_lines.append(line)

        def trailing_run(allowed) -> str:
            k = len(out)
            while k > 0 and out[k - 1] in allowed:
                k -= 1
            return "".join(out[k:])

        k = 0
        while k < n:
            c = chars[k]
            nxt = chars[k + 1] if k + 1 < n else ""
            line = lines[k]
            if k in spliced and k > 0 and c in _IDENT_CHARS and chars[k - 1] in _IDENT_CHARS:
                self.splice_ident_lines.append(lines[k - 1])
            if c == "/" and nxt == "/":
                while k < n and chars[k] != "\n":
                    k += 1
                emit(" ", line)
                continue
            if c == "/" and nxt == "*":
                k += 2
                while k + 1 < n and not (chars[k] == "*" and chars[k + 1] == "/"):
                    k += 1
                k = k + 2 if k + 1 < n else n
                emit(" ", line)
                continue
            if c == "'":
                run = trailing_run(_PPNUM_CHARS)
                if run[:1].isdigit() or (run[:1] == "." and run[1:2].isdigit()):
                    emit(c, line)  # 数字分隔符 1'000'000
                    k += 1
                    continue
            if c in "\"'":
                prefix = trailing_run(_IDENT_CHARS)
                if c == '"' and cpp and prefix in RAW_PREFIXES:
                    match = _RAW_DELIM.match(src, starts[k] + 1)
                    if match:
                        close = ")" + match.group(1) + '"'
                        end = src.find(close, match.end())
                        stop = len(src) if end < 0 else end + len(close)
                        value = src[match.end():] if end < 0 else src[match.end():end]
                        self.literals.append(_Literal("string", value, line, len(out)))
                        emit('"' + FILLER + '"', line)
                        k = bisect.bisect_left(starts, stop)
                        continue
                k += 1
                buf: List[str] = []
                while k < n:
                    ch = chars[k]
                    if ch == "\\" and k + 1 < n and chars[k + 1] != "\n":
                        buf.append(ch)
                        buf.append(chars[k + 1])
                        k += 2
                        continue
                    if ch == c:
                        k += 1
                        break
                    if ch == "\n":
                        break  # 未闭合：到行尾为止
                    buf.append(ch)
                    k += 1
                kind = "string" if c == '"' else "char"
                self.literals.append(_Literal(kind, "".join(buf), line, len(out)))
                emit(c + FILLER + c, line)
                continue
            if c == "%" and nxt == ":":
                self.digraph_lines.append(line)
                if k + 3 < n and chars[k + 2] == "%" and chars[k + 3] == ":":
                    emit("##", line)
                    k += 4
                else:
                    emit("#", line)
                    k += 2
                continue
            if c == "<" and nxt == ":":
                third = chars[k + 2] if k + 2 < n else ""
                fourth = chars[k + 3] if k + 3 < n else ""
                if not (cpp and third == ":" and fourth not in (":", ">")):
                    self.digraph_lines.append(line)
                    emit("[", line)
                    k += 2
                    continue
            digraph = {("<", "%"): "{", ("%", ">"): "}", (":", ">"): "]"}.get((c, nxt))
            if digraph:
                self.digraph_lines.append(line)
                emit(digraph, line)
                k += 2
                continue
            if c == "\\" and nxt in ("u", "U"):
                width = 4 if nxt == "u" else 8
                digits = "".join(chars[k + 2:k + 2 + width])
                if len(digits) == width and all(d in "0123456789abcdefABCDEF" for d in digits):
                    try:
                        decoded = chr(int(digits, 16))
                    except (ValueError, OverflowError):
                        decoded = "?"
                    if ord(decoded) > 127:
                        self.non_ascii_lines.append(line)
                    emit(decoded, line)
                    k += 2 + width
                    continue
            if ord(c) > 127 and ("a" + c).isidentifier():
                self.non_ascii_lines.append(line)
            emit(c, line)
            k += 1
        self.code = "".join(out)
        self.lines = out_lines

    def line_at(self, pos: int) -> int:
        if not self.lines:
            return 1
        return self.lines[min(max(pos, 0), len(self.lines) - 1)]

    def excerpt(self, line: int) -> str:
        if not 1 <= line <= len(self.src_lines):
            return ""
        text = "".join(
            "\\u%04x" % ord(ch) if ch in BIDI_CHARS or (ord(ch) < 32 and ch != "\t") else ch
            for ch in self.src_lines[line - 1].strip()
        )
        return text if len(text) <= EXCERPT_LIMIT else text[:EXCERPT_LIMIT - 1] + "…"


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gb18030", "replace")
    return text.replace("\r\n", "\n").replace("\r", "\n")


# --------------------------------------------------------------------------- #
# 规则
# --------------------------------------------------------------------------- #

FORBIDDEN_HEADERS = frozenset([
    "windows.h", "winsock.h", "winsock2.h", "ws2tcpip.h", "shellapi.h", "tlhelp32.h", "process.h",
    "sys/socket.h", "sys/un.h", "netdb.h", "sys/ptrace.h", "sys/syscall.h", "dlfcn.h", "sys/mman.h",
    "sys/shm.h", "sys/ipc.h", "spawn.h", "sys/wait.h", "sys/prctl.h",
])
FORBIDDEN_HEADER_DIRS = ("netinet/", "arpa/", "linux/", "net/")
SYSTEM_DIRS = ("/dev/", "/proc/", "/etc/", "/sys/")

#: 无歧义的系统调用名：作为标识符出现即拦（成员访问除外）
SYSCALL_NAMES = frozenset([
    "system", "_wsystem", "popen", "_popen", "fork", "vfork", "execl", "execlp", "execle", "execv",
    "execvp", "execvpe", "execve", "fexecve", "posix_spawn", "posix_spawnp", "ptrace", "syscall",
    "dlopen", "WinExec", "PROT_EXEC",
    "CreateProcess", "CreateProcessA", "CreateProcessW",
    "ShellExecute", "ShellExecuteA", "ShellExecuteW", "ShellExecuteEx", "ShellExecuteExA", "ShellExecuteExW",
])
#: 选手常拿来当自己的函数名（并查集 / LCT 的 connect、bind……）：只拦调用，且源码里没定义同名函数时才拦
AMBIGUOUS_SYSCALLS = ("kill", "socket", "connect", "bind")

FILE_FUNCS = ("fopen", "freopen", "fopen64", "freopen64", "_wfopen", "_wfreopen", "open", "open64", "creat")
ALLOWED_DEVICES = frozenset(["/dev/stdin", "/dev/stdout", "/dev/stderr"])

KEYWORDS = frozenset("""
if else for while do return switch case default break continue goto struct class union enum typedef
template typename namespace using public private protected sizeof new delete true false this operator
static extern register volatile inline const virtual friend throw try catch
main std cin cout cerr printf scanf puts gets getchar putchar exit abort NULL nullptr freopen fopen
system _Pragma __attribute__ asm define include
""".split())

#: ``##`` 可能拼出的危险标识符 → 规则 code
PASTE_TARGETS = dict(
    [(name, "pragma") for name in ("_Pragma", "__pragma")]
    + [(name, "inline_asm") for name in ("__asm__", "__asm")]
    + [(name, "attribute_optimize") for name in ("__attribute__", "__attribute")]
    + [(name, "syscall") for name in sorted(SYSCALL_NAMES)]
)

_DIRECTIVE = re.compile(r"[ \t\f\v]*#[ \t\f\v]*([A-Za-z_]\w*)?(.*)", re.S)
_RAW_INCLUDE = re.compile(r"^[ \t]*(?:#|%:|\?\?=)[ \t]*(include_next|include|import)[ \t]*([<\"])([^>\"\n]*)")
_IDENT = re.compile(r"[A-Za-z_]\w*")
_NUMBER = re.compile(r"(?<![\w.'])\.?\d[\w.']*")
_BLOB = re.compile(r"[A-Za-z0-9+/=_-]+")
_HEX_ESCAPES = re.compile(r"(?:\\x[0-9A-Fa-f]{1,2})+")
_ATTRIBUTE_OPT = re.compile(r"\b(?:__)?(?:optimize|target|target_clones)(?:__)?\b")
_GNU_ATTR_OPT = re.compile(r"\b(?:gnu|__gnu__)\s*::\s*(?:__)?(?:optimize|target|target_clones)(?:__)?\b")
_DEFINITION_TAIL = re.compile(r"\s*(?:(?:const|noexcept|override|final|&&|&)\s*)*(?:->\s*[\w:<>,\s*&]+?)?\s*\{")


class _Collector:
    def __init__(self, source: _Source) -> None:
        self.source = source
        self.blocked: List[Finding] = []
        self.warnings: List[Finding] = []
        self._seen: Set[Tuple[str, int, str]] = set()
        self._count: Dict[str, int] = {}

    def add(self, block: bool, code: str, line: int, message: str) -> None:
        key = (code, line, message)
        if key in self._seen or self._count.get(code, 0) >= MAX_FINDINGS_PER_CODE:
            return
        self._seen.add(key)
        self._count[code] = self._count.get(code, 0) + 1
        finding = Finding(code, line, message, self.source.excerpt(line))
        (self.blocked if block else self.warnings).append(finding)


def _match_paren(code: str, index: int) -> int:
    """``code[index]`` 是左括号，返回与之配对的右括号下标；没有返回 -1。"""
    depth = 0
    for pos in range(index, len(code)):
        ch = code[pos]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return pos
    return -1


def _is_member(code: str, pos: int) -> bool:
    before = code[max(0, pos - 16):pos].rstrip()
    return before.endswith(".") or before.endswith("->")


def _qualified(code: str, pos: int) -> Optional[str]:
    """``pos`` 前是 ``::`` 时返回限定的前缀（全局限定 ``::x`` 返回空串）；否则 None。"""
    before = code[max(0, pos - 64):pos].rstrip()
    if not before.endswith("::"):
        return None
    match = re.search(r"(\w*)\s*$", before[:-2])
    return match.group(1) if match else ""


def _user_defines(code: str, name: str) -> bool:
    """源码里有没有自己定义 ``name``：带函数体的函数定义，或赋值 / 初始化（lambda、函数对象）。

    只写声明不算 —— 与库函数原型一致的声明会直接链接到库函数。
    """
    if re.search(r"\b%s\s*=(?!=)" % re.escape(name), code):
        return True
    for match in re.finditer(r"\b%s\s*\(" % re.escape(name), code):
        if _is_member(code, match.start()):
            continue
        end = _match_paren(code, match.end() - 1)
        if end >= 0 and _DEFINITION_TAIL.match(code, end + 1):
            return True
    return False


def _first_argument(code: str, open_pos: int, source: _Source):
    """调用的第一个实参：返回 ``("literal", 值)`` / ``("null", None)`` / ``("dynamic", None)`` / ``("none", None)``。"""
    rest = code[open_pos + 1:open_pos + 512]
    stripped = rest.lstrip()
    offset = open_pos + 1 + (len(rest) - len(stripped))
    if not stripped or stripped[0] in ")}":
        return "none", None
    match = re.match(r"(?:u8|u|U|L)?\"" + FILLER + "\"", stripped)
    if match:
        literal = source.literal_at.get(offset + match.end() - 3)
        tail = stripped[match.end():].lstrip()
        if literal is not None and tail[:1] in (",", ")", "}"):
            return "literal", literal.value
        return "dynamic", None
    if re.match(r"(?:NULL|nullptr|0)\s*[,)]", stripped):
        return "null", None
    return "dynamic", None


def _check_path(value: str, allowed: FrozenSet[str]) -> Optional[str]:
    """文件名字面量不合规时返回说明。"""
    if value in ALLOWED_DEVICES:
        return None
    if "/" in value or "\\" in value or ".." in value or re.match(r"[A-Za-z]:", value):
        return "不能用路径打开文件 \"%s\"" % value[:40]
    if value not in allowed:
        names = " / ".join(sorted(allowed)) or "无"
        return "只能打开 %s，不能打开 \"%s\"" % (names, value[:40])
    return None


def _include_problem(target: str) -> Optional[str]:
    lowered = target.strip().replace("\\", "/").lower()
    if lowered.startswith("/") or re.match(r"[a-z]:", lowered):
        return "#include 不能用绝对路径"
    if ".." in lowered.split("/"):
        return "#include 不能跳到上级目录"
    if any(part in lowered for part in SYSTEM_DIRS):
        return "#include 不能读系统目录"
    if lowered in FORBIDDEN_HEADERS or lowered.startswith(FORBIDDEN_HEADER_DIRS):
        return "禁止包含 <%s>" % target.strip()
    return None


def _paste_decomposable(word: str, pieces: Set[str]) -> bool:
    """``word`` 能否切成 ≥2 段、每段都是源码里出现过的记号（长度 ≥2）。"""
    reach = [0] * (len(word) + 1)  # 到达位置 i 时最少/是否用过的段数
    reach[0] = 1
    for i in range(len(word)):
        if not reach[i]:
            continue
        for j in range(i + 2, len(word) + 1):
            if word[i:j] in pieces:
                reach[j] = max(reach[j], reach[i] + 1)
    return reach[len(word)] >= 3


def _check_directives(source: _Source, out: _Collector, config: PrecheckConfig) -> None:
    code = source.code
    defines = 0
    first_define_line = 0
    offset = 0
    for segment in code.split("\n"):
        match = _DIRECTIVE.match(segment)
        if match:
            line = source.line_at(offset + segment.index("#"))
            name = match.group(1) or ""
            body = match.group(2)
            if name == "pragma":
                out.add(True, "pragma", line, "禁止使用 #pragma")
            elif name in ("include", "include_next", "import"):
                operand = body.strip()
                target = None
                if operand.startswith("<") and ">" in operand:
                    target = operand[1:operand.index(">")]
                elif operand.startswith('"'):
                    pos = offset + match.start(2) + body.index('"')
                    literal = source.literal_at.get(pos)
                    target = literal.value if literal is not None else ""
                if target is None:
                    out.add(True, "forbidden_header", line, "#include 不能用宏")
                else:
                    problem = _include_problem(target)
                    if problem:
                        out.add(True, "forbidden_header", line, problem)
            elif name == "define":
                defines += 1
                first_define_line = first_define_line or line
                macro = _IDENT.match(body.strip())
                if macro and macro.group(0) in KEYWORDS:
                    out.add(False, "macro_heavy", line, "宏重定义 %s" % macro.group(0))
                if "##" in body:
                    out.add(False, "token_paste", line, "宏里用 ## 拼接记号")
        offset += len(segment) + 1
    if defines > config.max_defines:
        out.add(False, "macro_heavy", first_define_line, "宏定义 %d 个" % defines)
    # 规范化之前的原文指令上也查一遍 include 路径（防 <> 里的 // 被当成注释之类）
    for number, text in enumerate(source.src_lines, 1):
        match = _RAW_INCLUDE.match(text)
        if match:
            problem = _include_problem(match.group(3))
            if problem:
                out.add(True, "forbidden_header", number, problem)


def _check_code(source: _Source, out: _Collector, allowed: FrozenSet[str]) -> None:
    code = source.code
    for match in re.finditer(r"\b(?:_Pragma|__pragma)\b", code):
        out.add(True, "pragma", source.line_at(match.start()), "禁止使用 %s" % match.group(0))
    for match in re.finditer(r"\b__attribute(?:__)?\s*\(", code):
        end = _match_paren(code, match.end() - 1)
        inner = code[match.end():end if end >= 0 else len(code)]
        if _ATTRIBUTE_OPT.search(inner):
            out.add(True, "attribute_optimize", source.line_at(match.start()), "禁止用 __attribute__ 指定优化或目标指令集")
    for match in re.finditer(r"\[\s*\[", code):
        end = code.find("]", match.end())
        if _GNU_ATTR_OPT.search(code[match.end():end if end >= 0 else len(code)]):
            out.add(True, "attribute_optimize", source.line_at(match.start()), "禁止用 [[gnu::]] 指定优化或目标指令集")
    for match in re.finditer(r"\b(?:asm|__asm__|__asm)\b", code):
        out.add(True, "inline_asm", source.line_at(match.start()), "禁止内联汇编 %s" % match.group(0))

    identifiers = set()
    for match in _IDENT.finditer(code):
        word = match.group(0)
        identifiers.add(word)
        if word in SYSCALL_NAMES and not _is_member(code, match.start()):
            out.add(True, "syscall", source.line_at(match.start()), "禁止调用 %s" % word)
    for name in AMBIGUOUS_SYSCALLS:
        if name not in identifiers:
            continue
        user_owned = _user_defines(code, name)
        for match in re.finditer(r"\b%s\s*\(" % name, code):
            if _is_member(code, match.start()):
                continue
            qualifier = _qualified(code, match.start())
            if qualifier:  # std::bind 之类
                continue
            if user_owned and qualifier is None:
                continue
            out.add(True, "syscall", source.line_at(match.start()), "禁止调用 %s" % name)
    if "##" in code:
        pieces = {word for word in identifiers if len(word) >= 2}
        for word, rule in PASTE_TARGETS.items():
            if word not in identifiers and _paste_decomposable(word, pieces):
                out.add(True, rule, source.line_at(code.index("##")), "疑似用 ## 拼出 %s" % word)

    # 打开文件：字面量必须是题目的输入输出文件名
    pattern = r"\b(%s)\s*\(" % "|".join(FILE_FUNCS)
    open_owned = _user_defines(code, "open")
    for match in re.finditer(pattern, code):
        name = match.group(1)
        member = _is_member(code, match.start())
        if name != "open" and member:
            continue
        kind, value = _first_argument(code, match.end() - 1, source)
        line = source.line_at(match.start())
        if kind == "literal":
            problem = _check_path(value, allowed)
            if problem:
                out.add(True, "file_access", line, problem)
        elif kind == "dynamic" and not (name == "open" and open_owned and not member):
            out.add(False, "file_dynamic", line, "%s 的文件名不是字面量" % name)
    for match in re.finditer(r"\b(?:w?[io]?fstream)\b\s*(?:[A-Za-z_]\w*\s*)?([({])", code):
        kind, value = _first_argument(code, match.start(1), source)
        line = source.line_at(match.start())
        if kind == "literal":
            problem = _check_path(value, allowed)
            if problem:
                out.add(True, "file_access", line, problem)
        elif kind == "dynamic":
            out.add(False, "file_dynamic", line, "文件流的文件名不是字面量")


def _check_bidi(source: _Source, out: _Collector) -> None:
    for number, text in enumerate(source.src_lines, 1):
        for ch in text:
            if ch in BIDI_CHARS:
                out.add(True, "bidi", number, "含 Unicode 双向控制字符 U+%04X" % ord(ch))
                break


def _check_shape(source: _Source, out: _Collector, config: PrecheckConfig, size: int) -> None:
    for line in source.trigraph_lines:
        out.add(False, "trigraph_digraph", line, "使用三字符序列")
    for line in source.digraph_lines:
        out.add(False, "trigraph_digraph", line, "使用二合字母")
    for line in source.splice_ident_lines:
        out.add(False, "line_splice", line, "续行拆开标识符")
    for line in source.non_ascii_lines:
        out.add(False, "non_ascii_ident", line, "标识符含非 ASCII 字符")
    if size > config.max_source_bytes:
        out.add(False, "huge_source", 1, "源码 %d KB" % (size // 1024))

    lengths = [len(text) for text in source.src_lines]
    for number, length in enumerate(lengths, 1):
        if length > config.max_line_length:
            out.add(False, "long_line", number, "单行 %d 个字符" % length)
            break
    non_empty = [length for length in lengths if length]
    if len(non_empty) >= AVG_LINE_MIN_LINES and sum(non_empty) / len(non_empty) > config.max_avg_line_length:
        out.add(False, "long_line", 1, "平均行长 %d 个字符" % (sum(non_empty) // len(non_empty)))

    code = source.code
    total = sum(len(lit.value) for lit in source.literals)
    total += sum(len(m.group(0)) for m in _NUMBER.finditer(code))
    if total > config.max_literal_bytes:
        first = source.literals[0].line if source.literals else 1
        out.add(False, "big_table", first, "字面量共 %d 字节，疑似打表" % total)

    # 初始化列表：不含 ; 的 {} 区域里的字面量个数（嵌套的一并计入外层）
    stack: List[List[int]] = []  # [起点, 含分号, 计数]
    biggest, biggest_at = 0, 0
    for match in re.finditer(r"[{};]|\"\x01\"|'\x01'|(?<![\w.'])\.?\d[\w.']*", code):
        token = match.group(0)
        if token == "{":
            stack.append([match.start(), 0, 0])
        elif token == ";":
            if stack:
                stack[-1][1] = 1
        elif token == "}":
            if not stack:
                continue
            start, has_semi, count = stack.pop()
            if stack:
                if has_semi:
                    stack[-1][1] = 1
                else:
                    stack[-1][2] += count
            if not has_semi and count > biggest:
                biggest, biggest_at = count, start
        elif stack:
            stack[-1][2] += 1
    if biggest > config.max_initializer_items:
        out.add(False, "big_table", source.line_at(biggest_at), "初始化列表 %d 个元素，疑似打表" % biggest)

    for literal in source.literals:
        longest = max((len(m.group(0)) for m in _BLOB.finditer(literal.value)), default=0)
        escaped = max((len(m.group(0)) // 2 for m in _HEX_ESCAPES.finditer(literal.value)), default=0)
        length = max(longest, escaped)
        if length > config.min_blob_length:
            out.add(False, "encoded_blob", literal.line, "长编码串（%d 个字符）" % length)


def check_source(
    data: bytes,
    *,
    language: str = "cpp",
    rules: Optional[PrecheckConfig] = None,
    allowed_files: Iterable[str] = (),
) -> PrecheckReport:
    """检查一份 C / C++ 源码。``allowed_files`` 是可以用字面量打开的文件名（题目的输入输出文件）。"""
    config = rules or PrecheckConfig()
    if config.mode == "off":
        return PrecheckReport()
    source = _Source(_decode(data), language)
    out = _Collector(source)
    _check_bidi(source, out)
    _check_directives(source, out, config)
    _check_code(source, out, frozenset(allowed_files))
    _check_shape(source, out, config, len(data))

    def keep(items: Sequence[Finding]) -> List[Finding]:
        return sorted((f for f in items if f.code not in config.disabled_codes), key=lambda f: (f.line, f.code))

    blocked, warnings = keep(out.blocked), keep(out.warnings)
    if config.mode == "warn":
        warnings, blocked = sorted(blocked + warnings, key=lambda f: (f.line, f.code)), []
    return PrecheckReport(blocked=blocked, warnings=warnings)
