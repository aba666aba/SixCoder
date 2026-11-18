"""自检：前期架构问答（阶段 1）。

钉住四件事——每一条都对应一个真实失败现场：
1. **问得对**：只问能改变架构/判据的问题，每个都带默认值，至少两条要人给验收样例；
   问题写得再漂亮，只要人答了它、架构还是照旧，那就是白问。
2. **答得进**：人的安排必须真的进到目标/契约语境里（不是存成文件就完事）。
3. **样例变判据**：`kind=example` 的回答要能变成 specsample 文本，且能被 parse 成能力。
4. **不答也能跑**：人不回答就用默认安排，且架构文本要写明哪条是默认（事后可追责）。
另外钉住持久化（问题/答案/架构三份可溯源）与"问题缓存不重复花钱"。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from core import architect, specsample                      # noqa: E402

OK = 0


def ck(name: str, cond: bool, detail: str = "") -> None:
    global OK
    if not cond:
        print(f"[FAIL] {name}" + (f"：{detail}" if detail else ""))
        sys.exit(1)
    OK += 1
    print(f"[OK]   {name}")


RAW = {"questions": [
    {"id": "Q1", "q": "明确不做的是什么？", "why": "防范围膨胀", "kind": "text",
     "default": "只做 CSV 统计，不做清洗"},
    {"id": "Q2", "q": "入口文件名和参数怎么定？", "why": "入口错了全是重做", "kind": "choice",
     "options": ["csvstat.py --file", "csvstat.py --path"], "default": "csvstat.py --file"},
    {"id": "Q3", "q": "空字段怎么算？", "why": "口径不同结果全错", "kind": "text"},
    {"id": "Q4", "q": "给一条成功路径样例", "why": "最硬的判据", "kind": "example",
     "default": ""},
    # 口味问题：必须被"最多/只问"的规则排除不掉，但空问题与种类错乱要能容错
    {"id": "Q5", "q": "", "why": "空的"},
    {"id": "Q6", "q": "少字段文件该报什么？", "why": "错误路径最容易各写各的",
     "kind": "weird", "default": "退出码 1 并给行号"},
]}


def test_normalize() -> None:
    qs = architect.normalize(RAW["questions"])
    ids = [q["id"] for q in qs]
    ck("空问题被丢掉", "Q5" not in ids, str(ids))
    ck("未知 kind 归一成 text",
       next(q for q in qs if q["id"] == "Q6")["kind"] == "text")
    ck("没有默认值的 text 问题保留（允许人后补，仍可跑）",
       any(q["id"] == "Q3" and q["default"] == "" for q in qs))
    ck("choice 缺默认值时取第一个选项",
       next(q for q in qs if q["id"] == "Q2")["default"] == "csvstat.py --file")
    ck("自动补齐到至少两条验收样例问题",
       sum(1 for q in qs if q["kind"] == "example") >= 2,
       str([q["kind"] for q in qs]))
    ck("补齐的问题也带 id 且不撞号", len({q["id"] for q in qs}) == len(qs))
    ck("问题数不超过上限", len(qs) <= architect.MAX_QUESTIONS, str(len(qs)))
    many = {"questions": [{"id": f"Q{i}", "q": f"问题{i}", "kind": "text", "default": "d"}
                          for i in range(1, 30)]}
    ck("超量问题被截断到上限",
       len(architect.normalize(many["questions"])) == architect.MAX_QUESTIONS)


def test_ask() -> None:
    seen: list[str] = []

    def fake_llm(prompt: str, **kw):
        seen.append(prompt)
        return {"ok": True, "answer": "```json\n" + json.dumps(RAW) + "\n```"}

    got = architect.ask("做一个 CSV 统计工具", llm=fake_llm)
    ck("提问成功", got["ok"] and got["calls"] == 1)
    ck("只花一次调用", len(seen) == 1)
    p = seen[0]
    ck("提示词里带上了目标", "做一个 CSV 统计工具" in p)
    ck("提示词明令最多几个问题", str(architect.MAX_QUESTIONS) in p)
    ck("提示词明令不许问口味问题", "口味问题" in p)
    ck("提示词明令至少两条验收样例", "example" in p and "至少 2 个" in p)
    ck("提示词明令每个问题带默认值", "default" in p)
    ck("提问失败不算成功", not architect.ask("x", llm=lambda *a, **k: {"ok": False})["ok"])
    ck("空目标直接拒（不花钱）",
       not architect.ask("  ", llm=lambda *a, **k: (_ for _ in ()).throw(AssertionError()))["ok"])


def test_answers_flow() -> None:
    qs = architect.normalize(RAW["questions"])
    ans = {"Q1": "只做 CSV 统计", "Q2": "csvstat.py --path",
           "Q4": "$ python csvstat.py --file a.csv\n<<< a.csv\nname,score\nA,3\nB,5\n>>>\n--- expect\nA 3\nB 5"}
    md = architect.render_qa(qs, ans)
    ck("架构安排写明『人给的，权威』", "人给的，权威" in md)
    ck("人答的标记为人答", "[Q1·人答]" in md, md[:200])
    ck("没答的标记为默认", "[Q3·默认]" in md, md[:400])
    ck("两种来源都在", "人答" in md and "默认" in md)
    ck("答案真的写进去了", "csvstat.py --path" in md)
    ck("没答的那条用建议默认填上（不答也能跑）", "退出码 1 并给行号" in md, md[-300:])
    ck("人答了就不再用默认（答案覆盖默认）", "只做 CSV 统计，不做清洗" not in md)
    goal = architect.merge_goal("做一个 CSV 统计工具", qs, ans)
    ck("架构安排并进了目标", "做一个 CSV 统计工具" in goal and "架构安排" in goal)
    ck("目标里带上了人答的接口安排", "csvstat.py --path" in goal)

    # 验收样例：只有 example 类的回答才变样例，且能被 specsample 解析成能力
    sam = architect.samples_from(qs, ans)
    ck("只把 example 类的回答变样例", sam.count("### ") == 1, sam[:80])
    samples = specsample.parse(sam)
    ck("样例能被解析", len(samples) == 1, str(len(samples)))
    caps = specsample.to_caps(samples)
    ck("样例变成了能力", len(caps) >= 1)
    ck("样例能力是可执行 cli 探针",
       all((c.probe.get("kind") or c.probe.get("type")) == "cli" for c in caps), str(caps[0].probe))
    ck("样例能力带命令行 args（不是空探针）", all(c.probe.get("args") for c in caps))
    ck("入口被识别出来（用于固定入口文件名）",
       (specsample.entries(samples) or [""])[0].endswith("csvstat.py"))

    # 空答不发样例（避免造空能力）
    ck("没有 example 回答就没有样例", architect.samples_from(qs, {}) == "")


def test_answer_formats() -> None:
    """人写答案不该被格式卡住：JSON 与 `Q1:` 逐行都要认。"""
    d = architect.parse_answers('{"Q1": "只做统计", "Q2": "csvstat.py --path"}')
    ck("JSON 字典能读", d.get("Q1") == "只做统计" and d.get("Q2") == "csvstat.py --path")
    d2 = architect.parse_answers("Q1: 只做统计\nQ2: csvstat.py --path\n不带编号的噪声行\n")
    ck("逐行写法能读", d2.get("Q1") == "只做统计" and d2.get("Q2") == "csvstat.py --path",
       str(d2))
    ck("噪声行不被当成问题", "不带编号的噪声行" not in "".join(d2.keys()))
    d3 = architect.parse_answers("Q4: $ python a.py --file x.csv\n<<< x.csv\nn,s\nA,1\n>>>\n"
                                 "--- expect\nA 1")
    ck("多行答案（验收样例）整段保留",
       "--- expect" in d3.get("Q4", "") and "A,1" in d3.get("Q4", ""), str(d3)[:120])
    ck("全角冒号也认", architect.parse_answers("Q1：中文冒号").get("Q1") == "中文冒号")
    ck("空文本读成空字典（不炸）", architect.parse_answers("") == {})


def test_persist() -> None:
    qs = architect.normalize(RAW["questions"])
    ans = {"Q2": "csvstat.py --path"}
    with tempfile.TemporaryDirectory() as td:
        wrote = architect.save(td, qs, ans, "做一个 CSV 统计工具")
        ck("三份都落盘", set(wrote) == {architect.QUESTIONS_FILE, architect.ANSWERS_FILE,
                                       architect.ARCH_FILE}, str(wrote))
        for f in wrote:
            ck(f"{f} 真的存在", (Path(td) / f).exists())
        back = architect.load_questions(td)
        ck("问题能读回来", len(back) == len(qs))
        ck("答案能读回来", architect.load_answers(td).get("Q2") == "csvstat.py --path")
        ck("架构文本可读（人审那一页）",
           "csvstat.py --path" in (Path(td) / architect.ARCH_FILE).read_text(encoding="utf-8"))
        ck("缺文件时读成空（不炸）",
           architect.load_questions(td + "_nope") == [] and architect.load_answers(td + "_nope") == {})


def test_into_auto() -> None:
    """架构安排必须真的进到全自主流程（否则这一层就是装饰）。"""
    from core import auto
    import inspect
    sig = inspect.signature(auto.night_auto)
    ck("night_auto 收 arch 参数", "arch" in sig.parameters)
    src = inspect.getsource(auto.night_auto)
    ck("derive 之前就并进目标（不能事后补）",
       src.index("architect.merge_goal") < src.index("blueprint.derive"), "顺序不对")
    ck("验收样例会升级成权威判据", "architect.samples_from" in src)
    ck("有架构事件上报", 'emit("architecture"' in src)


# ---------------------------------------------------------------- 端到端（假远端，不花钱）
CAPS = [{"id": "cap.cli.count", "desc": "统计 CSV 条数",
         "probe": {"kind": "cli", "entry": "main.py", "args": ["count", "--file", "d.csv"],
                   "files": {"d.csv": "name\na\nb\n"}, "stdout_has": "2"},
         "deps": [], "funcs_est": 6, "modules": ["main.py"]}]


def test_end_to_end() -> None:
    """真跑一遍：人的安排要出现在**蓝图提示词**和**每片简报**里。

    这是这一层唯一真正要紧的事：问出来的安排如果没进到写代码那一步，问也白问。
    """
    import json as _json
    from core import auto
    from core import blueprint as bpm
    from core import contractgen, coverage as covmod

    # 全程不联网络：契约合成也换掉（否则这一条会真的打远端，自检就不可重复了）
    contractgen.synthesize = lambda requirement, **kw: {
        "ok": True, "contracts": [], "text": "# 假契约\n",
        "by": "fake", "problems": [], "allowance": 8, "allowance_why": ["测试"]}

    seen: list[str] = []

    def fake_llm(prompt, **kw):
        seen.append(prompt)
        obj = ({"issues": []} if "独立评审" in prompt else {"caps": CAPS})
        return {"ok": True, "answer": "```json\n" + _json.dumps(obj, ensure_ascii=False) + "\n```"}

    briefs: list[str] = []

    def fake_runner(ws, brief, contract_path, **kw):
        briefs.append(brief)
        return {"ok": True, "reason": "假执行器", "verified": 0, "total": 1, "spent_s": 0.1,
                "cost_cny": 0.01, "delivery": ""}

    def fake_recon(bp, ws):
        return {"at": 0.0, "caps": {c.id: {"status": "absent", "why": ""} for c in bp.caps}}

    qs = architect.normalize(RAW["questions"])
    ans = {"Q2": "csvstat.py --path", "Q3": "空字段算 0"}
    em: list[tuple[str, dict]] = []
    with tempfile.TemporaryDirectory() as td:
        res = auto.night_auto(Path(td), "做一个 CSV 统计工具", max_slices=1,
                              llm=fake_llm, runner=fake_runner, recon=fake_recon,
                              emit=lambda k, p=None: em.append((k, p or {})),
                              arch={"questions": qs, "answers": ans})
        ck("流程跑起来了（没抛异常）", isinstance(res, dict))
        ck("报告了架构问答事件", any(k == "architecture" for k, _ in em), str(em)[:200])
        dprompt = next((p for p in seen if "能力" in p or "cap" in p), "")
        ck("蓝图提示词里带着架构安排",
           any("架构安排" in p for p in seen), "提示词里没有人的安排")
        bp = bpm.load(Path(td))
        ck("蓝图目标里留着架构安排（可溯源）", "架构安排" in (bp.goal or ""))
        ck("人答的接口安排进了目标", "csvstat.py --path" in (bp.goal or ""))
        ck("没答的走默认也在目标里（可追责）", "默认" in (bp.goal or ""))
        ck("切片跑过一轮（简报有内容）", bool(briefs), str(len(briefs)))
        if briefs:
            b = briefs[0]
            ck("**简报里有人的架构安排**（写代码那一步看得到）", "架构安排" in b, b[:200])
            ck("简报里有人定的入口名", "csvstat.py --path" in b, b[:400])
            ck("简报里有人定的口径", "空字段算 0" in b, b[:400])
            ck("架构安排排在能力清单前面（不会被挤掉）",
               b.index("架构安排") < b.index("# 本轮要做的能力"), b[:200])
        ck("架构安排文本可溯源落盘",
           (Path(td) / architect.ARCH_FILE).exists() or bool(bp.goal))


if __name__ == "__main__":
    print(f"== 架构问答自检（{ROOT}）==")
    test_normalize()
    test_ask()
    test_answers_flow()
    test_answer_formats()
    test_persist()
    test_into_auto()
    test_end_to_end()
    print(f"\n共 {OK} 项断言全部通过")
