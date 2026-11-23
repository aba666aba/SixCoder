"""不变量自检：把"硬编码的限制有没有依据、有没有测试覆盖"变成每次自动追问。

为什么需要它（这是本项目最重要的一条元机制）：
    这套系统里踩过的坑，几乎全是**某个人为拍的数字没有依据**——
      · `patch.py` 里 `[:12000]` 把 2000 行文件砍成前 1/5 → 模型只能盲改；
      · 档案里 `max_output_tokens: 8000` 成了第二真相源 → 小任务配额被顶大；
      · 质量阈值 8/3/40 一刀切 → 一个"边界校验簇"被判红、整模块重写（¥0.5031）；
      · 提示词里"不许引入依赖" → 加一行 import 都得重写整个文件。
    这些都不是"写错了"，是**没人问过"这个数字凭什么"**。
    所以这里扫一遍 core/，把每条硬编码限制挖出来，逐条要求回答：
      依据是什么？超了会怎样？有没有自检覆盖？是不是物理上限？

判定规则：
    · 物理上限（模型能力/协议上限，如 384000、950000、393216）→ 天然合规，不用裁决；
    · 有自检覆盖 → 合规（说明它被钉住了）；
    · 既不是物理上限、又没自检覆盖 → **标红待裁决**，必须写进 baseline 并给出依据。

跑法:  python checks/check_invariants.py            # 报告
       python checks/check_invariants.py --baseline  # 把当前项收进 baseline（要人确认过）
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # prototype/
CORE = ROOT / "core"
CHECKS = ROOT / "checks"
BASELINE = CHECKS / "invariant_baseline.json"

# 物理上限：模型/协议给的硬边界，不是我们拍的
PHYSICAL = {
    384000: "模型最大输出 384K（协议上限）",
    393216: "同上，按 256 对齐后上报的最大输出",
    950000: "模型 1M 上下文扣除留白后的输入预算（实测）",
    1000000: "1M 上下文（协议）",
}

# 认为"这是限制/禁令"的行内信号
LIMIT_HINT = re.compile(r"上限|阈值|门槛|floor|ceil|cap|limit|最多|至少|不超过|不许|不允许|禁止|"
                        r"max_|min_|MAX_|MIN_")
PROHIBIT = re.compile(r"[\"'][^\"']{0,80}(不许|不允许|禁止|不要|不得)[^\"']{0,80}[\"']")
SLICE_TAIL = re.compile(r"\[\s*:\s*(\d{2,})\s*\]")
SLICE_HEAD = re.compile(r"\[\s*(\d{2,})\s*:\s*\]")
# 数字要支持千分位下划线：`max_lines=1_000_000` 以前被切成 1（2026-11-23 修）
_NUM = r"(-?\d[\d_]*)"
KWARG_NUM = re.compile(r"\b(max_[a-z_]+|min_[a-z_]+|floor|ceil|cap|cap_total)\s*[:=]\s*" + _NUM)
CMP_NUM = re.compile(r"(?:>=|<=|==|!=|>|<)\s*(\d[\d_]{1,})")


def _snippet(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip())[:120]


def _code_mask(line: str) -> str:
    """把**普通字符串**的字面量内容换成等长空格，只留生效代码用于数字匹配。

    目的：`label="> 100 万行"` 这类文案里的数字不是常量，不该被当成比较阈值
    （2026-11-23 实测：scale.py 的档位标签被扫成 3 条假阈值）。

    关键细节：**f-string 不掩**——`f"{x[:120]}"` 里的切片是生效代码，
    掩掉会漏掉大量真截断（第一版就踩了这个，误删 115 条）。
    引号本身保留，偏移量与原文一致。
    """
    out: list[str] = []
    q = ""
    fstr = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if not q:
            if ch in "\"'":
                q = ch
                pre = line[max(0, i - 2):i].lower()
                fstr = pre.endswith("f") or pre.endswith("rf") or pre.endswith("fr")
                out.append(ch)
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        # —— 字符串内部 ——
        if ch == "\\" and i + 1 < n:          # 转义：整体跳过（长度不变）
            out.append(ch if fstr else " ")
            out.append(line[i + 1] if fstr else " ")
            i += 2
            continue
        if ch == q:
            q = ""
            fstr = False
            out.append(ch)
            i += 1
            continue
        out.append(ch if fstr else " ")
        i += 1
    return "".join(out)


def _key(path: Path, kind: str, value: str, snip: str) -> str:
    """稳定的条目标识：文件 + 类型 + 值 + 代码片段指纹（行号会漂，不能用行号）。"""
    h = hashlib.sha256(snip.encode("utf-8")).hexdigest()[:8]
    return f"{path.name}|{kind}|{value}|{h}"


def _covered(value: str, extra_words: list[str], snip: str = "") -> bool:
    """这个限制有没有被自检**真的**覆盖。

    为什么不能只看数字：400/800/1200 这类值在自检代码里到处都是（超时、样例、长度），
    只按数字匹配会把"没人裁决过的限制"误判成"已被覆盖"——
    这本身就是假通过，是这个检查最该防的错。

    判定标准（满足其一）：
      · 同一个 check 文件里**同时**出现这条限制的标识符和数字；
      · 或出现带上下文的字面片段（如 `[:800]`、`= 800`）。
    """
    num = str(value).strip()
    ident = [w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", snip or "")
             if w not in ("self", "None", "True", "False", "return", "str", "int", "len",
                          "append", "strip", "items", "get")]
    literals = {f"[:{num}]", f"[: {num}]", f"={num}", f"= {num}", f"({num})", f" {num} "}
    extra = {w for w in extra_words if w}
    for f in CHECKS.glob("check_*.py"):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if extra and any(w in text for w in extra):
            return True
        # 裸切片字面量（如 "[:800]"）不算覆盖：它在自检里可能是完全无关的样例，
        # 只按字面量匹配会把待裁决项吞掉（实测把 14 条全吞了）。
        _ = literals
        # 必须**同一行**同时出现标识符与数字：否则 400/800/1200 这类常见数字
        # 会把所有待裁决项都误判成"已覆盖"（这本身是假通过）。
        for line in text.splitlines():
            if num in line and any(w in line for w in ident):
                return True
    return False


def scan() -> list[dict]:
    """扫 core/，列出所有硬编码限制与禁令。"""
    items: list[dict] = []
    for p in sorted(CORE.glob("*.py")):
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        quote_open = ""          # 多行文档字符串的状态：里面的正文不是生效代码
        for i, raw in enumerate(lines, 1):
            line = raw.strip()
            if quote_open:
                # 同一行再次出现同样的三引号 → 文档字符串结束（core/ 里的写法都是规整的）
                if line.count(quote_open) >= 1:
                    quote_open = ""
                continue
            if line.startswith(('"""', "'''")):
                q = '"""' if line.startswith('"""') else "'''"
                if line.count(q) < 2:
                    quote_open = q
                continue
            if not line or line.startswith("#"):
                continue
            base = {"file": p.name, "line": i, "snip": _snippet(line)}
            # 数字类只在**生效代码**里找：字符串字面量（文案/提示）里的数字不是常量
            code = _code_mask(line)

            # 1) 截断类：[:N] / [N:]
            for m in list(SLICE_TAIL.finditer(code)) + list(SLICE_HEAD.finditer(code)):
                n = int(m.group(1))
                items.append({**base, "kind": "截断长度", "value": str(n),
                              "key": _key(p, "截断长度", str(n), base["snip"])})
            # 2) 关键字参数里的上限
            for m in KWARG_NUM.finditer(code):
                n = int(m.group(2).replace("_", ""))
                if n and LIMIT_HINT.search(code):
                    items.append({**base, "kind": "默认上限", "value": str(n),
                                  "name": m.group(1),
                                  "key": _key(p, "默认上限", f"{m.group(1)}={n}", base["snip"])})
            # 3) 比较阈值
            for m in CMP_NUM.finditer(code):
                n = int(m.group(1).replace("_", ""))
                if n >= 10 and LIMIT_HINT.search(code):
                    items.append({**base, "kind": "比较阈值", "value": str(n),
                                  "key": _key(p, "比较阈值", str(n), base["snip"])})
            # 4) 禁令语句
            for m in PROHIBIT.finditer(line):
                txt = m.group(0).strip("\"'")
                items.append({**base, "kind": "禁令", "value": txt[:40], "text": txt,
                              "key": _key(p, "禁令", txt[:40], base["snip"])})
    # 去重（同一 key 只留第一次出现）
    seen: dict[str, dict] = {}
    for it in items:
        seen.setdefault(it["key"], it)
    return list(seen.values())


def load_baseline() -> dict:
    if not BASELINE.exists():
        return {}
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8"))
    except Exception:                                    # noqa: BLE001
        return {}


def evaluate(items: list[dict], baseline: dict) -> dict:
    ok, physical, covered, ruled, unruled, pending = [], [], [], [], [], []
    for it in items:
        val = it["value"]
        num = None
        try:
            num = int(val)
        except (TypeError, ValueError):
            num = None
        if num is not None and num in PHYSICAL:
            physical.append({**it, "为何合规": PHYSICAL[num]})
        elif _covered(val, [it.get("name") or "", it.get("text") or ""],
                  it.get("snip") or ""):
            covered.append(it)
        elif it["key"] in baseline:
            entry = {**it, "依据": baseline[it["key"]].get("依据") or "（baseline 未写依据）"}
            if str(entry["依据"]).startswith("待人工确认"):
                pending.append(entry)
            else:
                ruled.append(entry)
        else:
            unruled.append(it)
    stale = [k for k in baseline if k not in {i["key"] for i in items}]
    return {"total": len(items), "physical": physical, "covered": covered,
            "ruled": ruled, "unruled": unruled, "pending": pending,
            "stale_baseline": stale}


def main(argv: list[str]) -> int:
    items = scan()
    baseline = load_baseline()
    if "--baseline" in argv:
        for it in items:
            baseline.setdefault(it["key"], {
                "file": it["file"], "kind": it["kind"], "value": it["value"],
                "snip": it["snip"], "依据": "（待填：这个数字凭什么）"})
        BASELINE.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已把 {len(items)} 项收进 baseline：{BASELINE}")
        print("注意：每条的『依据』必须人来填 —— 没填依据的项下次仍然会提醒你。")
        return 0

    r = evaluate(items, baseline)
    print("硬编码限制/禁令自检")
    print(f"  扫描到 {r['total']} 条：物理上限 {len(r['physical'])}｜有自检覆盖 {len(r['covered'])}"
          f"｜已裁决 {len(r['ruled'])}｜**待人工确认 {len(r.get('pending') or [])}**"
          f"｜未裁决 {len(r['unruled'])}")
    if r["physical"]:
        print("\n  [物理上限 · 天然合规]")
        for it in r["physical"][:6]:
            print(f"    {it['file']}:{it['line']} {it['value']:>7} — {it['为何合规']}")
    if r["ruled"]:
        print("\n  [已裁决]")
        for it in r["ruled"][:8]:
            print(f"    {it['file']}:{it['line']} {it['kind']} {it['value']} — {it['依据'][:60]}")
    empty_reason = [i for i in r["ruled"] if "待填" in str(i.get("依据"))]
    if empty_reason:
        print(f"\n  [已裁决但没写依据] {len(empty_reason)} 条 —— 这些不算真的裁决，仍需人写清凭什么")
    if r.get("pending"):
        print(f"\n  [待人工确认] {len(r['pending'])} 条 —— 进了台账但没人回答『凭什么』，每次运行都会列出来")
        for it in r["pending"][:12]:
            print(f"    {it['file']}:{it['line']} {it['kind']} {it['value'][:40]}  ← {it['snip'][:80]}")
    if r["unruled"]:
        print("\n  [未裁决 · 必须回答『凭什么 / 超了会怎样 / 有没有测试』]")
        for it in r["unruled"][:12]:
            print(f"    {it['file']}:{it['line']} {it['kind']} {it['value'][:40]}")
            print(f"        {it['snip'][:100]}")
    if r["stale_baseline"]:
        print(f"\n  [baseline 里过期 {len(r['stale_baseline'])} 条]（代码已删，可以清掉）")

    fails = len(r["unruled"]) + len(empty_reason)
    print()
    if fails:
        print(f"有 {fails} 条限制没有依据：先回答『凭什么』再决定保留/改小/删除。")
        print("（确认过就运行 `python checks/check_invariants.py --baseline` 收进台账）")
        return 1
    print("全部通过（每条硬编码限制都有依据：物理上限 / 自检覆盖 / 人工裁决）")
    if r.get("pending"):
        print(f"注意：仍有 {len(r['pending'])} 条『待人工确认』——它们不算通过，只是被点名。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
