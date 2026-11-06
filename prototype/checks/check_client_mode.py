"""甲方模式 v1.1「规格优先 + 自愈 + 委托回传」自检。

用户定的目标（2026-11-05）：
    甲方模式 = **AI 完全从 0 生产代码，人不改代码**。在这个前提下尽力释放远端能力：
      1. 规格（接口/数据形状/错误语义/示例）由**最强档 + 思考开**产出
         —— 旧行为是"从最低档起生成再固化"，等于用最弱的算力做最高杠杆的决定；
      2. 规格有歧义时，甲方模式**自己拍板并把假设写进 purpose**（没人在回路里）；
         开发者模式**停下来问人**，不许猜；
      3. 单个模块允许**多轮内部自愈**（人不介入），但新一轮的失败证据与已用过的完全
         相同时立刻停 —— 同一个提示词再问一遍只是白烧钱；
      4. 机械活（样板/校验/日志/适配）允许远端 delegate 给本地模型；委托失败必须
         **回传给远端下一轮**，否则它会以为那块活已经有人做了。

全部只碰本地代码：`remote.complete` / `remote.available` 被替换成假实现，不发一次真请求。

跑法: python check_client_mode.py
"""
from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import author, company, contractgen, permissions, policy, remote, settings_store  # noqa: E402
from core import contract as contract_mod  # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    print(f"  {'OK  ' if cond else 'FAIL'} {name}" + (f"   {extra}" if extra and not cond else ""))
    if not cond:
        FAILS.append(name)


class Fake:
    """远端替身：记录调用参数，按脚本返回。"""

    def __init__(self, answers: list[str] | None = None):
        self.answers = list(answers or [])
        self.calls: list[dict] = []

    def available(self):
        return True, ""

    def complete(self, prompt, task_id="", node_id="", workspace="", emit=None,
                 thinking=None, step_index=None, json_mode=False, max_out=None, **_):
        self.calls.append({"prompt": prompt, "thinking": thinking, "step_index": step_index})
        ans = self.answers.pop(0) if self.answers else ""
        return {"ok": True, "answer": ans, "by": "fake:max", "usage": {}}


def patch(fake: Fake):
    """把 core.remote 的 complete/available 换成替身（所有模块共享同一个 module 对象）。"""
    old = (remote.complete, remote.available)
    remote.complete = fake.complete          # type: ignore[assignment]
    remote.available = fake.available        # type: ignore[assignment]
    return old


def unpatch(old) -> None:
    remote.complete, remote.available = old  # type: ignore[assignment]


def main() -> int:
    print("甲方模式 v1.1（规格优先 / 歧义策略 / 自愈收敛 / 委托回传）自检")

    # ---------------------------------------------------------------- 1 最强档
    print("[1] 规格走最强档 + 思考开")
    steps = settings_store.ladder()
    remote_idx = [i for i, s in enumerate(steps) if str(s.get("model") or "").strip()]
    want = remote_idx[-1] if remote_idx else 0
    check("strongest_step_index() = 阶梯里最后一个远端档",
          settings_store.strongest_step_index() == want,
          f"got={settings_store.strongest_step_index()} want={want}")
    check("阶梯访问住在 settings_store（不是 contractgen）",
          hasattr(settings_store, "ladder_step")
          and not hasattr(contractgen, "strongest_step_index"))
    _single = settings_store.single_effort()
    check("多档阶梯最强档 > 0；单档直通允许为 0",
          want > 0 or _single,
          f"strongest={want} single={_single}")

    fake = Fake([])
    old = patch(fake)
    try:
        contractgen.synthesize("写个计算器", step_index="strongest", thinking="on")
        call = fake.calls[-1]
        check("synthesize 把 'strongest' 解析成最强档并真的传下去",
              call["step_index"] == want, str(call["step_index"]))
        check("synthesize 把 thinking 传下去（规格该思考）", call["thinking"] == "on",
              str(call["thinking"]))

        # ------------------------------------------------------------ 2 歧义策略
        print("[2] 规格歧义：甲方自己拍板 / 开发者停下来问人")
        ok_json = ('{"modules":[{"path":"calc.py","purpose":"四则运算；假设：只支持 int",'
                   '"functions":[{"name":"add","params":[{"name":"a","type":"int"},'
                   '{"name":"b","type":"int"}],"returns":"int","desc":"相加",'
                   '"examples":["add(1, 2) == 3"]}]}]}')
        fake.answers = [ok_json]
        got = contractgen.synthesize("写个计算器", ambiguity="decide")
        check("decide 模式正常返回契约（『假设：』字样不影响解析）",
              got.get("ok") is True, str(got.get("problems"))[:120])
        check("契约里保留了远端写的假设（事后可追责）",
              "假设" in (got.get("text") or ""), (got.get("text") or "")[:80])

        fake.answers = ['{"question":"空输入返回什么？","options":["抛 ValueError","返回 0"],'
                        '"why":"影响判据"}']
        n_before = len(fake.calls)
        got = contractgen.synthesize("写个计算器", ambiguity="ask")
        check("ask 模式收到问题 → need_human（不当成失败去重试）",
              got.get("need_human") is True, str(got)[:120])
        check("问题原文带回给调用方", "空输入" in (got.get("question") or ""))
        check("ask 模式只发一次（不带着同一个问题反复问）",
              len(fake.calls) - n_before == 1, str(len(fake.calls) - n_before))
    finally:
        unpatch(old)

    check("decide 提示词要求把『假设：』写进 purpose",
          "假设" in contractgen._ambiguity_block("decide"))
    check("ask 提示词允许远端只回一个问题",
          "question" in contractgen._ambiguity_block("ask"))

    # ---------------------------------------------------------------- 3 自愈
    print("[3] 自愈轮数 + 『同证据即停』")
    sig = inspect.signature(author.author_module).parameters
    check("author_module 接受自愈轮数 attempts", "attempts" in sig)
    check("author_module 有『同证据即停』开关且默认开",
          "stop_on_repeat" in sig and sig["stop_on_repeat"].default is True)

    ws = Path(tempfile.mkdtemp())
    parsed = contract_mod.parse_requirement(
        "1) calc.py：\n   - add(a: int, b: int) -> int：两数相加\n"
        "示例（必须满足）：\nadd(1, 2) == 3\n")
    if "calc.py" not in parsed:
        check("check 自己的夹具能被 parse_requirement 解析", False, str(list(parsed)))
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    contract = parsed["calc.py"]

    calls: list[str] = []

    class SameFail(Fake):
        """每次都回同一份"差一点"的代码：验证不会拿同一个证据反复刺激远端。"""

        def complete(self, prompt, **kw):
            calls.append(prompt[:40])
            body = ('{"files":[{"path":"calc.py","content":'
                    '"def add(a: int, b: int) -> int:\\n    return a - b\\n"}]}')
            return {"ok": True, "by": "fake:max", "usage": {}, "answer": body}

    old = patch(SameFail())
    old_verify = author._verify
    author._verify = lambda *a, **k: {"ok": False, "feedback": "calc.py::add 期望 3 实际 -1"}
    try:
        r = author.author_module(ws, "calc.py", contract, {"calc.py": contract},
                                 attempts=4, stop_on_repeat=True)
    finally:
        unpatch(old)
        author._verify = old_verify
    check("同一失败证据在第 2 轮就被拦下（给了 4 轮只发了 2 次）",
          len(calls) == 2, f"calls={len(calls)}")
    check("提前停止写进 system_issues（不静默放弃）",
          any("证据完全相同" in str(x.get("issue")) for x in (r.get("system_issues") or [])),
          str(r.get("system_issues"))[:160])

    # ---------------------------------------------------------------- 4 委托
    print("[4] 机械活委托：整文件路径也要告诉远端 + 失败要回传")
    check("AUTHOR_PROMPT 提到 delegate", "delegate" in author.AUTHOR_PROMPT)
    check("AUTHOR_PROMPT 的 JSON 结构带 delegate 键", '"delegate"' in author.AUTHOR_PROMPT)
    _files, _issues, deleg = author._parse_reply(
        '{"files":[{"path":"a.py","content":"x=1"}],'
        '"delegate":[{"kind":"logging","target":"f","spec":"加日志"}]}')
    check("整文件回复里的 delegate 能被解析出来",
          len(deleg) == 1 and deleg[0]["target"] == "f", str(deleg))
    check("委托失败原因会拼进下一轮证据（回传给远端）",
          "委托失败" in inspect.getsource(author.author_module))

    # ---------------------------------------------------------------- 5 编排
    print("[5] 编排层真的用了这些开关 + 『一次通过率』")
    csrc = inspect.getsource(company)
    check("night 层按 policy 决定自愈轮数", "heal_attempts" in csrc)
    check("night 层把 stop_on_repeat 传下去", "stop_on_repeat" in csrc)
    check("规格生成按 policy 选择档位", 'pol.spec_tier' in csrc and "strongest" in csrc)
    check("规格歧义会让夜班停下并报人", "need_human" in csrc)
    check("统计里记『一次通过率』（没重试就全绿的模块占比）",
          "first_pass_rate" in csrc and "mod_first_pass" in csrc)
    # 写代码的起始档位：单模型下"从哪一档思考强度起"由 effort_mode 决定
    #   auto（甲方）→ 起档交给逐模块的难度路由（所以这里恒为 0，不是"固定最高档"）
    #   ladder（开发者）→ 第 0 档
    steps_now = settings_store.ladder()
    c_start = company._code_start_tier(policy.for_mode("client"), steps_now)
    d_start = company._code_start_tier(policy.for_mode("developer"), steps_now)
    check("甲方模式按难度自动路由（不写死最高档）",
          policy.for_mode("client").effort_mode == "auto" and c_start == 0, f"start={c_start}")
    check("开发者模式从最低强度起（省钱）",
          policy.for_mode("developer").effort_mode == "ladder" and d_start == 0, f"start={d_start}")
    check("逐模块难度路由存在且会把档位写进事件",
          "def _auto_effort" in csrc and '"effort_route"' in csrc)
    check("night 层把 effort_mode 传进 _author_round",
          "effort_mode=" in csrc and "start_tier=" in csrc)

    # ---------------------------------------------------------------- 6 人话
    permissions.set_mode("client")
    try:
        desc_c = policy.describe()
        perms_client = policy.for_mode("client")
    finally:
        permissions.set_mode("developer")
    check("describe() 说清规格走最强档", "规格由最强档" in desc_c, desc_c)
    check("甲方模式自愈轮数 ≥ 3", perms_client.heal_attempts >= 3, str(perms_client.heal_attempts))
    check("开发者模式仍是『歧义问人 + 人定范围』",
          policy.for_mode("developer").ambiguity == "ask"
          and policy.for_mode("developer").scope_authority == "human")

    # ---------------------------------------------------------------- 7 空契约
    print("[7] 『解析出模块但 0 个函数』不许静默空跑")
    check("_contract_func_count 对空契约返回 0", company._contract_func_count({}) == 0)
    _zero = contract_mod.parse_requirement(
        "1) cal/core/numeric.py：\n   - 求和\n   - 安全除法\n")
    check("只有模块名+行为清单 → 0 个函数（这不算契约）",
          company._contract_func_count(_zero) == 0,
          str({k: len(getattr(v, 'funcs', []) or []) for k, v in _zero.items()}))
    _real = contract_mod.parse_requirement(
        "1) calc.py：\n   - add(a: int, b: int) -> int：相加\n示例（必须满足）：\nadd(1, 2) == 3\n")
    check("正常契约 → 计数 > 0", company._contract_func_count(_real) >= 1)
    # 实现在 _night_inner 里（night() 只是包了一层保证写交付报告的壳）
    _impl = getattr(company, "_night_inner", None)
    check("找得到夜班实现（_night_inner）", _impl is not None)
    _nsrc = inspect.getsource(_impl) if _impl else ""
    check("夜班把空契约当成『没解析出来』继续走远端合成",
          "_contract_func_count(contracts)" in _nsrc, _nsrc[:0])
    check("夜班最后还有一道『0 个函数就如实失败』的兜底",
          "契约里 0 个函数" in _nsrc, _nsrc[:0])

    print()
    if FAILS:
        print(f"有失败项：{', '.join(FAILS)}")
        return 1
    print("全部通过（规格最强档 / 歧义策略 / 自愈收敛 / 委托回传 / 一次通过率）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
