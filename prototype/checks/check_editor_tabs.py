"""中栏「工作台 / 代码 IDE」双视图 + 多标签编辑器自检（2026-11-20）。

用户原话："左右栏都是可以收缩的嘛，把中栏变成 IDE 就可以有很大页面。"
钉住四条，防止以后又退回 fixed 全屏浮层：
  1. 编辑器是 .col-mid 的子节点，工作台三块用 v-show 让位，不是 inset:0 全屏盖住文件树；
  2. 有独立标签状态（edTabs / switchTab / closeTab / stashActiveTab）且关闭只关当前标签；
  3. 模式切换自动切中栏视图（开发者→代码，其余→工作台），标签不丢；
  4. 选中函数识别的正则 bug（word$）不许回来 —— 它让「定位/调试此函数」按钮
     在浏览器里直接抛 ReferenceError，源码编译和函数级自检都抓不到；
  5. 断点改成点行号列（VSCode 手感）：有红点样式、按文件存、编辑器里不再有断点按钮；
     定位/调试也从"先选中文字"改成函数下拉，且下拉真被调用（不是只定义函数）。

跑法: python checks/check_editor_tabs.py
"""
from __future__ import annotations

import sys
from pathlib import Path

PROTO = Path(__file__).resolve().parents[1]
APP = PROTO / "web" / "src" / "App.vue"
CSS = PROTO / "web" / "src" / "style.css"
FAILS: list[str] = []


def ck(name: str, ok: bool, detail: object = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + name + (f"  {detail}" if detail != "" else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    src = APP.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")
    mid = src.split('<div class="col col-mid">', 1)[1].split("<!-- ============ 右栏", 1)[0]
    block = src[src.rfind('<div v-if="ed.open && midView === \'code\'" class="editor-overlay">'):
                src.rfind("</template>")]

    print("[1] 编辑器嵌在中栏")
    ck("编辑器渲染在中栏里", 'class="editor-overlay"' in mid)
    ck("工作台三块用 v-show 让位", mid.count('v-show="midView === \'work\'"') >= 3,
       mid.count('v-show="midView === \'work\'"'))
    ck("无 Teleport 兜圈子", "Teleport to=" not in src)
    ck("CSS 不再 fixed 全屏", "position: fixed; inset: 0; z-index: 70" not in css
       and "position: relative; flex: 1 1 auto" in css)
    ck("悬浮对话限制在编辑区内", "position: absolute; right: 14px; bottom: 46px" in css)

    print("[2] 多标签状态")
    for token in ("const edTabs = ref([])", "function stashActiveTab", "function hydrateTab",
                  "async function switchTab", "async function closeTab",
                  'v-for="t in edTabs"', "@click=\"switchTab(t.path)\"",
                  "@click.stop=\"closeTab(t.path)\""):
        ck("有 " + token, token in src)
    ck("关闭当前标签不连坐其它标签", "edTabs.value.splice(idx, 1)" in src
       and "return closeTab(ed.path, force)" in src)
    ck("回滚能从磁盘重读当前标签", "openFile({ path: ed.path, is_dir: false }, true)" in src)

    print("[3] 模式切换自动换视图")
    ck("setMode 切中栏", "midView.value = (currentMode.value?.id === 'developer'"
       in src and "stashActiveTab()" in src)
    ck("点文件树进代码视图", "ed.open = true; midView.value = 'code'" in src)
    ck("回工作台按钮", "stashActiveTab(); midView = 'work'" in block)

    print("[4] 选中函数识别不许再抛 ReferenceError")
    ck("没有 word$ 笔误", "word$" not in src)
    ck("正则变量用对", ".match(word)?" in src)
    ck("定位/调试按钮还在", 'data-testid="editor-locate-symbol"' in src
       and 'data-testid="editor-debug-symbol"' in src)

    print("[5] 行号断点 + 函数下拉 + 页边距")
    ck("页边距 18px", "padding: 18px; gap: 8px;" in css)
    ck("断点点在行号列", "@click=\"toggleBreakpointAt(n)\"" in block
       and 'data-testid="editor-breakpoint"' in block)
    ck("旧断点按钮已删", '@click="toggleBreakpoint()"' not in src
       and "data-testid=\"editor-breakpoint\"" not in src.split('class="editor-head"', 1)[1].split('</div>', 1)[0])
    ck("红点样式", ".gutter-line.bp .bp-dot" in css and "#e51400" in css)
    ck("断点按文件存（切标签不串）", "bpByPath: {}" in src and "stashBreakpoints()" in src
       and "loadBreakpoints()" in src and "delete run.bpByPath[path]" in src)
    ck("编辑器里没有 Git 按钮（顶栏还有）", "openGitFromEditor" not in block
       and 'toggleRepoDrop(\'git\'' in src)
    ck("函数下拉存在", 'class="sym-select"' in block and "function activeSymbol" in src)
    ck("符号表真的被加载（不是只定义）", src.count("loadSymbolHints()") >= 3,
       src.count("loadSymbolHints()"))

    print()
    if FAILS:
        print(f"{len(FAILS)} 项失败: " + ", ".join(FAILS))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
