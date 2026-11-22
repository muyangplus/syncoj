"""自带评测的源码预检（§12）：规范化之后再匹配，绕过写法照样拦下；字符串、注释里的字不误报。"""

from __future__ import annotations

import json
import shutil
import sys
import zipfile
from pathlib import Path

import pytest

from syncoj_server.judging import precheck, runner, testdata
from syncoj_server.judging.precheck import PrecheckConfig, check_source

ALLOWED = ("ab.in", "ab.out")


def check(code: str, **kwargs):
    kwargs.setdefault("allowed_files", ALLOWED)
    return check_source(code.encode("utf-8"), **kwargs)


def blocked(code: str, **kwargs):
    return [item.code for item in check(code, **kwargs).blocked]


def warned(code: str, **kwargs):
    return [item.code for item in check(code, **kwargs).warnings]


NORMAL = """#include <bits/stdc++.h>
using namespace std;
#define int long long
#define rep(i, a, b) for (int i = (a); i <= (b); ++i)
#define debug(x) cerr << #x << " = " << x << endl
const int N = 1'000'005;
int a[N];
struct DSU { int f[N]; int find(int x) { return f[x] == x ? x : f[x] = find(f[x]); } } d;
void connect(int u, int v) { d.f[d.find(u)] = d.find(v); }
signed main() {
    freopen("ab.in", "r", stdin);
    freopen("ab.out", "w", stdout);
    // system("pause"); fork(); #pragma 都在注释里
    /* asm volatile("nop"); */
    string s = "system(\\"ls\\") _Pragma __attribute__((optimize))";
    char q = '"';
    printf("%:pragma <%%>\\n");
    auto f = std::bind(connect, 1, 2);
    rep(i, 1, 3) connect(i, i + 1);
    cout << s << q << endl;
    return 0;
}
"""


def test_正常代码不拦也不预警() -> None:
    report = check(NORMAL)
    assert report.blocked == [] and report.warnings == []


# ---------------------------------------------------------------- pragma ---- #


@pytest.mark.parametrize("code", [
    "#pragma GCC optimize(2)\nint main(){}",
    "  #  pragma once\n",
    "#/**/pragma GCC optimize(2)\n",
    "#/* 跨\n行 */pragma GCC optimize(2)\n",
    "# \\\npragma GCC optimize(2)\n",
    "#\\   \npra\\\ngma GCC optimize(2)\n",
    "%:pragma GCC optimize(2)\n",
    "??=pragma GCC optimize(2)\n",
    '_Pragma("GCC optimize(2)")\nint main(){}',
    "__pragma(optimize(\"\", on))\n",
    "#define P(x) _Pragma(#x)\nP(GCC optimize(2))\n",
    "#define CAT(a, b) a##b\nCAT(_Pra, gma)(\"GCC optimize(2)\")\n",
])
def test_pragma的各种写法都拦(code) -> None:
    assert "pragma" in blocked(code)


def test_pragma拦截信息带行号与原文() -> None:
    report = check("int x;\n#pragma GCC optimize(\"O3\")\nint main(){}\n")
    item = report.blocked[0]
    assert (item.code, item.line) == ("pragma", 2)
    assert item.excerpt == '#pragma GCC optimize("O3")'
    assert "源码检查未通过：禁止使用 #pragma（第 2 行）" in report.blocked_message()


def test_原始字符串里的内容不算代码() -> None:
    assert blocked('auto s = R"(#pragma\nsystem("ls"); )" ;\nint main(){}') == []
    assert blocked('auto s = R"x( )" system("ls") )x";') == []
    # 原始字符串内部不做续行拼接：结尾以原文为准，后面的 system 是真代码
    assert blocked('auto s = R"x()x\\\n")x" system("rm"); "";') == ["syscall"]
    # C 没有原始字符串
    assert "syscall" in blocked('char *s = R"( )"; system("ls"); ")";', language="c")


def test_字符串注释里的字不误报() -> None:
    code = ('int main(){ printf("system(\\"ls\\") fork() #pragma"); // system("x")\n'
            "/* asm(\"nop\") __attribute__((optimize(3))) */ char c = '\\''; puts(\"\\\"\"); }")
    assert blocked(code) == []
    # 续行把注释延长到下一行
    assert blocked('// 注释 \\\nsystem("ls");') == []


def test_数字分隔符不当成字符字面量() -> None:
    assert blocked("int x = 1'000'000; int main(){ system(\"x\"); }") == ["syscall"]
    assert blocked("long long y = 0x1'F'F; char c = 'a';") == []


# --------------------------------------------------------- 其它拦截规则 ---- #


@pytest.mark.parametrize("code", [
    '__attribute__((optimize("O3"))) int f();',
    '__attribute__((target("avx2"))) int f();',
    'int f() __attribute__ ((__optimize__ (2)));',
    '[[gnu::optimize("O2")]] int f();',
    '[[gnu::always_inline, gnu::target("avx")]] int f();',
    '<:<:gnu::optimize("O2"):>:> int f();',
])
def test_属性里指定优化或目标指令集拦截(code) -> None:
    assert "attribute_optimize" in blocked(code)


def test_普通属性不拦() -> None:
    assert blocked("__attribute__((always_inline)) inline int f(); [[nodiscard]] int g(); int a[2][3]; int x = a[1][2];") == []


@pytest.mark.parametrize("code", ['asm("nop");', '__asm__ __volatile__("");', "__asm { nop }"])
def test_内联汇编拦截(code) -> None:
    assert blocked("int main(){" + code + "}") == ["inline_asm"]


@pytest.mark.parametrize("code", [
    'system("ls");', 'std::system("ls");', 'popen("ls", "r");', "fork();", "vfork();",
    'execl("/bin/sh", "sh", 0);', 'execvp(a, b);', "kill(1, 9);", "ptrace(0, 0, 0, 0);",
    "syscall(60);", "socket(2, 1, 0);", "connect(s, a, l);", "bind(s, a, l);", "::connect(s, a, l);",
    'CreateProcessA(0);', 'ShellExecute(0);', 'WinExec("x", 0);', 'dlopen("x", 1);',
    "mmap(0, 4096, PROT_READ | PROT_EXEC, 0, -1, 0);", "auto f = system;",
])
def test_系统调用拦截(code) -> None:
    assert "syscall" in blocked("int main(){" + code + "}")


def test_同名的自定义函数与成员不算系统调用() -> None:
    code = ("void connect(int u, int v) {}\nauto kill = [](int x) { return x; };\n"
            "struct T { void bind(int) {} } t;\n"
            "int main(){ connect(1, 2); kill(3); t.bind(1); auto g = std::bind(connect, 1, 2); }")
    assert blocked(code) == []
    # 只写声明不算自己定义：与库函数原型一致的声明会直接链接到库函数
    assert set(blocked("int kill(int, int);\nint main(){ kill(1, 9); }")) == {"syscall"}


def test_宏拼出系统调用名拦截() -> None:
    code = "#define C(a, b) a##b\nint main(){ C(sys, tem)(\"ls\"); }"
    assert "syscall" in blocked(code)
    assert "token_paste" in warned(code)


@pytest.mark.parametrize("code", [
    "#include <windows.h>", "#include <sys/socket.h>", "#include <netinet/in.h>", "#include <arpa/inet.h>",
    "#include <sys/ptrace.h>", "#include <dlfcn.h>", "#include <sys/mman.h>", "#include <spawn.h>",
    "#include <sys/wait.h>", "#include <process.h>", '#include "/dev/stdin"', "#include </etc/passwd>",
    '#include "../../secret.h"', '#include "C:\\\\x.h"', "#include_next <windows.h>", "#import \"/proc/self/environ\"",
    "#define H </dev/urandom>\n#include H", "%:include <windows.h>", "??=include \"/dev/stdin\"",
    "#include \\\n<windows.h>", "#/**/include <sys/socket.h>",
])
def test_禁用头文件与危险include拦截(code) -> None:
    assert "forbidden_header" in blocked(code)


def test_常用头文件放行() -> None:
    code = "#include <bits/stdc++.h>\n#include <cstdio>\n#include <ext/pb_ds/assoc_container.hpp>\n#include \"testlib.h\""
    assert blocked(code) == []


def test_打开文件_题目文件放行() -> None:
    code = ('int main(){ freopen("ab.in", "r", stdin); freopen("ab.out", "w", stdout);'
            ' std::ifstream fin("ab.in"); std::ofstream fout("ab.out"); FILE *f = fopen("ab.in", "r");'
            ' freopen(NULL, "rb", stdin); fopen("/dev/stdin", "r"); }')
    report = check(code)
    assert report.blocked == [] and report.warnings == []


@pytest.mark.parametrize("code", [
    'freopen("/etc/passwd", "r", stdin);', 'fopen("../ab.in", "r");', 'fopen("C:\\\\a.in", "r");',
    'fopen("data\\\\ab.in", "r");', 'freopen("other.in", "r", stdin);', 'std::ifstream fin("/proc/self/maps");',
    'std::ofstream o{"x.txt"};', 'f.open("/tmp/x");', 'open("/dev/mem", 0);', 'fopen(L"x.in", "r");',
])
def test_打开文件_路径或非题目文件拦截(code) -> None:
    assert blocked("int main(){" + code + "}") == ["file_access"]


def test_打开文件_文件名不是字面量预警() -> None:
    code = 'int main(){ char s[9]; freopen(s, "r", stdin); std::ifstream in(s); fopen("a" "b", "r"); }'
    assert blocked(code) == []
    assert warned(code) == ["file_dynamic"] * 3


def test_自定义open函数不预警() -> None:
    assert warned("void open(int x) {}\nint main(){ int k = 1; open(k); }") == []


@pytest.mark.parametrize("ch", ["\u202e", "\u2066", "\u200f", "\u061c"])
def test_双向控制字符拦截(ch) -> None:
    report = check("int main(){ /* " + ch + " } */ return 0; }")
    assert [item.code for item in report.blocked] == ["bidi"]
    assert "\\u" in report.blocked[0].excerpt


# ------------------------------------------------------------------ 预警 ---- #


def test_宏_重定义关键字预警_常见写法不预警() -> None:
    assert warned("#define if while\n") == ["macro_heavy"]
    assert warned("#define return exit(0)\n") == ["macro_heavy"]
    assert warned("#define int long long\n#define ll long long\n#define pb push_back\n") == []
    many = "".join("#define M%d %d\n" % (i, i) for i in range(31))
    assert warned(many) == ["macro_heavy"]
    assert warned(many, rules=PrecheckConfig(max_defines=100)) == []


def test_三字符序列二合字母续行预警() -> None:
    assert warned("int a ??( 3 ??);") == ["trigraph_digraph"]
    assert warned("int a <: 3 :>; int main() <% %>") == ["trigraph_digraph"]
    assert warned("int ma\\\nin(){}") == ["line_splice"]
    assert warned("#define X 1 + \\\n 2\n") == []
    # C++11：<:: 不是二合字母
    assert warned("std::vector<::std::string> v;") == []


def test_长行与源码过大预警() -> None:
    assert warned("int x = 0;" + " " * 600 + "\n") == ["long_line"]
    assert warned(("int x = 0;" + " " * 250 + "\n") * 12) == ["long_line"]
    assert warned(("int x = 0;" + " " * 250 + "\n") * 5) == []
    huge = ("int x%d = 0;\n" * 30000) % tuple(range(30000))
    assert "huge_source" in warned(huge)


def test_打表预警() -> None:
    items = ",".join(str(i % 10) for i in range(6000))
    assert "big_table" in warned("int a[] = {" + items + "};\n")
    rows = "\n".join("{" + ",".join(["1"] * 100) + "}," for _ in range(60))
    assert "big_table" in warned("int a[60][100] = {\n" + rows + "\n};\n")
    long_text = '"' + "x " * 40000 + '"'
    assert "big_table" in warned("const char *s = " + long_text + ";\n")
    assert "big_table" not in warned("int a[] = {1, 2, 3};\nint main(){ for(;;){} }")


def test_编码串预警() -> None:
    blob = "QUJD" * 60
    assert warned('const char *s = "%s";' % blob) == ["encoded_blob"]
    hexes = "\\\\x41" * 120
    assert "encoded_blob" in warned('const char *s = "%s";' % hexes.replace("\\\\", "\\"))
    assert warned('const char *s = "%s";' % ("ab " * 100)) == []


def test_非ASCII标识符预警() -> None:
    assert warned("int 变量 = 1;") == ["non_ascii_ident"]
    assert warned("int \\u4e2d = 1;") == ["non_ascii_ident"]
    assert warned('int x = 1; // 中文注释\nchar *s = "中文";') == []


# ------------------------------------------------------------------ 配置 ---- #


def test_配置_关闭规则与模式() -> None:
    code = '#pragma GCC optimize(2)\nint main(){ int a ??( 1 ??); }'
    assert blocked(code, rules=PrecheckConfig(disabled_codes=frozenset({"pragma"}))) == []
    report = check(code, rules=PrecheckConfig(mode="warn"))
    assert report.blocked == [] and [i.code for i in report.warnings] == ["pragma", "trigraph_digraph"]
    off = check(code, rules=PrecheckConfig(mode="off"))
    assert off.blocked == [] and off.warnings == []


@pytest.mark.parametrize("raw, message", [
    ([], "必须是对象"),
    ({"mode": "strict"}, "off / warn / block"),
    ({"disabled": "pragma"}, "数组"),
    ({"disabled": ["nope"]}, "不存在的规则"),
    ({"mode": "warn", "extra": 1}, "不认识"),
])
def test_配置_不对报出来(raw, message) -> None:
    with pytest.raises(ValueError, match=message):
        PrecheckConfig.from_json(raw)


def test_配置_数据包里的precheck(tmp_path) -> None:
    path = tmp_path / "d.zip"
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("1.in", "")
        archive.writestr("1.ans", "")
        archive.writestr("config.json", json.dumps({"precheck": {"mode": "warn", "disabled": ["bidi"]}}))
    with zipfile.ZipFile(str(path)) as archive:
        data = testdata.parse_testdata(archive)
    assert data.precheck.mode == "warn" and data.precheck.disabled_codes == {"bidi"}
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("1.in", "")
        archive.writestr("1.ans", "")
        archive.writestr("config.json", json.dumps({"precheck": {"mode": "x"}}))
    with zipfile.ZipFile(str(path)) as archive, pytest.raises(testdata.TestdataError, match="precheck"):
        testdata.parse_testdata(archive)


def test_规则表完整且不重名() -> None:
    assert not set(precheck.BLOCK_RULES) & set(precheck.WARN_RULES)
    assert len(precheck.BLOCK_RULES) == 7 and len(precheck.WARN_RULES) == 10


def test_摘录截断到80字符() -> None:
    report = check("#pragma " + "x" * 200 + "\n")
    assert len(report.blocked[0].excerpt) <= 80


def test_GBK源码也能查() -> None:
    data = "// 中文注释\n#pragma GCC optimize(2)\n".encode("gb18030")
    assert [item.code for item in check_source(data).blocked] == ["pragma"]


# ------------------------------------------------------------ 接入评测 ---- #


def _data_zip(tmp_path: Path, config=None) -> Path:
    path = tmp_path / "data.zip"
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("1.in", "1 2\n")
        archive.writestr("1.ans", "3\n")
        if config is not None:
            archive.writestr("config.json", json.dumps(config))
    return path


class _NoSandbox:
    """拦截时根本不该建沙箱、不该编译。"""

    calls = 0

    def __call__(self):
        _NoSandbox.calls += 1
        raise AssertionError("拦截后不该再编译")


def test_接入_拦截记CE且不编译(tmp_path) -> None:
    source = tmp_path / "ab.cpp"
    source.write_text('#pragma GCC optimize(2)\nint main(){ system("ls"); }\n', encoding="utf-8")
    outcome = runner.judge(source, "ab.cpp", _data_zip(tmp_path), runner.ProblemConfig(ident="ab"), _NoSandbox())
    assert _NoSandbox.calls == 0
    assert (outcome.status, outcome.score, outcome.max_score) == ("CE", 0, 100)
    assert "源码检查未通过：禁止使用 #pragma（第 1 行）" in outcome.compile_message
    assert "禁止调用 system（第 2 行）" in outcome.compile_message
    document = outcome.to_result_json()
    assert document["status"] == "CE" and "precheck_warnings" not in document


def test_接入_拦截项也带上预警(tmp_path) -> None:
    source = tmp_path / "ab.cpp"
    source.write_text('int a ??( 1 ??);\nint main(){ fork(); }\n', encoding="utf-8")
    outcome = runner.judge(source, "ab.cpp", _data_zip(tmp_path), runner.ProblemConfig(ident="ab"), _NoSandbox())
    assert outcome.status == "CE"
    assert [item["code"] for item in outcome.to_result_json()["precheck_warnings"]] == ["trigraph_digraph"]


needs_gcc = pytest.mark.skipif(
    shutil.which("g++") is None or sys.platform == "win32", reason="需要 Linux + g++"
)


def _factory(tmp_path: Path):
    import os

    from syncoj_server.judging import sandbox

    run_as = "nobody" if hasattr(os, "geteuid") and os.geteuid() == 0 else None
    if run_as:
        os.chmod(str(tmp_path), 0o755)
    return lambda: sandbox.RlimitSandbox(base=tmp_path, run_as=run_as)


@needs_gcc
def test_接入_预警写进结果且正常评测(tmp_path) -> None:
    source = tmp_path / "ab.cpp"
    source.write_text(
        "#include <cstdio>\nint main(){ char n[8] = \"ab.in\"; freopen(n, \"r\", stdin);"
        " long long a, b; scanf(\"%lld%lld\", &a, &b); printf(\"%lld\\n\", a + b); }\n",
        encoding="utf-8",
    )
    outcome = runner.judge(source, "ab.cpp", _data_zip(tmp_path), runner.ProblemConfig(ident="ab", io_mode="file",
                           output_file="ab.out"), _factory(tmp_path))
    document = outcome.to_result_json()
    # 只读了输入文件、输出写到了标准输出：文件 IO 下没有输出文件
    assert document["status"] == "WA"
    warnings = document["precheck_warnings"]
    assert [item["code"] for item in warnings] == ["file_dynamic"] and warnings[0]["line"] == 2


@needs_gcc
def test_接入_warn模式拦截项降为预警照常编译(tmp_path) -> None:
    source = tmp_path / "ab.cpp"
    source.write_text(
        "#pragma GCC optimize(2)\n#include <cstdio>\nint main(){ long long a, b; scanf(\"%lld%lld\", &a, &b);"
        " printf(\"%lld\\n\", a + b); }\n",
        encoding="utf-8",
    )
    data = _data_zip(tmp_path, {"precheck": {"mode": "warn"}})
    outcome = runner.judge(source, "ab.cpp", data, runner.ProblemConfig(ident="ab"), _factory(tmp_path))
    assert outcome.status == "AC"
    assert [item["code"] for item in outcome.to_result_json()["precheck_warnings"]] == ["pragma"]
