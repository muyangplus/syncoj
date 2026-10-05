"""三态凭据、配对码展示、以及"哪些错误该重新注册"。

配对模型是这一轮改动最大的地方。机器注册上来**没有归属**：它是一个"还没有配对
到人"的东西，看起来像注册失败了 —— 而这是整条链路上最容易做错的一段：

* 服务端回了 200，但 ``claimed`` 是 false
* 它不能扫代码（不知道准考证号，``scan.roots`` 里的 ``{player_no}`` 展开不出来）
* 它不知道要等多久

做错的表现是：Agent 把"还没有归属"当成"凭据坏了"，于是反复重新注册 —— 每注册
一次服务端就换一个新配对码，教师刚在机器上读到的那个立刻失效，而且日志里刷满
注册记录，真正的状态反而看不见。

``claimed`` 与 ``bound`` 组合出三种状态，三者"看起来都像注册成功了"：

===================  ============================  ============================
``claimed=False``    还没配对到人                    写配对码.txt，不扫描，等 tick
``bound=False``      配对好了但这场没这个人            写等待场次.txt，不扫描，等 tick
``bound=True``       正常                            扫代码、收下发
===================  ============================  ============================
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional

import pytest

from syncoj_agent.client import (
    AuthError,
    BootstrapKeyError,
    RateLimited,
    UnboundError,
)
from syncoj_agent.config import WAITING_FILE_NAME, AgentConfig
from syncoj_agent.main import Agent, state_from_payload
from syncoj_agent.state import (
    STATE_READY,
    STATE_UNCLAIMED,
    STATE_WAITING,
    Credential,
    load_credential,
    save_credential,
)


# --------------------------------------------------------------------------- #
# 假服务端
# --------------------------------------------------------------------------- #


class FakeServer:
    """记录调用、按测试设定回响应的假服务端。

    只实现 Agent 在"不能干活"时真的会碰的那几个接口。剩下的（上传、下载）
    一旦被调用就断言失败 —— "这台机器在没有归属的时候居然去传代码了"是要当场
    炸出来的错误，不是记一条日志就过去的事。
    """

    def __init__(self) -> None:
        self.enroll_calls: List[Dict[str, object]] = []
        self.tick_calls: List[Dict[str, object]] = []
        self.event_batches: List[List[dict]] = []
        self.upload_calls: List[str] = []
        self.download_calls: List[int] = []
        self.token: Optional[str] = None
        self.closed = False
        self.last_retry_after = 0

        self.enroll_response: Dict[str, object] = {
            "token": "tok-1",
            "agent_id": 7,
            "claimed": False,
            "bound": False,
            "pair_code": "482913",
            "reason": "这台机器还没有配对到人，请把配对码告诉老师",
            "config": {},
        }
        self.tick_response: Dict[str, object] = {
            "server_time": 1767225600,
            "next_tick_seconds": 7,
            "need_upload": [],
            "deploy_jobs": [],
            "cancel_assets": [],
            "upgrade": None,
            "config": {},
            "claimed": False,
            "bound": False,
            "pair_code": "482913",
            "reason": "这台机器还没有配对到人，请把配对码告诉老师",
            "player_no": "",
            "contest_slug": "",
        }
        self.enroll_error: Optional[BaseException] = None
        self.tick_error: Optional[BaseException] = None
        #: 错误抛几次；-1 = 每次都抛。默认"每次都抛"，因为绝大多数测试关心的是
        #: "这个错误一直存在时会怎样"；要测"错一次之后恢复了"就把它设成 1。
        self.enroll_error_times = -1
        self.tick_error_times = -1
        self.enroll_errors_raised = 0
        self.tick_errors_raised = 0

    @staticmethod
    def _within(raised: int, limit: int) -> bool:
        return limit < 0 or raised < limit

    # -- 接口 ------------------------------------------------------------ #

    def enroll(self, **kwargs) -> dict:
        self.enroll_calls.append(kwargs)
        if self.enroll_error is not None and self._within(
            self.enroll_errors_raised, self.enroll_error_times
        ):
            self.enroll_errors_raised += 1
            raise self.enroll_error
        return dict(self.enroll_response)

    def tick(self, payload: dict) -> dict:
        self.tick_calls.append(payload)
        if self.tick_error is not None and self._within(
            self.tick_errors_raised, self.tick_error_times
        ):
            self.tick_errors_raised += 1
            raise self.tick_error
        return dict(self.tick_response)

    def report_events(self, events) -> dict:
        self.event_batches.append(list(events))
        return {}

    def set_token(self, token: Optional[str]) -> None:
        self.token = token

    def close(self) -> None:
        self.closed = True

    def upload_file(self, rel_path, *args) -> dict:
        self.upload_calls.append(rel_path)
        raise AssertionError("这台机器还没有归属，不该上传任何代码")

    def open_download(self, asset_id, *args):
        self.download_calls.append(asset_id)
        raise AssertionError("这台机器还没有归属，不该收到任何下发")

    def drop_transfer_connection(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture()
def agent_config(workdir: Path) -> AgentConfig:
    config = AgentConfig.load(None)
    config.state_dir = workdir / "state"
    config.deploy_root = workdir / "桌面"
    config.scan_roots = [workdir / "code"]
    config.bootstrap_key_file = workdir / "bootstrap.key"
    config.server_url = "https://127.0.0.1:8000"
    return config


def make_agent(config: AgentConfig) -> Agent:
    config.state_dir.mkdir(parents=True, exist_ok=True)
    config.deploy_root.mkdir(parents=True, exist_ok=True)
    config.scan_roots[0].mkdir(parents=True, exist_ok=True)
    # 密钥文件必须存在，否则 _enroll 会在读密钥那一步就抛 ConfigError
    (config.bootstrap_key_file).write_text("SECRET-BOOTSTRAP-KEY\n", encoding="utf-8")
    return Agent(config)


def attach(agent: Agent, server: FakeServer) -> FakeServer:
    agent.client.close()
    agent.client = server  # type: ignore[assignment]
    return server


def run_rounds(agent: Agent, rounds: int = 1) -> List[float]:
    """跑 ``rounds`` 轮主循环后停下，返回每轮之间请求的等待秒数。

    必须走 ``run_forever``：401/429 那几种"整轮停下来"的处理逻辑只存在于它的
    ``except`` 分支里，直接调 ``cycle()`` 覆盖不到 —— 而那正是最容易写错的地方。
    """
    delays: List[float] = []

    def fake_sleep(seconds: float) -> None:
        delays.append(seconds)
        if len(delays) >= rounds:
            agent.request_stop()

    agent._sleep = fake_sleep  # type: ignore[assignment]
    agent.run_forever()
    return delays


def pair_code_file(config: AgentConfig) -> Path:
    return config.deploy_root / config.pairing_file_name


def waiting_file(config: AgentConfig) -> Path:
    return config.deploy_root / WAITING_FILE_NAME


# --------------------------------------------------------------------------- #
# 三态本身
# --------------------------------------------------------------------------- #


def test_unclaimed_credential_is_still_usable() -> None:
    """**这是整件事的关键。**

    老判据是 ``token and agent_id > 0``。未配对的机器可能压根没有 agent_id，
    按老判据会被当成"没有凭据"，于是反复重新注册，把配对码刷成新的 ——
    教师刚读到的那个立刻失效，而且日志里只看得到注册记录。
    """
    credential = Credential(token="tok", claimed=False, pair_code="482913")
    assert credential.is_usable() is True
    assert credential.state == STATE_UNCLAIMED


def test_the_three_states_are_distinguished() -> None:
    assert Credential(token="t", claimed=False, bound=False).state == STATE_UNCLAIMED
    assert Credential(token="t", claimed=True, bound=False).state == STATE_WAITING
    assert Credential(token="t", claimed=True, bound=True).state == STATE_READY


def test_unclaimed_wins_over_a_contradictory_bound_flag() -> None:
    """``claimed=False, bound=True`` 是自相矛盾的文件（没人认领却已经能干活）。

    必须按"没配对"处理：相信那个 ``bound`` 会让 Agent 去扫一个用准考证号
    展开不出来的目录，每一轮都失败，而日志里只看得到路径错误。
    """
    credential = Credential.from_dict(
        {"token": "t", "claimed": False, "bound": True, "player_no": "S001"}
    )
    assert credential.state == STATE_UNCLAIMED


def test_old_credential_files_mean_ready(workdir: Path) -> None:
    """升级前的凭据文件里没有 claimed/bound 字段，默认必须是"能干活"。

    默认成"未配对"的话，所有老机器升级后都会突然开始等配对 ——
    而它们其实早就配好了。
    """
    path = workdir / "credential.json"
    path.write_text(
        json.dumps({"token": "tok", "agent_id": 3, "player_no": "S001"}),
        encoding="utf-8",
    )
    credential = load_credential(path)
    assert credential is not None
    assert credential.state == STATE_READY
    assert credential.token == "tok"


def test_state_round_trips_through_the_credential_file(workdir: Path) -> None:
    path = workdir / "credential.json"
    save_credential(
        path, Credential(token="tok", claimed=True, bound=False, player_no="S001")
    )
    loaded = load_credential(path)
    assert loaded is not None
    assert loaded.state == STATE_WAITING


def test_state_from_payload_defaults_to_ready() -> None:
    """缺字段时按"能干活"处理 —— 这个函数只用来**发现变化**，不是判权限。"""
    assert state_from_payload({}) == STATE_READY
    assert state_from_payload({"claimed": True}) == STATE_READY
    assert state_from_payload({"claimed": False, "bound": True}) == STATE_UNCLAIMED
    assert state_from_payload({"claimed": True, "bound": False}) == STATE_WAITING


# --------------------------------------------------------------------------- #
# 状态一：还没配对到人
# --------------------------------------------------------------------------- #


def test_unclaimed_machine_shows_the_pair_code(agent_config: AgentConfig) -> None:
    """配对码的价值在于**被人在机器前读到**。

    日志要 journalctl 才看得到，而教师是走到机器前看屏幕的 ——
    桌面上一个「配对码.txt」是他最可能看见的东西。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 1)

    path = pair_code_file(agent_config)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "482913" in text
    assert "配对" in text
    assert server.enroll_calls, "应该注册过一次"


def test_unclaimed_machine_never_scans(agent_config: AgentConfig) -> None:
    """没有准考证号，``scan.roots`` 里的 ``{player_no}`` 展开不出来。

    收上去的相对路径没法归属到任何人，只会污染台账。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 2)

    assert len(server.tick_calls) == 2
    for payload in server.tick_calls:
        assert payload["scan"] == []
        # 而且必须老实说"这次没扫"，不能说"扫完了，一个文件都没有" ——
        # 后者与"选手把文件全删了"在报文里长得一模一样
        assert payload["scan_complete"] is False


def test_unclaimed_machine_does_not_re_enroll_every_round(
    agent_config: AgentConfig,
) -> None:
    """**这是最要紧的一条。**

    反复重新注册会让服务端每次换一个新配对码，教师刚在机器上读到的那个立刻
    失效 —— 表现是"配对码怎么输都不对"，而日志里刷满注册记录。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 4)

    assert len(server.enroll_calls) == 1, "未配对的机器只能注册一次"
    assert len(server.tick_calls) == 4
    # 凭据必须留在磁盘上：丢了它就等于下一轮从"没有凭据"重新开始
    credential = load_credential(agent_config.credential_path)
    assert credential is not None
    assert credential.pair_code == "482913"


def test_unclaimed_machine_does_not_re_enroll_after_a_restart(
    agent_config: AgentConfig,
) -> None:
    """**重启之后也必须只注册一次。**

    这才是真实场景：机器还没配对时，恰是最可能被反复重启的时候（教师看到
    "没反应"就重启服务）。每个新进程都从磁盘上的凭据起来 —— 如果它认不出
    "未配对但可用"的凭据，每轮都会重新注册，而服务端每次注册都会换一个新
    配对码，教师手上那个永远对不上。
    """
    server = FakeServer()

    for _ in range(3):
        agent = make_agent(agent_config)
        attach(agent, server)
        agent.cycle()

    assert len(server.enroll_calls) == 1, "重启之后重新注册了"
    assert len(server.tick_calls) == 3


def test_unclaimed_machine_follows_the_server_tick_pace(
    agent_config: AgentConfig,
) -> None:
    """节奏由服务端决定，Agent 不得自行决定。"""
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_response["next_tick_seconds"] = 23

    delays = run_rounds(agent, 1)

    assert delays == [23.0]


def test_pair_code_refresh_from_tick_is_written_out(agent_config: AgentConfig) -> None:
    """服务端可能在 tick 里带一个**新的**配对码过来（旧的过期了）。

    不接住它的话，桌面上一直是那个已经作废的码。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_response["pair_code"] = "999000"

    run_rounds(agent, 1)

    assert "999000" in pair_code_file(agent_config).read_text(encoding="utf-8")
    credential = load_credential(agent_config.credential_path)
    assert credential is not None
    assert credential.pair_code == "999000"


def test_missing_pair_code_file_is_not_an_error(agent_config: AgentConfig) -> None:
    """没配对过的机器本来就没有这个文件，不该报错。"""
    agent = make_agent(agent_config)
    agent.clear_pair_code_file()  # 不抛
    agent.clear_waiting_file()


def test_desktop_showing_can_be_turned_off(agent_config: AgentConfig) -> None:
    """考点规定桌面必须干净时，关掉它不该影响别的行为。"""
    agent_config.pairing_show_on_desktop = False
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 1)

    assert not pair_code_file(agent_config).exists()
    assert len(server.tick_calls) == 1


def test_unwritable_desktop_does_not_crash(agent_config: AgentConfig, workdir: Path) -> None:
    """桌面写不进去不算致命：日志里还有一份，教师可以查日志。"""
    agent = make_agent(agent_config)
    # 把 deploy_root 指向一个"文件"，mkdir 一定失败
    blocker = workdir / "blocker"
    blocker.write_text("x", encoding="utf-8")
    agent_config.deploy_root = blocker

    agent._show_pair_code("482913")  # 不抛


# --------------------------------------------------------------------------- #
# 状态二：配对好了，但这场没有这个人
# --------------------------------------------------------------------------- #


#: 故意用一个**与任何兜底文案都不同**的句子。
#:
#: 服务端给的那句话必须原样落到「等待场次.txt」上：只有服务端知道为什么
#: （"这场没有你"还是"名单还没应用"），客户端猜不出来。用兜底文案当测试数据
#: 会让这条断言变成同义反复 —— 实现里把 reason 丢掉了它也照样绿。
SERVER_REASON = "这场（mock-1）的名单里还没有你，请联系监考老师"


def make_waiting_server(reason: str = SERVER_REASON) -> FakeServer:
    server = FakeServer()
    server.enroll_response.update(
        {
            "claimed": True,
            "bound": False,
            "pair_code": None,
            "player_no": "S001",
            "reason": reason,
            "contest_name": "",
        }
    )
    server.tick_response.update(
        {
            "claimed": True,
            "bound": False,
            "pair_code": None,
            "player_no": "S001",
            "reason": reason,
        }
    )
    return server


def test_waiting_machine_writes_the_reason_to_the_desktop(
    agent_config: AgentConfig,
) -> None:
    """说不出来的话，客户端只能猜；而教师看到的是"这台机器就是不收代码"。

    这个中间状态是"机器绑的是人"带来的必然结果：某个场次有没有这个人，
    取决于那份名单有没有被应用到场次里。

    写下去的必须是**服务端给的原话** —— 客户端不知道原因，只有服务端知道。
    """
    agent = make_agent(agent_config)
    server = attach(agent, make_waiting_server())

    run_rounds(agent, 2)

    path = waiting_file(agent_config)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert SERVER_REASON in text, "服务端给的 reason 没有写到桌面上：\n%s" % text
    assert len(server.enroll_calls) == 1
    assert len(server.tick_calls) == 2


def test_waiting_machine_does_not_scan(agent_config: AgentConfig) -> None:
    agent = make_agent(agent_config)
    server = attach(agent, make_waiting_server())

    run_rounds(agent, 2)

    for payload in server.tick_calls:
        assert payload["scan"] == []
        assert payload["scan_complete"] is False


def test_waiting_state_is_reported_once_not_every_round(
    agent_config: AgentConfig,
) -> None:
    """事件通道是给"出事了"用的，每轮报一条会把它刷满。"""
    agent = make_agent(agent_config)
    server = attach(agent, make_waiting_server())

    run_rounds(agent, 5)

    categories = [event["category"] for batch in server.event_batches for event in batch]
    assert categories.count("pairing_wait") == 1


def test_waiting_machine_clears_a_stale_pair_code_file(
    agent_config: AgentConfig,
) -> None:
    """配对成功后桌面上那个配对码必须消失。

    留着它会让人以为还没配对 —— 下一个人走过来看见就会去问老师，白折腾一轮。
    """
    agent = make_agent(agent_config)
    # 先造出"上一轮还是未配对"的现场
    agent.clear_waiting_file()
    pair_code_file(agent_config).parent.mkdir(parents=True, exist_ok=True)
    pair_code_file(agent_config).write_text("老配对码 482913", encoding="utf-8")

    attach(agent, make_waiting_server())
    run_rounds(agent, 1)

    assert not pair_code_file(agent_config).exists()
    assert waiting_file(agent_config).is_file()


# --------------------------------------------------------------------------- #
# 状态三：能干活了
# --------------------------------------------------------------------------- #


def make_ready_server() -> FakeServer:
    server = FakeServer()
    ready = {
        "claimed": True,
        "bound": True,
        "pair_code": None,
        "player_no": "S001",
        "contest_id": 1,
        "contest_slug": "mock-1",
        "contest_name": "校内模拟赛",
        "reason": None,
        "need_upload": [],
        "deploy_jobs": [],
        "next_tick_seconds": 60,
    }
    server.enroll_response.update(ready)
    server.tick_response.update(ready)
    return server


def test_ready_machine_clears_both_notice_files(agent_config: AgentConfig) -> None:
    """配对成功、拿到场次之后，桌面上那两个提示文件都必须自动消失。"""
    agent = make_agent(agent_config)
    pair_code_file(agent_config).write_text("482913", encoding="utf-8")
    waiting_file(agent_config).write_text("等待场次", encoding="utf-8")

    attach(agent, make_ready_server())
    run_rounds(agent, 1)

    assert not pair_code_file(agent_config).exists()
    assert not waiting_file(agent_config).exists()
    credential = load_credential(agent_config.credential_path)
    assert credential is not None
    assert credential.state == STATE_READY
    assert credential.player_no == "S001"
    assert credential.contest_slug == "mock-1"


def test_ready_machine_scans_and_lists_files(agent_config: AgentConfig) -> None:
    """能干活了就该走正常路径：扫描 -> tick。"""
    code = agent_config.scan_roots[0]
    (code / "p1").mkdir(parents=True, exist_ok=True)
    (code / "p1" / "p1.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")

    agent = make_agent(agent_config)
    server = attach(agent, make_ready_server())

    run_rounds(agent, 1)

    assert len(server.enroll_calls) == 1
    payload = server.tick_calls[0]
    assert payload["scan_complete"] is True
    # 默认 scan.prefix=none，所以上报路径里不带根目录名
    assert [entry["path"] for entry in payload["scan"]] == ["p1/p1.cpp"]


def test_ready_machine_that_is_unbound_again_falls_back_to_waiting(
    agent_config: AgentConfig,
) -> None:
    """中途被解绑（比如教师在场次里把这个人删了）不是故障，是状态变化。

    它必须退回"安静等待"，而不是先进指数退避、再当成凭据坏了去重新注册。
    """
    agent = make_agent(agent_config)
    server = attach(agent, make_ready_server())
    # 第一轮正常，之后服务端改口说"这个人已经不在这场里了"
    server.tick_response.update(
        {"claimed": True, "bound": False, "pair_code": None, "reason": "还没有包含你的场次"}
    )

    run_rounds(agent, 2)

    credential = load_credential(agent_config.credential_path)
    assert credential is not None
    assert credential.state == STATE_WAITING
    assert waiting_file(agent_config).is_file()
    assert len(server.enroll_calls) == 1, "状态变化不该触发重新注册"


# --------------------------------------------------------------------------- #
# 403 与 401 的分工
# --------------------------------------------------------------------------- #


def unbound_error(code: str = "machine_unbound") -> UnboundError:
    return UnboundError(
        "tick 失败（HTTP 403 %s）这台机器还没有配对到人" % code,
        status=403,
        code=code,
        detail="这台机器还没有配对到人",
    )


def test_403_on_tick_does_not_re_enroll(agent_config: AgentConfig) -> None:
    """**403 绝不能清凭据。**

    403 的意思是"凭据有效，但还没有配对/无权访问"。把它当成凭据坏了去重新注册，
    会让服务端每次换一个新配对码 —— 教师刚读到的那个当场失效。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_error = unbound_error()

    run_rounds(agent, 3)

    assert len(server.enroll_calls) == 1, "403 不该触发重新注册"
    credential = load_credential(agent_config.credential_path)
    assert credential is not None, "403 不该删掉本地凭据"
    assert credential.token == "tok-1"
    assert credential.state == STATE_UNCLAIMED


def test_403_on_tick_keeps_showing_the_pair_code(agent_config: AgentConfig) -> None:
    """403 之后仍然要把配对码摆在桌面上 —— 那正是教师接下来要用的东西。"""
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_error = unbound_error()

    run_rounds(agent, 2)

    path = pair_code_file(agent_config)
    assert path.is_file()
    assert "482913" in path.read_text(encoding="utf-8")


def test_403_pairing_required_behaves_like_machine_unbound(
    agent_config: AgentConfig,
) -> None:
    """两个 code 都表示"还没配对"，处理必须一样。

    分支判断只能看 ``code``，不能看 ``detail`` —— 否则改一次文案就会静默失效。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_error = unbound_error("pairing_required")

    run_rounds(agent, 2)

    assert len(server.enroll_calls) == 1
    assert pair_code_file(agent_config).is_file()


def test_unknown_403_code_still_does_not_re_enroll(agent_config: AgentConfig) -> None:
    """403 里出现不认识的 code 时，保守做法仍是"不要重新注册"。

    §0.2 是照状态码定性的：403 = 凭据有效。把凭据扔掉去换一个新的，是这里
    唯一会造成实际损失的"猜错"（配对码失效），所以宁可什么都不做。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_error = UnboundError("403 无权限", status=403, code="forbidden")

    run_rounds(agent, 2)

    assert len(server.enroll_calls) == 1
    credential = load_credential(agent_config.credential_path)
    assert credential is not None and credential.token == "tok-1"


def test_401_clears_the_token_and_re_enrolls(agent_config: AgentConfig) -> None:
    """401 才是"凭据本身无效" —— 这时候必须清掉本地凭据重新注册。

    整个流程里**只有**这一种情况该重新注册。
    """
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.enroll_response.update({"claimed": True, "bound": False, "pair_code": None})
    server.tick_error = AuthError(
        "tick 失败（HTTP 401 unauthorized）凭据无效", status=401, code="unauthorized"
    )
    # 401 只错一次：错的是那个 token，重新注册拿到新 token 之后就该恢复正常
    server.tick_error_times = 1

    run_rounds(agent, 2)

    # 第 1 轮 tick 401 -> 清凭据；第 2 轮重新注册 -> 得到新 token
    assert len(server.enroll_calls) == 2, "401 之后必须重新注册一次"
    assert server.token == "tok-1"


def test_re_enroll_after_401_uses_the_same_machine_uuid(
    agent_config: AgentConfig,
) -> None:
    """重新注册靠 ``machine_uuid`` 认回同一台机器（这是快照还原后的自愈路径）。"""
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.tick_error = AuthError("401", status=401, code="token_expired")
    # 只错一次：第二次注册要成功，否则测的是"一直 401"而不是"401 之后会重新注册"
    server.tick_error_times = 1

    run_rounds(agent, 2)

    uuids = {call.get("machine_uuid") for call in server.enroll_calls}
    assert len(server.enroll_calls) == 2
    assert len(uuids) == 1 and None not in uuids


def test_enroll_always_carries_the_fingerprint_when_available(
    agent_config: AgentConfig,
) -> None:
    agent = make_agent(agent_config)
    agent.machine_fingerprint = "4c4c4544-0031-3010-8043-b7c04f4d4432"
    server = attach(agent, FakeServer())

    run_rounds(agent, 1)

    assert server.enroll_calls[0]["machine_fingerprint"] == agent.machine_fingerprint
    assert server.enroll_calls[0]["machine_uuid"] == agent.machine_uuid


def test_enroll_never_sends_enroll_code(agent_config: AgentConfig) -> None:
    """每选手注册码那条链路已经被整个删除了 —— 报文里不该再出现它。"""
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 1)

    assert "enroll_code" not in server.enroll_calls[0]
    assert set(server.enroll_calls[0]) >= {
        "bootstrap_key",
        "machine_id",
        "machine_uuid",
        "hostname",
        "agent_version",
        "os_info",
    }


# --------------------------------------------------------------------------- #
# 密钥错、限速
# --------------------------------------------------------------------------- #


def test_bootstrap_key_rejection_stops_hammering(agent_config: AgentConfig) -> None:
    """密钥不对是**部署问题**，重试解决不了。

    这里不断言"完全不重试"（那会让运维换完密钥还得逐台重启），而是断言它退到
    最慢的节奏：MAX_BACKOFF。
    """
    from syncoj_agent.main import MAX_BACKOFF

    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.enroll_error = BootstrapKeyError(
        "注册失败（HTTP 403 bootstrap_key_revoked）已吊销",
        status=403,
        code="bootstrap_key_revoked",
    )

    delays = run_rounds(agent, 3)

    assert delays == [MAX_BACKOFF] * 3
    # 密钥错时必须留一条人看得见的事件。注意它**送不出去** —— /events 需要
    # 凭据，而我们手里恰好没有可用的，所以它只会堆在本地队列里，等密钥修好
    # 之后随第一轮成功上报一起发上去。
    pending = [event["category"] for event in agent._pending_events]
    assert pending.count("bootstrap_key_rejected") >= 1


def test_rate_limit_is_honoured(agent_config: AgentConfig) -> None:
    """429 的退避节奏由服务端决定 —— 它才知道限速窗口有多长。"""
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.enroll_error = RateLimited(
        "注册失败（HTTP 429 rate_limited）太快了",
        status=429,
        code="rate_limited",
        retry_after=17,
    )

    delays = run_rounds(agent, 2)

    assert delays == [17.0, 17.0]


def test_absurd_rate_limit_is_capped_locally(agent_config: AgentConfig) -> None:
    """服务端写错一个数量级（比如 86400）不该让整间机房停摆一整天。"""
    from syncoj_agent.main import MAX_BACKOFF

    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())
    server.enroll_error = RateLimited(
        "429", status=429, code="rate_limited", retry_after=86400
    )

    delays = run_rounds(agent, 1)

    assert delays == [MAX_BACKOFF]


# --------------------------------------------------------------------------- #
# 注册时的凭据选择
# --------------------------------------------------------------------------- #


def test_missing_bootstrap_key_gives_an_actionable_error(
    agent_config: AgentConfig, workdir: Path
) -> None:
    """没有密钥时要说清"下一步该做什么"，而不是笼统地"注册失败"。"""
    from syncoj_agent.config import ConfigError

    agent = make_agent(agent_config)
    agent_config.bootstrap_key_file = workdir / "nowhere.key"
    agent = Agent(agent_config)

    with pytest.raises(ConfigError) as excinfo:
        agent.ensure_credential()

    message = str(excinfo.value)
    assert "统一密钥" in message
    assert "syncoj-enroll.service" in message
    assert "enroll_code" not in message, "已经不存在这条路了，别再提它"


def test_unreadable_bootstrap_key_explains_root_only(
    agent_config: AgentConfig, workdir: Path
) -> None:
    """密钥文件在但读不出来时，要说清"这是设计如此"。

    否则运维会去改权限 —— 而把那把钥匙开放给选手读，等于把"无限注册"
    的能力交出去。
    """
    agent = make_agent(agent_config)
    key_path = workdir / "bootstrap.key"
    key_path.write_text("SECRET\n", encoding="utf-8")

    # 模拟"文件在但读不到"：read_bootstrap_key 返回空串
    import syncoj_agent.main as main_module

    original = main_module.read_bootstrap_key
    main_module.read_bootstrap_key = lambda path: ""  # type: ignore[assignment]
    try:
        from syncoj_agent.config import ConfigError

        with pytest.raises(ConfigError) as excinfo:
            agent.ensure_credential()
        assert "root" in str(excinfo.value)
    finally:
        main_module.read_bootstrap_key = original  # type: ignore[assignment]


def test_credential_from_another_server_is_not_reused(
    agent_config: AgentConfig,
) -> None:
    """换服务端之后旧凭据必须失效 —— 否则会拿着 A 的 token 去问 B。"""
    save_credential(
        agent_config.credential_path,
        Credential(token="stale", server_url="https://other.example", claimed=True),
    )
    agent = make_agent(agent_config)
    server = attach(agent, FakeServer())

    run_rounds(agent, 1)

    assert len(server.enroll_calls) == 1, "换了服务端必须重新注册"
