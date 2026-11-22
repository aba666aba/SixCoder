"""check_night_requirement.py — 夜班开工不许"有规格却说没需求"。

用户报障（2026-11-21）："夜班总是显示需要仓库和需求"。
根因：协商定稿落在 <repo>/.sixcoder/clarify.json，重启/切仓库后 loadClarify()
只恢复了 clarify.requirement，而 startNight() 只认内存里的 form.requirement；
界面按钮明明叫「开始夜班」，点下去永远被拦。

这个自检钉住四件事（都是纯静态断言 + 一条文本契约，跑得比浏览器快）：
  1. 需求来源只有一处解析（nightRequirement），且回落到协商定稿；
  2. 两条开工路径（/v1/night 与 /v1/night/detached）都发这份需求；
  3. loadClarify 把定稿写回 form.requirement（重开界面才不会又空）；
  4. 报错文案分得清"没仓库 / 没需求 / 没目标"，不再给一句模糊话。
顺带钉住探测面板的 signals 空值防护（缺字段会把整棵树渲染打断）。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "prototype" / "web" / "src" / "App.vue").read_text(encoding="utf-8")

FAILS: list[str] = []
PASSED = 0


def ck(name: str, cond, extra="") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ✓ {name}")
    else:
        FAILS.append(name)
        print(f"  ✗ {name}" + (f"  <- {extra}" if extra else ""))


print("[1] 需求只有一个解析入口")
ck("定义了 nightRequirement", "const nightRequirement = computed(" in APP)
snippet = APP.split("const nightRequirement = computed(", 1)[-1][:420]
ck("非目标模式回落到协商定稿", "clarify.requirement || form.requirement" in snippet, snippet[:160])
ck("目标模式用 night.goal", "night.goalMode) return (night.goal" in snippet)

print("[2] 两条开工路径都发这份需求")
night_fn = APP.split("async function startNight()", 1)[-1].split("\n/* 后台长跑的状态轮询", 1)[0]
ck("同步夜班发 nightRequirement", "requirement: auto ? '' : nightRequirement.value" in night_fn)
ck("后台长跑发 nightRequirement", night_fn.count("nightRequirement.value") >= 3,
   night_fn.count("nightRequirement.value"))
ck("startNight 里不再直接用 form.requirement 开工",
   "requirement: auto ? '' : form.requirement" not in night_fn)
ck("开工前把定稿写回表单", "if (need !== form.requirement.trim()) form.requirement = need" in night_fn)

print("[3] 重启后协商定稿能恢复成表单值")
clarify_fn = APP.split("async function loadClarify()", 1)[-1].split("function startClarifyTick", 1)[0]
ck("loadClarify 写回 form.requirement", "form.requirement = clarify.requirement" in clarify_fn)

print("[4] 报错文案分得清缺什么")
ck("不再出现模糊的「夜班需要仓库和需求」", "夜班需要仓库和需求" not in APP)
ck("缺仓库有自己的提示", "夜班需要先选仓库" in night_fn)
ck("缺需求指向「开始分析」", "还缺需求" in night_fn and "开始分析" in night_fn)
ck("目标模式单独提示", "全自主模式需要一句话目标" in night_fn)
ck("估算按钮用同一个需求解析", "|| !form.repo || !nightRequirement" in APP
   and "const text = nightRequirement.value" in APP)

print("[5] 探测面板不许因缺 signals 崩整棵树")
ck("signals 有存在性判断", '<template v-if="probeInfo.signals">' in APP)
ck("缺 signals 有可见兜底", "探测结果缺少细节" in APP)

print()
if FAILS:
    print("失败：", FAILS)
    raise SystemExit(1)
print(f"全部通过（{PASSED} 条）：需求来源 / 两条开工路径 / 重启恢复 / 报错文案 / signals 防护")
