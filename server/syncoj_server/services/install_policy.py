"""安装策略：随包 / 随台账带下去的三条规则。

为什么要有它
------------
同一个包发给不同机器，要求可能不同 —— "这次是重装，包里那把密钥必须覆盖机器上
已经吊销的旧钥" vs "这台机器是教师手工配过的，别动它"。把答案写进包与台账，
装机与升级时就有一个**明确、可复现**的结果，而不是靠人记着在命令行上补参数。

三条策略的字段名是**冻结的接口**，机器侧（``agent/packaging/install.py``）按
同样的名字读：

``bootstrap_key_policy``
    机器上已有的注册密钥要不要被包内那把覆盖（``keep`` / ``replace``）。
``config_policy``
    ``agent.ini`` 逐键三态（``keep`` / ``default`` / ``force``）。
``upgrade_mode``
    自更新模式（``apply`` / ``stage`` / ``off``）。

**同一份真相只能有一个出口**
----------------------------
这三条要出现在三个地方：装机台账、升级清单（机器 tick 拿到的那份）、以及离线包根
目录下的 ``install_policy.json``。三处各写一遍必然漂 —— 漂的方式还特别难查：
机器按 A 处给出的策略装机、按 B 处给出的策略升级。所以这里只提供
:func:`build_install_policy` 一个出口，三处都调它；测试也会核对三处逐字相同。

``config_policy`` 的键名有**规范清单**（:data:`CONFIG_POLICY_KEYS`），而它必须与
``agent/config.example.ini`` 逐键对得上（有测试盯着）。两边漂移的后果是"教师以
为配了一条策略，机器上什么都没发生"。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

__all__ = [
    "BOOTSTRAP_KEY_POLICIES",
    "BOOTSTRAP_KEY_POLICY_KEEP",
    "BOOTSTRAP_KEY_POLICY_REPLACE",
    "CONFIG_POLICIES",
    "CONFIG_POLICY_DEFAULT",
    "CONFIG_POLICY_FORCE",
    "CONFIG_POLICY_KEEP",
    "CONFIG_POLICY_KEYS",
    "POLICY_JSON_NAME",
    "UPGRADE_MODES",
    "UpgradePolicyError",
    "ResolvedPolicy",
    "build_install_policy",
    "config_policy_payload",
    "dump_config_policy",
    "load_config_policy",
    "resolve_policy",
    "validate_config_policy",
]

#: 包内策略文件名（tar 根目录）。机器侧 ``POLICY_JSON_FILENAME`` 就是它 ——
#: 名字猜错的表现是"策略静默不生效"，所以两边写同一个字面量，并由测试盯住。
POLICY_JSON_NAME = "install_policy.json"

BOOTSTRAP_KEY_POLICY_KEEP = "keep"
BOOTSTRAP_KEY_POLICY_REPLACE = "replace"
BOOTSTRAP_KEY_POLICIES = (BOOTSTRAP_KEY_POLICY_KEEP, BOOTSTRAP_KEY_POLICY_REPLACE)

CONFIG_POLICY_KEEP = "keep"
CONFIG_POLICY_DEFAULT = "default"
CONFIG_POLICY_FORCE = "force"
CONFIG_POLICIES = (CONFIG_POLICY_KEEP, CONFIG_POLICY_DEFAULT, CONFIG_POLICY_FORCE)

UPGRADE_MODES = ("apply", "stage", "off")

#: 机器侧（``agent/packaging/install.py``）对这两个的默认值，**必须一致**。
#: 不一致的表现是"服务端说 apply、机器按 off 做"。
DEFAULT_BOOTSTRAP_KEY_POLICY = BOOTSTRAP_KEY_POLICY_KEEP
DEFAULT_CONFIG_POLICY = CONFIG_POLICY_KEEP
DEFAULT_UPGRADE_MODE = "apply"

#: ``config_policy`` 认得的所有键，形如 ``<section>.<key>``。
#:
#: **这份清单是规范清单**，与 ``agent/config.example.ini`` 逐键对应（有测试对账：
#: 多一个、少一个、改个名字都红）。加一条之前先问"安装器真的会写这个键吗" ——
#: 列一个没人会读的键，等于让教师以为他配了一条生效的策略。
CONFIG_POLICY_KEYS: Sequence[str] = (
    "server.url",
    "server.verify_tls",
    "server.ca_file",
    "agent.bootstrap_key_file",
    "agent.run_user",
    "agent.state_dir",
    "agent.machine_id",
    "agent.deploy_root",
    "pairing.show_on_desktop",
    "pairing.file_name",
    "scan.roots",
    "scan.prefix",
    "scan.interval",
    "scan.max_file_size",
    "log.level",
    "log.file",
    "log.to_stderr",
    "upgrade.mode",
    "upgrade.install_root",
    "upgrade.public_key",
)

#: 键名里没有段名时该落到哪个段。
#:
#: 这是给界面与手工调用留的**便利入口**，不是规范写法：``roots`` 唯一地指向
#: ``scan.roots``，而 ``mode`` 同时属于 ``upgrade.mode`` 与实验性的扫描模式，
#: 所以它**必须**写全。歧义在这里直接报错，绝不猜 —— 猜错的后果是策略落在另一个
#: 键上，而机器那边一切"正常"（只是没按教师想的做）。
_BARE_KEY_SECTIONS: Dict[str, Tuple[str, ...]] = {}
for _qualified in CONFIG_POLICY_KEYS:
    _section, _name = _qualified.split(".", 1)
    _BARE_KEY_SECTIONS[_name] = _BARE_KEY_SECTIONS.get(_name, ()) + (_section,)
del _qualified, _section, _name


class UpgradePolicyError(ValueError):
    """策略不合法。``detail`` 是一句能直接显示给教师的中文。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _known_keys_text() -> str:
    return "、".join(CONFIG_POLICY_KEYS)


def qualify_key(raw: str) -> str:
    """把 ``roots`` 这类简写补成 ``scan.roots``；补不出来就抛错。

    规范形式始终是 ``<段>.<键>`` —— 简写只为了让界面少写一点，不改变契约。
    """
    text = (raw or "").strip()
    if not text:
        raise UpgradePolicyError("config_policy 里有空的键名")
    if "." in text:
        return text
    sections = _BARE_KEY_SECTIONS.get(text)
    if not sections:
        raise UpgradePolicyError(
            "config_policy 里的键 %r 不在可配清单里。可配的键是：%s"
            % (raw, _known_keys_text())
        )
    if len(sections) > 1:
        raise UpgradePolicyError(
            "config_policy 里的 %r 有歧义（它同时是 %s），请写成 "
            "<段>.<键> 的形式" % (raw, "、".join("%s.%s" % (s, text) for s in sections))
        )
    return "%s.%s" % (sections[0], text)


def validate_config_policy(policy: Optional[Dict[str, str]]) -> Dict[str, str]:
    """校验 ``config_policy``，返回规范化之后的字典。

    两条都**拒绝**而不是静默丢弃：

    * 键必须在 :data:`CONFIG_POLICY_KEYS` 里 —— 一个没人会读的键看起来像配好
      了策略，实际上机器上什么都不会发生；
    * 值必须是三态之一 —— 拼错一个 ``force`` 的后果同上。

    返回的字典**只含教师显式给过的键**：没提到的键按"不改"处理，把它们全部展开
    只会让回执与审计难以阅读，也让"他到底改了什么"看不出来。
    """
    if policy is None:
        return {}
    if not isinstance(policy, dict):
        raise UpgradePolicyError("config_policy 必须是一个对象：{键: keep/default/force}")

    out: Dict[str, str] = {}
    for raw_key, raw_value in policy.items():
        key = qualify_key(str(raw_key))
        if key not in CONFIG_POLICY_KEYS:
            raise UpgradePolicyError(
                "config_policy 里的键 %r 不在可配清单里。可配的键是：%s"
                % (raw_key, _known_keys_text())
            )
        value = str(raw_value).strip().lower()
        if value not in CONFIG_POLICIES:
            raise UpgradePolicyError(
                "config_policy 里 %s 的值必须是 %s 之一，实际是 %r"
                % (key, " / ".join(CONFIG_POLICIES), raw_value)
            )
        out[key] = value
    return out


def config_policy_payload(stored: Dict[str, str]) -> Dict[str, str]:
    """把库里那份策略展开成"每个键都有值"的完整映射。

    界面要显示**某一版的具体策略**，而库里只存教师改过的那些键（没提到的按不改
    处理）。所以这里补全成完整映射：留空让界面自己去推默认值，等于把"默认是不改"
    这条规矩抄到第二个地方 —— 那种抄写迟早会漂。
    """
    merged = {key: DEFAULT_CONFIG_POLICY for key in CONFIG_POLICY_KEYS}
    for key, value in (stored or {}).items():
        if key in merged:
            merged[key] = value
    return merged


def load_config_policy(raw: Optional[str]) -> Dict[str, str]:
    """从库里那一列读回策略。任何坏内容都当成"没有策略"，不抛异常。

    这一列是**我们自己**写进去的，坏内容只可能来自手工改库或半截写入；为它让
    整个发布列表接口 500，代价远大于收益。
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {str(key): str(value) for key, value in parsed.items()}


def dump_config_policy(policy: Dict[str, str]) -> Optional[str]:
    """把策略存进库。空策略存成 ``NULL``：它与"存了一个空对象"是同一件事，
    而 ``NULL`` 让"这个版本根本没配过逐键策略"在库里一眼可辨。"""
    if not policy:
        return None
    return json.dumps(policy, ensure_ascii=False, sort_keys=True)


def _default_bootstrap_key_policy(has_bundled_key: bool) -> str:
    """"教师没明确选"时的默认值 —— 唯一一处判据。

    ``build_install_policy`` 与 :func:`resolve_policy` 都走它：两处各写一遍迟早
    会在某一次改动里分叉，而分叉的那天表现是"构建时算的是 replace、事后从库里
    读回来的是 keep"。
    """
    return BOOTSTRAP_KEY_POLICY_REPLACE if has_bundled_key else DEFAULT_BOOTSTRAP_KEY_POLICY


def build_install_policy(
    *,
    bootstrap_key_policy: Optional[str],
    config_policy: Optional[Dict[str, str]],
    upgrade_mode: Optional[str],
    has_bundled_key: bool = False,
) -> Dict[str, Any]:
    """生成下发给机器的那份策略 —— **三处共用的唯一出口**。

    ``None`` 一律按默认值补，而且**默认值本身要看这一版有没有夹带密钥**：

    * 夹带了 → ``replace``（用户拍板的默认：包内那把是权威）；
    * 没夹带 → ``keep``。

    **不能只写成一个常量。** 迁移之前建的发布记录没有策略列，可它当时**确实**
    往包里塞过统一注册密钥 —— 拿一个固定的 ``keep`` 去补，就等于把那批"包里明明
    有新钥、机器上却留着旧的已吊销钥"的坑重新埋回去，而这正是这个功能要修的现场。
    判据用 ``bootstrap_key_id``（服务端自己记的"这个包有没有夹带"），不是猜。
    """
    return {
        "bootstrap_key_policy": (
            bootstrap_key_policy
            if bootstrap_key_policy
            else _default_bootstrap_key_policy(has_bundled_key)
        ),
        "config_policy": config_policy_payload(config_policy or {}),
        "upgrade_mode": upgrade_mode or DEFAULT_UPGRADE_MODE,
    }


@dataclass
class ResolvedPolicy:
    """构建请求校验之后的三份形态。

    ``install_policy`` 是下发给机器的完整字典（同一份真相），另外三个是入库形态：
    查询、回执、审计各自要的是不同粒度，分开存免得每次从字典里反推。
    """

    bootstrap_key_policy: str
    config_policy: Dict[str, str]
    upgrade_mode: str
    install_policy: Dict[str, Any]


def resolve_policy(
    *,
    include_bootstrap_key: bool,
    bootstrap_key_policy: Optional[str],
    config_policy: Optional[Dict[str, str]],
    upgrade_mode: Optional[str],
) -> ResolvedPolicy:
    """校验「发布当前版本」提交的三条策略，返回可入库、可下发的那一份。

    默认值是三条规矩：

    * ``bootstrap_key_policy``：**跟着"附带密钥"走** —— 勾了就默认 ``replace``
      （包内那把是权威）。现场踩过一次：机器上躺着一把已吊销的旧钥，安装器按
      "已有一份就不动"跳过，而那把新钥因此永远用不上，机器永远注册不上。
    * ``config_policy``：默认全 ``keep``（不改）。
    * ``upgrade_mode``：默认 ``apply``。

    显式给了值就听显式的。``keep`` 尤其重要：把它当成"没给"，教师就没法在勾了
    附带密钥的同时**保住**机器上那把旧钥。
    """
    if bootstrap_key_policy is None:
        # 与 build_install_policy 用**同一条**规矩（夹带了就 replace），
        # 免得"构建时算出来的"与"后来从库里读回来的"各有一套默认值。
        resolved_bootstrap = _default_bootstrap_key_policy(include_bootstrap_key)
    else:
        candidate = str(bootstrap_key_policy).strip().lower()
        if candidate not in BOOTSTRAP_KEY_POLICIES:
            raise UpgradePolicyError(
                "bootstrap_key_policy 只能是 %s 之一，实际是 %r"
                % (" / ".join(BOOTSTRAP_KEY_POLICIES), bootstrap_key_policy)
            )
        resolved_bootstrap = candidate
        if not include_bootstrap_key:
            # "不附带密钥"时写 replace 是自相矛盾的：包里压根没有那把钥。
            # 静默当成 keep 会让教师以为他的选择生效了 —— 直接拒。
            raise UpgradePolicyError(
                "没有勾选「附带统一注册密钥」时 bootstrap_key_policy 只能是 keep"
                "（包里没有那把钥匙，replace 无从谈起）"
            )

    resolved_config = validate_config_policy(config_policy)

    if upgrade_mode is None:
        resolved_upgrade = DEFAULT_UPGRADE_MODE
    else:
        candidate_mode = str(upgrade_mode).strip().lower()
        if candidate_mode not in UPGRADE_MODES:
            raise UpgradePolicyError(
                "upgrade_mode 只能是 %s 之一，实际是 %r"
                % (" / ".join(UPGRADE_MODES), upgrade_mode)
            )
        resolved_upgrade = candidate_mode

    return ResolvedPolicy(
        bootstrap_key_policy=resolved_bootstrap,
        config_policy=resolved_config,
        upgrade_mode=resolved_upgrade,
        install_policy=build_install_policy(
            bootstrap_key_policy=resolved_bootstrap,
            config_policy=resolved_config,
            upgrade_mode=resolved_upgrade,
            has_bundled_key=include_bootstrap_key,
        ),
    )
