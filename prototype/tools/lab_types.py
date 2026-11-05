"""实验台用的「不同系统类型」需求库。

每个类型 = 一份**可直接机判**的需求（契约式），夜班拿它整文件生成 + 机械验收。
挑选原则：覆盖市面上的不同系统形态，而不是同一个玩具换名字。

铁律（踩过才写下来的）：
  · 示例必须能字面量比较（`expr == 字面量`），否则 prop 生成不出判据；
  · 契约里说的"抛 X"要真的在示例或说明里，别留"声明会抛但无法判"的空档；
  · 纯函数优先（不真起服务/不真连网），这样判据才能复现，成本也低。
"""

BATCH1 = [
    {
        "slug": "pipeline-db",
        "name": "数据管道：清洗 → SQLite 入库 → 汇总报表",
        "scale": "S",
        "req": """代码规模：S

1) clean.py：把脏文本行清洗成规范字段：年龄、日期、邮箱，非法输入抛 ValueError。
   - normalize_age(x: str) -> int：去首尾空格后转整数；结果须在 0 到 150 之间，越界或无法转整数抛 ValueError
   - parse_date(s: str) -> str：接受 "YYYY-MM-DD" 或 "YYYY/MM/DD"，统一返回 "YYYY-MM-DD"；其他格式或非法日期抛 ValueError
   - clean_email(s: str) -> str：去首尾空格并转小写；必须含 "@" 且 "@" 前后均非空，否则抛 ValueError
   - clean_row(row: dict) -> dict：返回新字典：age 走 normalize_age、date 走 parse_date 后改名成 birth、email 走 clean_email，其余键原样保留；缺字段或任一字段非法一律抛 ValueError（不许 KeyError）

2) store.py：SQLite 建表、按 id 覆盖写入与按 id 升序读取。conn 是真实的 sqlite3 连接，
   本模块**不写字面量示例**（空 dict 当连接是假示例），由端到端判据覆盖：真实建库 → 写两行 → 读回。
   - create_table(conn: object) -> None：在给定连接上建表 people(id INTEGER PRIMARY KEY, name TEXT, age INTEGER, email TEXT, birth TEXT)
   - upsert(conn: object, rows: list[dict]) -> int：按 id 覆盖写入 rows（每行含 id/name/age/email/birth），返回写入条数
   - fetch_all(conn: object) -> list[dict]：按 id 升序返回全部行，每行为含 id/name/age/email/birth 的字典

3) report.py：对清洗后的行做汇总统计。
   - summarize(rows: list[dict]) -> dict：返回 {"count": 行数, "avg_age": 平均年龄保留 1 位小数, "domains": 邮箱 "@" 后域名计数}；空列表时 avg_age 为 0.0

示例（必须满足）：
normalize_age(" 42 ") == 42
normalize_age("0") == 0
parse_date("2024/01/02") == "2024-01-02"
clean_email(" Foo@Bar.COM ") == "foo@bar.com"
clean_row({"age": " 42 ", "date": "2024/01/02", "email": " Foo@Bar.COM "}) == {"age": 42, "birth": "2024-01-02", "email": "foo@bar.com"}
summarize([{"age": 10, "email": "a@x.com"}]) == {"count": 1, "avg_age": 10.0, "domains": {"x.com": 1}}
summarize([]) == {"count": 0, "avg_age": 0.0, "domains": {}}
""",
    },
    {
        "slug": "api-layer",
        "name": "REST 接口业务层：校验 → 响应体（不起服务）",
        "scale": "S",
        "req": """代码规模：S

1) api.py：用户注册接口的业务层（纯函数，不启动 Web 服务）：校验请求体、拼响应体。
   - validate(payload: dict) -> dict：校验 name 为非空字符串、age 为 0 到 150 的整数、email 含 "@"；全部通过返回 {"ok": True, "value": 规范化后的请求体}，否则返回 {"ok": False, "code": 400, "error": 第一个不合法字段的字段名}
   - create(payload: dict, store: dict) -> dict：先 validate；不通过就原样返回；通过则以 name 为键写入 store 并返回 {"ok": True, "code": 201, "value": 记录}
   - read(name: str, store: dict) -> dict：命中返回 {"ok": True, "code": 200, "value": 记录}，未命中返回 {"ok": False, "code": 404, "error": "not found"}

示例（必须满足）：
validate({"name": "a", "age": 20, "email": "a@b"}) == {"ok": True, "value": {"name": "a", "age": 20, "email": "a@b"}}
validate({"name": "", "age": 20, "email": "a@b"}) == {"ok": False, "code": 400, "error": "name"}
validate({"name": "a", "age": 200, "email": "a@b"}) == {"ok": False, "code": 400, "error": "age"}
validate({"name": "a", "age": 20, "email": "ab"}) == {"ok": False, "code": 400, "error": "email"}
create({"name": "a", "age": 20, "email": "a@b"}, {}) == {"ok": True, "code": 201, "value": {"name": "a", "age": 20, "email": "a@b"}}
read("x", {}) == {"ok": False, "code": 404, "error": "not found"}
""",
    },
    {
        "slug": "cli-tool",
        "name": "命令行工具：参数解析 + 子命令 + 退出码",
        "scale": "S",
        "req": """代码规模：S

1) todocli.py：命令行待办清单的业务层：解析参数、执行子命令、给出退出码（不读 stdin、不打印）。
   - parse(argv: list[str]) -> dict：把 ["add", "-t", "买牛奶"] 解析成 {"cmd": "add", "text": "买牛奶"}；["list"] 解析成 {"cmd": "list", "text": ""}；未知子命令抛 ValueError；空参数列表同样抛 ValueError（不许 IndexError 崩掉）
   - run(argv: list[str], items: list[str]) -> dict：执行并返回 {"code": 退出码, "items": 结果列表}；add 追加到末尾、done 删掉指定文本、list 原样返回；done 找不到该文本时 code 为 1 且 items 不变
   - render(items: list[str]) -> str：一行一条，格式为 "1. 第一项"，序号从 1 开始；空列表返回 ""

示例（必须满足）：
parse(["add", "-t", "买牛奶"]) == {"cmd": "add", "text": "买牛奶"}
parse(["list"]) == {"cmd": "list", "text": ""}
run(["add", "-t", "a"], []) == {"code": 0, "items": ["a"]}
run(["done", "-t", "z"], ["a"]) == {"code": 1, "items": ["a"]}
run(["done", "-t", "a"], ["a", "b"]) == {"code": 0, "items": ["b"]}
render(["a", "b"]) == "1. a\\n2. b"
render([]) == ""
""",
    },
    {
        "slug": "rule-engine",
        "name": "规则引擎：加载 JSON 规则、匹配事件、给出动作",
        "scale": "S",
        "req": """代码规模：S

1) rules.py：JSON 配置驱动的规则引擎（纯函数）。
   - load(text: str) -> list[dict]：解析 JSON 数组，每项含 field/op/value/action；文本不是合法 JSON 数组或缺少字段时抛 ValueError
   - match(rule: dict, event: dict) -> bool：op 支持 eq、ne、gt、lt、contains 五种；事件里没有该字段视为不匹配；未知 op 抛 ValueError
   - first_match(rules: list[dict], event: dict) -> str：按顺序返回第一条命中规则的 action；全不命中返回空字符串

示例（必须满足）：
load('[{"field":"age","op":"gt","value":18,"action":"vip"}]') == [{"field": "age", "op": "gt", "value": 18, "action": "vip"}]
load("[]") == []
match({"field": "age", "op": "gt", "value": 18}, {"age": 20}) == True
match({"field": "age", "op": "gt", "value": 18}, {"name": "x"}) == False
match({"field": "mail", "op": "contains", "value": "@a"}, {"mail": "x@a.com"}) == True
first_match([{"field": "age", "op": "gt", "value": 18, "action": "vip"}], {"age": 20}) == "vip"
first_match([], {"age": 20}) == ""
""",
    },
    {
        "slug": "incr-sync",
        "name": "增量同步：清单比对、摘要、体积统计",
        "scale": "S",
        "req": """代码规模：S

1) filesync.py：目录增量同步的计划计算（纯函数，不碰磁盘）。
   - digest(data: str) -> str：返回 32 位小写十六进制摘要，必须与 hashlib.md5 对同样字节的结果一致
   - plan(old: list[dict], new: list[dict]) -> dict：两个清单每项含 path 与 hash；返回 {"add": [路径], "update": [路径], "remove": [路径]}，三组内部都按路径字典序排列
   - total_size(items: list[dict]) -> int：把每项的 size 求和，缺 size 视为 0

示例（必须满足）：
digest("a") == "0cc175b9c0f1b6a831c399e269772661"
plan([], [{"path": "a", "hash": "1"}]) == {"add": ["a"], "update": [], "remove": []}
plan([{"path": "a", "hash": "1"}], [{"path": "a", "hash": "2"}]) == {"add": [], "update": ["a"], "remove": []}
plan([{"path": "b", "hash": "1"}], []) == {"add": [], "update": [], "remove": ["b"]}
total_size([{"path": "a", "size": 3}, {"path": "b"}]) == 3
total_size([]) == 0
""",
    },
]

BATCH2 = [
    {
        "slug": "time-tz",
        "name": "时间处理：ISO 解析、日期加减、时间桶",
        "scale": "S",
        "req": """代码规模：S

1) timeline.py：时间处理的纯函数库（只用标准库，不依赖第三方）。
   - to_minutes(s: str) -> int：把 "YYYY-MM-DD HH:MM" 解析成当天起的分钟数（0 到 1439）；格式不对抛 ValueError
   - add_days(s: str, n: int) -> str：把 "YYYY-MM-DD" 加 n 天，返回同样格式；格式不对抛 ValueError
   - bucket(s: str, size: int) -> str：把 "YYYY-MM-DD HH:MM" 归到 size 分钟一档的档首，返回 "YYYY-MM-DD HH:MM"；size 小于等于 0 抛 ValueError

示例（必须满足）：
to_minutes("2024-01-02 03:04") == 184
to_minutes("2024-01-02 00:00") == 0
add_days("2024-01-31", 1) == "2024-02-01"
add_days("2024-03-01", -1) == "2024-02-29"
bucket("2024-01-02 03:07", 15) == "2024-01-02 03:00"
bucket("2024-01-02 03:59", 60) == "2024-01-02 03:00"
""",
    },
    {
        "slug": "stats-num",
        "name": "统计数值：均值/中位数/分位/直方图",
        "scale": "S",
        "req": """代码规模：S

1) stats.py：数值统计的纯函数库。
   - mean(xs: list[float]) -> float：平均值；空列表抛 ValueError
   - median(xs: list[float]) -> float：中位数（偶数个取中间两个的平均）；空列表抛 ValueError
   - p95(xs: list[float]) -> float：最近秩法分位数：排序后取第 95 百分位，索引为 ceil(0.95 * n) - 1；空列表抛 ValueError
   - histogram(xs: list[float], bins: int) -> dict：把数据按等宽分桶，返回 {"counts": 每桶个数列表, "edges": 长度为 bins+1 的边界列表}；bins 小于 1 抛 ValueError

示例（必须满足）：
mean([1.0, 2.0, 3.0]) == 2.0
mean([2.5]) == 2.5
median([1.0, 3.0]) == 2.0
median([3.0, 1.0, 2.0]) == 2.0
p95([float(i) for i in range(1, 21)]) == 19.0
histogram([1.0, 2.0, 3.0, 4.0], 2) == {"counts": [2, 2], "edges": [1.0, 2.5, 4.0]}
""",
    },
    {
        "slug": "ratelimit",
        "name": "限流器：带显式状态的滑动窗口计数",
        "scale": "S",
        "req": """代码规模：S

1) ratelimit.py：限流/配额判断（状态由调用方显式传入，函数本身不存全局变量）。
   - allow(state: dict, key: str, now: int, limit: int, window: int) -> bool：滑动窗口；state 形如 {key: [时间戳...]}；now 秒内最近 window 秒的命中数小于 limit 时放行并把 now 记入 state，返回 True，否则不放行且不改 state，返回 False
   - prune(state: dict, key: str, now: int, window: int) -> int：删掉 key 下早于 now - window 的时间戳，返回剩余条数；key 不存在返回 0
   - remaining(state: dict, key: str, now: int, limit: int, window: int) -> int：在不记录新请求的前提下算还能放行几次，最小为 0

示例（必须满足）：
allow({}, "u", 100, 2, 60) == True
allow({"u": [100]}, "u", 100, 1, 60) == False
prune({"u": [1, 2, 100]}, "u", 100, 60) == 1
prune({}, "u", 100, 60) == 0
remaining({"u": [100]}, "u", 120, 3, 60) == 2
remaining({}, "u", 120, 3, 60) == 3
""",
    },
    {
        "slug": "order-fsm",
        "name": "订单状态机：合法迁移 + 非法拒绝 + 可用动作",
        "scale": "S",
        "req": """代码规模：S

1) orderfsm.py：订单状态机（纯函数，状态用字符串表示）。
   状态：created、paid、shipped、done、cancelled。动作：pay、ship、finish、cancel。
   - next_state(state: str, action: str) -> str：按 created --pay--> paid、paid --ship--> shipped、shipped --finish--> done、created --cancel--> cancelled、paid --cancel--> cancelled 迁移；其余组合抛 ValueError
   - allowed_actions(state: str) -> list[str]：该状态可执行的动作，按动作名字典序；未知状态抛 ValueError
   - is_terminal(state: str) -> bool：done 与 cancelled 为终态；未知状态抛 ValueError

示例（必须满足）：
next_state("created", "pay") == "paid"
next_state("paid", "ship") == "shipped"
next_state("shipped", "finish") == "done"
next_state("created", "cancel") == "cancelled"
allowed_actions("created") == ["cancel", "pay"]
allowed_actions("done") == []
is_terminal("done") == True
is_terminal("paid") == False
""",
    },
    {
        "slug": "text-template",
        "name": "文本处理：分词、模板渲染、摘要",
        "scale": "S",
        "req": """代码规模：S

1) textproc.py：文本处理的纯函数库（只用标准库与正则）。
   - words(s: str) -> list[str]：按非字母数字字符切分，全部转小写，丢掉空串
   - render(tpl: str, ctx: dict) -> str：把模板里的 {name} 替换成 ctx[name]；缺字段抛 ValueError；ctx 里多余键忽略
   - snippet(s: str, width: int) -> str：取前 width 个字符，超出部分用 "…" 结尾（总长度不超过 width）；width 小于等于 0 抛 ValueError

示例（必须满足）：
words("Hello, world! 42") == ["hello", "world", "42"]
words("") == []
render("Hi {name}", {"name": "Ann"}) == "Hi Ann"
render("{a}-{b}", {"a": "1", "b": "2", "c": "3"}) == "1-2"
snippet("abcdef", 3) == "ab…"
snippet("ab", 3) == "ab"
""",
    },
]

BATCH3 = [
    {
        "slug": "service-layer-m",
        "name": "M 级多模块服务层：模型 → 服务 → 报表（跨模块调用）",
        "scale": "M",
        "req": """代码规模：M

1) models.py：领域对象的构造与读取（纯数据，不做校验以外的副作用）。
   - make_user(name: str, age: int) -> dict：返回 {"name": name, "age": age}；name 为空或 age 不在 0 到 150 之间抛 ValueError
   - user_age(user: dict) -> int：取 user 的 age；缺 age 抛 ValueError

2) service.py：注册与查询服务，内部必须调用 models 里的函数（不许重复实现）。
   - register(store: dict, name: str, age: int) -> dict：用 models.make_user 造对象，写入 store（以 name 为键），返回该对象；重复注册同名用户抛 ValueError
   - find(store: dict, name: str) -> dict：命中返回该对象，未命中返回 {}

3) report.py：对用户列表做汇总。
   - summary(users: list[dict]) -> dict：返回 {"count": 人数, "avg_age": 平均年龄保留 1 位小数}；空列表时 avg_age 为 0.0

示例（必须满足）：
make_user("ann", 30) == {"name": "ann", "age": 30}
user_age({"name": "ann", "age": 30}) == 30
register({}, "ann", 30) == {"name": "ann", "age": 30}
find({}, "ann") == {}
summary([]) == {"count": 0, "avg_age": 0.0}
summary([{"name": "a", "age": 20}, {"name": "b", "age": 31}]) == {"count": 2, "avg_age": 25.5}
""",
    },
    {
        "slug": "file-io-cli",
        "name": "带真实文件副作用的 CLI：CSV 写入与读回",
        "scale": "S",
        "req": """代码规模：S

1) csvkit.py：CSV 读写的业务层，会真的读写磁盘文件（相对当前目录）。
   - write_csv(path: str, rows: list[dict]) -> int：把行写成带表头的 CSV，返回写入的行数；rows 为空时只写表头并返回 0
   - read_csv(path: str) -> list[dict]：读回 CSV，每行是"列名→字符串值"的字典，保持文件里的顺序
   - summarize_csv(path: str) -> dict：返回 {"rows": 行数, "columns": 表头列表}

示例（必须满足）：
write_csv("t_empty.csv", []) == 0
read_csv("t_empty.csv") == []
write_csv("t_two.csv", [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]) == 2
read_csv("t_two.csv") == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
summarize_csv("t_two.csv") == {"rows": 2, "columns": ["a", "b"]}
""",
    },
    {
        "slug": "concurrency",
        "name": "并发任务池：分块 + 线程池 + 保序聚合",
        "scale": "S",
        "req": """代码规模：S

1) pool.py：并发执行的业务层（用标准库线程池，返回值必须保序）。
   - chunk(xs: list[int], n: int) -> list[list[int]]：按 n 切块，最后一块可以不满；n 小于等于 0 抛 ValueError；空列表返回 []
   - run_parallel(xs: list[int], workers: int) -> list[int]：并发计算每个元素的平方，**结果顺序与输入一致**；workers 小于 1 抛 ValueError
   - sum_parallel(xs: list[int], workers: int) -> int：并发求和；空列表返回 0

示例（必须满足）：
chunk([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]
chunk([], 3) == []
run_parallel([1, 2, 3], 2) == [1, 4, 9]
run_parallel([], 2) == []
sum_parallel([1, 2, 3, 4], 4) == 10
sum_parallel([], 1) == 0
""",
    },
    {
        "slug": "local-model",
        "name": "纯本地模型（不走远端）：降级路径是否诚实",
        "scale": "S",
        "local": True,
        "req": """代码规模：S

1) units.py：单位换算（纯函数）。
   - km_to_mi(km: float) -> float：公里换英里，保留 4 位小数（1 公里 = 0.621371 英里）
   - c_to_f(c: float) -> float：摄氏度换华氏度
   - bytes_to_human(n: int) -> str：把字节数变成 "1.5 KB" 这种字符串，单位取 B/KB/MB/GB，保留 1 位小数；n 为负抛 ValueError

示例（必须满足）：
km_to_mi(1.0) == 0.6214
km_to_mi(0.0) == 0.0
c_to_f(0.0) == 32.0
c_to_f(100.0) == 212.0
bytes_to_human(1536) == "1.5 KB"
bytes_to_human(0) == "0.0 B"
""",
    },
    {
        "slug": "contradictory",
        "name": "故意自相矛盾的契约：必须在花钱前如实拒绝",
        "scale": "S",
        "expect": "refuse",
        "req": """代码规模：S

1) fee.py：手续费计算（纯函数）。
   - calc_fee(amount: float) -> float：按 1% 收手续费，保留 2 位小数

示例（必须满足）：
calc_fee(100.0) == 1.0
calc_fee(100.0) == 1.5
""",
    },
    {
        "slug": "large-l",
        "name": "L 级仓库：L 级门禁（覆盖/变异/架构）真的会执行吗",
        "scale": "L",
        "req": """代码规模：L

1) shapes.py：几何计算（纯函数）。
   - area_rect(w: float, h: float) -> float：矩形面积；任一参数小于 0 抛 ValueError
   - area_circle(r: float) -> float：圆面积，保留 2 位小数（π 取 3.141592653589793）；r 小于 0 抛 ValueError
   - perimeter_rect(w: float, h: float) -> float：矩形周长

2) parse.py：解析几何输入（纯函数）。
   - parse_size(s: str) -> float：接受 "10" 或 "10px"，返回数值；无法解析或为负抛 ValueError
   - parse_pair(s: str) -> list[float]：接受 "3x4"，返回 [3.0, 4.0]；格式不对抛 ValueError

3) agg.py：批量统计（纯函数）。
   - total_area(items: list[dict]) -> float：每项含 w 与 h，返回面积之和，保留 2 位小数；空列表返回 0.0
   - max_area(items: list[dict]) -> float：返回最大面积；空列表抛 ValueError

示例（必须满足）：
area_rect(2.0, 3.0) == 6.0
area_circle(1.0) == 3.14
perimeter_rect(2.0, 3.0) == 10.0
parse_size("10") == 10.0
parse_size("10px") == 10.0
parse_pair("3x4") == [3.0, 4.0]
total_area([]) == 0.0
total_area([{"w": 2.0, "h": 3.0}]) == 6.0
max_area([{"w": 2.0, "h": 3.0}, {"w": 1.0, "h": 1.0}]) == 6.0
""",
    },
]

BATCH4 = [
    {
        "slug": "codec-binary",
        "name": "编码/哈希：十六进制、Base64、异或、汉明距离",
        "scale": "S",
        "req": """代码规模：S

1) codec.py：二进制与文本编码的纯函数（只用标准库）。
   - to_hex(data: str) -> str：按 UTF-8 编码后转小写十六进制；空串返回空串
   - from_hex(s: str) -> str：把十六进制解回文本；长度是奇数或含非十六进制字符抛 ValueError；空串返回空串
   - b64(data: str) -> str：按 UTF-8 编码后做标准 Base64
   - xor_bytes(data: str, key: int) -> str：把每个字节与 key 异或，返回十六进制；key 不在 0 到 255 之间抛 ValueError
   - hamming(a: str, b: str) -> int：两个十六进制串之间不同字节的个数；长度不等抛 ValueError

示例（必须满足）：
to_hex("a") == "61"
to_hex("") == ""
from_hex("61") == "a"
from_hex("") == ""
b64("a") == "YQ=="
b64("") == ""
xor_bytes("a", 0) == "61"
xor_bytes("a", 1) == "60"
hamming("61", "61") == 0
hamming("6162", "6062") == 1
""",
    },
    {
        "slug": "search-index",
        "name": "倒排索引：分词 → 建索引 → 检索（多模块）",
        "scale": "M",
        "req": """代码规模：M

1) text.py：分词。
   - tokenize(s: str) -> list[str]：按非字母数字切分、全部转小写、丢掉空串
   - normalize(s: str) -> str：去首尾空格并把连续空白压成一个空格

2) index.py：建索引与检索。
   - build(docs: list[dict]) -> dict：每篇文档形如 {"id": 1, "text": "..."}；返回 {"index": {词: [id 升序]}, "docs": {id: text}}；id 重复抛 ValueError
   - search(idx: dict, q: str) -> list[int]：命中查询里任意一个词的文档 id，升序去重；空查询返回 []
   - doc_text(idx: dict, doc_id: int) -> str：取回文档原文；不存在抛 ValueError

示例（必须满足）：
tokenize("Hello, World") == ["hello", "world"]
tokenize("") == []
normalize("  a   b ") == "a b"
build([{"id": 1, "text": "a b"}, {"id": 2, "text": "b c"}]) == {"index": {"a": [1], "b": [1, 2], "c": [2]}, "docs": {1: "a b", 2: "b c"}}
search({"index": {"a": [1], "b": [1, 2]}, "docs": {1: "x", 2: "y"}}, "b") == [1, 2]
search({"index": {"a": [1]}, "docs": {1: "x"}}, "") == []
doc_text({"index": {}, "docs": {1: "x"}}, 1) == "x"
""",
    },
    {
        "slug": "http-retry",
        "name": "HTTP 客户端策略：状态分类、重试判定、退避、头解析",
        "scale": "S",
        "req": """代码规模：S

1) retry.py：HTTP 客户端策略的纯函数层（不发请求）。
   - classify(status: int) -> str：2xx 返回 "ok"，3xx 返回 "redirect"，4xx 返回 "client"，5xx 返回 "server"；其他抛 ValueError
   - should_retry(status: int, attempt: int, max_attempts: int) -> bool：server 类与 429 在 attempt 小于 max_attempts 时重试，其余不重试；max_attempts 小于 1 抛 ValueError
   - backoff(attempt: int, base: float, cap: float) -> float：返回 base 乘以 2 的 attempt 次方与 cap 的较小值；attempt 小于 0 抛 ValueError
   - parse_headers(raw: str) -> dict：每行 "键: 值"，键统一转小写、值去掉首尾空格，返回字典；空行跳过；没有冒号的行抛 ValueError

示例（必须满足）：
classify(200) == "ok"
classify(301) == "redirect"
classify(404) == "client"
classify(503) == "server"
should_retry(503, 0, 3) == True
should_retry(503, 3, 3) == False
should_retry(404, 0, 3) == False
should_retry(429, 0, 3) == True
backoff(0, 0.5, 10.0) == 0.5
backoff(3, 0.5, 10.0) == 4.0
backoff(9, 0.5, 10.0) == 10.0
parse_headers("Content-Type: text/html\\nX-Id: 7") == {"content-type": "text/html", "x-id": "7"}
parse_headers("") == {}
""",
    },
    {
        "slug": "config-merge",
        "name": "配置处理：深合并、拍平、类型化取值",
        "scale": "S",
        "req": """代码规模：S

1) cfg.py：配置字典的处理（纯函数，不读文件）。
   - merge(base: dict, over: dict) -> dict：递归合并，over 覆盖 base 的同名键，返回新字典且不改动入参
   - flatten(d: dict, prefix: str) -> dict：把嵌套字典拍平成 "a.b" 形式的键；prefix 为空时键就是原始键名
   - get_int(d: dict, key: str, default: int) -> int：取 d[key] 并转成整数；取不到返回 default；值不是整数抛 ValueError

示例（必须满足）：
merge({"a": 1}, {"b": 2}) == {"a": 1, "b": 2}
merge({"a": 1}, {"a": 2}) == {"a": 2}
merge({"s": {"x": 1, "y": 2}}, {"s": {"y": 3}}) == {"s": {"x": 1, "y": 3}}
flatten({"a": {"b": 1}, "c": 2}, "") == {"a.b": 1, "c": 2}
flatten({"a": {"b": 1}}, "p") == {"p.a.b": 1}
get_int({"n": "7"}, "n", 0) == 7
get_int({}, "n", 0) == 0
""",
    },
]

BATCH5 = [
    {
        "slug": "embedded-frame",
        "name": "嵌入式协议帧：CRC16 + 定宽位域 + 帧组包/解包（bytes）",
        "scale": "S",
        "req": """代码规模：S

1) frame.py：嵌入式串口协议帧（纯函数，不碰硬件）。
   - crc16(data: bytes) -> int：CRC-16/MODBUS（初值 65535、多项式 0xA001、低位在前）
   - build_frame(payload: bytes) -> bytes：返回 0xAA + 长度字节 + payload + CRC 低字节 + CRC 高字节
   - parse_frame(raw: bytes) -> dict：校验帧头与 CRC，通过返回 {"ok": True, "payload": <bytes>}，否则返回 {"ok": False, "error": "bad_crc"}
   - get_bits(reg: int, width: int, shift: int) -> int：从寄存器里取出从 shift 位开始的 width 位
   - set_bits(reg: int, width: int, shift: int, value: int) -> int：把 value 写进从 shift 位开始的 width 位，其余位保持不变；value 超出该宽度范围或 width 小于 1 抛 ValueError

示例（必须满足）：
crc16(b"123456789") == 19255
build_frame(b"\\x01\\x02") == b"\\xaa\\x02\\x01\\x02\\x81\\xe1"
parse_frame(b"\\xaa\\x02\\x01\\x02\\x81\\xe1") == {"ok": True, "payload": b"\\x01\\x02"}
parse_frame(b"\\xaa\\x02\\x01\\x02\\x00\\x00") == {"ok": False, "error": "bad_crc"}
get_bits(62975, 4, 8) == 5
set_bits(65535, 4, 8, 5) == 62975
""",
    },
    {
        "slug": "os-kernel",
        "name": "操作系统内核风格：轮转调度 + 首次适配分配 + 环形缓冲",
        "scale": "S",
        "req": """代码规模：S

1) sched.py：内核风格的数据结构与调度算法（纯函数，不碰真内核）。
   - rr_order(procs: list[dict], quantum: int) -> list[str]：每个进程形如 {"name": "a", "burst": 2}；按轮转逐个给一个时间片（长度 quantum），时间片用完还在运行的进程回到队尾，返回执行序列的名字；quantum 小于 1 抛 ValueError
   - first_fit(blocks: list[int], size: int) -> int：返回第一个容量不小于 size 的块下标，没有返回 -1；size 小于 0 抛 ValueError
   - ring_push(buf: list[int], cap: int, val: int) -> list[int]：环形缓冲入队，满了丢最旧的，返回新列表；cap 小于 1 抛 ValueError

示例（必须满足）：
rr_order([{"name": "a", "burst": 2}, {"name": "b", "burst": 1}], 1) == ["a", "b", "a"]
rr_order([{"name": "a", "burst": 2}, {"name": "b", "burst": 1}], 2) == ["a", "b"]
rr_order([], 1) == []
first_fit([5, 10, 3], 4) == 0
first_fit([1, 2], 5) == -1
ring_push([], 3, 1) == [1]
ring_push([1, 2, 3], 3, 4) == [2, 3, 4]
""",
    },
    {
        "slug": "user-system",
        "name": "用户系统：口令哈希 + 角色判定 + 登录（会话）",
        "scale": "S",
        "req": """代码规模：S

1) auth.py：用户系统的业务层（纯函数，口令只算哈希，不存明文）。
   - hash_pw(pw: str, salt: str) -> str：返回 sha256(salt 拼 pw) 十六进制的前 16 位
   - allowed(roles: list[str], need: str) -> bool：roles 里有 need 或者有 "*" 时通过
   - login(users: dict, name: str, pw: str, salt: str) -> dict：users 形如 {"ann": {"pw_hash": "...", "roles": ["admin"]}}；用户不存在返回 {"ok": False, "error": "no_user"}，口令不对返回 {"ok": False, "error": "bad_password"}，成功返回 {"ok": True, "roles": 角色列表}

示例（必须满足）：
hash_pw("p", "s") == "be18b85f77fc024d"
allowed(["admin"], "admin") == True
allowed([], "admin") == False
allowed(["*"], "ops") == True
login({}, "ann", "p", "s") == {"ok": False, "error": "no_user"}
login({"ann": {"pw_hash": "be18b85f77fc024d", "roles": ["admin"]}}, "ann", "p", "s") == {"ok": True, "roles": ["admin"]}
login({"ann": {"pw_hash": "be18b85f77fc024d", "roles": ["admin"]}}, "ann", "wrong", "s") == {"ok": False, "error": "bad_password"}
""",
    },
    {
        "slug": "stateful-class",
        "name": "类 API（不是函数库）：LRU 缓存类 —— 撞契约语法的兼容边界",
        "scale": "S",
        "expect": "class-mode",
        "req": """代码规模：S

1) lru_cache.py：最近最少使用缓存，对外是一个**类**。
   - class LRU：
     - __init__(self, cap: int) -> None：容量小于 1 抛 ValueError
     - get(self, k: str) -> int：命中返回值并把它标记为最近使用；未命中返回 -1
     - put(self, k: str, v: int) -> None：写入并标记为最近使用；超出容量时淘汰最久未使用的键
     - keys(self) -> list[str]：按"最近使用 → 最久未使用"的顺序返回现有键
   - 用法示例：
     c = LRU(2); c.put("a", 1); c.put("b", 2); c.get("a"); c.keys() -> ["a", "b"]

示例（必须满足）：
c = LRU(2)
c.keys() == []
c.get("a") == -1
c.put("a", 1)
c.get("a") == 1
c.put("b", 2)
c.get("a")
c.put("c", 3)
c.keys() == ["c", "a"]
""",
    },
]

ALL = {t["slug"]: t for t in BATCH1 + BATCH2 + BATCH3 + BATCH4 + BATCH5}
BATCHES = {"1": BATCH1, "2": BATCH2, "3": BATCH3, "4": BATCH4, "5": BATCH5}
