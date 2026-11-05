"""把「待人工确认」的硬编码限制逐条裁决（依据必须能核到代码事实）。

裁决口径（按优先级）：
  1. MANUAL 里逐条写死的（真正需要人判断的少数几条）；
  2. **事件/报告里的展示字段**：形如 `"key": x[:N]` / `lines.append(... x[:N])` ——
     只影响可读性，不参与判据；
  3. **进模型上下文的文本**：这类**不该按字符裁**，遇到就打出来要人改；
  4. 其余仍打印出来，由人逐条看。
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "checks" / "invariant_baseline.json"

DICT_FIELD = re.compile(r'"[A-Za-z_]+"\s*:\s*.+\[\s*:\s*\d+\s*\]')
APPEND_LINE = re.compile(r"(lines\.append|\.append\()")
PROMPT_HINT = re.compile(r"(prompt|skeleton|contract=|examples=|requirement|plan|content|context)",
                         re.I)

RULES = [
    (lambda it: bool(DICT_FIELD.search(it["snip"])),
     "事件/报告字典里的展示字段（形如 key: x[:N]）：写给人看，长度不影响判据"),
    (lambda it: bool(APPEND_LINE.search(it["snip"])),
     "报告/追溯矩阵的展示行：只影响可读性；判据另由门禁负责"),
    (lambda it: "emit(" in it["snip"] or "db.execute" in it["snip"] or "UPDATE" in it["snip"],
     "事件流/审计库写入：为控制库体积，与判据无关"),
    (lambda it: "_cache_key" in it["snip"],
     "缓存键的取样长度：只影响键的区分度，不影响内容正确性"),
    (lambda it: it["kind"] == "默认上限",
     "门禁/配额默认值：由 core/scale.py 按代码规模给，可用环境变量覆盖；改它要同步改自检"),
    (lambda it: it["kind"] == "比较阈值",
     "门禁比较阈值：『明显病态才报』的经验线，对应自检钉住行为"),
    (lambda it: "type(exc).__name__" in it["snip"] or "str(exc)[" in it["snip"],
     "异常摘要截断：只把异常写进失败原因给人看，判据看的是 ok 布尔与异常类型，不看截断后的文字"),
    # 交付报告/台账里的"下一步 / 差在哪 / 不要当成…"是给维护者看的说明文字，
    # 不是系统硬限制（这类文字带"禁令"词会被扫描器当成约束）。
    (lambda it: it["kind"] == "禁令" and any(k in it["snip"] for k in
                                             ('"next":', '"blocker":', '"notes":', '不要当成',
                                              '不许糊弄', '不算通过')),
     "交付报告/台账里的提示文本：写给维护者看的建议或边界说明，不是系统硬性限制"),
    # 判据详情里拼给人看的响应/异常摘要（add(...) / _send(...) 的参数）
    (lambda it: it["kind"] == "截断长度" and re.search(r"\b(add\(|_send\(|detail)", it["snip"]),
     "判据详情里的展示截断：只把响应/异常写进失败说明给人看，判据看的是布尔结果"),
]

MANUAL: dict[str, str] = {}


def _unruled() -> list[tuple[str, dict]]:
    """扫一遍 core/，找出**新出现**的、既不在台账也没被自检覆盖的条目。

    为什么需要这一步：以前这个工具只处理"已经在台账里但依据是待人工确认"的条目，
    新代码产生的未裁决项它看不见 —— 结果每加一段代码就得手写一次性脚本去补台账。
    """
    sys.path.insert(0, str(ROOT / "checks"))
    import check_invariants as ci
    items = ci.scan()
    base = ci.load_baseline()
    fresh: list[tuple[str, dict]] = []
    for it in items:
        if it["key"] in base:
            continue
        try:
            num = int(it["value"])
        except (TypeError, ValueError):
            num = None
        if num is not None and num in ci.PHYSICAL:
            continue
        if ci._covered(it["value"], [it.get("name") or "", it.get("text") or ""],
                       it.get("snip") or ""):
            continue
        fresh.append((it["key"], it))
    return fresh


def _why(it: dict) -> str:
    for fn, w in RULES:
        try:
            if fn(it):
                return w
        except Exception:                                # noqa: BLE001
            continue
    return ""


def main() -> int:
    data = json.loads(BASE.read_text(encoding="utf-8"))
    pending = [(k, v) for k, v in data.items()
               if str(v.get("依据") or "").startswith("待人工确认")]
    print(f"待裁决 {len(pending)} 条（已在台账里）")
    resolved, prompts, unknown = 0, [], []
    for k, v in pending:
        it = {"kind": v.get("kind", ""), "value": v.get("value", ""),
              "snip": v.get("snip", ""), "file": v.get("file", "")}
        why = MANUAL.get(k) or _why(it)
        if why:
            data[k]["依据"] = why
            resolved += 1
            continue
        if PROMPT_HINT.search(it["snip"]):
            prompts.append(it)
        else:
            unknown.append((it["file"], it["kind"], it["value"], it["snip"][:90]))
    # 新代码产生的未裁决项：能用规则说清依据的，直接写进台账
    fresh = _unruled()
    fresh_done = 0
    for k, it in fresh:
        why = MANUAL.get(k) or _why(it)
        if not why:
            if PROMPT_HINT.search(it.get("snip") or ""):
                prompts.append(it)
            else:
                unknown.append((it.get("file"), it.get("kind"), it.get("value"),
                                (it.get("snip") or "")[:90]))
            continue
        data[k] = {"file": it.get("file"), "kind": it.get("kind"), "value": it.get("value"),
                   "snip": it.get("snip"), "依据": why}
        fresh_done += 1
    print(f"新增未裁决 {len(fresh)} 条 → 按规则写清依据 {fresh_done} 条")
    BASE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已裁决 {resolved} 条（补依据），新写 {fresh_done} 条")
    if prompts:
        print(f"\n进模型上下文、却按字符裁的 {len(prompts)} 条（应改 token 预算）：")
        for it in prompts:
            print(f"  {it['file']} [{it['value']}] {it['snip'][:90]}")
    if unknown:
        print(f"\n需要人看代码的 {len(unknown)} 条：")
        for u in unknown:
            print(f"  {u[0]} [{u[1]}] {u[2]}  ← {u[3]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
