"""自检：片内自验闭环（给作者一个受限 shell）。

这一层存在的唯一理由：**同一模型，差别在信息**。会话里的模型能"写一行就跑、看真输出、
再改"；流水线里的作者是盲写的，事后只拿到一句判红，然后整片重来。
所以这里把"跑"接回作者，但用硬边界焊死——自检必须同时钉住**能力**和**边界**：

1. 命令通道真能跑（真执行、真退出码、真 stdout/stderr、真超时）；
2. 边界不许破（管道/重定向/串联/删除/联网/非 python 一律拒，cwd 锁在仓库内）；
3. 文件通道只许改本片涉及的模块（越界、空文件、省略号一律拒）；
4. 证据必须是**探针的真实输出**，不许重跑出一个假现场（探针在临时目录里准备素材，
   在仓库根重跑会得到"文件不存在"，作者会照着假证据把好代码改坏）；
5. 判定权在探针：作者说 @DONE 不算数；
6. 空转/预算用尽/步数到顶都要停，不许自己加餐；
7. 接到全自主流程里要有账，且真能把红的能力修绿。

跑法: python check_selfcheck_loop.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core import selfcheck_loop as sl                     # noqa: E402

OK = 0


def ck(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if not cond:
        print(f"[FAIL] {name}" + (f"：{detail}" if detail else ""))
        sys.exit(1)
    OK += 1
    print(f"[OK]   {name}")


# ---------------------------------------------------------------- 1. 命令通道

def test_cmd_gate() -> None:
    ck("允许 python", sl.check_cmd("python main.py count", ".") == "")
    ck("允许 pytest", sl.check_cmd("pytest -q", ".") == "")
    for bad, why in [
        ("python a.py | head", "管道"),
        ("python a.py > out.txt", "重定向"),
        ("python a.py && python b.py", "串联"),
        ("python a.py; rm -rf x", "串联"),
        ("del x.py", "删除"),
        ("curl http://x", "联网"),
        ("pip install requests", "装包"),
        ("powershell -c dir", "换壳"),
        ("ls -la", "非 python"),
        ("", "空命令"),
    ]:
        ck(f"拒绝{why}：{bad[:22]}", sl.check_cmd(bad, ".") != "")
    ck("拒绝理由能告诉作者怎么写（提到 python）",
       "python" in sl.check_cmd("ls -la", "."))


def test_run_cmd() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "ok.py").write_text("print('hello-42')\n", encoding="utf-8")
        r = sl.run_cmd(ws, "python ok.py")
        ck("真的跑起来了", r.get("ok") and r.get("exit") == 0, str(r)[:200])
        ck("拿到真实 stdout", "hello-42" in (r.get("out") or ""))
        (ws / "bad.py").write_text("import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n",
                                   encoding="utf-8")
        r2 = sl.run_cmd(ws, "python bad.py")
        ck("拿到真实退出码 3", r2.get("exit") == 3, str(r2.get("exit")))
        ck("拿到真实 stderr", "boom" in (r2.get("err") or ""))
        bad = sl.run_cmd(ws, "python ok.py | head")
        ck("越界命令不执行（标记 rejected）", bad.get("rejected") is True)
        ck("越界命令没有输出证据", not (bad.get("out") or ""))
        # 超时：把上限临时压到 0.5s
        old = sl.CMD_TIMEOUT
        try:
            sl.CMD_TIMEOUT = 0.5
            (ws / "slow.py").write_text("import time\ntime.sleep(3)\n", encoding="utf-8")
            r3 = sl.run_cmd(ws, "python slow.py")
            ck("死循环/等待会被超时掐掉", "超时" in (r3.get("err") or ""), str(r3)[:160])
        finally:
            sl.CMD_TIMEOUT = old


def test_evidence_format() -> None:
    ev = [{"cmd": "python a.py", "exit": 1, "out": "x" * 4000, "err": "Traceback: keyerror"},
          {"cmd": "python b.py", "exit": 0, "out": "", "err": ""},
          {"cmd": "ls", "rejected": True, "err": "只允许 python"}]
    s = sl.format_evidence(ev)
    ck("证据里有命令原文", "python a.py" in s)
    ck("证据里有退出码", "退出码：1" in s and "退出码：0" in s)
    ck("证据里有 stderr", "Traceback" in s)
    ck("超长输出被截断", len(s) < 6000, str(len(s)))
    ck("截断有明确提示", "省略" in s)
    ck("空输出如实写空", "（空）" in s)
    ck("被拒的命令也如实回报", "被拒绝" in s)


# ---------------------------------------------------------------- 2. 文件通道

def test_files() -> None:
    text = ("说明文字\n"
            "=== FILE: cli_core.py\n"
            "def cli_run(argv):\n    return 0, '2'\n"
            "=== END\n"
            "=== FILE: other.py\nx=1\n=== END\n"
            "=== FILE: ../escape.py\ny=1\n=== END\n"
            "=== FILE: cli_core.py\n...\n...\n=== END\n")
    writes, rejected = sl.parse_files(text, ["cli_core.py"])
    ck("能取出整份文件", "cli_core.py" in writes, str(list(writes)))
    ck("内容逐字保留", "def cli_run" in writes["cli_core.py"])
    ck("文件内容以换行结尾（避免语法噪音）", writes["cli_core.py"].endswith("\n"))
    ck("没声明的路径被拒", any("other.py" in r for r in rejected), str(rejected))
    ck("越出仓库的路径被拒", any("escape" in r for r in rejected), str(rejected))
    ck("省略号占位被拒（防止只交半份代码）",
       any("省略号" in r for r in rejected), str(rejected))
    ck("只写一份（后一份非法不覆盖前一份）", len(writes) == 1)

    t2 = "=== FILE: ./a/b.py\nprint(1)\n=== END"
    w2, _ = sl.parse_files(t2, ["a/b.py"])
    ck("./ 前缀会被归一", "a/b.py" in w2, str(list(w2)))
    t3 = "=== FILE: a\\b.py\nprint(1)\n=== END"
    w3, _ = sl.parse_files(t3, ["a/b.py"])
    ck("反斜杠路径会被归一", "a/b.py" in w3, str(list(w3)))

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        done = sl.apply_files(ws, {"a/b.py": "print(1)\n"})
        ck("落盘成功（含建目录）", done == ["a/b.py"] and (ws / "a/b.py").is_file())
        ck("越界写入被挡", sl.apply_files(ws, {"../evil.py": "x\n"}) == []
           and not (Path(td).parent / "evil.py").exists())


# ---------------------------------------------------------------- 3. 判定权

def _ws_with_entry(td: Path, body: str) -> Path:
    ws = Path(td)
    (ws / "cli_core.py").write_text(body, encoding="utf-8")
    (ws / "main.py").write_text(
        "import sys\nfrom cli_core import cli_run\n"
        "code, text = cli_run(sys.argv[1:])\n"
        "print(text)\nsys.exit(code)\n", encoding="utf-8")
    return ws


def _cap():
    from core import blueprint as bpmod
    return bpmod.Cap(id="cap.cli.count", desc="count 打 2",
                     probe={"kind": "cli", "entry": "main.py", "args": ["count"],
                            "stdout_has": "2"},
                     deps=[], funcs_est=1, modules=["cli_core.py"])


WRONG = "def cli_run(argv):\n    return 0, '0'\n"
RIGHT = "def cli_run(argv):\n    return 0, '2'\n"


def test_verify_and_evidence() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        res = sl.verify_caps(ws, [c])
        ck("探针把错的判红", res[c.id]["ok"] is False)
        ev = sl.evidence_from_verify(ws, [c], res)
        ck("给出证据条", len(ev) == 1)
        ck("证据里是真实 stdout（'0'）", "0" in (ev[0].get("out") or ""), str(ev[0])[:200])
        ck("证据带退出码", ev[0].get("exit") == 0)
        ck("证据带验收命令", "main.py" in (ev[0].get("cmd") or ""))
        # 证据必须来自探针，不是重跑：探针在临时目录准备素材，重跑会得到别的现场
        ck("没有第二条命令被额外执行（不重跑造现场）",
           "d.csv" not in (ev[0].get("out") or ""))

    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), RIGHT)
        c = _cap()
        res = sl.verify_caps(ws, [c])
        ck("探针把对的判绿", res[c.id]["ok"] is True)
        ck("通过的能力不产生证据（不浪费预算）",
           sl.evidence_from_verify(ws, [c], res) == [])


class ScriptLLM:
    """按脚本回话的假远端（不联网络）。"""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt, **kw):
        self.prompts.append(prompt)
        if not self.answers:
            return {"ok": True, "answer": ""}
        return {"ok": True, "answer": self.answers.pop(0)}


def test_repair_fixes_it() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        llm = ScriptLLM([
            "@RUN python main.py count\n"
            "=== FILE: cli_core.py\n" + RIGHT + "=== END\n@DONE\n",
        ])
        em: list[tuple[str, dict]] = []
        got = sl.repair(ws, [c], llm=llm, emit=lambda k, p=None: em.append((k, p or {})))
        ck("修好了", got["ok"] is True, str(got.get("stop")))
        ck("记为已修好", got["fixed"] == [c.id])
        ck("没有剩余红", got["still"] == [])
        ck("写了文件", "cli_core.py" in (got.get("changed") or []))
        ck("停机原因是全部通过", got["stop"] == "全部通过", got.get("stop"))
        ck("真跑了作者的 @RUN", any(k == "selfcheck_run" for k, _ in em), str([k for k, _ in em]))
        ck("每步都有留痕", any(k == "selfcheck_step" for k, _ in em))
        ck("提示词里有真实输出（不是让模型自评）",
               "退出码" in (llm.prompts[0] if llm.prompts else ""), "提示词缺证据")

    # 作者说完成但没改任何东西 → 不许算过
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        llm = ScriptLLM(["@DONE\n"])
        got = sl.repair(ws, [c], llm=llm)
        ck("空白 @DONE 不算过（判定权在探针）",
           got["ok"] is False and got["still"] == [c.id])
        ck("空转会被停下来", "空转" in got["stop"], got.get("stop"))

    # 改了声明的模块之外的路径 → 拒绝，且不打回扣
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        em2: list[tuple[str, dict]] = []
        llm = ScriptLLM(["=== FILE: sneaky.py\nx=1\n=== END\n"])
        got = sl.repair(ws, [c], llm=llm, emit=lambda k, p=None: em2.append((k, p or {})))
        ck("越界写入被拒并留痕", any(k == "selfcheck_reject" for k, _ in em2))
        ck("越界写入真的没落盘", not (ws / "sneaky.py").exists())
        ck("越界写入修不好能力", got["ok"] is False)

    # 没有可改的模块 → 直接说清楚，不乱花钱
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        c = _cap()
        llm = ScriptLLM(["x"])
        got = sl.repair(ws, [c], llm=llm)
        ck("没模块时不动手", "没有可改的模块" in got["stop"], got.get("stop"))
        ck("没模块时不花远端调用", not llm.prompts)

    # 预算上限机制已删（2026-11-10）：repair() 不再收预算参数、不再按钱停——静态钉死
    import inspect as _insp
    _rp = set(_insp.signature(sl.repair).parameters)
    ck("repair 不再收 budget_cny / cost_used 参数",
       not ({"budget_cny", "cost_used"} & _rp), str(sorted(_rp)))

    # 步数到顶 → 如实报仍红
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        llm = ScriptLLM(["@RUN python main.py count\n"] * 5)
        got = sl.repair(ws, [c], llm=llm, max_steps=2)
        ck("步数到顶就停", got["steps"] <= 2, str(got["steps"]))
        ck("没修好如实说仍红", c.id in got["still"])
        ck("步数上限真的约束了调用次数", len(llm.prompts) <= 2, str(len(llm.prompts)))


# ---------------------------------------------------------------- 4. 接进全自主流程

def test_into_auto() -> None:
    import inspect
    from core import auto, contractgen

    contractgen.synthesize = lambda requirement, **kw: {
        "ok": True, "contracts": [], "text": "# 假契约\n",
        "by": "fake", "problems": [], "allowance": 8, "allowance_why": ["测试"]}

    sig = inspect.signature(auto.night_auto)
    ck("night_auto 收 selfcheck 开关", "selfcheck" in sig.parameters)
    src = inspect.getsource(auto.night_auto)
    ck("在切片之后接上（不是别处）", "selfcheck_loop.repair" in src)
    ck("改完必须重新观测（作者说好不算数）",
       src.index("selfcheck_loop.repair") < src.index('covmod.record(ws, cov, f"片内自验'))

    CAPS = [{"id": "cap.cli.count", "desc": "count 打 2",
             "probe": {"kind": "cli", "entry": "main.py", "args": ["count"],
                       "stdout_has": "2"},
             "deps": [], "funcs_est": 1, "modules": ["cli_core.py"]}]

    def fake_llm_factory(broken: bool):
        def _bp_llm(prompt, **kw):
            if "独立评审" in prompt:
                obj = {"issues": []}
            elif "你能真的跑命令" in prompt:          # 片内自验那一路
                if broken:
                    return {"ok": True, "answer": "=== FILE: cli_core.py\n" + RIGHT + "=== END\n"}
                return {"ok": True, "answer": ""}
            else:
                obj = {"caps": CAPS}
            return {"ok": True, "answer": "```json\n" + json.dumps(obj, ensure_ascii=False) + "\n```"}
        return _bp_llm

    def fake_runner(ws, brief, contract_path, **kw):
        (Path(ws) / "cli_core.py").write_text(WRONG, encoding="utf-8")
        return {"ok": True, "reason": "假执行器", "verified": 0, "total": 1,
                "spent_s": 0.1, "cost_cny": 0.01, "delivery": ""}

    def fake_recon(bp, ws):
        """假观测：形状必须与 coverage.run 完全一致。

        这里踩过一次：只造了 {"at","caps"}，于是 ratios() 读到空的 done/total 报 0/0 ——
        机制明明是好的，是**测试替身不真实**。替身失真比没有替身更坏。
        """
        from core import probe as p
        st, done = {}, []
        for c in bp.caps:
            r = p.run(ws, c.probe)
            ok = bool(r.get("ok"))
            st[c.id] = {"status": "done" if ok else "absent",
                        "why": "" if ok else str(r.get("detail") or "")[:80]}
            if ok:
                done.append(c.id)
        return {"at": 0.0, "version": 1, "caps": st,
                "total": len(bp.caps), "done": sorted(done)}

    def run_once(selfcheck: bool, broken: bool):
        td = tempfile.mkdtemp(prefix="sc-")
        res = auto.night_auto(Path(td), "做一个计数器", max_slices=1,
                              llm=fake_llm_factory(broken), runner=fake_runner,
                              recon=fake_recon, selfcheck=selfcheck,
                              emit=lambda k, p=None: None)
        return res, Path(td)

    res_on, _ = run_once(True, broken=True)
    res_off, _ = run_once(False, broken=True)
    ck("开自验：能力被修绿", res_on["ratios"]["done"] >= 1, str(res_on["ratios"]))
    ck("关自验：同样的坏代码修不绿（对照组成立）",
       res_off["ratios"]["done"] == 0, str(res_off["ratios"]))
    ck("报告里有自验账", bool(res_on.get("selfchecks")), str(res_on.get("selfchecks"))[:200])
    ck("账里有修好几条", res_on.get("selfcheck_fixed", 0) >= 1)
    ck("关掉时没有账（不虚报）", not res_off.get("selfchecks"))


def test_pycache_trap() -> None:
    """真 bug 回归钉：同尺寸同秒改写会被旧 .pyc 毒化（正确实现被判红）。

    pyc 头里存的是**秒级** mtime + 源码字节数。作者自己跑 `python xxx.py` 会生成 pyc；
    紧接着引擎改写同尺寸内容（只差一个字符），在同一秒内完成 —— 两个校验字段都没变，
    CPython 认为缓存有效，继续跑旧字节码。`-B` 只拦"写"，不拦"读"。
    """
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        r1 = sl.run_cmd(ws, "python main.py count")
        ck("作者跑命令能看到自己的真实输出", "0" in (r1.get("out") or ""), repr(r1.get("out")))
        ck("跑命令不产生 pycache（不然会毒化探针）", not (ws / "__pycache__").exists())
        ck("同尺寸改写前：探针判红", sl.verify_caps(ws, [_cap()])[_cap().id]["ok"] is False)
        sl.apply_files(ws, {"cli_core.py": RIGHT})       # 与 WRONG 等长，且几乎同一时刻
        r2 = sl.run_cmd(ws, "python main.py count")
        ck("同尺寸同秒改写后立刻读到新代码", "2" in (r2.get("out") or ""), repr(r2.get("out")))
        ck("探针也立刻读到新代码（不再被旧字节码骗）",
           sl.verify_caps(ws, [_cap()])[_cap().id]["ok"] is True)
        cache = ws / "__pycache__"
        cache.mkdir(exist_ok=True)
        stale = cache / "cli_core.cpython-999.pyc"
        stale.write_bytes(b"\x00")
        sl.apply_files(ws, {"cli_core.py": WRONG})
        ck("写入模块会顺手清掉该模块的字节码缓存", not stale.exists())


# ---------------------------------------------------------------- 5. 多轮（省 token、保上下文）

def test_multi_turn_wire() -> None:
    """`remote.complete` 必须真的把多轮 messages 发出去（不是只收不用）。"""
    from core import guards, remote
    msgs = [{"role": "user", "content": "第一轮：契约与代码"},
            {"role": "assistant", "content": "我交了一版"},
            {"role": "user", "content": "第二轮：这是你刚跑出来的结果"}]
    step = {"profile_id": "p", "effort": "none", "model": "fake-model",
            "base_url": "http://x", "api_key": "k", "pricing": {}}
    seen: list[dict] = []
    old = (remote.available, remote.ladder, remote._post, remote._record,
           remote.settings_store.get_remote, guards.gate_remote_call)
    try:
        remote.available = lambda: (True, "")
        remote.ladder = lambda *a, **k: [step]
        remote.settings_store.get_remote = lambda: {}
        guards.gate_remote_call = lambda *a, **k: {"ok": True}
        remote._record = lambda **kw: None

        def fake_post(payload, base, key):
            seen.append(payload)
            return ({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5}}, "", 1)
        remote._post = fake_post
        r = remote.complete("", messages=msgs, task_id="t", node_id="n")
        ck("多轮调用成功", r.get("ok") is True, str(r.get("error")))
        ck("请求体里带的是完整多轮历史",
           seen and seen[0]["messages"] == msgs, str(seen[0]["messages"] if seen else None))
        ck("assistant 轮次没被吞掉",
           any(m["role"] == "assistant" for m in (seen[0]["messages"] if seen else [])))
        ck("不再退化成单条 user 消息",
           len(seen[0]["messages"]) == 3, str(len(seen[0]["messages"]) if seen else 0))
        # 单轮仍照旧（不能因为加了多轮入口就破坏老调用）
        seen.clear()
        r2 = remote.complete("一句话", task_id="t", node_id="n")
        ck("单轮调用仍然是单条 user 消息",
           r2.get("ok") is True and [m["role"] for m in seen[0]["messages"]] == ["user"])
        ck("单轮内容就是 prompt 本身", seen[0]["messages"][0]["content"] == "一句话")
    finally:
        (remote.available, remote.ladder, remote._post, remote._record,
         remote.settings_store.get_remote, guards.gate_remote_call) = old


def test_fit_messages() -> None:
    from core import remote
    small = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    ck("没超限就原样返回", remote._fit_messages(small, 1000) == small)
    big = [{"role": "user", "content": "契约与代码" * 100}]
    for i in range(6):
        big += [{"role": "assistant", "content": f"第{i}版" * 100},
                {"role": "user", "content": f"证据{i}" * 100}]
    out = remote._fit_messages(big, 1200)
    ck("超限会收缩", len(out) < len(big), f"{len(out)}/{len(big)}")
    ck("第一条（契约/代码）保住", out[0] == big[0])
    ck("最后一轮（最新证据）保住", out[-1] == big[-1])
    ck("中间被丢时留了醒目标记",
       any("省略" in (m.get("content") or "") for m in out), str(out)[:200])
    ck("收缩后整体不超限太多",
       sum(len(m["content"]) for m in out) <= 1200 + 400,
       str(sum(len(m["content"]) for m in out)))


class ConvoLLM:
    """记录多轮历史的假远端。"""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.histories: list[list[dict]] = []
        self.flat_prompts: list[str] = []

    def __call__(self, prompt, *, messages=None, **kw):
        self.flat_prompts.append(prompt)
        self.histories.append([dict(m) for m in (messages or [])])
        return {"ok": True, "answer": self.answers.pop(0) if self.answers else ""}


def test_repair_multi_turn() -> None:
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        llm = ConvoLLM([
            "@RUN python main.py count\n",            # 第 1 步：先跑一下看真实输出
            "=== FILE: cli_core.py\n" + RIGHT + "=== END\n@DONE\n",   # 第 2 步：照着证据改好
        ])
        got = sl.repair(ws, [c], llm=llm, max_steps=4)
        ck("多轮也修得好", got["ok"] is True, str(got.get("stop")))
        ck("调了两次", len(llm.histories) == 2, str(len(llm.histories)))
        h1, h2 = llm.histories
        ck("第一轮只有一条 user（契约+代码+证据）", len(h1) == 1, str(len(h1)))
        ck("第二轮带上了上一轮的回复（模型看得见自己交了什么）",
           len(h2) == 3 and h2[1]["role"] == "assistant", str([m["role"] for m in h2]))
        ck("第二轮没有重发整份代码（省 token 的关键）",
           "当前代码（你要在此基础上修" not in h2[-1]["content"], h2[-1]["content"][:80])
        ck("第二轮给的是新事实", "刚跑出来的结果" in h2[-1]["content"])
        ck("第二轮带着退出码/输出（真实证据）", "退出码" in h2[-1]["content"])

    # 老签名（不支持多轮）要能退化，不许炸
    with tempfile.TemporaryDirectory() as td:
        ws = _ws_with_entry(Path(td), WRONG)
        c = _cap()
        calls = {"n": 0}

        def old_llm(prompt):                      # 注意：没有 messages 参数
            calls["n"] += 1
            if calls["n"] == 1:
                return {"ok": True, "answer": "@RUN python main.py count\n"}
            return {"ok": True, "answer": "=== FILE: cli_core.py\n" + RIGHT + "=== END\n"}

        got = sl.repair(ws, [c], llm=old_llm, max_steps=3)
        ck("不支持多轮的远端会退化成单轮", got["ok"] is True, str(got.get("stop")))
        ck("退化路径也真的调到了远端", calls["n"] >= 2, str(calls["n"]))


if __name__ == "__main__":
    try:
        import sys as _sys
        _sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
        from core.console import encode_stdout
        encode_stdout()
    except Exception:                          # noqa: BLE001 —— 编码修复失败不许成为新的失败点
        pass
    print(f"== 片内自验闭环自检（{ROOT}）==")
    test_cmd_gate()
    test_run_cmd()
    test_evidence_format()
    test_files()
    test_verify_and_evidence()
    test_pycache_trap()
    test_repair_fixes_it()
    test_multi_turn_wire()
    test_fit_messages()
    test_repair_multi_turn()
    test_into_auto()
    print(f"\n共 {OK} 项断言全部通过")
