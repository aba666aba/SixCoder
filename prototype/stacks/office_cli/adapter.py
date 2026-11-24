"""Office 文档处理工具生态包。

能力边界（诚实声明）：
  · 识别：有 office_main.py -> Office 文档处理工具
  · 验收：cli 探针真跑入口，并用 openpyxl/python-docx/pptx **读回产物断言内容**
  · 不做：格式排版完美（那是人眼的事）；Office 宏；实时协同

## 为什么判据必须"读回产物"

一个 openpyxl 程序写出 xlsx 很容易，但内容对不对只有读回来才知道：
  · 文件存在但只有一个空 sheet        -> 假绿
  · 单元格 A1 是该有的值、不是 None   -> 真绿
  · docx 里有段落文字、不是空文档     -> 真绿

所以本包的 probe 约定里，期望值必须写到"读回后看到什么"这一层。
"""
from core import stack


class OfficeCliAdapter(stack.StackAdapter):
    name = "office_cli"
    label = "Office 文档处理工具（xlsx / docx / pptx）"
    entry_path = "office_main.py"
    entry_import_hint = "import openpyxl"
    route_hint = ("用 argparse 定义子命令；每个子命令读输入文档 -> 处理 -> 写输出文档，"
                  "写完必须能被 openpyxl/python-docx 重新打开")
    router_decl = "parser = argparse.ArgumentParser()"
    router_marker = "add_parser("
    module_dirs = ("lib", "commands", "src")
    module_file_fmt = "commands/{seg}.py"
    module_globs = ("**/*.py",)
    module_prefix = ""
    route_decorator_re = r"add_parser\(\s*[\"' ]([^\"' ]+)[\"' ]"
    route_verb_default = "GET"
    route_hosts = ("parser", "subparsers")
    template_dirs = ()
    static_dirs = ()
    static_mount = "/static"
    start_cmd = "python office_main.py --help"
    app_ref = ""
    probe_app_candidates = ()
    deps = ("openpyxl", "python-docx", "python-pptx", "pandas")
    app_marker = "openpyxl"

    def asgi_app(self) -> bool:
        """Office 工具不是 ASGI 服务。"""
        return False

    def web_rules(self) -> str:
        """喂给远端的验收约定。"""
        return """1. 用 cli 探针真跑入口，并且**读回产物**断言（只看文件存在等于没验）：
   {"kind":"cli","entry":"office_main.py","args":["to_csv","in.xlsx"],
    "files":{"in.xlsx":"<夹具生成>"},"stdout_has":"完成","expect_exit":0}
2. **写出的 Office 文件必须能被读回**：
   · xlsx -> openpyxl.load_workbook(path)，断言 sheet 名与单元格值
   · docx -> docx.Document(path)，断言段落文字
   · pptx -> Presentation(path)，断言幻灯片数与文字
   空文档 / 只有一个空 sheet / 单元格全是 None，都算没做。
3. **不要改输入文件**：读进来的 xlsx 处理完应当另存，而不是覆盖原文件
   （覆盖会让第二次运行结果不可复现）。
4. **大文件要能跑**：1000 行以内的表处理不应超时；
   用 pandas 读大表时注意 dtype（数字被读成字符串是常见错）。"""
