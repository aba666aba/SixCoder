<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref } from 'vue'

/* ---------------- 状态 ---------------- */
const health = ref(null)
const models = ref([])
const toolchains = ref([])
const sandboxInfo = ref(null)
const probeInfo = ref(null)
const busy = ref(false)
const errMsg = ref('')
const events = ref([])
const nodeMap = reactive({})
const taskInfo = ref(null)
const audit = ref(null)
const history = ref([])
const repos = ref([])
const fileList = ref([])
const fileView = ref(null)
const showTc = ref(false)

/* ---------------- 桌面外壳（Electron）原生能力 ---------------- */
const shellApi = typeof window !== 'undefined' ? window.electronAPI : undefined
const isShell = ref(!!shellApi?.isElectron)
const shellInfo = ref(null)

async function pickRepoDir() {
  if (!shellApi?.pickDirectory) return
  const p = await shellApi.pickDirectory({ title: '选择仓库目录' })
  if (p) { form.repo = p; await doProbe() }
}

async function pickToolchainDir(tc) {
  if (!shellApi?.pickDirectory) return
  const p = await shellApi.pickDirectory({ title: `指定 ${tc.id} 的安装目录（含 bin）` })
  if (!p) return
  try {
    const d = await api('/v1/toolchains/override', {
      method: 'POST', body: JSON.stringify({ id: tc.id, path: p })
    })
    await loadToolchains()
    errMsg.value = `${tc.id} → ${d.item?.version || '版本未知'}  ${d.item?.path || ''}`
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function clearToolchainDir(tc) {
  try {
    await api('/v1/toolchains/override', { method: 'POST', body: JSON.stringify({ id: tc.id, path: '' }) })
    await loadToolchains()
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function openWorkspace() { if (shellApi?.openPath && taskInfo.value?.workspace) shellApi.openPath(taskInfo.value.workspace) }
async function openRepo() { if (shellApi?.openPath && form.repo) shellApi.openPath(form.repo) }
const timelineEl = ref(null)
let es = null

const form = reactive({
  repo: '',
  requirement: '',
  target: '',
  model: ''
})

/* ---------------- API ---------------- */
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...opts })
  if (!res.ok) {
    let detail = res.statusText
    try { detail = (await res.json()).detail || detail } catch {}
    throw new Error(detail)
  }
  return res.json()
}

async function loadHealth() {
  try {
    health.value = await api('/v1/health')
    models.value = health.value.models || []
    modelPolicy.value = health.value.model_policy || null
    if (!form.model) form.model = health.value.default_model
  } catch (e) { errMsg.value = '后端未就绪：' + e.message }
}

/* 模型门槛（与后端 core/model_policy.py 同一口径）：本地工人必须 ≥7B。
   实测 3B 在同一个夜班任务上只有机械验收 2/8、独立验收 2/16，且多数失败是
   "生成代码无法解析" —— 所以下拉里照样列出来，但不让选，并说明原因。 */
const MIN_PARAMS_B = 7
const modelPolicy = ref(null)
function paramsB(name) {
  const m = /(\d+(?:\.\d+)?)\s*b\b/i.exec(String(name || ''))
  return m ? parseFloat(m[1]) : 0
}
function tooSmall(name) {
  const n = paramsB(name)
  return n > 0 && n < (modelPolicy.value?.min_params_b || MIN_PARAMS_B)
}
const currentModelTooSmall = computed(() => tooSmall(form.model))

/* 模型列表实时化：打开下拉、窗口聚焦、每 60s 各刷一次。
   之前只在页面加载时取一次 —— ollama pull 了新模型必须 F5 才能看到。 */
let modelsFetchedAt = 0
const modelsLoading = ref(false)

async function loadModels(force = false) {
  if (modelsLoading.value) return
  if (!force && Date.now() - modelsFetchedAt < 3000) return     // 3s 内合并重复请求
  modelsLoading.value = true
  try {
    const d = await api('/v1/models')
    const list = d.models || []
    if (list.length) models.value = list
    modelsFetchedAt = Date.now()
    if (list.length && !list.includes(form.model)) {
      const old = form.model
      form.model = list.includes(d.default) ? d.default : list[0]
      errMsg.value = old ? `模型 ${old} 已不可用，已切到 ${form.model}` : ''
    }
  } catch {} finally { modelsLoading.value = false }
}

async function loadToolchains() {
  try { toolchains.value = (await api('/v1/toolchains')).items || [] } catch {}
}

async function loadHistory() {
  try {
    history.value = (await api('/v1/tasks')).tasks || []
    repos.value = (await api('/v1/repos')).repos || []
  } catch {}
}

function normRepo(p) { return String(p || '').replace(/\\/g, '/').toLowerCase() }
function sameRepo(p) { return !!taskInfo.value && normRepo(taskInfo.value.repo) === normRepo(p) }

/* 点「用过的仓库」：切到该仓库目录 + 探测 + 载入它的对话 */
async function enterRepo(r) {
  if (busy.value) return
  resetSession()
  form.repo = r.repo
  saveSession()
  await doProbe()          // doProbe → syncRepoContext 会打开该仓库的对话
}

function askDeleteRepo(r) {
  delTarget.value = { id: r.task_id, repo: r.repo, state: r.state, requirement: r.requirement }
}

async function doProbe(deep = false) {
  if (!form.repo) return
  busy.value = true
  errMsg.value = deep ? '正在跑全量测试建立基线，大仓库可能需要几分钟…' : ''
  try {
    probeInfo.value = await api('/v1/probe', { method: 'POST',
      body: JSON.stringify({ repo: form.repo, deep: !!deep }) })
    sandboxInfo.value = await api('/v1/sandbox?repo=' + encodeURIComponent(form.repo))
    busy.value = false
    await syncRepoContext()
    return
  } catch (e) { errMsg.value = e.message } finally { busy.value = false }
}

/* ---------------- 设置 / 帮助 ---------------- */
const showSettings = ref(false)

/* 远端模型（DeepSeek）：key 只存本机设置文件，界面只显示掩码 */
const remote = ref({ available: false, reason: '', remote: {}, usage: {} })
const remoteForm = reactive({ api_key: '', model: '', base_url: '' })

async function loadRemote(reveal = false) {
  try {
    const d = await api('/v1/settings/remote' + (reveal ? '?reveal=true' : ''))
    remote.value = d
    remoteForm.model = d.remote?.model || ''
    remoteForm.base_url = d.remote?.base_url || ''
    if (reveal) remoteForm.api_key = d.remote?.key_plain || ''
    loadPricing(d)
    loadProfiles()        // 模型档案与"单模型思考强度阶梯"一起刷新（改了立刻能看到新阶梯）
  } catch (e) { errMsg.value = '[FAIL] 读取远端设置失败：' + e.message }
}

function revealRemoteKey() {
  if (remoteForm.api_key) { remoteForm.api_key = ''; return }   // 再点一次 = 隐藏
  loadRemote(true)
}

async function saveRemote(patch) {
  const body = {}
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === undefined || v === null) continue
    if (k === 'api_key') { if (String(v).trim()) body.api_key = String(v).trim(); continue }
    if (typeof v === 'string' && !v.trim()) continue
    body[k] = v
  }
  if (!Object.keys(body).length) { errMsg.value = '没有要保存的改动（key 输入框为空不会清除已有 key）'; return }
  try {
    const d = await api('/v1/settings/remote', { method: 'POST', body: JSON.stringify(body) })
    remote.value = d
    if (body.api_key) remoteForm.api_key = ''      // 保存后清空输入框，不回显
    errMsg.value = body.api_key ? '远端 key 已保存（明文存本地设置文件，重启不丢）' : '远端参数已更新，下一次请求即生效'
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function clearRemoteKey() {
  if (!window.confirm('确定清除已保存的远端 key？清除后远端协助不可用。')) return
  try {
    const d = await api('/v1/settings/remote', { method: 'POST', body: JSON.stringify({ clear_key: true }) })
    remote.value = d
    remoteForm.api_key = ''
    errMsg.value = '远端 key 已清除'
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

/* 价目表：改成跟着模型档案走（价格在档案里，人民币口径），这里只留闲时窗口 */
const windowForm = reactive({ start: '00:30', end: '08:30', tz: 'UTC' })

function loadPricing(d) {
  const w = (d && d.remote && d.remote.offpeak_window) || {}
  windowForm.start = w.start || '00:30'
  windowForm.end = w.end || '08:30'
  windowForm.tz = w.tz || 'UTC'
}

/* 当前模型的三个连接参数一起保存（模型名 / 接口 / key） */
async function saveCurrentModel() {
  const patch = { model: remoteForm.model, base_url: remoteForm.base_url }
  if (remoteForm.api_key) patch.api_key = remoteForm.api_key
  await saveRemote(patch)
  await loadRemote()
}

/* 切换当前模型（单模型：切换 = 整条阶梯换成它） */
async function useProfile(p) {
  try {
    const d = await api('/v1/settings/remote', { method: 'POST',
      body: JSON.stringify({ active_model: p.id }) })
    remote.value = d
    await loadRemote()
    errMsg.value = `当前模型已切换为 ${p.id}（思考强度仍由系统自动选）`
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function savePricingWindow() {
  try {
    const d = await api('/v1/settings/remote', { method: 'POST',
      body: JSON.stringify({ offpeak_window: { ...windowForm } }) })
    remote.value = d
    loadPricing(d)
    errMsg.value = '闲时窗口已保存'
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

/* 回归用例库：远端成功过的判据，同需求每次运行都要过 */
const regressions = ref([])

async function loadRegressions() {
  try {
    const d = await api('/v1/regressions?limit=50')
    regressions.value = d.items || []
  } catch (e) { errMsg.value = '[FAIL] 读取回归用例失败：' + e.message }
}

async function deleteRegression(id) {
  if (!window.confirm('删除这条回归用例？删除后同类问题不再自动检查。')) return
  try {
    const d = await api('/v1/regressions/' + id, { method: 'DELETE' })
    regressions.value = d.items || []
    errMsg.value = '已删除回归用例 #' + id
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function clearRegressions() {
  if (!window.confirm('清空整个回归用例库？')) return
  try {
    const d = await api('/v1/regressions', { method: 'DELETE' })
    regressions.value = d.items || []
    errMsg.value = '已清空 ' + (d.cleared || 0) + ' 组回归用例'
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}
const settingsTab = ref('help')

const HELP_POINTS = [
  '写出要改的**文件路径**（如 calc.py、service.py）：规划器靠它识别模块清单，写得越明确，计划越稳。',
  '一句话一个行为：要什么函数/类、参数、返回值，以及**异常怎么处理**（如"金额为 0 时抛 ValueError"）。',
  '多模块项目请逐个点名：`models.py（Transaction+校验）`、`storage.py（JSON 原子写）`…，规划器会按依赖顺序给每个模块配一对"测试→实现"节点。',
  '不要写"优化一下代码"这类模糊目标——它无法被测试验证，闸门只会判它没有完成。'
]

const HELP_EXAMPLES = [
  {
    title: '单函数 + 异常（最快上手）',
    target: 'calc.py',
    text: '给 calc.py 增加一个 divide(a, b) 函数：返回 a/b，当 b 为 0 时抛 ValueError("除数不能为 0")。并补上对应的 pytest 测试，覆盖正常除法与除零两种路径。'
  },
  {
    title: '带业务规则的函数',
    target: 'calc.py',
    text: '给 calc.py 增加一个 clamp(v, lo, hi) 函数：把 v 限制在 [lo, hi]；当 lo > hi 时抛 ValueError("区间非法")。并补上 pytest 测试覆盖正常与非法区间。'
  },
  {
    title: '多模块项目（4 文件）',
    target: '',
    text: '用 Python 从零实现一个个人记账命令行工具，所有模块放在仓库根目录：\n1) models.py：Transaction 数据类，字段 id、date、amount、category、note；amount 不能为 0（否则抛 ValueError），正数收入、负数支出；date 必须是 YYYY-MM-DD。\n2) storage.py：JSON 持久化，load(path) 返回 list[dict]（文件不存在返回空列表），save(path, rows) 原子写。\n3) service.py：依赖 models 与 storage，add(...) 追加并保存，summary(path) 返回收入/支出/余额。\n4) cli.py：argparse 子命令 add / list / summary，输出中文。\n每个模块都要有对应的 pytest 测试，覆盖正常与异常路径。'
  }
]

function useExample(ex) {
  form.requirement = ex.text
  if (ex.target) form.target = ex.target
  showSettings.value = false
  errMsg.value = '示例已填入需求框，按需修改后点「发送」'
}

/* ---------------- 提交信息规范 ---------------- */
const COMMIT_TYPES = [
  { id: 'feat', label: 'feat: 新功能', desc: '新增功能或行为。示例：feat: 增加 divide 函数与除零校验' },
  { id: 'fix', label: 'fix: 修缺陷', desc: '修复 bug。示例：fix: calc_total 未处理折扣导致金额偏高' },
  { id: 'test', label: 'test: 测试', desc: '只改测试或补测试覆盖。示例：test: 为 clamp 补边界与非法区间用例' },
  { id: 'refactor', label: 'refactor: 重构', desc: '不改变外部行为的结构调整。示例：refactor: 把 storage 的读写拆成两个私有函数' },
  { id: 'perf', label: 'perf: 性能', desc: '性能优化。示例：perf: 索引改为增量更新，减少全量重建' },
  { id: 'docs', label: 'docs: 文档', desc: '文档 / 注释 / README。示例：docs: 补充 CLI 子命令用法' },
  { id: 'style', label: 'style: 格式', desc: '格式、空行、import 顺序等（不改逻辑）' },
  { id: 'chore', label: 'chore: 杂项', desc: '构建配置、依赖、脚本。示例：chore: 升级 pytest 到 9.x' },
  { id: 'revert', label: 'revert: 回滚', desc: '回滚此前的提交。示例：revert: 回滚 feat: 增加 divide 函数' }
]

const commitType = ref('feat')
const commitMsg = ref('')

const commitFull = computed(() => {
  const body = (commitMsg.value || '').trim()
  // 用户自己写了规范前缀就原样用，不重复加
  if (/^[a-z]+(\([\w\-.]+\))?!?:\s/.test(body)) return body
  if (!body) return `${commitType.value}: 更新（未填描述）`
  return `${commitType.value}: ${body}`
})

/* ---------------- 顶栏下拉菜单：帮助 / 登录选项 ---------------- */
const openMenu = ref('')            // '' | 'settings'
const loginOpen = ref(false)        // 兼容旧菜单，保留但不再使用
const showRemoteModal = ref(false)
const showLoginModal = ref(false)
const buildId = ref('')

function openRemote() { showRemoteModal.value = true; loadRemote(); loadRegressions() }
function openLogin() { showLoginModal.value = true; loadLoginState() }
const openAtLogin = ref(false)
const loginSupported = ref(false)   // 只有桌面外壳且主进程支持时才可切换

function toggleMenu(name) { openMenu.value = openMenu.value === name ? '' : name }

function openHelp(section) {
  settingsTab.value = section
  showSettings.value = true
  openMenu.value = ''
  if (section === 'remote') loadRemote()
}

async function loadLoginState() {
  loginSupported.value = !!(shellApi && shellApi.getLoginItem)
  if (!loginSupported.value) return
  try { openAtLogin.value = !!(await shellApi.getLoginItem()) } catch { openAtLogin.value = false }
}

async function toggleAutoStart() {
  if (!loginSupported.value) { errMsg.value = '开机自启需要桌面版（Electron）运行'; return }
  const next = !openAtLogin.value
  try {
    const applied = await shellApi.setLoginItem(next)
    openAtLogin.value = !!applied
    errMsg.value = applied ? '已开启开机自启' : '已关闭开机自启'
  } catch (e) { errMsg.value = '[FAIL] 设置开机自启失败：' + e.message }
}

/* 所有输入框的填写规范（集中放在设置里，界面上不预填、不塞长说明） */
const FIELD_HELP = [
  {
    group: '任务',
    rows: [
      { field: '仓库路径', where: '左栏·仓库', how: '要改造的仓库绝对路径。桌面版点「浏览…」选目录；网页版手填。点「探测」会显示形态与工具链。', demo: 'E:\\proj\\ledger' },
      { field: '目标文件', where: '左栏·任务', how: '可选。只想改某个文件时填相对路径；留空则由规划器按需求里的文件名决定。', demo: 'calc.py' },
      { field: '需求', where: '左栏·任务', how: '写清三件事：改哪个文件（直接在需求里写文件名，如 clean.py）、要什么行为、异常怎么处理。多模块项目请逐个点名 xxx.py，规划器会按依赖顺序给每个模块配一对"测试→实现"节点。', demo: '给 calc.py 增加 divide(a,b)：返回 a/b，b 为 0 时抛 ValueError("除数不能为 0")，并补 pytest 测试。' }
    ]
  },
  {
    group: '代码审查',
    rows: [
      { field: '预期功能', where: '中栏·工作台→代码审查', how: '写"本该怎样"，审查会拿它和实现比对，指出缺功能/行为不一致。留空则只跑确定性规则（不调模型）。', demo: '所有入口参数需校验，非法输入抛 ValueError；金额计算需支持折扣；每个公开函数必须有测试。' }
    ]
  },
  {
    group: 'Git',
    rows: [
      { field: '提交类型', where: '中栏·工作台→Git', how: '下拉选一个前缀（feat/fix/test/refactor/perf/docs/style/chore/revert），说明见「提交规范」页。', demo: 'feat' },
      { field: '提交描述', where: '中栏·工作台→Git', how: '只写"改了什么"，不用写前缀，引擎拼成「类型: 描述」。正文自带规范前缀时不会重复加。', demo: '增加 divide 函数与除零校验' },
      { field: '文件路径（撤回）', where: '中栏·工作台→Git', how: '相对仓库的路径。更省事的做法：直接点上方「改动文件」列表里那一行的「撤回」/「沙箱撤回」。', demo: 'calc.py' },
      { field: '新分支名', where: '中栏·工作台→Git', how: '点「新建并切换」使用；不能含空格与 ~^:?*[\\ 等字符。', demo: 'feature/divide' },
      { field: '切换到的分支 / 合并源分支', where: '中栏·工作台→Git', how: '填已存在的分支名。合并会把源分支合进当前分支（--no-ff），冲突时会列出冲突文件。', demo: 'master' },
      { field: '远端仓库地址', where: '中栏·工作台→Git', how: '本地裸仓库路径或 https/ssh 地址。点「设置远端」写入 origin；远端相关按钮需要「甲方模式」或「开发者模式」。', demo: 'E:\\remotes\\ledger.git' }
    ]
  },
  {
    group: '设置',
    rows: [
      { field: '工具链目录', where: '设置→工具链', how: '桌面版点「浏览…」选安装目录（如 JDK 根目录，里面应有 bin\\java.exe）。探测顺序：设置覆盖 > 环境变量 > PATH > 常见目录。', demo: 'D:\\JDK17' }
    ]
  }
]

/* ---------------- 删除历史任务（需确认） ---------------- */
const delTarget = ref(null)      // 待删除的任务对象
const delBusy = ref(false)

function askDelete(t) {
  delTarget.value = t
}

async function doDelete(withWorkspace) {
  const t = delTarget.value
  if (!t) return
  delBusy.value = true
  try {
    const d = await api(`/v1/tasks/${t.id}?with_workspace=${withWorkspace ? 'true' : 'false'}`,
      { method: 'DELETE' })
    if (taskInfo.value?.id === t.id) {
      taskInfo.value = null; events.value = []; audit.value = null
      fileList.value = []; fileView.value = null
      Object.keys(nodeMap).forEach((k) => delete nodeMap[k])
      if (es) { es.close(); es = null }
    }
    delTarget.value = null
    errMsg.value = `已删除 ${t.id}${d.workspace_removed ? '（含工作区副本）' : ''}`
    await loadHistory()
    await refreshGit()
  } catch (e) {
    errMsg.value = '[FAIL] ' + e.message
  } finally { delBusy.value = false }
}

/* ---------------- 一键清空历史 ---------------- */
const showClear = ref(false)
const clearBusy = ref(false)

async function doClearAll(withWorkspace) {
  clearBusy.value = true
  try {
    const d = await api(`/v1/tasks?confirm=all&with_workspace=${withWorkspace ? 'true' : 'false'}`, { method: 'DELETE' })
    taskInfo.value = null; events.value = []; audit.value = null
    fileList.value = []; fileView.value = null; reviewResult.value = null
    Object.keys(nodeMap).forEach((k) => delete nodeMap[k])
    if (es) { es.close(); es = null }
    showClear.value = false
    errMsg.value = `已清空 ${d.deleted_count} 个历史任务`
      + (d.kept_running?.length ? `（保留运行中的 ${d.kept_running.length} 个）` : '')
      + (d.workspace_removed ? `，删除工作区 ${d.workspace_removed} 个` : '')
    await loadHistory()
    await refreshGit()
  } catch (e) { errMsg.value = '[FAIL] ' + e.message } finally { clearBusy.value = false }
}

/* ---------------- stuck（有节点没通过）处置：接受 / 丢弃 / 续跑 ---------------- */
const resolveTarget = ref(null)     // { node, reason }
const resolveBusy = ref(false)

async function doResolve(action) {
  const t = resolveTarget.value
  if (!t || !taskInfo.value?.id) return
  resolveBusy.value = true
  try {
    const d = await api(`/v1/tasks/${taskInfo.value.id}/nodes/${t.node.id}/resolve`, {
      method: 'POST', body: JSON.stringify({ action, reason: t.reason || '' })
    })
    errMsg.value = `节点 ${t.node.path} 已${action === 'accept' ? '接受' : '丢弃'}；任务状态 ${d.task_state}`
    resolveTarget.value = null
    await refresh(taskInfo.value.id)
    await loadHistory()
  } catch (e) { errMsg.value = '[FAIL] ' + e.message } finally { resolveBusy.value = false }
}

async function doRetryTask() {
  if (!taskInfo.value?.id) return
  try {
    const d = await api(`/v1/tasks/${taskInfo.value.id}/retry`, { method: 'POST' })
    errMsg.value = `已基于该工作区新建任务 ${d.task_id}（在其中继续修改）`
    await loadHistory()
    await openTask(d.task_id)
    connect(d.task_id)
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

/* ---------------- 事件流 ---------------- */
function pushEvent(ev) {
  events.value.push(ev)
  const p = ev.payload || {}
  if (ev.kind === 'node_start') {
    nodeMap[p.node] = { id: p.node, kind: p.kind, path: p.path, mode: p.mode,
                        contract: p.contract, status: 'running' }
  } else if (ev.kind === 'node_done' && nodeMap[p.node]) {
    nodeMap[p.node].status = 'done'
  } else if (ev.kind === 'node_failed' && nodeMap[p.node]) {
    nodeMap[p.node].status = 'stuck'      // 有节点没通过（不是"等审批"）
  } else if (ev.kind === 'audit') {
    audit.value = p
  } else if (ev.kind === 'done') {
    taskInfo.value = { ...(taskInfo.value || {}), state: p.state }
    loadFiles(p.workspace ? taskInfo.value.id : null)
    refreshGit()
  }
  nextTick(() => {
    const el = timelineEl.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function connect(taskId) {
  if (es) es.close()
  es = new EventSource(`/v1/tasks/${taskId}/stream`)
  es.onmessage = (m) => {
    const ev = JSON.parse(m.data)
    if (ev.kind === 'stream_end') {
      es.close(); es = null; busy.value = false
      refresh(taskId)
      return
    }
    pushEvent(ev)
  }
}

/* 系统自检不在界面上：它只回答"这台机器/这套流程现在好不好"，属于排查工具。
   入口只有两个（见 README）：
     python cli.py selfcheck           # 命令行，跑一遍最小的完整任务
     POST /v1/demo → POST /v1/tasks    # 端口，给自动化用
   真实水平看 projects/ 里的 DELIVERY.md / TRACE.md，或用 tools/bench_night.py 做多次基准。 */

/* ---------------- 夜班（甲方模式的正常用法）----------------
   甲方模式 = 写代码的完全体：提需求 → 系统内部做完所有决策 → 早上看交付报告。
   与「发送」的单任务不同，夜班会反复体检/派工/开发/机械验收，直到收敛或预算耗尽。
   协作方式由 core/policy.py 决定：甲方模式代码默认由远端写（本地 7B 兜底），
   开发者模式必须由人给实现方案。界面只负责把这件事摆到手边。 */
const night = reactive({ hours: 6, plan: '', taskId: '', busy: false, info: null,
                         // 右上角选定的远端模型档案（flash / pro）；空 = 整条八档阶梯
                         remoteProfile: '',
                         // 每夜花费硬上限（元）。默认 1 元，自己可改；0 = 不设上限
                         maxCny: 1,
                         delivery: '', lines: [],
                         // 目标代码规模：不同规模的约束完全不同（core/scale.py）。
                         // 甲方模式的硬要求：提需求时就写明，不写就不是"明确的需求"。
                         scale: 'S',
                         scaleTiers: [
                           { id: 'S', label: 'S 级 · < 10 万行（小项目/单团队）',
                             hint: '只挡基本错误：构建/类型/Lint/单测/密钥；不启用架构测试与变异测试' },
                           { id: 'M', label: 'M 级 · 10–50 万行（多模块生产）',
                             hint: '加架构依赖方向测试 + 覆盖率地板 80% + 工作集声明' },
                           { id: 'L', label: 'L 级 · 50–100 万行（多团队/受控自主）',
                             hint: '加变更行变异测试 + 受保护区自动拒绝 + 依赖白名单' },
                           { id: 'XL', label: 'XL 级 · > 100 万行（超大规模）',
                             hint: '持续架构健康度指标；人类保留架构 owner（无全自主实证）' },
                         ] })
let nightEs = null
const isClientMode = computed(() => currentMode.value?.id === 'client')

/* ---------------- 模型档案（八档阶梯，可增删改） ---------------- */
const remoteModels = ref([])
const remoteLadder = ref([])
const escalateAfter = ref(2)

async function loadProfiles() {
  try {
    const d = await api('/v1/settings/remote/models')
    remoteModels.value = (d.models || []).map((p) => ({
      ...p,
      effortsText: (p.efforts || []).join(','),
      pricing: p.pricing || { offpeak: { in_miss: 0, in_hit: 0, out: 0 },
                              peak: { in_miss: 0, in_hit: 0, out: 0 } },
    }))
    remoteLadder.value = d.ladder || []
    escalateAfter.value = d.escalate_after ?? 2
  } catch (e) { /* 设置没打开时不必打扰用户 */ }
}

async function saveProfile(p) {
  try {
    const d = await api('/v1/settings/remote/models', { method: 'POST', body: JSON.stringify({
      id: p.id, model: p.model, label: p.label, priority: Number(p.priority),
      enabled: !!p.enabled, efforts: (p.effortsText || '').split(',').map((s) => s.trim()).filter(Boolean),
      max_output_tokens: Number(p.max_output_tokens), pricing: p.pricing,
    }) })
    remoteLadder.value = d.ladder || []
    errMsg.value = `模型档案「${p.id}」已保存，下一次请求立即生效`
    loadRemote()
  } catch (e) { errMsg.value = e.message }
}

async function addProfile() {
  const id = window.prompt('新档案 id（短名字，例如 pro2）')
  if (!id) return
  const model = window.prompt('模型名（DeepSeek 目前只认 deepseek-flash / deepseek-v4-pro）',
                              'deepseek-flash')
  if (!model) return
  try {
    const d = await api('/v1/settings/remote/models', { method: 'POST', body: JSON.stringify({
      id, model, label: id, priority: 90, enabled: true,
      efforts: ['none', 'low', 'high', 'max'], max_output_tokens: 8000,
    }) })
    remoteModels.value = d.models || []
    remoteLadder.value = d.ladder || []
    await loadProfiles()
    errMsg.value = `已新增模型档案「${id}」`
  } catch (e) { errMsg.value = e.message }
}

async function deleteProfile(id) {
  if (!window.confirm(`删除模型档案「${id}」？删完阶梯里就没有它了。`)) return
  try {
    const d = await api(`/v1/settings/remote/models/${encodeURIComponent(id)}`,
                        { method: 'DELETE' })
    remoteModels.value = d.models || []
    remoteLadder.value = d.ladder || []
    errMsg.value = `已删除「${id}」`
  } catch (e) { errMsg.value = e.message }
}

function nightBrief(p) {
  const o = p || {}
  const keys = ['round', 'strategy', 'verified', 'total', 'symbol', 'path', 'order',
                'why', 'reason', 'ok', 'used', 'message', 'text', 'question']
  const out = keys.filter((k) => o[k] !== undefined && o[k] !== null && o[k] !== '')
    .map((k) => `${k}=${String(o[k]).replace(/\s+/g, ' ').slice(0, 90)}`)
  return out.join(' ').slice(0, 220)
}

async function startNight() {
  if (!form.repo || !form.requirement.trim()) {
    errMsg.value = '夜班需要仓库和需求'; return
  }
  if (!night.scale) { errMsg.value = '请选择目标代码规模（S/M/L/XL）——约束按规模分级，选错等于没约束'; return }
  night.busy = true; errMsg.value = ''; night.lines = []; night.delivery = ''
  night.info = null
  try {
    const d = await api('/v1/night', { method: 'POST', body: JSON.stringify({
      repo: form.repo, requirement: form.requirement, model: form.model,
      hours: Number(night.hours) || 6, plan: night.plan, scale: night.scale,
      remote_profile: night.remoteProfile,
      max_cny: Number(night.maxCny) || 0 }) })
    night.taskId = d.task_id
    night.info = { state: 'running', mode: d.mode, policy: d.policy, verified: 0, total: 0 }
    connectNight(d.task_id)
  } catch (e) { night.busy = false; errMsg.value = e.message }
}

function connectNight(taskId) {
  if (nightEs) nightEs.close()
  nightEs = new EventSource(`/v1/tasks/${taskId}/stream`)
  nightEs.onmessage = (m) => {
    const ev = JSON.parse(m.data)
    if (ev.kind === 'stream_end') {
      nightEs.close(); nightEs = null; night.busy = false
      loadNight(taskId)
      return
    }
    night.lines.push({ kind: ev.kind, ts: ev.ts, text: nightBrief(ev.payload) })
    if (night.lines.length > 500) night.lines.shift()
  }
}

async function loadNight(taskId) {
  const id = taskId || night.taskId
  if (!id) return
  try {
    const d = await api(`/v1/night/${id}`)
    night.info = d
    night.delivery = d.delivery_md || ''
    if (d.state && d.state !== 'running') night.busy = false
  } catch (e) { errMsg.value = e.message }
}

function onSendShortcut() {
  if (!busy.value && can('agent.run')) startTask()
}

async function startTask() {
  if (!form.repo || !form.requirement.trim()) { errMsg.value = '请先选择仓库并填写需求'; return }
  busy.value = true; errMsg.value = ''
  events.value = []; audit.value = null; taskInfo.value = null; fileList.value = []; fileView.value = null
  Object.keys(nodeMap).forEach((k) => delete nodeMap[k])
  try {
    const d = await api('/v1/tasks', { method: 'POST', body: JSON.stringify({
      repo: form.repo, requirement: form.requirement, target: form.target, model: form.model
    }) })
    taskInfo.value = { id: d.task_id, model: d.model, state: 'running' }
    saveSession()
    connect(d.task_id)
  } catch (e) { busy.value = false; errMsg.value = e.message }
}

/* ---------------- 会话上下文：记住上次仓库 / 同仓库回到上次任务 ---------------- */
const LS_REPO = 'sixcoder.lastRepo'
const SESSION_KEY = 'sixcoder.session'   // { repo, taskId, ts } —— 关闭/崩溃后仍能恢复
let pendingTaskId = ''                   // 挂载时从会话快照带出来的"上次看的那条任务"
let sessionTimer = null

function saveSession() {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify({
      repo: (form.repo || '').trim(),
      taskId: taskInfo.value?.id || '',
      ts: Date.now(),
    }))
    if (form.repo) localStorage.setItem(LS_REPO, (form.repo || '').trim())
  } catch {}
}

function saveSessionSoon() {          // 防抖：仓库框边打字边记，崩溃也不丢
  if (sessionTimer) clearTimeout(sessionTimer)
  sessionTimer = setTimeout(saveSession, 400)
}

function loadSession() {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    if (raw) {
      const d = JSON.parse(raw)
      return { repo: d.repo || '', taskId: d.taskId || '' }
    }
    const legacy = localStorage.getItem(LS_REPO)
    return { repo: legacy || '', taskId: '' }
  } catch { return { repo: '', taskId: '' } }
}

function resetSession() {
  if (es) { es.close(); es = null }
  taskInfo.value = null
  events.value = []
  audit.value = null
  fileList.value = []
  fileView.value = null
  reviewResult.value = null
  Object.keys(nodeMap).forEach((k) => delete nodeMap[k])
}

async function syncRepoContext() {
  const repo = (form.repo || '').trim()
  if (!repo) return
  try { localStorage.setItem(LS_REPO, repo) } catch {}
  let list = []
  try { list = (await api('/v1/tasks?repo=' + encodeURIComponent(repo))).tasks || [] } catch { return }
  if (!list.length) {
    if (taskInfo.value) resetSession()
    errMsg.value = '该仓库还没有历史任务，已开启新对话'
    await refreshGit()
    return
  }
  // 优先回到"上次关页面时正在看的那条"，它不在了才退到该仓库最新任务
  const preferred = pendingTaskId && list.find((t) => t.id === pendingTaskId)
  const target = preferred || list[0]
  pendingTaskId = ''
  if (taskInfo.value?.id === target.id) return
  await openTask(target.id)
  await refreshGit()
  errMsg.value = preferred
    ? `已回到上次离开时的任务 ${target.id}（${target.state}）`
    : `已回到这个仓库最新的任务 ${target.id}（${target.state}）`
}

async function refresh(taskId) {
  try {
    const d = await api(`/v1/tasks/${taskId}`)
    taskInfo.value = d.task
    audit.value = d.audit
    d.nodes.forEach((n) => {
      nodeMap[n.id] = { id: n.id, kind: n.kind, path: n.path, mode: n.mode,
                        contract: n.contract, status: n.status }
    })
    await loadFiles(taskId)
    loadHistory()
  } catch {}
}

async function openTask(id) {
  // 记录"当前正在看哪条"，供下次进入时恢复
  if (es) { es.close(); es = null }
  events.value = []
  Object.keys(nodeMap).forEach((k) => delete nodeMap[k])
  const d = await api(`/v1/tasks/${id}`)
  taskInfo.value = d.task
  audit.value = d.audit
  d.nodes.forEach((n) => { nodeMap[n.id] = { ...n } })
  d.events.forEach((e) => events.value.push({ kind: e.kind, ts: e.ts, payload: JSON.parse(e.payload || '{}') }))
  await loadFiles(id)
  saveSession()
}

async function loadFiles(taskId) {
  if (!taskId) return
  try {
    const d = await api(`/v1/tasks/${taskId}/files`)
    fileList.value = d.files || []
  } catch { fileList.value = [] }
}

async function viewFile(path) {
  if (!taskInfo.value?.id) return
  try {
    const d = await api(`/v1/tasks/${taskInfo.value.id}/file?path=${encodeURIComponent(path)}`)
    fileView.value = d
  } catch (e) { fileView.value = { path, content: '[FAIL] ' + e.message } }
}

/* ---------------- Git 客户端（人类通道，完整能力） ---------------- */
const gitStatus = ref(null)
const gitOut = ref('')
const gitBusy = ref(false)
const remoteUrl = ref('')
const branchInput = ref('')
const switchTarget = ref('')
const mergeSource = ref('')
const undoPathInput = ref('')

function gitTarget() {
  if (taskInfo.value?.id) return { task_id: taskInfo.value.id }
  const repo = (form.repo || '').trim()
  return repo ? { repo } : null
}

// 没有任务也没有仓库时，Git 面板的按钮不该发出请求
const gitReady = computed(() => !!gitTarget())

const gitNoTargetMsg = '还没有 Git 目标：先在左栏「仓库」里选一个目录（桌面版点「浏览…」），或先跑一个任务（Git 会作用于该任务的工作区副本）'

async function refreshGit() {
  const t = gitTarget()
  if (!t) { gitStatus.value = null; return }
  try { gitStatus.value = await api('/v1/git/status?' + new URLSearchParams(t).toString()) }
  catch { gitStatus.value = null }
}

async function gitRead(op, params = {}) {
  const t = gitTarget()
  if (!t) { gitOut.value = '[提示] ' + gitNoTargetMsg; return }
  const q = new URLSearchParams({ ...t, ...params }).toString()
  try { gitOut.value = JSON.stringify(await api(`/v1/git/${op}?${q}`), null, 2) }
  catch (e) { gitOut.value = '[FAIL] ' + e.message }
}

async function gitDo(op, args = {}) {
  const t = gitTarget()
  if (!t) { gitOut.value = '[提示] ' + gitNoTargetMsg; return }
  gitBusy.value = true
  try {
    const data = await api('/v1/git/do', { method: 'POST', body: JSON.stringify({ op, ...t, args }) })
    gitOut.value = JSON.stringify(data, null, 2)
  } catch (e) {
    gitOut.value = '[FAIL] ' + e.message
  } finally {
    gitBusy.value = false
    await refreshGit()
    if (taskInfo.value?.id) loadFiles(taskInfo.value.id)
  }
}

/* ---------------- 权限模式：甲方模式 / 开发者模式 / 只读模式 ---------------- */
const modes = ref([])
const currentMode = ref(null)
const capabilityLabels = ref({})
const keyNodeLabels = ref({})

function can(cap) {
  return !!currentMode.value && (currentMode.value.capabilities || []).includes(cap)
}

/* 开发者模式：关键节点要人点"通过"，这里轮询待审单据 */
const pendingApprovals = ref([])
let approvalTimer = null

function developerMode() {
  return currentMode.value?.id === 'developer'
}
function autoApprove() {
  return currentMode.value?.auto_approve !== false
}

async function loadApprovals() {
  if (!developerMode()) { pendingApprovals.value = []; return }
  try {
    const d = await api('/v1/approvals')
    pendingApprovals.value = d.pending || []
  } catch {}
}

async function decideApproval(ticket, decision) {
  try {
    await api(`/v1/approvals/${ticket.id}`, {
      method: 'POST', body: JSON.stringify({ decision, by: 'ui' }),
    })
    pendingApprovals.value = pendingApprovals.value.filter((t) => t.id !== ticket.id)
  } catch (e) { errMsg.value = e.message }
}

function startApprovalPolling() {
  if (approvalTimer) clearInterval(approvalTimer)
  approvalTimer = setInterval(() => { loadApprovals(); loadQuestions() }, 1500)
  loadApprovals()
  loadQuestions()
}

/* ---------------- 待人回答（本地模型主动提问，开发者模式） ----------------
   与"审批"不同：审批是"这个动作能不能做"（是/否），提问是"我该怎么做"（要内容）。
   答案会作为 human 证据入账，同一问题以后不再问。 */
const questions = ref([])
const answers = reactive({})

async function loadQuestions() {
  if (!developerMode() || !form.repo) { questions.value = []; return }
  try {
    const d = await api('/v1/questions?repo=' + encodeURIComponent(form.repo))
    questions.value = d.questions || []
  } catch {}
}

async function answerQuestion(q, text) {
  const ans = (text ?? answers[q.qid] ?? '').trim()
  if (!ans) { errMsg.value = '请先填写回答，或点一个选项'; return }
  try {
    await api(`/v1/questions/${q.qid}/answer`, {
      method: 'POST', body: JSON.stringify({ text: ans, repo: form.repo }),
    })
    questions.value = questions.value.filter((x) => x.qid !== q.qid)
    answers[q.qid] = ''
    errMsg.value = `已记录回答，下一轮会带着它继续（问题：${String(q.question).slice(0, 40)}）`
  } catch (e) { errMsg.value = e.message }
}

async function loadModes() {
  try {
    const d = await api('/v1/modes')
    modes.value = d.modes || []
    capabilityLabels.value = d.capability_labels || {}
    keyNodeLabels.value = d.key_node_labels || {}
    currentMode.value = (d.modes || []).find((m) => m.is_current) || (d.modes || [])[0]
    if (developerMode()) startApprovalPolling()
    else pendingApprovals.value = []
  } catch {}
}

async function setMode(id) {
  try {
    const target = modes.value.find((m) => m.id === id || m.name === id)
    await api('/v1/mode', { method: 'POST', body: JSON.stringify({ mode: target?.id || id }) })
    await loadModes()
  } catch (e) { errMsg.value = e.message }
}

/* ---------------- 代码审查 / 改进意见 ---------------- */
const reviewUseModel = ref(true)
const reviewBusy = ref(false)
const reviewResult = ref(null)

async function runReview() {
  if (!form.repo && !taskInfo.value?.id) { errMsg.value = '请先选择仓库（或用历史任务载入一个工作区）'; return }
  if (!form.requirement.trim()) { errMsg.value = '审查需要「预期功能」：请在上面需求框里写清本该怎样'; return }
  reviewBusy.value = true
  reviewResult.value = null
  try {
    reviewResult.value = await api('/v1/review', {
      method: 'POST',
      body: JSON.stringify({
        repo: taskInfo.value?.id ? '' : form.repo,
        task_id: taskInfo.value?.id || '',
        expectation: form.requirement,
        use_model: reviewUseModel.value
      })
    })
  } catch (e) { errMsg.value = '[FAIL] ' + e.message } finally { reviewBusy.value = false }
}

function findingToTask(f) {
  form.requirement = `修复代码质量问题（${f.rule}，${f.file}:${f.line}）：${f.title}。` +
    `预期：${f.expected}。当前实际：${f.actual}。请按建议修改并补上测试：${f.suggestion}`
  form.target = f.file
  errMsg.value = '已填进任务表单，点「发送」即可让 Agent 按这条意见修改'
}

const severityClass = (s) => (s === 'P0' ? 'b-bad' : s === 'P1' ? 'b-warn' : 'b-dim')

/* ---------------- 派生 ---------------- */
const nodes = computed(() => Object.values(nodeMap).sort((a, b) => String(a.id).localeCompare(String(b.id))))
const gateRows = computed(() => events.value.filter((e) => e.kind === 'gate').map((e) => ({
  node: e.payload.node, gate: e.payload.gate, ok: e.payload.ok,
  kind: e.payload.evidence_kind, detail: e.payload.detail
})))
const modelAssertionRate = computed(() => (audit.value ? audit.value.model_assertion_rate : null))
const testSpecs = computed(() => events.value.filter((e) => e.kind === 'test_spec').map((e) => e.payload.spec))

function evClass(ev) {
  if (ev.kind === 'gate') return 'ev gate ' + (ev.payload.ok ? 'ok' : 'bad')
  return 'ev ' + ev.kind
}

function summarise(ev) {
  const p = ev.payload || {}
  switch (ev.kind) {
    case 'probe': return `${p.state} — ${p.policy || ''}`
    case 'toolchains': { const it = p.items || []; return `工具链 ${it.filter((i) => i.ok).length}/${it.length} 可用` }
    case 'workspace': return p.path
    case 'files': return `${p.count} 个文件，测试 ${p.tests?.length || 0} 个`
    case 'plan': return (p.nodes || []).map((n) => `${n.kind}:${n.path}`).join('  →  ') + (p.reasoning ? '\n' + p.reasoning : '')
    case 'plan_normalized': return (p.nodes || []).map((n) => `${n.kind}/${n.mode}:${n.path}`).join('  →  ')
    case 'llm_call': return `${p.stage} · ${p.model} · ${p.ms}ms · ${p.tokens} tok`
    case 'attempt_start': return `第 ${p.attempt} 次 · T=${p.temperature}`
    case 'escalate': return `升级 L${p.attempt}：${String(p.feedback || '').slice(0, 120)}`
    case 'test_spec': return `func=${p.spec?.func} module=${p.spec?.module}`
    case 'rendered_test': return '引擎模板渲染的测试代码'
    case 'patch': return p.path
    case 'postprocess': return `确定性补齐：${(p.added || []).join(', ')}`
    case 'gate': return `${p.gate} · ${p.ok ? 'PASS' : 'FAIL'} · evidence=${p.evidence_kind}${p.detail ? ' · ' + String(p.detail).slice(0, 150) : ''}`
    case 'node_start': return `${p.kind}/${p.mode} ${p.path} — ${p.contract}`
    case 'node_done': return `快照 ${p.snapshot || '(none)'}`
    case 'node_failed': return String(p.reason || '').slice(0, 180)
    case 'notice': return p.message
    case 'audit': return `通过 ${p.passed} · 模型断言率 ${p.model_assertion_rate} · 变异杀死率 ${p.mutation_kill_rate}`
    case 'done': return `${p.state} · ${p.workspace}`
    case 'error': return p.message
    default: return JSON.stringify(p).slice(0, 160)
  }
}

function diffLines(diff) {
  return String(diff || '').split('\n').map((line) => ({
    text: line,
    cls: line.startsWith('+++') || line.startsWith('---') ? 'dim'
      : line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : ''
  }))
}

onMounted(async () => {
  try {
    const s = document.querySelector('script[src*="assets/index-"]')
    const m = (s?.getAttribute('src') || '').match(/index-([\w-]+)\.js/)
    buildId.value = m ? m[1] : ''
  } catch { buildId.value = '' }
  await loadHealth()
  await loadToolchains()
  await loadRemote()
  await loadHistory()
  await loadModes()
  await refreshGit()
  await loadModels(true)
  await loadLoginState()
  // 恢复上次选的仓库，并自动回到该仓库上次的任务
  try {
    const sess = loadSession()
    if (sess.repo) {
      form.repo = sess.repo
      pendingTaskId = sess.taskId
      await doProbe()             // 内部会 syncRepoContext → 优先打开 pendingTaskId
    }
  } catch {}
  // 本地随时可能 ollama pull / rm：定时刷新 + 窗口聚焦时刷新
  // 关闭页面、切后台、崩溃前都尽量落一次（beforeunload 在强制结束时可能不触发，
  // 所以另外用 input 监听 + 切任务时立即写，形成三层保险）
  window.addEventListener('beforeunload', saveSession)
  window.addEventListener('pagehide', saveSession)
  document.addEventListener('visibilitychange', () => { if (document.hidden) saveSession() })
  modelsTimer = setInterval(() => loadModels(true), 60000)
  window.addEventListener('focus', onWindowFocus)
  document.addEventListener('visibilitychange', onVisibilityChange)
})

let modelsTimer = null
function onWindowFocus() { loadModels() }
function onVisibilityChange() { if (!document.hidden) loadModels() }

onUnmounted(() => {
  if (modelsTimer) clearInterval(modelsTimer)
  if (approvalTimer) clearInterval(approvalTimer)
  if (es) { es.close(); es = null }
  saveSession()
  window.removeEventListener('focus', onWindowFocus)
  window.removeEventListener('beforeunload', saveSession)
  window.removeEventListener('pagehide', saveSession)
  document.removeEventListener('visibilitychange', onVisibilityChange)
})
</script>

<template>
  <div class="app">
    <!-- 顶栏 -->
    <div class="topbar">
      <div class="brand">SixCoder</div>
      <span class="spacer"></span>
      <!-- 远端模型：这一夜让哪个远端档案来写（flash / pro），用于区分不同模型的产出 -->
      <select v-if="(remoteModels || []).length" class="mode-select" v-model="night.remoteProfile"
              title="远端模型档案：只走这个档案的档位；选「整条阶梯」则按 flash→pro 自动升档">
        <option value="">远端模型：整条阶梯（自动升档）</option>
        <option v-for="m in remoteModels" :key="m.id" :value="m.id"
                :disabled="!m.enabled">
          远端模型：{{ m.label || m.id }}{{ m.enabled ? '' : '（未启用）' }}
        </option>
      </select>
      <select v-if="modes.length" class="mode-select" v-model="currentMode"
              @change="setMode(currentMode.id)"
              :title="currentMode ? currentMode.desc + '\n边界：' + currentMode.boundary : ''">
        <option v-for="m in modes" :key="m.id" :value="m">{{ m.name }}</option>
      </select>
      <span class="pill" v-if="currentMode" :class="autoApprove() ? 'b-ok' : 'b-warn'"
            :title="autoApprove() ? '人工处置全部放行（无人值守也能跑完）' : '关键节点必须人工审核，不会自动放行'">
        {{ autoApprove() ? '自动放行' : '需人审' }} {{ currentMode.key_nodes.length || 0 }}
      </span>
      <span v-if="questions.length" class="pill b-warn"
            style="cursor:pointer" @click="settingsOpen = true"
            :title="'有 ' + questions.length + ' 个问题在等你回答'">
        待答 {{ questions.length }}
      </span>
      <span v-if="pendingApprovals.length" class="pill b-warn"
            style="cursor:pointer" @click="settingsOpen = true"
            :title="'有 ' + pendingApprovals.length + ' 个待审节点等你通过'">
        待审 {{ pendingApprovals.length }}
      </span>
      <span class="pill"><span class="dot" :class="health?.ollama ? 'on' : 'off'"></span> Ollama {{ health?.ollama ? '在线' : '离线' }}</span>
      <select v-model="form.model" class="mode-select model-select"
              :class="{ 'model-bad': currentModelTooSmall }"
              :title="'本地工人模型（Ollama，门槛 ≥' + (modelPolicy?.min_params_b || MIN_PARAMS_B) + 'B；打开下拉会自动刷新）'
                + (modelPolicy?.blocked?.length ? '｜不可选：' + modelPolicy.blocked.join(', ') : '')
                + (modelsLoading ? ' 刷新中…' : '')"
              @focus="loadModels()" @mousedown="loadModels()">
        <option v-for="m in models" :key="m" :value="m" :disabled="tooSmall(m)">
          {{ m }}{{ tooSmall(m) ? '（' + paramsB(m) + 'B 低于门槛，不可选）' : '' }}
        </option>
      </select>
      <div class="menu-wrap">
        <button class="ghost menu-btn" @click="openRemote()"
                :title="remote.available ? '远端模型已配置（点此修改）' : '配置远端模型密钥（DeepSeek）'">
          远端 <span class="dot" :class="remote.available ? 'on' : 'off'"></span>
        </button>
        <button class="ghost menu-btn" @click="openHelp('help')">帮助</button>
        <button class="ghost menu-btn" @click="openLogin()"
                :title="openAtLogin ? '已开启开机自启' : '开机自启未开启'">
          登录选项 <span class="dot" :class="openAtLogin ? 'on' : 'off'"></span>
        </button>
        <span class="dim" :title="'当前前端 bundle：' + (buildId || '未知')"
              style="font-size:10px;opacity:.6">#{{ buildId || 'dev' }}</span>
      </div>
      <div v-if="openMenu" class="menu-mask" @click="openMenu = ''"></div>
    </div>

    <div v-if="errMsg" class="alert-bar" @click="errMsg = ''" title="点击关闭">
      <span class="wrap">{{ errMsg }}</span>
      <span class="spacer"></span><span class="dim" style="font-size:10px">点击关闭 ✕</span>
    </div>

    <div class="cols">
      <!-- ============ 左栏 ============ -->
      <div class="col col-left">
        <div class="panel">
          <div class="panel-title">仓库</div>
          <div class="row">
            <input v-model="form.repo" placeholder="仓库路径" @input="saveSessionSoon" />
            <button v-if="isShell" class="ghost" title="打开目录选择器" @click="pickRepoDir">浏览…</button>
            <button class="ghost" :disabled="busy" @click="doProbe">探测</button>
          </div>
          <div v-if="probeInfo" style="margin-top:6px">
            <span class="badge b-acc">{{ probeInfo.state }}</span>
            <span class="dim" style="font-size:11px;margin-left:6px">{{ probeInfo.policy }}</span>
            <table style="margin-top:6px">
              <tr><td class="dim">提交</td><td class="mono">{{ probeInfo.signals.commit_count }}</td>
                  <td class="dim">代码</td><td class="mono">{{ probeInfo.signals.code_files }}</td>
                  <td class="dim">测试</td><td class="mono">{{ probeInfo.signals.test_files }}</td></tr>
              <tr>
                <td class="dim" style="white-space:nowrap">基线</td>
                <td colspan="5">
                  <span v-if="probeInfo.signals.baseline === 'verified'" class="badge b-ok">
                    已全量验证 · {{ probeInfo.signals.test_count }} 项全绿</span>
                  <span v-else-if="probeInfo.signals.baseline === 'failing'" class="badge b-bad">
                    测试不绿 · {{ probeInfo.signals.test_count }} 项里有失败</span>
                  <span v-else-if="probeInfo.signals.baseline === 'collect-failed'" class="badge b-bad">
                    用例收集失败（导入/语法问题）</span>
                  <span v-else-if="probeInfo.signals.baseline === 'unverified-large'" class="badge b-warn">
                    仅收集未执行 · {{ probeInfo.signals.test_count }} 项（超过一次跑完的阈值）</span>
                  <span v-else class="badge b-dim">无测试</span>
                  <button class="ghost tiny" style="margin-left:6px" :disabled="busy"
                          title="强制跑一次全量测试建立基线（大仓库可能要几分钟）" @click="doProbe(true)">深验</button>
                </td>
              </tr>
            </table>
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">
            工具链
            <span class="badge" :class="toolchains.every(t => t.ok) ? 'b-ok' : 'b-warn'" style="margin-left:6px">
              {{ toolchains.filter(t => t.ok).length }}/{{ toolchains.length }} 可用
            </span>
            <span class="spacer"></span>
            <span class="tab" :class="{ active: showTc }" @click="showTc = !showTc">{{ showTc ? '收起' : '展开' }}</span>
          </div>
          <div v-if="showTc" class="table-wrap" style="max-height:180px">
            <table>
              <tr v-for="t in toolchains" :key="t.id">
                <td class="mono" style="width:52px">{{ t.id }}</td>
                <td style="width:20px">
                  <span v-if="t.ok" class="badge b-ok">✓</span>
                  <span v-else-if="t.version" class="badge b-warn">!</span>
                  <span v-else class="badge b-bad">✗</span>
                </td>
                <td class="mono dim" style="width:64px">{{ t.version || '—' }}</td>
                <td class="mono dim wrap" style="font-size:9.5px">{{ t.path || '未找到' }}</td>
                <td style="width:34px">
                  <button v-if="isShell && can('toolchain.config')" class="ghost tiny"
                          :title="'指定 ' + t.id + ' 目录'" @click="pickToolchainDir(t)">…</button>
                </td>
              </tr>
            </table>
          </div>
          <div v-if="showTc && !isShell" class="dim" style="font-size:10px;margin-top:4px">
            「指定目录」需桌面版外壳（Electron）；网页版可编辑 <code>data/toolchains.json</code>
          </div>
        </div>

        <div class="panel">
          <div class="panel-title">Git 客户端
            <span class="spacer"></span>
            <span class="badge" :class="gitReady ? 'b-acc' : 'b-warn'">{{ gitReady ? (taskInfo?.id ? '目标：任务工作区' : '目标：所选仓库') : '未选目标' }}</span>
          </div>
          <div v-if="!gitReady" class="dim" style="font-size:10.5px;margin-bottom:6px">{{ gitNoTargetMsg }}</div>
            <div class="git-status" v-if="gitStatus">
              <span class="badge b-acc">{{ gitStatus.branch }}</span>
              <span class="mono dim">@{{ gitStatus.head }}</span>
              <span class="badge" :class="gitStatus.clean ? 'b-ok' : 'b-warn'">{{ gitStatus.clean ? '干净' : gitStatus.changes.length + ' 处改动' }}</span>
              <span class="badge b-dim">↑{{ gitStatus.ahead }} ↓{{ gitStatus.behind }}</span>
              <span class="mono dim wrap" style="font-size:10px">
                {{ Object.entries(gitStatus.remotes).map(([k, v]) => k + ' → ' + v).join('  ') || '无远端' }}
              </span>
            </div>

            <!-- 改动文件：可逐个撤回 -->
            <div v-if="gitStatus && gitStatus.changes.length" class="table-wrap" style="max-height:150px;margin-bottom:8px">
              <table>
                <thead><tr><th style="width:40px">状态</th><th>文件</th><th style="width:150px">撤回</th></tr></thead>
                <tbody>
                  <tr v-for="c in gitStatus.changes" :key="c.path">
                    <td><span class="badge" :class="c.code === '??' ? 'b-acc' : 'b-warn'">{{ c.code }}</span></td>
                    <td class="mono wrap" style="font-size:11px;cursor:pointer" @click="gitRead('diff', { path: c.path })">{{ c.path }}</td>
                    <td>
                      <button class="ghost tiny" :disabled="gitBusy || !can('git.commit')"
                              :title="c.code === '??' ? '删除这个新文件（删前备份）' : '恢复该文件到上次提交版本'"
                              @click="gitDo('undo', { path: c.path })">撤回</button>
                      <button class="ghost tiny" :disabled="gitBusy || !can('git.commit')"
                              title="按引擎写盘日志回滚该文件（连未提交的改动也能撤）"
                              @click="gitDo('undo_sandbox_file', { path: c.path })">沙箱撤回</button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div v-else-if="gitStatus" class="dim" style="font-size:10.5px;margin-bottom:6px">
              工作区没有改动（上表会列出每个改动文件，可逐个撤回）
            </div>

            <div class="row" style="flex-wrap:wrap;gap:6px">
              <button class="ghost" :disabled="gitBusy || !can('git.commit')"
                      title="在当前仓库目录里 git init：不需要填仓库名（名字就是目录名），默认分支 main，并做一次首次快照"
                      @click="gitDo('init', { branch: 'main' })">初始化仓库</button>
              <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="refreshGit(); gitRead('status')">状态</button>
              <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('log', { n: 20 })">历史</button>
              <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('diff')">差异</button>
              <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('branches')">分支列表</button>
            </div>

            <div class="row" style="flex-wrap:wrap;gap:6px">
              <select v-model="commitType" class="commit-type" :title="COMMIT_TYPES.find(t => t.id === commitType)?.desc">
                <option v-for="t in COMMIT_TYPES" :key="t.id" :value="t.id">{{ t.label }}</option>
              </select>
              <input v-model="commitMsg" placeholder="提交描述（不写前缀）" style="flex:1;min-width:180px" />
              <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('stage')">暂存全部</button>
              <button :disabled="gitBusy || !can('git.commit')" @click="gitDo('commit', { message: commitFull })">提交</button>
            </div>
            <div class="dim mono" style="font-size:10.5px;margin:-2px 0 6px;word-break:break-all">
              将提交：{{ commitFull }}
            </div>

            <div class="row" style="flex-wrap:wrap;gap:6px">
              <input v-model="remoteUrl" placeholder="远端仓库地址（本地裸仓库路径或 https/ssh）" style="flex:1;min-width:170px" />
              <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('set_remote', { name: 'origin', url: remoteUrl })">设置远端</button>
              <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('fetch')">获取</button>
              <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('pull')">拉取</button>
              <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('push')">推送</button>
              <button :disabled="gitBusy || !can('git.remote')" @click="gitDo('sync')">一键同步</button>
            </div>
            <div v-if="!can('git.remote')" class="dim" style="font-size:10.5px;margin:-2px 0 6px">
              远端同步需要「甲方模式」或「开发者模式」（当前：{{ currentMode?.name }}）
            </div>

            <div class="row" style="flex-wrap:wrap;gap:6px">
              <input v-model="branchInput" placeholder="新分支名" style="max-width:130px" />
              <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('new_branch', { name: branchInput })">新建并切换</button>
              <input v-model="switchTarget" placeholder="要切换到的分支" style="max-width:120px" />
              <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('switch', { name: switchTarget })">切换</button>
              <input v-model="mergeSource" placeholder="合并源分支" style="max-width:120px" />
              <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('merge', { source: mergeSource })">合并</button>
            </div>

            <div class="row" style="flex-wrap:wrap;gap:6px">
              <input v-model="undoPathInput" placeholder="文件路径" style="max-width:180px" />
              <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('undo', { path: undoPathInput })">撤回该文件</button>
              <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('undo_sandbox_file', { path: undoPathInput })">沙箱撤回该文件</button>
              <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('undo_sandbox', { steps: 1 })">沙箱撤回一步</button>
            </div>
            <pre v-if="gitOut" style="max-height:170px">{{ gitOut }}</pre>
        </div>

        <div class="panel grow">
          <div class="panel-title">
            用过的仓库
            <span class="badge b-dim" style="margin-left:6px">{{ repos.length }}</span>
            <span class="spacer"></span>
            <button class="ghost tiny" @click="loadHistory()">刷新</button>
            <button class="ghost tiny del-btn" title="清空全部历史任务" @click="showClear = true">清空</button>
          </div>
          <div class="table-wrap">
            <table>
              <thead>
                <tr><th>仓库</th><th>状态</th><th>最近需求</th><th style="width:26px"></th></tr>
              </thead>
              <tbody>
                <tr v-for="r in repos" :key="r.repo" class="hist-row"
                    :class="{ current: sameRepo(r.repo) }" @click="enterRepo(r)">
                  <td class="mono" style="font-size:10.5px">
                    {{ r.name }}<br /><span class="dim" style="font-size:9px">{{ (r.updated_at || '').slice(5, 16) }}</span>
                  </td>
                  <td><span class="badge" :class="r.state === 'done' ? 'b-ok' : (r.state === 'failed' ? 'b-bad' : 'b-warn')">{{ r.state }}</span></td>
                  <td class="dim wrap" style="font-size:10.5px">
                    {{ String(r.requirement || '').slice(0, 30) }}
                    <span class="dim" style="font-size:9px">· {{ r.nodes }} 节点</span>
                  </td>
                  <td>
                    <button class="ghost tiny del-btn" title="删除这个对话（该仓库的记录与工作区副本）" @click.stop="askDeleteRepo(r)">✕</button>
                  </td>
                </tr>
                <tr v-if="!repos.length"><td colspan="4" class="empty">还没有用过的仓库</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- ============ 中栏 ============ -->
      <div class="col col-mid">
        <div class="panel" style="display:flex;align-items:center;gap:8px">
          <div class="panel-title" style="margin:0">执行时间线</div>
          <span class="spacer"></span>
          <span class="pill" v-if="testSpecs.length">测试规约 {{ testSpecs.length }}</span>
        </div>
        <div class="timeline" ref="timelineEl">
          <div v-if="!events.length" class="empty">选好仓库、写好需求，点「发送」：这里实时显示 探测 → 计划 → 测试规约 → 补丁 → 闸门 → 审计</div>
          <div v-for="(ev, i) in events" :key="i" :class="evClass(ev)">
            <div class="ev-head">
              <span class="ev-time">{{ ev.ts }}</span>
              <span class="ev-name">{{ ev.kind }}</span>
              <span v-if="ev.kind === 'gate'" class="badge" :class="ev.payload.ok ? 'b-ok' : 'b-bad'">{{ ev.payload.gate }}</span>
              <span v-if="ev.kind === 'gate'" class="badge b-dim">{{ ev.payload.evidence_kind }}</span>
            </div>
            <div class="ev-body wrap" style="white-space:pre-wrap">{{ summarise(ev) }}</div>
            <pre v-if="ev.kind === 'rendered_test'" class="ok-border">{{ ev.payload.code }}</pre>
            <pre v-else-if="ev.kind === 'patch'"><span v-for="(l, j) in diffLines(ev.payload.diff)" :key="j" :class="l.cls">{{ l.text }}
</span></pre>
          </div>
        </div>

        <!-- 工作台：Git / 代码审查 -->
        <!-- 工作台：一个输入区，两个主操作（发送 / 只审查）+ 需求帮助 -->
        <div class="panel">
          <div class="panel-title" style="margin-bottom:6px">
            需求
            <span class="spacer"></span>
            <span class="badge b-acc">{{ currentMode?.name || '—' }}</span>
          </div>

          <!-- 目标文件、示例、字段填法一律在「需求帮助」里讲清楚；输入区只留需求本身 -->
          <textarea v-model="form.requirement" style="min-height:120px"
                    :placeholder="isClientMode
                      ? '写清要什么、写进哪个文件（如 clean.py）、行为与异常怎么处理｜点「开始夜班」'
                      : '写清改哪个文件、要什么行为、异常怎么处理｜Ctrl+Enter 发送'"
                    @keydown.ctrl.enter.prevent="onSendShortcut"></textarea>

          <!-- 甲方模式：只有「开始夜班」这一个动作（内部自己拍板、自己写） -->
          <div v-if="isClientMode" class="row" style="margin-top:8px;gap:10px;flex-wrap:wrap;align-items:flex-end">
            <button style="min-width:120px" :disabled="night.busy || !can('agent.run')"
                    title="提需求 → 系统反复体检/派工/开发/机械验收 → 交付报告"
                    @click="startNight">{{ night.busy ? '夜班进行中…' : '开始夜班' }}</button>
            <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px">
              预算
              <input type="number" min="0.1" step="0.5" v-model.number="night.hours"
                     style="width:64px" /> 小时
            </label>
            <!-- 花费上限：自己填，超过就自动收工（防止跑飞了烧钱） -->
            <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px"
                   title="每夜的远端花费硬上限（人民币）。超过就立刻收工，收工原因写「花费到上限」。填 0 = 不设上限">
              上限
              <input type="number" min="0" step="0.5" v-model.number="night.maxCny"
                     style="width:64px" /> 元
            </label>
            <!-- 目标代码规模：约束按规模分级；每级到底管什么，见「需求帮助」里那张表 -->
            <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px">
              规模
              <select v-model="night.scale" style="width:190px">
                <option v-for="t in night.scaleTiers" :key="t.id" :value="t.id">{{ t.label }}</option>
              </select>
            </label>
            <button class="ghost tiny" :disabled="!night.taskId" @click="loadNight()">刷新进度</button>
            <button class="ghost tiny" :disabled="night.busy"
                    title="需求怎么写、规模怎么选、字段怎么填"
                    @click="settingsTab = 'help'; showSettings = true">帮助</button>
          </div>

          <!-- 开发者模式：单任务，照人给的方案实现 -->
          <div v-else class="row" style="margin-top:8px;gap:10px;flex-wrap:wrap;align-items:flex-end">
            <button style="min-width:104px" :disabled="busy || !can('agent.run')"
                    title="把这条需求发给 Agent"
                    @click="startTask">{{ busy ? '执行中…' : '发送' }}</button>
            <button class="ghost tiny" :disabled="busy"
                    title="需求怎么写、字段怎么填"
                    @click="settingsTab = 'help'; showSettings = true">帮助</button>
          </div>

          <!-- 只读审查留在开发者模式（工具模式本来就有这一步），甲方模式不出现 -->
          <div v-if="!isClientMode" class="row" style="margin-top:6px;gap:10px;flex-wrap:wrap">
            <button class="ghost" :disabled="reviewBusy || !can('review.run')"
                    title="静态审查：只出改进意见，不改任何文件"
                    @click="runReview">{{ reviewBusy ? '审查中…' : '审查' }}</button>
            <label class="dim" style="display:flex;align-items:center;gap:4px;font-size:10.5px">
              <input type="checkbox" v-model="reviewUseModel" :disabled="!can('review.model')" style="width:auto" />
              用模型比对
            </label>
          </div>

          <!-- 夜班进度 + 交付报告（甲方模式的主舞台） -->
          <div v-if="isClientMode && (night.lines.length || night.delivery || night.info)" style="margin-top:8px">
            <div class="row" style="gap:6px;flex-wrap:wrap">
              <span v-if="night.info?.state" class="badge"
                    :class="night.info.state === 'done' ? 'b-ok'
                          : (night.info.state === 'failed' ? 'b-bad' : 'b-warn')">
                {{ night.info.state }}</span>
              <span v-if="night.info?.total" class="badge b-acc">
                {{ night.info.verified }}/{{ night.info.total }}</span>
              <span v-if="night.info?.policy" class="dim" style="font-size:10.5px">
                协作方式：{{ night.info.policy }}</span>
            </div>
            <div v-if="night.lines.length" class="table-wrap" style="max-height:150px;margin-top:6px">
              <div v-for="(l, i) in night.lines.slice(-60)" :key="i"
                   class="dim mono" style="font-size:10px;white-space:pre-wrap">
                <span class="badge b-dim" style="margin-right:4px">{{ l.kind }}</span>{{ l.text }}
              </div>
            </div>
            <div v-if="night.info?.reason" class="dim" style="font-size:10.5px;margin-top:4px">
              收工原因：{{ night.info.reason }}
            </div>
            <div v-if="night.delivery" style="margin-top:8px;border-top:1px solid var(--line);padding-top:8px">
              <div class="row" style="gap:6px">
                <b>交付报告</b><span class="spacer"></span>
                <span class="dim mono" style="font-size:10px">{{ night.info?.delivery || '' }}</span>
              </div>
              <pre style="max-height:300px;overflow:auto;font-size:11px;white-space:pre-wrap">{{ night.delivery }}</pre>
            </div>
          </div>
          <div v-if="!can('agent.run')" class="dim" style="font-size:10.5px;margin-top:6px">
            只读模式：只有右侧「审查」可用（它不写任何文件）。要改代码请把顶栏模式切到「甲方模式」或「开发者模式」。
          </div>
          <div v-if="taskInfo" class="dim mono" style="margin-top:6px;font-size:11px">
            当前任务：{{ taskInfo.id }} ·
            <span :class="taskInfo.state === 'done' ? 'ok-t' : 'warn-t'">{{ taskInfo.state }}</span>
          </div>

          <!-- 审查结果（同一面板内，不再单独一个 Tab） -->
          <div v-if="reviewResult" style="margin-top:10px;border-top:1px solid var(--line);padding-top:8px">
            <div class="row" style="gap:6px;flex-wrap:wrap;margin-bottom:6px">
              <span class="badge b-bad">P0 {{ reviewResult.counts.P0 }}</span>
              <span class="badge b-warn">P1 {{ reviewResult.counts.P1 }}</span>
              <span class="badge b-dim">P2 {{ reviewResult.counts.P2 }}</span>
              <span class="badge b-acc">{{ reviewResult.files_scanned }} 文件</span>
              <span class="badge b-dim" v-if="reviewResult.model?.used">模型 {{ reviewResult.model.tokens }} tok</span>
              <span class="badge b-warn" v-else-if="reviewResult.model?.note">未调用模型</span>
              <span class="spacer"></span>
              <button class="ghost tiny" @click="reviewResult = null">清空</button>
            </div>
            <div class="dim" style="font-size:10px;margin-bottom:6px">{{ reviewResult.note }}</div>
            <div v-if="!reviewResult.findings.length" class="empty">没有发现问题</div>
            <div v-for="(f, i) in reviewResult.findings" :key="i" class="finding">
              <div class="ev-head">
                <span class="badge" :class="severityClass(f.severity)">{{ f.severity }}</span>
                <span class="mono dim">{{ f.file }}:{{ f.line }}</span>
                <span class="badge" :class="f.source === 'model' ? 'b-warn' : 'b-dim'">{{ f.rule }}</span>
                <span class="spacer"></span>
                <button class="ghost tiny" @click="findingToTask(f)">转成任务 →</button>
              </div>
              <div style="font-size:12px;margin-top:2px">{{ f.title }}</div>
              <div class="dim" style="font-size:11px">预期：{{ f.expected }}</div>
              <div class="dim" style="font-size:11px">实际：{{ f.actual }}</div>
              <div class="ok-t" style="font-size:11px">建议：{{ f.suggestion }}</div>
          </div>
        </div>
        </div>
        </div>

      <!-- ============ 右栏 ============ -->
      <div class="col col-right">
        <div class="panel">
          <div class="panel-title">节点</div>
          <div v-if="!nodes.length" class="empty">暂无</div>
          <table v-else>
            <tr><th>节点</th><th>kind</th><th>文件</th><th>状态</th><th style="width:78px">处置</th></tr>
            <tr v-for="n in nodes" :key="n.id">
              <td class="mono">{{ String(n.id).split('-').pop() }}</td>
              <td class="mono dim">{{ n.kind }}</td>
              <td class="mono wrap">{{ n.path }}</td>
              <td><span class="badge" :class="{
                'b-ok': n.status === 'done' || n.status === 'accepted',
                'b-bad': n.status === 'stuck' || n.status === 'needs_human',
                'b-acc': n.status === 'running', 'b-dim': n.status === 'pending' || n.status === 'dropped' }">
                {{ n.status === 'stuck' ? 'stuck（未通过）' : n.status }}</span></td>
              <td>
                <button v-if="n.status === 'stuck' || n.status === 'needs_human'" class="ghost tiny"
                        title="人工审过后放行（记 human 证据）"
                        @click.stop="resolveTarget = { node: n, reason: '' }">处置</button>
              </td>
            </tr>
          </table>
        </div>

        <div class="panel" style="max-height:230px">
          <div class="panel-title">闸门证据 <span class="badge b-dim" style="margin-left:6px">evidence_kind 决定通过是否算数</span></div>
          <div v-if="!gateRows.length" class="empty">暂无</div>
          <table v-else>
            <tr><th>闸门</th><th>结果</th><th>证据</th></tr>
            <tr v-for="(g, i) in gateRows.slice(-12)" :key="i">
              <td class="mono">{{ g.gate }}</td>
              <td><span class="badge" :class="g.ok ? 'b-ok' : 'b-bad'">{{ g.ok ? 'PASS' : 'FAIL' }}</span></td>
              <td><span class="badge" :class="g.kind === 'model_text' ? 'b-bad' : 'b-dim'">{{ g.kind }}</span></td>
            </tr>
          </table>
        </div>

        <div class="panel grow">
          <div class="panel-title">
            产物文件
            <span class="badge b-dim" style="margin-left:6px">{{ fileList.length }}</span>
            <span class="spacer"></span>
            <button v-if="isShell && taskInfo?.workspace" class="ghost tiny" @click="openWorkspace">打开工作区</button>
          </div>
          <div class="files">
            <span v-for="f in fileList" :key="f" class="file" @click="viewFile(f)">{{ f }}</span>
            <span v-if="!fileList.length" class="empty">运行后显示</span>
          </div>
          <pre v-if="fileView" style="max-height:220px">{{ fileView.content }}</pre>
        </div>

        <div class="panel">
          <div class="panel-title">自认可审计（docs/13）</div>
          <div class="cards">
            <div class="card">
              <div class="k">模型断言率</div>
              <div class="v" :style="{ color: modelAssertionRate === 0 ? 'var(--ok)' : (modelAssertionRate ? 'var(--fail)' : 'inherit') }">
                {{ modelAssertionRate === null ? '—' : modelAssertionRate }}
              </div>
            </div>
            <div class="card"><div class="k">变异杀死率</div><div class="v">{{ audit?.mutation_kill_rate ?? '—' }}</div></div>
            <div class="card"><div class="k">通过证据</div><div class="v">{{ audit?.passed ?? '—' }}</div></div>
            <div class="card"><div class="k">节点成功/总</div><div class="v">{{ audit ? `${audit.nodes_ok}/${audit.nodes_total}` : '—' }}</div></div>
            <div class="card"><div class="k">墙钟</div><div class="v">{{ audit?.wall_ms ? (audit.wall_ms / 1000).toFixed(1) + 's' : '—' }}</div></div>
            <div class="card"><div class="k">闸门失败/总</div><div class="v">{{ audit ? `${audit.gates_failed}/${audit.gates_total}` : '—' }}</div></div>
          </div>
          <div v-if="audit?.evidence_by_kind" class="dim mono" style="margin-top:6px;font-size:10.5px">
            {{ JSON.stringify(audit.evidence_by_kind) }}
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 远端模型（顶栏「远端」按钮直达） ============ -->
    <div v-if="showRemoteModal" class="modal-mask" @click.self="showRemoteModal = false">
      <div class="modal" style="max-width:620px">
        <div class="modal-head">
          <strong>远端模型（DeepSeek）</strong>
          <span class="badge" :class="remote.available ? 'b-ok' : 'b-dim'">
            {{ remote.available ? '可用' : '未配置' }}
          </span>
          <span class="spacer"></span>
          <button class="ghost tiny" @click="showRemoteModal = false">关闭 ✕</button>
        </div>
        <div class="modal-body">
          <div class="dim" style="font-size:12px;margin-bottom:10px">
            <b>一次只用这一个模型。</b>下面是它的连接参数；思考强度不用你填 —— 系统按需求自动挑。
            key 只存本机设置文件，界面只显示掩码。
          </div>

          <!-- ① 当前模型：唯一的连接参数填写处 -->
          <div class="panel-title">当前模型</div>
          <div class="row" style="gap:8px;align-items:center;margin-bottom:6px">
            <span class="dim" style="font-size:11px;width:64px">模型名</span>
            <input v-model="remoteForm.model" placeholder="deepseek-flash" style="width:190px" />
            <span class="dim" style="font-size:11px;width:48px">接口</span>
            <input v-model="remoteForm.base_url" placeholder="https://api.deepseek.com" style="flex:1" />
          </div>
          <div class="row" style="gap:8px;align-items:center;margin-bottom:6px">
            <span class="dim" style="font-size:11px;width:64px">API key</span>
            <input v-model="remoteForm.api_key" type="password" placeholder="sk-...（留空则不修改）"
                   style="flex:1" @keyup.enter="saveCurrentModel()" />
            <button class="ghost tiny" @click="revealRemoteKey()">{{ remoteForm.api_key ? '隐藏' : '显示' }}</button>
            <button class="ghost tiny" @click="clearRemoteKey()">清除</button>
          </div>
          <div class="row" style="gap:8px;align-items:center">
            <button class="primary tiny" @click="saveCurrentModel()">保存当前模型</button>
            <span class="badge" :class="remote.available ? 'b-ok' : 'b-dim'">
              {{ remote.available ? '可用' : '未配置' }}</span>
            <span v-if="remote.reason" class="dim" style="font-size:11.5px">{{ remote.reason }}</span>
            <span class="spacer"></span>
            <span class="dim mono" style="font-size:11px">
              key：{{ remote.remote?.key_masked || '—' }}（{{ remote.remote?.key_source || '无' }}）
            </span>
          </div>

          <!-- ② 思考强度：只读，系统自动选 -->
          <div class="panel-title" style="margin-top:14px">思考强度（系统自动，无需填写）</div>
          <div class="dim" style="font-size:11.5px;line-height:1.7">
            当前模型支持的档位：<span class="mono">
              {{ (remoteLadder || []).map(x => x.effort).join(' → ') || '（未配置）' }}</span><br />
            选档规则：<b>规格/方案 → 最深一档</b>（决定后面每个模块的对错）；<b>样板/校验/日志/适配 → 不思考</b>；
            <b>写代码 → 按难度自动挑</b>（依据：依赖数、函数数、契约示例数、历史失败次数、是否还是空桩）。<br />
            失败时只在<b>同一个模型</b>内升档；升到最深还不过，就是"这个模型到极限了"，换模型由你决定。
          </div>
          <div class="dim mono" style="font-size:11px;margin-top:4px">
            完整阶梯：{{ (remoteLadder || []).map(x => x.profile_id + ':' + x.effort).join(' → ') || '（空）' }}
          </div>

          <div v-if="currentMode?.id === 'developer'" class="row" style="gap:8px;align-items:center;margin-top:10px">
            <label class="dim" style="font-size:11.5px;display:flex;align-items:center;gap:6px">
              <input type="checkbox" :checked="remote.remote?.dev_remote" style="width:auto"
                     @change="saveRemote({ dev_remote: $event.target.checked })" />
              开发者模式允许调用远端（默认关；打开等于一定程度加强本地模型能力）
            </label>
          </div>
          <div v-if="currentMode?.id === 'client'" class="dim" style="font-size:11px;margin-top:10px">
            甲方模式（= 夜班）：远端<b>强制开启</b>，没有开关 —— 上限就是远端能力。
          </div>
          <div v-if="currentMode?.id === 'readonly'" class="dim" style="font-size:11px;margin-top:10px">
            只读模式：远端<b>强制关闭</b>。
          </div>

          <!-- ③ 用量与花费：只读 -->
          <div class="panel-title" style="margin-top:14px">用量与花费（人民币）</div>
          <table>
            <tr><td class="dim" style="width:130px">调用次数</td><td class="mono">{{ remote.usage?.calls ?? 0 }}</td></tr>
            <tr><td class="dim">prompt / completion</td>
                <td class="mono">{{ remote.usage?.prompt_tokens ?? 0 }} / {{ remote.usage?.completion_tokens ?? 0 }}</td></tr>
            <tr><td class="dim">缓存命中率</td>
                <td class="mono">{{ ((remote.usage?.cache_hit_rate ?? 0) * 100).toFixed(0) }}%
                  <span class="dim" style="font-size:11px">（命中输入便宜 50 倍：flash 0.02 vs 1 元/百万）</span></td></tr>
            <tr><td class="dim">reasoning tokens</td><td class="mono">{{ remote.usage?.reasoning_tokens ?? 0 }}</td></tr>
            <tr><td class="dim">高峰（全价）</td>
                <td class="mono">{{ remote.usage?.peak?.calls ?? 0 }} 次 · ¥{{ (remote.usage?.peak?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">空闲（半价）</td>
                <td class="mono">{{ remote.usage?.offpeak?.calls ?? 0 }} 次 · ¥{{ (remote.usage?.offpeak?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">总花费</td><td class="mono">¥{{ (remote.usage?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">缓存省下</td><td class="mono">¥{{ (remote.usage?.cache_saved_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">高峰时段</td>
                <td class="mono">{{ (remote.usage?.window?.ranges || []).join('、') || '09:00-12:00、14:00-18:00' }}
                  （北京时间工作日；其余含周末半价）</td></tr>
          </table>
          <details style="margin-top:6px">
            <summary class="dim" style="font-size:11.5px;cursor:pointer">按模型 / 按思考档位拆账</summary>
            <table v-if="remote.usage?.by_model && Object.keys(remote.usage.by_model).length" style="margin-top:6px">
              <tr><th>按模型</th><th>次数</th><th>输入</th><th>输出</th><th>花费</th></tr>
              <tr v-for="(v, k) in remote.usage.by_model" :key="k">
                <td class="mono">{{ k }}</td><td class="mono">{{ v.calls }}</td>
                <td class="mono">{{ v.prompt_tokens }}</td><td class="mono">{{ v.completion_tokens }}</td>
                <td class="mono">¥{{ (v.cost_cny ?? 0).toFixed(4) }}</td>
              </tr>
            </table>
            <table v-if="remote.usage?.by_effort && Object.keys(remote.usage.by_effort).length" style="margin-top:6px">
              <tr><th>按思考档位</th><th>次数</th><th>reasoning</th><th>花费</th></tr>
              <tr v-for="(v, k) in remote.usage.by_effort" :key="k">
                <td class="mono">{{ k }}</td><td class="mono">{{ v.calls }}</td>
                <td class="mono">{{ v.reasoning_tokens }}</td>
                <td class="mono">¥{{ (v.cost_cny ?? 0).toFixed(4) }}</td>
              </tr>
            </table>
          </details>

          <!-- ④ 其它模型 + 价格：都折叠，默认不干扰 -->
          <details style="margin-top:14px">
            <summary class="dim" style="font-size:12px;cursor:pointer;font-weight:600">
              其它模型（可存多个，一次仍只用一个）　〔{{ (remoteModels || []).length }} 个〕
            </summary>
            <div class="dim" style="font-size:11.5px;margin:6px 0">
              每个模型各自带自己的接口地址 / key / 档位。点「用这个」= 切换当前模型；
              没点的那几个只是保存着，不参与运行。
            </div>
            <div class="row" style="gap:8px;align-items:center;margin-bottom:6px">
              <button class="ghost tiny" @click="addProfile()">＋ 新增模型</button>
              <span class="dim" style="font-size:11px">升档阈值</span>
              <input type="number" min="1" max="9" v-model.number="escalateAfter"
                     style="width:56px" @change="saveRemote({escalate_after: escalateAfter})" />
              <span class="dim" style="font-size:11px">次（同一档连续失败几次才升思考强度）</span>
            </div>
            <div class="table-wrap" style="max-height:260px">
              <table>
                <tr><th>id</th><th>模型名</th><th style="width:130px">思考档位</th>
                    <th style="width:44px">启用</th><th style="width:62px"></th><th style="width:44px"></th></tr>
                <tr v-for="p in remoteModels" :key="p.id">
                  <td class="mono">{{ p.id }}
                    <span v-if="(remote.remote?.active_model || '') === p.id" class="badge b-ok"
                          style="margin-left:4px">当前</span></td>
                  <td><input v-model="p.model" style="width:150px" @change="saveProfile(p)" /></td>
                  <td><input v-model="p.effortsText" style="width:120px"
                             title="用逗号分隔：none,low,high,max"
                             @change="p.efforts = p.effortsText.split(',').map(s => s.trim()).filter(Boolean); saveProfile(p)" /></td>
                  <td><input type="checkbox" v-model="p.enabled" style="width:auto"
                             @change="saveProfile(p)" /></td>
                  <td><button class="ghost tiny" @click="useProfile(p)"
                              :disabled="(remote.remote?.active_model || '') === p.id">用这个</button></td>
                  <td><button class="ghost tiny" @click="deleteProfile(p.id)" title="删掉这个档案">删除</button></td>
                </tr>
              </table>
            </div>
            <div class="dim" style="font-size:11px;margin-top:6px">
              不填价格就用内置价目估算费用；价格只在"算钱"时用，不影响调用。
            </div>
          </details>

          <details style="margin-top:10px">
            <summary class="dim" style="font-size:12px;cursor:pointer;font-weight:600">
              价格与闲时窗口（可选）
            </summary>
            <div class="dim" style="font-size:11.5px;margin:6px 0">
              单位：<b>人民币 / 每百万 token</b>。高峰价 = 闲时价 × 2；改价只影响"花费"怎么算。
            </div>
            <table>
              <tr><th>模型</th><th style="width:92px">未命中输入</th><th style="width:80px">命中输入</th>
                  <th style="width:70px">输出</th><th style="width:70px">闲时</th></tr>
              <tr v-for="p in remoteModels" :key="'pz' + p.id">
                <td class="mono">{{ p.id }}</td>
                <td><input type="number" step="0.01" v-model.number="p.pricing.offpeak.in_miss"
                           style="width:80px" @change="saveProfile(p)" /></td>
                <td><input type="number" step="0.01" v-model.number="p.pricing.offpeak.in_hit"
                           style="width:70px" @change="saveProfile(p)" /></td>
                <td><input type="number" step="0.01" v-model.number="p.pricing.offpeak.out"
                           style="width:60px" @change="saveProfile(p)" /></td>
                <td class="dim mono" style="font-size:10.5px">高峰 ×2</td>
              </tr>
            </table>
            <div class="row" style="gap:8px;align-items:center;margin-top:8px">
              <span class="dim" style="font-size:11px">闲时窗口</span>
              <input v-model="windowForm.start" placeholder="00:30" style="width:80px" />
              <span class="dim">–</span>
              <input v-model="windowForm.end" placeholder="08:30" style="width:80px" />
              <input v-model="windowForm.tz" placeholder="UTC" style="width:70px" />
              <button class="ghost tiny" @click="savePricingWindow()">保存闲时窗口</button>
            </div>
          </details>

          <details style="margin-top:10px">
            <summary class="dim" style="font-size:12px;cursor:pointer;font-weight:600">
              回归用例库（{{ regressions.length }} 组）
            </summary>
            <div class="dim" style="font-size:11.5px;margin:6px 0">
              远端成功过的判据，同一需求每次运行都会强制检查 —— 登记后同类错误不会再静默通过。
            </div>
            <table v-if="regressions.length">
              <tr><th style="width:60px">id</th><th>模块</th><th>来源</th><th>登记时间</th><th style="width:70px"></th></tr>
              <tr v-for="r in regressions" :key="r.id">
                <td class="mono">#{{ r.id }}</td>
                <td class="mono">{{ r.module }}</td>
                <td class="dim">{{ r.source }}</td>
                <td class="dim" style="font-size:11.5px">{{ r.created_at }}</td>
                <td><button class="ghost tiny" @click="deleteRegression(r.id)">删除</button></td>
              </tr>
            </table>
            <div v-else class="dim" style="font-size:11.5px">
              还没有回归用例。远端协助成功一次后会自动登记。
            </div>
            <div class="row" style="gap:8px;margin-top:8px" v-if="regressions.length">
              <button class="ghost tiny" @click="loadRegressions()">刷新</button>
              <button class="ghost tiny" @click="clearRegressions()">清空全部</button>
            </div>
          </details>

          <div class="dim" style="font-size:11.5px;margin-top:10px">
            命令行等价：<code>python cli.py remote --key sk-...</code>　|　交接：<code>python cli.py handoff &lt;task&gt; --node N --remote --apply</code>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 登录选项（弹窗，不再用下拉） ============ -->
    <div v-if="showLoginModal" class="modal-mask" @click.self="showLoginModal = false">
      <div class="modal" style="max-width:480px">
        <div class="modal-head">
          <strong>登录选项</strong>
          <span class="spacer"></span>
          <button class="ghost tiny" @click="showLoginModal = false">关闭 ✕</button>
        </div>
        <div class="modal-body">
          <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
            <input type="checkbox" :checked="openAtLogin" :disabled="!loginSupported"
                   style="width:auto" @change="toggleAutoStart()" />
            <span>开机自启</span>
            <span v-if="!loginSupported" class="dim" style="font-size:11px">（需桌面版）</span>
          </label>
          <div class="dim" style="font-size:12px;margin-top:10px">
            开启后，登录系统时自动启动本程序并拉起本地服务；关闭则只在你手动打开时启动。
          </div>
          <div class="panel-title" style="margin-top:14px">当前状态</div>
          <table>
            <tr><td class="dim" style="width:120px">开机自启</td>
                <td><span class="badge" :class="openAtLogin ? 'b-ok' : 'b-dim'">{{ openAtLogin ? '已开启' : '未开启' }}</span></td></tr>
            <tr><td class="dim">运行环境</td>
                <td class="mono">{{ loginSupported ? '桌面版（可设置）' : '浏览器版（无此能力）' }}</td></tr>
          </table>
        </div>
      </div>
    </div>

    <!-- ============ 设置 / 帮助 ============ -->
    <div v-if="showSettings" class="modal-mask" @click.self="showSettings = false">
      <div class="modal">
        <div class="modal-head">
          <span class="tab" :class="{ active: settingsTab === 'help' }" @click="settingsTab = 'help'">需求帮助</span>
          <span class="tab" :class="{ active: settingsTab === 'fields' }" @click="settingsTab = 'fields'">输入说明</span>
          <span class="tab" :class="{ active: settingsTab === 'mode' }" @click="settingsTab = 'mode'">权限模式</span>
          <span class="tab" :class="{ active: settingsTab === 'commit' }" @click="settingsTab = 'commit'">提交规范</span>
          <span class="tab" :class="{ active: settingsTab === 'tc' }" @click="settingsTab = 'tc'">工具链</span>
          <span class="tab" :class="{ active: settingsTab === 'remote' }" @click="settingsTab = 'remote'">远端模型</span>
          <span class="tab" :class="{ active: settingsTab === 'status' }" @click="settingsTab = 'status'">状态说明</span>
          <span class="spacer"></span>
          <button class="ghost tiny" @click="showSettings = false">关闭 ✕</button>
        </div>

        <div class="modal-body">
          <!-- 需求帮助 -->
          <template v-if="settingsTab === 'help'">
            <!-- ① 先给一句话：这套系统怎么用 -->
            <div class="panel-title">三步开始（先看这个）</div>
            <ol class="help-list">
              <li><b>写需求</b>：说清要什么、写进哪个文件名、函数长什么样（细节见下面「需求怎么写」）。</li>
              <li><b>选规模</b>：S / M / L / XL 决定这一夜上多少约束（细节见「目标代码规模怎么选」）。</li>
              <li><b>点「开始夜班」</b>：系统自己生成规格 → 写代码 → 跑机械判据 → 交付报告；你不用守着。</li>
            </ol>
            <div class="dim" style="font-size:11.5px;margin-top:6px">
              关键点：<b>验收权在本地</b>。远端（或本地模型）写出来的东西，必须过完机械判据才算数；
              过不了就带着失败证据自己改，改到全绿为止（甲方模式最多 4 轮），不需要你中途插手。
            </div>

            <!-- ② 需求怎么写 -->
            <div class="panel-title" style="margin-top:16px">需求怎么写</div>
            <ul class="help-list">
              <li v-for="(p, i) in HELP_POINTS" :key="i">{{ p }}</li>
            </ul>

            <div class="panel-title" style="margin-top:14px">目标文件写在哪</div>
            <ul class="help-list">
              <li>界面上没有单独的「目标文件」框：<b>在需求里写清文件名即可</b>（如「写进 clean.py」）。</li>
              <li>写了文件名 → 只改那个文件；没写 → 系统按需求里的函数签名自己决定放哪，并在报告里说明。</li>
              <li>要新建文件就直说「新建 store.py，包含 create_table / upsert / fetch_all」。</li>
            </ul>

            <!-- ③ 规模 -->
            <div class="panel-title" style="margin-top:16px">目标代码规模怎么选（约束按规模分级）</div>
            <div class="dim" style="font-size:11.5px;margin-bottom:6px">
              规模不同，该上的约束完全不同：小项目上重约束是过度工程化，大项目上轻约束等于没有约束。
              选错档 = 这一夜的验收标准错了，所以必须自己选，系统不会替你猜。
            </div>
            <table>
              <tr><th style="width:170px">档位</th><th>这一档管什么 / 明确不管什么</th></tr>
              <tr v-for="t in night.scaleTiers" :key="t.id">
                <td class="mono" style="font-size:11px">{{ t.label }}</td>
                <td class="dim" style="font-size:11.5px">{{ t.hint }}</td>
              </tr>
            </table>
            <ul class="help-list" style="margin-top:8px">
              <li>说不准就选 <b>S</b>：小仓库尤其别上重约束；代码长到 10 万行系统会提示你升档。</li>
              <li>也可以在需求正文里写一行「代码规模：M」，系统会照它跑，并在交付报告里写明依据。</li>
              <li>规模同时决定：可维护性阈值（S 12/4/60 → L 8/3/40）、每档允许重写次数（S 3 → L 1）、
                默认预算（S 0.5h → L 6h）、以及要不要跑架构测试/覆盖率/变异测试。</li>
            </ul>

            <!-- ④ 模型与思考强度（单模型机制的说明） -->
            <div class="panel-title" style="margin-top:16px">模型与思考强度（为什么只有一个模型）</div>
            <ul class="help-list">
              <li><b>一次运行只用一个模型</b>：你在「远端模型」里填的接口地址 / 模型名 / key 就是它。</li>
              <li><b>思考强度由系统自动挑</b>，不需要你填：
                规格与方案 → 最深一档（它决定后面每个模块的对错）；
                样板/校验/日志/适配 → 不思考；写代码 → 按难度（依赖数、函数数、示例数、历史失败次数）。</li>
              <li>失败只在<b>同一个模型内</b>升思考强度；升到最深还不过 = 这个模型到极限了，
                换模型由你决定（「其他模型」里点「用这个」）。</li>
              <li>想同时存好几个模型（比如便宜的和贵的）：在「其他模型」里各存一条、各填各的链接参数，
                但一次只会用其中一个。</li>
            </ul>

            <!-- ⑤ 示例 -->
            <div class="panel-title" style="margin-top:16px">示例（点「填入」写进需求框）</div>
            <div v-for="(ex, i) in HELP_EXAMPLES" :key="i" class="help-item">
              <div class="ev-head">
                <strong>{{ ex.title }}</strong>
                <span v-if="ex.target" class="badge b-dim">目标 {{ ex.target }}</span>
                <span class="spacer"></span>
                <button class="ghost tiny" @click="useExample(ex)">填入 →</button>
              </div>
              <div class="dim" style="font-size:11.5px;white-space:pre-wrap;margin-top:4px">{{ ex.text }}</div>
            </div>
          </template>

          <!-- 输入说明：所有输入框怎么填都写在这里，界面上一律不预填 -->
          <template v-else-if="settingsTab === 'fields'">
            <div class="dim" style="font-size:12px;margin-bottom:10px">
              界面上所有输入框默认都是空的，只留字段名。下表说明每个字段填什么、示例长什么样。
            </div>
            <template v-for="grp in FIELD_HELP" :key="grp.group">
              <div class="panel-title" style="margin-top:12px">{{ grp.group }}</div>
              <table>
                <tr><th style="width:120px">字段</th><th style="width:128px">位置</th><th>怎么填</th><th style="width:170px">示例</th></tr>
                <tr v-for="r in grp.rows" :key="r.field">
                  <td class="mono">{{ r.field }}</td>
                  <td class="dim" style="font-size:11px">{{ r.where }}</td>
                  <td class="dim" style="font-size:11.5px">{{ r.how }}</td>
                  <td class="mono wrap" style="font-size:10.5px">{{ r.demo }}</td>
                </tr>
              </table>
            </template>
          </template>

          <!-- 权限模式 -->
          <template v-else-if="settingsTab === 'mode'">
            <div class="panel-title">当前模式：{{ currentMode?.name }}</div>
            <div class="dim" style="font-size:12px;margin-bottom:4px">{{ currentMode?.desc }}</div>
            <div class="dim" style="font-size:11.5px;margin-bottom:10px">
              边界：{{ currentMode?.boundary }}
            </div>
            <div class="row">
              <select v-model="currentMode" @change="setMode(currentMode.id)" style="max-width:240px">
                <option v-for="m in modes" :key="m.id" :value="m">{{ m.name }}</option>
              </select>
              <span class="badge" :class="autoApprove() ? 'b-ok' : 'b-warn'">
                {{ autoApprove() ? '人工处置全部放行' : '关键节点需人工审核' }}
              </span>
            </div>
            <ul class="dim" style="font-size:11.5px;margin:8px 0 0 18px">
              <li v-for="x in (currentMode?.extras || [])" :key="x">{{ x }}</li>
            </ul>

            <div class="panel-title" style="margin-top:14px">三种模式</div>
            <table>
              <tr><th style="width:96px">模式</th><th style="width:104px">人工审核</th>
                  <th>能做什么</th><th style="width:200px">边界</th></tr>
              <tr v-for="m in modes" :key="m.id">
                <td><strong>{{ m.name }}</strong>
                  <span v-if="m.is_current" class="badge b-acc">当前</span></td>
                <td>
                  <span class="badge" :class="m.auto_approve ? 'b-ok' : 'b-warn'">
                    {{ m.auto_approve ? '全部放行' : '关键节点要人批' }}
                  </span>
                </td>
                <td class="dim" style="font-size:11.5px">{{ m.desc }}</td>
                <td class="dim" style="font-size:11px">{{ m.boundary }}</td>
              </tr>
            </table>

            <template v-if="developerMode()">
              <div class="panel-title" style="margin-top:14px">
                待审节点
                <span class="badge b-warn" v-if="pendingApprovals.length">{{ pendingApprovals.length }}</span>
                <span class="badge b-dim" v-else>无</span>
              </div>
              <div class="dim" style="font-size:11.5px;margin-bottom:8px">
                开发者模式下这些节点<b>不会自动放行</b>：写文件 / 提交 / 推送 / 上报远端 / 收尾交付。
                没人处理时超时按拒绝算（不会因为"没人回答"就当成通过）。
              </div>
              <div v-if="!pendingApprovals.length" class="dim" style="font-size:11.5px">
                当前没有等待中的节点。
              </div>              <div v-for="t in pendingApprovals" :key="t.id" class="approval-card">
                <div class="mono" style="font-size:12px">
                  <span class="badge b-warn">{{ keyNodeLabels[t.node] || t.node }}</span>
                  {{ t.title }}
                  <span class="dim" style="font-size:11px">（已等 {{ t.seconds_waiting }}s）</span>
                </div>
                <pre class="approval-diff">{{ t.detail }}</pre>
                <div class="row">
                  <button class="primary" @click="decideApproval(t, 'approved')">通过</button>
                  <button class="ghost" @click="decideApproval(t, 'rejected')">拒绝</button>
                </div>
              </div>

              <!-- 待人回答：本地模型"不猜、发问"时的问题列表（与上面的审批分开） -->
              <div class="panel-title" style="margin-top:14px">
                待人回答
                <span class="badge b-warn" v-if="questions.length">{{ questions.length }}</span>
                <span class="badge b-dim" v-else>无</span>
              </div>
              <div class="dim" style="font-size:11.5px;margin-bottom:8px">
                本地模型<b>遇到关键问题会停下来问</b>，不会自己胡写。答案作为 <b>human 证据</b>入账，
                同一问题以后不再问；没有选项也能直接打字回答。
              </div>
              <div v-if="!questions.length" class="dim" style="font-size:11.5px">
                当前没有待人回答的问题。
              </div>
              <div v-for="q in questions" :key="q.qid" class="approval-card">
                <div class="mono" style="font-size:12px">
                  <span class="badge b-warn">提问</span>
                  {{ q.question }}
                </div>
                <div v-if="q.why" class="dim" style="font-size:11px;margin-top:2px">为什么问：{{ q.why }}</div>
                <div v-if="q.node" class="dim mono" style="font-size:10.5px">节点：{{ q.node }}</div>
                <div class="row" style="margin-top:6px;gap:6px;flex-wrap:wrap">
                  <button v-for="opt in (q.options || [])" :key="opt" class="primary"
                          @click="answerQuestion(q, opt)">{{ opt }}</button>
                </div>
                <div class="row" style="margin-top:6px;gap:6px">
                  <input v-model="answers[q.qid]" placeholder="或自己写一句回答…" style="flex:1"
                         @keyup.enter="answerQuestion(q)" />
                  <button class="ghost" @click="answerQuestion(q)">提交回答</button>
                </div>
              </div>
            </template>
            <div class="panel-title" style="margin-top:14px">模式 → 界面上哪些按钮可用</div>
            <div class="dim" style="font-size:11.5px;margin-bottom:6px">
              权限模式管"能不能"，界面按钮只管"这次做什么"。下面这张表就是两者的对应关系。
            </div>
            <table>
              <tr><th style="width:110px">模式</th><th>发送</th><th>审查</th><th>Git 提交·分支</th><th>Git 远端</th></tr>
              <tr>
                <td class="mono">只读模式</td>
                <td><span class="badge b-bad">禁用</span></td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-bad">禁用</span></td>
                <td><span class="badge b-bad">禁用</span></td>
              </tr>
              <tr>
                <td class="mono">开发者模式</td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-bad">禁用</span></td>
              </tr>
              <tr>
                <td class="mono">甲方模式</td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-ok">可用</span></td>
                <td><span class="badge b-ok">可用</span></td>
              </tr>
            </table>

            <table style="margin-top:10px">
              <tr><th>能力</th><th>说明</th><th>当前</th></tr>
              <tr v-for="(label, cap) in capabilityLabels" :key="cap">
                <td class="mono">{{ cap }}</td>
                <td class="dim">{{ label }}</td>
                <td><span class="badge" :class="can(cap) ? 'b-ok' : 'b-bad'">{{ can(cap) ? '允许' : '禁止' }}</span></td>
              </tr>
            </table>
          </template>

          <!-- 提交规范 -->
          <template v-else-if="settingsTab === 'commit'">
            <div class="panel-title">提交信息怎么选前缀</div>
            <div class="dim" style="font-size:12px;margin-bottom:10px">
              Git 面板里先选一个类型，再填一句话描述，引擎拼成
              <code>类型: 描述</code>。规范化的前缀让历史可读、让
              <code>git log --grep</code> 和自动生成 CHANGELOG 都能用。
            </div>
            <table>
              <tr><th style="width:120px">类型</th><th>什么时候用</th></tr>
              <tr v-for="t in COMMIT_TYPES" :key="t.id">
                <td class="mono">{{ t.label }}</td>
                <td class="dim">{{ t.desc }}</td>
              </tr>
            </table>
            <div class="panel-title" style="margin-top:14px">规则</div>
            <ul class="help-list">
              <li>一句话说清"改了什么"，不要写"update""修改一下"。</li>
              <li>类型只选一个；同时有新功能和修复，就分两次提交。</li>
              <li>不兼容改动在类型后加 <code>!</code>，如 <code>feat!: 移除旧接口</code>。</li>
              <li>如需标注模块，可在类型后加作用域：<code>fix(storage): 原子写未关闭临时文件</code>。</li>
              <li>正文里自带规范前缀时，Git 面板不会再重复加一层。</li>
            </ul>
            <div class="panel-title" style="margin-top:14px">预览</div>
            <div class="mono" style="font-size:12px">{{ commitFull }}</div>
          </template>

          <!-- 远端模型：DeepSeek key 可随时更新，改完下一次请求即生效 -->
          <template v-else-if="settingsTab === 'remote'">
            <div class="panel-title">远端协助（DeepSeek）</div>
            <div class="dim" style="font-size:12px;margin-bottom:10px">
              本地模型卡住时可以"出方案"交给远端：远端只回符号级编辑，本地用确定性渲染器落盘并重跑全部闸门，
              远端声明不算数。key 只存在本机设置文件，界面上永远只显示掩码。
            </div>
            <table>
              <tr><td class="dim" style="width:140px">状态</td>
                  <td><span class="badge" :class="remote.available ? 'b-ok' : 'b-dim'">
                    {{ remote.available ? '可用' : '未配置' }}</span>
                    <span v-if="remote.reason" class="dim" style="margin-left:8px;font-size:11.5px">{{ remote.reason }}</span>
                  </td></tr>
              <tr><td class="dim">当前 key</td>
                  <td class="mono">{{ remote.remote?.key_masked || '—' }}
                    <span class="dim" style="font-size:11px">（来源：{{ remote.remote?.key_source || '无' }}）</span></td></tr>
            </table>
            <div class="panel-title" style="margin-top:12px">更新密钥</div>
            <div class="row" style="gap:8px;align-items:center">
              <input v-model="remoteForm.api_key" type="password" placeholder="sk-...（留空则不修改）" style="flex:1" />
              <button class="ghost tiny" @click="remoteForm.api_key && saveRemote({api_key: remoteForm.api_key})">保存 key</button>
              <button class="ghost tiny" @click="revealRemoteKey()">{{ remoteForm.api_key ? '隐藏' : '显示' }}</button>
            <button class="ghost tiny" @click="clearRemoteKey()">清除</button>
            </div>
            <div class="panel-title" style="margin-top:12px">连接参数</div>
            <div class="row" style="gap:8px;align-items:center">
              <input v-model="remoteForm.model" placeholder="deepseek-flash" style="width:180px" />
              <input v-model="remoteForm.base_url" placeholder="https://api.deepseek.com" style="flex:1" />
              <button class="ghost tiny" @click="saveRemote({model: remoteForm.model, base_url: remoteForm.base_url})">保存</button>
            </div>
            <div class="panel-title" style="margin-top:14px">用量（本地累计，含缓存命中）</div>
            <table>
              <tr><td class="dim" style="width:140px">调用次数</td><td class="mono">{{ remote.usage?.calls ?? 0 }}</td></tr>
              <tr><td class="dim">prompt / completion</td>
                  <td class="mono">{{ remote.usage?.prompt_tokens ?? 0 }} / {{ remote.usage?.completion_tokens ?? 0 }}</td></tr>
              <tr><td class="dim">缓存命中率</td>
                  <td class="mono">{{ ((remote.usage?.cache_hit_rate ?? 0) * 100).toFixed(0) }}%
                    <span class="dim" style="font-size:11px">（DeepSeek 前缀缓存，命中部分约 1/10 价）</span></td></tr>
              <tr><td class="dim">同证据复用</td><td class="mono">{{ remote.usage?.cached_replies ?? 0 }} 次（0 token）</td></tr>
              <tr><td class="dim">reasoning tokens</td><td class="mono">{{ remote.usage?.reasoning_tokens ?? 0 }}</td></tr>
            <tr><td class="dim">忙时（全价）</td>
                <td class="mono">{{ remote.usage?.peak?.calls ?? 0 }} 次 · {{ remote.usage?.peak?.prompt_tokens ?? 0 }}p / {{ remote.usage?.peak?.completion_tokens ?? 0 }}c · ${{ (remote.usage?.peak?.cost_usd ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">闲时（折扣）</td>
                <td class="mono">{{ remote.usage?.offpeak?.calls ?? 0 }} 次 · {{ remote.usage?.offpeak?.prompt_tokens ?? 0 }}p / {{ remote.usage?.offpeak?.completion_tokens ?? 0 }}c · ${{ (remote.usage?.offpeak?.cost_usd ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">总成本</td><td class="mono">${{ (remote.usage?.cost_usd ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">缓存省下</td><td class="mono">${{ (remote.usage?.cache_saved_usd ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">闲时窗口</td>
                <td class="mono">{{ remote.usage?.window?.start || '00:30' }}–{{ remote.usage?.window?.end || '08:30' }} {{ remote.usage?.window?.tz || 'UTC' }}</td></tr>
            </table>
            <div class="dim" style="font-size:11.5px;margin-top:8px">
              命令行等价操作：<code>python cli.py remote --key sk-...</code>；
              交接用法：<code>python cli.py handoff &lt;task&gt; --node N --remote --apply</code>
            </div>
          </template>

          <!-- 状态说明：原来顶栏那两个看不懂的药丸，含义写在这里 -->
          <template v-else-if="settingsTab === 'status'">
            <div class="panel-title">当前状态</div>
            <table>
              <tr><td class="dim" style="width:150px">Ollama</td>
                  <td><span class="badge" :class="health?.ollama ? 'b-ok' : 'b-bad'">{{ health?.ollama ? '在线' : '离线' }}</span></td></tr>
              <tr><td class="dim">可用模型</td><td class="mono" style="font-size:11px">{{ (models || []).join(' , ') || '—' }}</td></tr>
              <tr><td class="dim">当前模型</td>
                  <td class="mono">{{ form.model || '—' }}
                    <span v-if="currentModelTooSmall" class="badge b-warn" style="margin-left:6px">低于门槛</span>
                  </td></tr>
              <tr><td class="dim">模型门槛</td>
                  <td class="wrap" style="font-size:11px">本地工人 ≥{{ modelPolicy?.min_params_b || MIN_PARAMS_B }}B
                    <span class="dim">· 低于门槛的不能选：这类小模型实测连结构化输出都不稳
                    （机械验收 2/8、独立验收 2/16），跑一夜只会产出"看起来完成"的东西。</span>
                  </td></tr>
              <tr><td class="dim">权限模式</td><td>{{ currentMode?.name }} · {{ currentMode?.capabilities.length }} 项能力</td></tr>
              <tr><td class="dim">沙箱模式 / 拦截规则</td>
                  <td class="mono">{{ sandboxInfo ? sandboxInfo.mode + ' · ' + sandboxInfo.deny_rules + ' 条规则' : '（先选仓库后探测）' }}</td></tr>
              <tr><td class="dim">工作区</td><td class="mono wrap" style="font-size:10.5px">{{ sandboxInfo?.workspace || '—' }}</td></tr>
              <tr><td class="dim">本页事件数</td><td class="mono">{{ events.length }}</td></tr>
            </table>

            <div class="panel-title" style="margin-top:14px">这两个指标是什么意思</div>
            <ul class="help-list">
              <li><b>沙箱模式</b>：Agent 操作只允许发生在任务工作区副本里（不是你的原始仓库）。
                  <code>readonly</code> 表示当前只读，<code>write</code> 表示允许写文件（写前自动备份，可撤回）。</li>
              <li><b>拦截规则 N 条</b>：命令策略里被禁止的模式数量，例如 <code>curl/wget/ssh</code>（禁外联）、
                  <code>pip install</code>（禁装包）、<code>rm -rf / format / shutdown</code>（禁破坏）、
                  <code>git push / reset --hard</code>（禁不可逆操作）、以及越界路径与 <code>.env/.ssh</code> 等敏感文件。</li>
              <li><b>事件数</b>：本次页面里时间线累计收到的执行事件条数（探测、计划、补丁、闸门、审计…）。
                  它只是"看得见的过程流水"，刷新页面会归零，不代表任务进度或版本。</li>
            </ul>
          </template>

          <!-- 工具链 -->
          <template v-else>
            <div class="panel-title">工具链（设置里的覆盖优先于环境变量）</div>
            <div v-if="!isShell" class="dim" style="font-size:11.5px;margin-bottom:8px">
              网页版不能弹目录选择器；桌面版（Electron）可用「浏览…」直接指定 JDK 等安装目录。
            </div>
            <table>
              <tr><th>工具</th><th>状态</th><th>版本</th><th>路径</th><th></th></tr>
              <tr v-for="t in toolchains" :key="t.id">
                <td class="mono">{{ t.id }}</td>
                <td>
                  <span v-if="t.ok" class="badge b-ok">✓</span>
                  <span v-else-if="t.version" class="badge b-warn">!</span>
                  <span v-else class="badge b-bad">✗</span>
                </td>
                <td class="mono dim">{{ t.version || '—' }}</td>
                <td class="mono dim wrap" style="font-size:10.5px">{{ t.path || '未找到' }}</td>
                <td>
                  <button v-if="isShell && can('toolchain.config')" class="ghost tiny" @click="pickToolchainDir(t)">浏览…</button>
                </td>
              </tr>
            </table>
            <div class="dim" style="font-size:10.5px;margin-top:8px">
              探测顺序：设置覆盖 &gt; 环境变量（JAVA_HOME 等）&gt; PATH &gt; 常见安装目录。
            </div>
          </template>
        </div>
      </div>
    </div>
    <!-- ============ 清空历史确认 ============ -->
    <div v-if="showClear" class="modal-mask" @click.self="showClear = false">
      <div class="modal confirm">
        <div class="modal-head"><strong>清空全部历史任务？</strong><span class="spacer"></span>
          <button class="ghost tiny" @click="showClear = false">取消</button>
        </div>
        <div class="modal-body">
          <div class="dim" style="font-size:12px;margin-bottom:10px">
            当前共 <b>{{ history.length }}</b> 条记录。<b>正在运行的任务会保留</b>（可重启服务清理僵尸任务）。
            你的原始仓库不会被改动。
          </div>
          <div class="row" style="justify-content:flex-end;gap:8px">
            <button class="ghost" :disabled="clearBusy" @click="doClearAll(false)">仅清空记录</button>
            <button class="danger" :disabled="clearBusy" @click="doClearAll(true)">清空记录 + 工作区</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ stuck（有节点没通过）处置 ============ -->
    <div v-if="resolveTarget" class="modal-mask" @click.self="resolveTarget = null">
      <div class="modal confirm">
        <div class="modal-head"><strong>处置需要人工确认的节点</strong><span class="spacer"></span>
          <button class="ghost tiny" @click="resolveTarget = null">取消</button>
        </div>
        <div class="modal-body">
          <table>
            <tr><td class="dim" style="width:70px">节点</td><td class="mono">{{ resolveTarget.node.id }}</td></tr>
            <tr><td class="dim">文件</td><td class="mono">{{ resolveTarget.node.path }}</td></tr>
            <tr><td class="dim">失败原因</td><td class="wrap" style="font-size:11px">{{ String(resolveTarget.node.last_error || '').slice(0, 200) }}</td></tr>
          </table>
          <div class="dim" style="font-size:11.5px;margin:10px 0">
            处置都会记成 <code>human</code> 证据：接受＝你人工审过并放行；丢弃＝这条不作数。
            想再让 Agent 试一次，请用「基于此工作区续跑」。
          </div>
          <div class="row" style="margin-bottom:8px">
            <input v-model="resolveTarget.reason" placeholder="处置理由（会写进证据与报告）" style="flex:1" />
          </div>
          <div class="row" style="justify-content:flex-end;gap:8px">
            <button class="ghost" :disabled="resolveBusy" @click="doResolve('drop')">丢弃</button>
            <button :disabled="resolveBusy" @click="doResolve('accept')">接受（人工放行）</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ============ 删除确认 ============ -->
    <div v-if="delTarget" class="modal-mask" @click.self="delTarget = null">
      <div class="modal confirm">
        <div class="modal-head"><strong>删除任务？</strong><span class="spacer"></span>
          <button class="ghost tiny" @click="delTarget = null">取消</button>
        </div>
        <div class="modal-body">
          <table>
            <tr><td class="dim" style="width:70px">任务</td><td class="mono">{{ delTarget.id }}</td></tr>
            <tr><td class="dim">状态</td><td><span class="badge b-dim">{{ delTarget.state }}</span></td></tr>
            <tr><td class="dim">需求</td><td class="wrap" style="font-size:11.5px">{{ String(delTarget.requirement || '').slice(0, 120) }}</td></tr>
          </table>
          <div class="dim" style="font-size:11.5px;margin:10px 0">
            删除会移除该任务的记录、节点、闸门证据与事件。<br />
            工作区副本（`workspaces/&lt;id&gt;/`，含产出的代码）可以一起删，也可以留着继续看。<b>你的原始仓库不会被改动。</b>
          </div>
          <div class="row" style="justify-content:flex-end;gap:8px">
            <button class="ghost" :disabled="delBusy" @click="doDelete(false)">仅删除记录</button>
            <button class="danger" :disabled="delBusy" @click="doDelete(true)">删除记录 + 工作区</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
