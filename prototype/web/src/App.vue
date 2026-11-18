<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'

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
    await loadTree('')      // 探测通过后同步刷新左栏文件树
    return
  } catch (e) { errMsg.value = e.message } finally { busy.value = false }
}

/* ---------------- 设置 / 帮助 ---------------- */
const showSettings = ref(false)

/* 远端模型（DeepSeek）：key 只存本机设置文件，界面只显示掩码 */
const remote = ref({ available: false, reason: '', remote: {}, usage: {} })
const remoteForm = reactive({ api_key: '', model: '', base_url: '', compat: 'deepseek' })

async function loadRemote(reveal = false) {
  try {
    const d = await api('/v1/settings/remote' + (reveal ? '?reveal=true' : ''))
    remote.value = d
    remoteForm.model = d.remote?.model || ''
    remoteForm.base_url = d.remote?.base_url || ''
    remoteForm.compat = d.remote?.compat || 'deepseek'
    if (reveal) remoteForm.api_key = d.remote?.key_plain || ''
    loadPricing(d)
    loadProfiles()        // 模型档案与"单模型思考强度阶梯"一起刷新（改了立刻能看到新阶梯）
  } catch (e) { errMsg.value = '[FAIL] 读取远端设置失败：' + e.message }
}

// ---------------------------------------------------------------- 厂商（一等公民）
// 2026-11-09 用户口径：点开「远端」→ 一个下拉（deepseek / glm / openai）+ 三个长方矩形；
// 矩形中间是大字厂商名，右侧偏右是「正在使用」的三选一；点矩形弹出该厂商的配置弹窗
// （apikey + 思考强度等级 + 该厂商的模型列表，可添加/删除）。
const providers = ref([])
const providerModal = ref(null)             // 正在配置哪个厂商（null = 没开二级弹窗）
const providerDraft = ref(null)             // 二级弹窗里的草稿
const EFFORT_LEVELS = ['none', 'low', 'high', 'max']

function providerById(pid) {
  return providers.value.find((p) => p.id === pid) || null
}
function currentProvider() {
  return providers.value.find((p) => p.current) || providers.value[0] || null
}
function providerKeyText(pid) {
  const p = providerById(pid)
  return p ? (p.key_configured ? (p.key_masked || '已配置') : '未设置') : '未设置'
}
function providerModelCount(pid) {
  const p = providerById(pid)
  return p ? (p.models || []).length : 0
}

async function loadProviders() {
  try {
    const d = await api('/v1/settings/remote')
    // 厂商在 `d.remote.providers`（外层是 {remote:{...}, available, usage}）
    providers.value = d.remote?.providers || d.providers || []
  } catch (e) { errMsg.value = '[FAIL] 读取厂商设置失败：' + e.message }
}

function openProviderModal(pid) {
  closeOverlays('provider')
  const p = providerById(pid)
  if (!p) return
  providerModal.value = pid
  keyViewKey.value = null
  providerDraft.value = JSON.parse(JSON.stringify({
    id: p.id, label: p.label, base_url: p.base_url,
    efforts: [...(p.efforts || [])],
    api_key: '',                       // 留空 = 不修改已有 key
    // 2026-11-18 UI 改版：**每个 key 类独立一份配置**（key/备注/自己的模型列表），
    // 厂商不再有统一的模型表 —— 模型挂在哪把 key 下，审核者/写作者就各用各的。
    keys: (p.keys || []).map((k) => ({
      ...k, api_key: '',
      models: (k.models || []).map((m) => ({ ...m })),
    })),
  }))
}

// ---------------------------------------------------------------- key 类卡片操作
// 层级（2026-11-18 用户定稿）：厂商 → **key 卡片列表**（和厂商卡一样）→ 点进去才是
// 这把 key 的模型配置。新增只填 名字/用途/key，模型自动按厂商默认填好。
const keyViewKey = ref(null)        // 正在看详情的 key（null = 列表视图）

function roleName(r) {
  return ({ writer: '写作者', reviewer1: '审核者1', reviewer2: '审核者2' })[r] || '备用'
}

function draftAddKey() {
  const d = providerDraft.value
  if (!d) return
  const used = new Set(d.keys.map((k) => k.role))
  const free = ['writer', 'reviewer1', 'reviewer2'].find((r) => !used.has(r)) || ''
  const k = { id: '', label: '', role: free,
              api_key: '', current: free === 'writer',
              models: [], _draft: true }
  d.keys.push(k)
  keyViewKey.value = k          // 新增后直接进详情（只填三样，模型不在这里配）
}

// 角色指派（2026-11-18 晚）：三个角色可以是**同一厂商的三把 key**。
// 每个角色只有一个位置；指派给这把，就把别的同类角色清掉。
function draftSetRole(k, role) {
  const d = providerDraft.value
  if (!d) return
  k.role = role || ''
  if (k.role) {
    d.keys.forEach((x) => { if (x !== k && x.role === k.role) x.role = '' })
  }
  d.keys.forEach((x) => { x.current = (x.role === 'writer') })
}

async function draftRemoveKey(k) {
  const d = providerDraft.value
  if (!d) return
  const wasSaved = !!(k && k.id)
  d.keys = d.keys.filter((x) => x !== k)
  if (d.keys.length && !d.keys.some((x) => x.role === 'writer')) {
    d.keys[0].role = 'writer'
    d.keys[0].current = true
  }
  if (keyViewKey.value === k) keyViewKey.value = null
  if (!wasSaved) return                    // 草稿：没落过盘，删本地就行
  // **已保存的 key：删除立即写进配置文件**（用户口径），不依赖"保存厂商"按钮——
  // 列表页已经没有那个按钮了，靠它兜底会让删除只活在草稿里。
  try {
    const r = await api(`/v1/settings/remote/providers/${d.id}/keys/${encodeURIComponent(k.id)}`,
      { method: 'DELETE' })
    providers.value = r.providers || providers.value
    if (providerModal.value === d.id) openProviderModal(d.id)
    errMsg.value = `已删除 key「${k.label || k.id}」，配置已同步`
  } catch (e) { errMsg.value = '[FAIL] 删除 key 失败：' + (e && e.message ? e.message : e) }
}

function draftAddModelTo(k) {
  const d = providerDraft.value
  if (!k) return
  if (!Array.isArray(k.models)) k.models = []
  k.models.push({
    id: '', label: '', model: '', efforts: [...(d.efforts || [])],
    max_output_tokens: 0, temperature: 0.2, priority: 30,
    enabled: true, pricing: {}, api_key: '',
  })
}

function draftRemoveModelOf(k, m) {
  k.models = (k.models || []).filter((x) => x !== m)
}

function _normKeyOut(k) {
  return {
    id: String(k.id || '').trim(),        // 留空 → 后端自动编号（k2/k3…）
    label: String(k.label || '').trim().slice(0, 48),
    api_key: String(k.api_key || '').trim(),   // 留空 = 不修改已有
    role: String(k.role || ''),           // writer / reviewer1 / reviewer2 / ''
    current: k.role === 'writer',
    // 新 key 且没配模型 → 不带 models 字段（后端自动继承厂商默认）；
    // 老 key 必须带（否则服务端视为"不修改"，删模型就删不掉）
    ...(k._draft && !(k.models || []).length ? {} : { models: (k.models || [])
      .filter((m) => String(m.model || '').trim())
      .map((m) => ({
        id: String(m.id || m.model || '').trim().slice(0, 32),
        label: String(m.label || m.model || '').trim().slice(0, 48),
        model: String(m.model || '').trim().slice(0, 64),
        efforts: (m.efforts && m.efforts.length) ? m.efforts : (providerDraft.value?.efforts || ['none']),
        supports_tools: m.supports_tools === true ? 'true'
          : (m.supports_tools === false ? 'false' : ''),
        max_output_tokens: Number(m.max_output_tokens) || 0,
        temperature: Number(m.temperature) || 0.2,
        priority: Number(m.priority) || 30,
        enabled: m.enabled !== false,
        pricing: m.pricing || {},
        api_key: String(m.api_key || '').trim(),
      })),
    }),
  }
}

function closeProviderModal() {
  providerModal.value = null
  providerDraft.value = null
  keyViewKey.value = null
}

function draftToggleEffort(eff) {
  const d = providerDraft.value
  if (!d) return
  const i = d.efforts.indexOf(eff)
  if (i >= 0) d.efforts.splice(i, 1)
  else d.efforts.push(eff)
}

async function saveProviderDraft() {
  const d = providerDraft.value
  if (!d) return
  try {
    // **按 key 类提交**（2026-11-18 UI 改版）：每把 key 带自己的模型列表。
    // 角色在每把 key 上显式指派（writer / reviewer1 / reviewer2 / 空）。
    const keys = (d.keys || []).map(_normKeyOut)
    for (const k of keys) {
      // 新 key 没有 models 字段（由后端按厂商默认填）→ 必须兜住 undefined，
      // 否则这里抛 TypeError 整个保存静默失败（实测：点了没任何反应）。
      for (const m of (k.models || [])) {
        if (!m.id) { errMsg.value = '[FAIL] 每个模型都要有 ID（没填就保存不了）'; return }
      }
    }
    if (keys.length && !keys.some((k) => k.role === 'writer')) {
      // 没指 writer → 第一把顶上（后端也会兜底，这里提前说清楚）
      keys[0].role = 'writer'
      keys[0].current = true
    }
    const r = await api('/v1/settings/remote/providers', {
      method: 'POST',
      body: JSON.stringify({
        id: d.id, label: d.label, base_url: d.base_url,
        efforts: d.efforts,
        keys,
        replace_keys: true,     // 界面上删掉的 key 要真的从配置里删掉
      }),
    })
    providers.value = r.providers || providers.value
    // **保存后不关窗、原地刷新列表**（2026-11-18 用户口径）：草稿从服务端回读，
    // 新 key 的自动编号（k2/k3…）与掩码立即出现在列表里，不用重开窗口。
    if (providerModal.value === d.id) {
      openProviderModal(d.id)
    }
    errMsg.value = `厂商「${d.label || d.id}」已保存（${keys.length} 把 key / ` +
      `${keys.reduce((n, k) => n + (k.models || []).length, 0)} 个模型），列表已刷新`
  } catch (e) {
    errMsg.value = '[FAIL] 保存厂商失败：' + (e && e.message ? e.message : e)
  }
}

async function useProvider(pid) {
  try {
    const r = await api('/v1/settings/remote/providers', {
      method: 'POST',
      body: JSON.stringify({ id: pid, current: true }),
    })
    providers.value = r.providers || providers.value
    errMsg.value = `已切换到「${(providerById(pid) || {}).label || pid}」`
  } catch (e) { errMsg.value = '[FAIL] 切换厂商失败：' + e.message }
}

async function deleteProviderModel(pid, modelId) {
  if (!window.confirm(`删除模型「${modelId}」？`)) return
  try {
    const r = await api(`/v1/settings/remote/providers/${pid}/models/${encodeURIComponent(modelId)}`,
      { method: 'DELETE' })
    providers.value = r.providers || providers.value
    const p = providerById(pid)
    if (p && providerDraft.value && providerDraft.value.id === pid) {
      providerDraft.value.models = (p.models || []).map((m) => ({ ...m }))
    }
  } catch (e) { errMsg.value = '[FAIL] 删除模型失败：' + e.message }
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

/* 闲时判定：高峰时段之外都算闲时（半价）。
   注意必须写 peak_window —— is_offpeak() 读的就是它；
   旧界面写 offpeak_window 是死字段，填了不生效。 */
const windowForm = reactive({ range1: '09:00-12:00', range2: '14:00-18:00',
                              weekdays_only: true, tz: 'Asia/Shanghai' })

function loadPricing(d) {
  const w = (d && d.remote && d.remote.peak_window) || {}
  const rs = w.ranges || ['09:00-12:00', '14:00-18:00']
  windowForm.range1 = rs[0] || ''
  windowForm.range2 = rs[1] || ''
  windowForm.weekdays_only = w.weekdays_only !== false
  windowForm.tz = w.tz || 'Asia/Shanghai'
}

/* 切换当前模型（单模型：切换 = 整条阶梯换成它） */
async function useProfile(p) {
  if (p._draft) { errMsg.value = '请先保存这张新档案，再设为当前模型'; return }
  try {
    const d = await api('/v1/settings/remote', { method: 'POST',
      body: JSON.stringify({ active_model: p.id }) })
    remote.value = d
    await loadRemote()
    errMsg.value = `当前模型已切换为 ${p.label || p.id}（思考强度仍由系统自动选）`
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

async function savePricingWindow() {
  const ranges = [windowForm.range1.trim(), windowForm.range2.trim()].filter(Boolean)
  try {
    const d = await api('/v1/settings/remote', { method: 'POST',
      body: JSON.stringify({ peak_window: {
        ranges, weekdays_only: !!windowForm.weekdays_only, tz: windowForm.tz,
      } }) })
    remote.value = d
    loadPricing(d)
    errMsg.value = '闲时时段已保存（高峰之外均为半价）'
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
const showRemotePanel = ref(false)   // 顶栏「远端」按钮弹出的**下拉面板**

// ---------------------------------------------------------------- 弹层互斥（2026-11-18 用户口径）
// **任何时刻只允许一个弹层/弹窗打开**：开任何一个，先把其它已开的全部关掉。
// 各打开入口先调 closeOverlays(自己的名字)，自己的开关逻辑照旧。
function closeOverlays(except = '') {
  if (except !== 'settings') showSettings.value = false
  if (except !== 'provider') providerModal.value = null
  if (except !== 'remote') showRemotePanel.value = false
  if (except !== 'login') showLoginModal.value = false
  if (except !== 'git') showGitPanel.value = false
  if (except !== 'tc') showTcPanel.value = false
  if (except !== 'menu') openMenu.value = ''
}

function openSettingsAt(tab) {
  closeOverlays('settings')
  settingsTab.value = tab || 'help'
  showSettings.value = true
  if (settingsTab.value === 'remote') loadRemote()
}
const showLoginModal = ref(false)
const buildId = ref('')

function openRemote(ev) {
  closeOverlays('remote')
  showRemotePanel.value = !showRemotePanel.value   // 点一下开、再点一下关
  if (showRemotePanel.value) {
    loadRemote(); loadProviders()
    // 贴着「远端」按钮展开（和 Git / 工具链浮层一致）
    if (ev && ev.currentTarget) {
      const r = ev.currentTarget.getBoundingClientRect()
      dropAnchorLeft.value = Math.max(12, Math.min(r.left, window.innerWidth - 480))
    }
  }
}
function openLogin() { closeOverlays('login'); showLoginModal.value = true; loadLoginState() }
const openAtLogin = ref(false)
const loginSupported = ref(false)   // 只有桌面外壳且主进程支持时才可切换

/* ---------------- 仓库行左侧的两个下拉浮层：Git 客户端 / 工具链 ----------------
   用户口径（2026-11-09）：这两个都放在「仓库」那一行、靠左排列，
   点击像顶栏「远端」一样弹出下拉浮层，不再各占左栏一整块面板。 */
const showGitPanel = ref(false)      // Git 客户端浮层
const showTcPanel = ref(false)       // 工具链浮层
const dropAnchorLeft = ref(18)       // 浮层左边缘：贴着触发它的那个按钮

/* ---------------- 左右栏折叠（VSCode 逻辑：点侧边栏图标收/展） ---------------- */
const leftCollapsed = ref(false)
const rightCollapsed = ref(false)
function toggleLeft() { leftCollapsed.value = !leftCollapsed.value }
function toggleRight() { rightCollapsed.value = !rightCollapsed.value }

function toggleRepoDrop(name, ev) {
  // 两个浮层互斥：只开一个（同时关掉其它所有弹层，2026-11-18 弹层互斥口径）
  closeOverlays(name === 'git' ? 'git' : 'tc')
  showGitPanel.value = name === 'git' ? !showGitPanel.value : false
  showTcPanel.value = name === 'tc' ? !showTcPanel.value : false
  if (showGitPanel.value) refreshGit()
  if (showTcPanel.value) loadToolchains()
  // 贴着按钮展开（像浏览器菜单那样对齐按钮左边缘）
  if (ev && ev.currentTarget) {
    const r = ev.currentTarget.getBoundingClientRect()
    dropAnchorLeft.value = Math.max(12, Math.min(r.left, window.innerWidth - 540))
  }
}


/* ---------------- VSCode 式文件树（浏览所选仓库目录，左栏常驻面板） ---------------- */
const treeRoot = ref('')            // 当前树对应的仓库绝对路径
const treeNodes = ref({})           // { 相对目录路径: [{name,path,is_dir,size}] }
const treeExpanded = ref({})        // { 相对目录路径: true }
const treeLoading = ref(false)
const treeError = ref('')

// 换了仓库就清掉旧的树缓存并重新加载（避免显示上一个仓库的文件）
watch(() => form.repo, () => {
  treeNodes.value = {}
  treeExpanded.value = {}
  treeRoot.value = ''
  treeError.value = ''
  if (form.repo) loadTree('')
})

async function loadTree(rel) {
  const repo = form.repo
  if (!repo) { treeError.value = '先选一个仓库目录'; return }
  treeError.value = ''
  treeLoading.value = true
  try {
    const q = new URLSearchParams({ repo, path: rel || '' })
    const d = await api('/v1/repo/tree?' + q.toString())
    treeRoot.value = d.repo || repo
    treeNodes.value = { ...treeNodes.value, [rel || '']: d.items || [] }
  } catch (e) {
    treeError.value = e.message
  } finally { treeLoading.value = false }
}

// 树按「相对路径的父目录」分组渲染，展开的目录渲染子项
function treeChildrenOf(parentPath) { return treeNodes.value[parentPath || ''] || [] }
function treeHasChildren(parentPath) { return treeChildrenOf(parentPath).length > 0 }

async function toggleDir(node) {
  const key = node.path
  if (treeExpanded.value[key]) { treeExpanded.value = { ...treeExpanded.value, [key]: false }; return }
  treeExpanded.value = { ...treeExpanded.value, [key]: true }
  if (!treeNodes.value[key]) await loadTree(key)
}

async function openInEditor(node) {
  if (!node || node.is_dir) return
  const abs = treeRoot.value ? treeRoot.value + '/' + node.path : node.path
  if (shellApi?.openPath) { shellApi.openPath(abs); return }
  errMsg.value = '打开系统编辑器需要桌面版外壳（网页版请直接用仓库路径）'
}

function toggleMenu(name) {
  closeOverlays('menu')
  openMenu.value = openMenu.value === name ? '' : name
}

function openHelp(section) {
  closeOverlays('settings')
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
const night = reactive({ plan: '', taskId: '', busy: false, info: null,
                         // 全自主模式：只给一句话目标，系统自己规划切片/验收对象/预算分配
                         // （对应 core/auto.py：蓝图 → 切片 → 逐片生成 → 覆盖观测 → 重规划）
                         goalMode: false, goal: '',
                         // 右上角选定的远端模型档案（flash / pro）；空 = 整条八档阶梯
                         remoteProfile: '',
                         // 两步法：先「估算花费」构建蓝图 + 估算，再开工；无用户上限输入框
                         estimateText: '', estimateBusy: false,
                         // 唯一停机线（系统预算 ×1.05），从 .sixcoder/budget.json 读，用户不可改
                         sysBudget: 0, stopThreshold: 0, spent: null,
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

/* ---------------- 远端模型：按协议分两类，列表 → 点开详情 ----------------
   DeepSeek 类：四档思考强度（off / low / high / max），走思考强度切换机制。
   OpenAI 兼容类：只提供通用框架，固定单档（不切思考强度）。
--------------------------------------------------------------------------- */
const remoteModels = ref([])
const remoteLadder = ref([])
const escalateAfter = ref(2)
const openProfileId = ref('')            // 一次只展开一个，避免长列表刷屏

const EFFORT_DEFS = [
  { v: 'none', label: 'off', hint: '不思考（简单样板 / 校验 / 日志）' },
  { v: 'low', label: 'low', hint: '轻思考' },
  { v: 'high', label: 'high', hint: '深思考（写代码常规档）' },
  { v: 'max', label: 'max', hint: '最深（规格 / 方案 / 卡住时升档）' },
]

/* 后端已按模型名定族：deepseek-* 档案的 compat 会被规范成 deepseek；
   显式 openai（或智谱/混元等）才是 OpenAI 兼容类。空值按 DeepSeek 处理。 */
function isOpenAI(p) { return String(p?.compat || '').toLowerCase() === 'openai' }
function kindLabel(p) { return isOpenAI(p) ? 'OpenAI 兼容' : 'DeepSeek' }
function effortSummary(p) {
  if (isOpenAI(p)) return '单档（通用框架）'
  const n = (p.efforts || []).length
  return n ? `${n} 档：${(p.efforts || []).map(e => e === 'none' ? 'off' : e).join(' / ')}` : '未设档位'
}

function blankPricing() {
  return { offpeak: { in_miss: 0, in_hit: 0, out: 0 },
           peak: { in_miss: 0, in_hit: 0, out: 0 } }
}

function normalizeProfile(p) {
  const pr = p.pricing || {}
  const block = (b) => ({ in_miss: Number(b?.in_miss) || 0,
                          in_hit: Number(b?.in_hit) || 0,
                          out: Number(b?.out) || 0 })
  // 后端已脱敏：只有 key_configured / key_masked，没有明文 api_key
  return {
    ...p,
    _draft: false,
    keyInput: '',                        // 新输入的 key；留空 = 不修改已有 key
    efforts: isOpenAI(p) ? ['none'] : (p.efforts || []),
    temperature: Number(p.temperature ?? 0.2),
    pricing: { offpeak: block(pr.offpeak), peak: block(pr.peak || pr.offpeak) },
  }
}

async function loadProfiles() {
  try {
    const d = await api('/v1/settings/remote/models')
    remoteModels.value = (d.models || []).map(normalizeProfile)
    remoteLadder.value = d.ladder || []
    escalateAfter.value = d.escalate_after ?? 2
  } catch (e) { /* 设置没打开时不必打扰用户 */ }
}

const dsModels = computed(() => remoteModels.value.filter((p) => !isOpenAI(p)))
const oaModels = computed(() => remoteModels.value.filter((p) => isOpenAI(p)))
const modelGroups = computed(() => [
  { key: 'deepseek', title: 'DeepSeek', tag: '四档思考强度',
    desc: 'off / low / high / max，系统按难度自动切、失败时逐档升。',
    items: dsModels.value },
  { key: 'openai', title: 'OpenAI 兼容', tag: '单档通用框架',
    desc: '只提供基础请求能力，固定一个档位，不做思考强度切换。',
    items: oaModels.value },
])

function isCurrent(p) { return (remote.value?.remote?.active_model || '') === p.id }
function toggleProfile(p) { openProfileId.value = openProfileId.value === p.id ? '' : p.id }
function priceLine(p) {
  const o = p.pricing?.offpeak || {}
  return `¥${o.in_miss ?? 0} / ¥${o.in_hit ?? 0} / ¥${o.out ?? 0}`
}

function toggleEffort(p, v) {
  if (isOpenAI(p)) return                 // OpenAI 兼容类固定单档，不允许切
  const cur = new Set(p.efforts || [])
  if (cur.has(v)) {
    if (cur.size <= 1) { errMsg.value = '至少要保留一个档位'; return }
    cur.delete(v)
  } else cur.add(v)
  p.efforts = EFFORT_DEFS.filter((d) => cur.has(d.v)).map((d) => d.v)
}

function setKind(p, openai) {
  p.compat = openai ? 'openai' : 'deepseek'
  p.efforts = openai ? ['none'] : ['none', 'low', 'high', 'max']
}

async function saveProfile(p) {
  if (!String(p.model || '').trim()) { errMsg.value = '模型名不能为空'; return }
  const body = {
    id: p.id, model: p.model, label: p.label || p.id,
    base_url: p.base_url || '', compat: p.compat || '',
    priority: Number(p.priority), enabled: !!p.enabled,
    // OpenAI 兼容端点不认 reasoning_effort，强制单档
    efforts: isOpenAI(p) ? ['none'] : (p.efforts || []),
    supports_tools: p.supports_tools === true || p.supports_tools === 'true' ? true
      : (p.supports_tools === false || p.supports_tools === 'false' ? false : ''),
    max_output_tokens: Number(p.max_output_tokens),
    temperature: Number(p.temperature),
    pricing: p.pricing,
  }
  // 留空不传：后端 upsert 收到空 api_key 会把已存的 key 清掉
  if (p.keyInput && p.keyInput.trim()) body.api_key = p.keyInput.trim()
  try {
    const d = await api('/v1/settings/remote/models', { method: 'POST', body: JSON.stringify(body) })
    remoteLadder.value = d.ladder || []
    p.keyInput = ''
    p._draft = false
    errMsg.value = `模型「${p.label || p.id}」已保存，下一次请求立即生效`
    await loadProfiles()
    await loadRemote()
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

function addProfile(openai) {
  const id = `${openai ? 'oai' : 'ds'}-${Date.now().toString(36).slice(-5)}`
  const p = normalizeProfile({
    id, label: '', model: '', compat: openai ? 'openai' : 'deepseek',
    base_url: openai ? '' : 'https://api.deepseek.com', enabled: true,
    priority: 90, efforts: openai ? ['none'] : ['none', 'low', 'high', 'max'],
    max_output_tokens: 8000, temperature: 0.2, pricing: blankPricing(),
    key_configured: false, key_masked: '',
  })
  p._draft = true
  remoteModels.value = [...remoteModels.value, p]
  openProfileId.value = id
  errMsg.value = `已加一张 ${kindLabel(p)} 空档案：填好模型名后点「保存」才会写入设置`
}

async function deleteProfile(p) {
  if (p._draft) {
    remoteModels.value = remoteModels.value.filter((x) => x !== p)
    openProfileId.value = ''
    return
  }
  if (!window.confirm(`删除模型「${p.label || p.id}」？删完阶梯里就没有它了。`)) return
  try {
    const d = await api(`/v1/settings/remote/models/${encodeURIComponent(p.id)}`,
                        { method: 'DELETE' })
    remoteModels.value = d.models || []
    remoteLadder.value = d.ladder || []
    if (openProfileId.value === p.id) openProfileId.value = ''
    errMsg.value = `已删除「${p.id}」`
    await loadRemote()
  } catch (e) { errMsg.value = '[FAIL] ' + e.message }
}

function nightBrief(p) {
  const o = p || {}
  const keys = ['round', 'strategy', 'verified', 'total', 'symbol', 'path', 'order',
                'why', 'reason', 'ok', 'used', 'message', 'text', 'question',
                // 全自主模式的进度：蓝图 / 切片 / 覆盖 / 花费
                'goal', 'caps', 'version', 'slice', 'gained', 'left', 'cap',
                'cost_cny', 'spent_cny', 'pct', 'replans', 'coverage', 'descoped']
  const out = keys.filter((k) => o[k] !== undefined && o[k] !== null && o[k] !== '')
    .map((k) => `${k}=${String(o[k]).replace(/\s+/g, ' ').slice(0, 90)}`)
  return out.join(' ').slice(0, 260)
}

async function startNight() {
  const auto = !!night.goalMode
  if (!form.repo) { errMsg.value = '夜班需要先选仓库'; return }
  if (auto ? !night.goal.trim() : !form.requirement.trim()) {
    errMsg.value = auto ? '全自主模式需要一句话目标' : '夜班需要仓库和需求'; return
  }
  if (!auto && !night.scale) {
    errMsg.value = '请选择目标代码规模（S/M/L/XL）——约束按规模分级，选错等于没约束'; return
  }
  night.busy = true; errMsg.value = ''; night.lines = []; night.delivery = ''
  night.info = null; night.estimateText = ''; night.sysBudget = 0
  night.stopThreshold = 0; night.spent = null
  try {
    const d = await api('/v1/night', { method: 'POST', body: JSON.stringify({
      repo: form.repo,
      requirement: auto ? '' : form.requirement,
      goal: auto ? night.goal : '',
      model: form.model,
      plan: night.plan, scale: night.scale,
      remote_profile: night.remoteProfile }) })
    night.taskId = d.task_id
    night.info = { state: 'running', mode: d.mode, policy: d.policy, verified: 0, total: 0 }
    connectNight(d.task_id)
  } catch (e) { night.busy = false; errMsg.value = e.message }
}

/* 两步法第一步：只构建蓝图 + 估算花费，不跑夜班；单价与系统预算都在 .sixcoder/budget.json */
async function estimateNight() {
  const text = night.goalMode ? night.goal : form.requirement
  if (!form.repo || !text.trim()) { errMsg.value = '请先选择仓库并填写目标/需求'; return }
  night.estimateBusy = true; night.estimateText = ''
  try {
    const d = await api('/v1/night/estimate', { method: 'POST', body: JSON.stringify({
      repo: form.repo,
      requirement: night.goalMode ? '' : form.requirement,
      goal: night.goalMode ? night.goal : '',
      model: form.model }) })
    night.estimateText = d.report || (d.ok ? '估算完成' : '估算不可用')
  } catch (e) { night.estimateText = '估算失败：' + e.message }
  finally { night.estimateBusy = false }
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
    // 唯一停机线与实际花费：从事件里抓出来常驻展示（系统预算用户不可改）
    if (ev.kind === 'night_start') {
      if (ev.payload && ev.payload.system_budget_cny) night.sysBudget = ev.payload.system_budget_cny
      if (ev.payload && ev.payload.stop_threshold_cny) night.stopThreshold = ev.payload.stop_threshold_cny
    }
    if (ev.kind === 'night_done' && ev.payload && ev.payload.spent_cny != null) {
      night.spent = ev.payload.spent_cny
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
    <!-- 顶栏：所有按钮从左往右排列 -->
    <div class="topbar">
      <!-- 左端：左栏开关（VSCode：侧栏收起后从屏幕左边滑出把手条） -->
      <button class="ghost menu-btn rail-toggle" :class="{ active: !leftCollapsed }"
              @click="toggleLeft"
              :title="leftCollapsed ? '展开左栏' : '收起左栏'">☰</button>
      <div class="menu-wrap">
        <button class="ghost menu-btn" :class="{ active: showGitPanel }"
                @click="toggleRepoDrop('git', $event)" title="Git 客户端：状态 / 提交 / 分支 / 远端同步">⑂ Git 客户端</button>
        <button class="ghost menu-btn" :class="{ active: showTcPanel }"
                @click="toggleRepoDrop('tc', $event)" title="工具链配置：JDK / Python / Node 等可用性">🔧 工具链</button>
        <button class="ghost menu-btn" @click="openRemote($event)"
                :title="remote.available ? '远端厂商已配置（点此切换）' : '配置远端厂商'">
          远端 <span class="dot" :class="remote.available ? 'on' : 'off'"></span>
        </button>
        <button class="ghost menu-btn" @click="openHelp('help')">帮助</button>
        <button class="ghost menu-btn" @click="openLogin()"
                :title="openAtLogin ? '已开启开机自启' : '开机自启未开启'">
          登录选项 <span class="dot" :class="openAtLogin ? 'on' : 'off'"></span>
        </button>
      </div>
      <span class="spacer"></span>
      <select v-if="modes.length" class="mode-select" v-model="currentMode"
              @change="setMode(currentMode.id)"
              :title="currentMode ? currentMode.desc + '\n边界：' + currentMode.boundary : ''">
        <option v-for="m in modes" :key="m.id" :value="m">{{ m.name }}</option>
      </select>
      <span v-if="questions.length" class="pill b-warn"
            style="cursor:pointer" @click="openSettingsAt('help')"
            :title="'有 ' + questions.length + ' 个问题在等你回答'">
        待答 {{ questions.length }}
      </span>
      <span v-if="pendingApprovals.length" class="pill b-warn"
            style="cursor:pointer" @click="openSettingsAt('help')"
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
      <!-- 右端：右栏开关 -->
      <button class="ghost menu-btn rail-toggle" :class="{ active: !rightCollapsed }"
              @click="toggleRight"
              :title="rightCollapsed ? '展开右栏' : '收起右栏'">☰</button>
      <div v-if="openMenu" class="menu-mask" @click="openMenu = ''"></div>
    </div>

    <!-- ============ 远端厂商（顶栏「远端」按钮 → 下拉面板） ============ -->
    <div v-if="showRemotePanel" class="remote-panel" :style="{ left: dropAnchorLeft + 'px', right: 'auto' }" @click.self="showRemotePanel = false">
      <div class="remote-panel-head">
        <strong>远端厂商</strong>
        <span class="badge" :class="remote.available ? 'b-ok' : 'b-dim'">
          {{ remote.available ? '可用' : '未配置' }}
        </span>
        <span class="spacer"></span>
        <span class="dim" style="font-size:11px">
          正在使用：{{ (currentProvider() || {}).label || '（未配置）' }}
        </span>
        <button class="ghost tiny" @click="showRemotePanel = false">关闭 ✕</button>
      </div>

      <div class="pv-cards">
        <!-- **整张卡片可点**（2026-11-18 用户口径：不要只点小按钮）：
             点卡片 = 关面板 + 打开该厂商的 key 列表；只有右侧「正在使用」是独立动作。 -->
        <div v-for="p in providers" :key="p.id"
             class="pv-card"
             :class="{ 'is-current': p.current }"
             style="cursor:pointer"
             :title="'点开配置「' + (p.label || p.id) + '」的所有 key 与模型'"
             @click="showRemotePanel = false; openProviderModal(p.id)">
          <div class="pv-name">{{ p.label || p.id }}</div>
          <!-- 右侧：切换「正在使用」（单独点击，不连带打开配置） -->
          <div class="pv-current" @click.stop="useProvider(p.id)">
            <span class="pv-radio" :class="{ 'on': p.current }"></span>
            <span class="pv-current-text">{{ p.current ? '正在使用' : '设为使用' }}</span>
          </div>
        </div>
      </div>

      <div class="dim" style="font-size:11px;line-height:1.7;margin-top:9px;padding:0 2px">
        点厂商卡片进 <b>key 列表</b>（每把 key 起名字/选用途/填 <b>API key</b>）、
        管理该厂商的<b>模型</b>（可添加/删除）。
        DeepSeek 与 GLM 已预置默认模型，只需填 key；OpenAI 没有默认模型，需要自己添加。
      </div>
    </div>


    <!-- ============ Git 客户端浮层 ============ -->
    <div v-if="showGitPanel" class="repo-pop repo-pop-git" :style="{ left: dropAnchorLeft + 'px', right: 'auto' }" @click.self="showGitPanel = false">
      <div class="repo-pop-head">
        <strong>Git 客户端</strong>
        <span class="spacer"></span>
        <span class="badge" :class="gitReady ? 'b-acc' : 'b-warn'">{{ gitReady ? (taskInfo?.id ? '目标：任务工作区' : '目标：所选仓库') : '未选目标' }}</span>
        <button class="ghost tiny" @click="showGitPanel = false">关闭 ✕</button>
      </div>
      <div v-if="!gitReady" class="alert-inline">{{ gitNoTargetMsg }}</div>
      <div class="git-status" v-if="gitStatus">
        <span class="badge b-acc">{{ gitStatus.branch }}</span>
        <span class="mono dim">@{{ gitStatus.head }}</span>
        <span class="badge" :class="gitStatus.clean ? 'b-ok' : 'b-warn'">{{ gitStatus.clean ? '干净' : gitStatus.changes.length + ' 处改动' }}</span>
        <span class="badge b-dim">↑{{ gitStatus.ahead }} ↓{{ gitStatus.behind }}</span>
      </div>
      <div v-if="gitStatus && gitStatus.changes.length" class="table-wrap" style="max-height:120px;margin:6px 0">
        <table>
          <thead><tr><th style="width:36px">状态</th><th>文件</th><th style="width:110px">撤回</th></tr></thead>
          <tbody>
            <tr v-for="c in gitStatus.changes" :key="c.path">
              <td><span class="badge" :class="c.code === '??' ? 'b-acc' : 'b-warn'">{{ c.code }}</span></td>
              <td class="mono wrap" style="font-size:10.5px;cursor:pointer" @click="gitRead('diff', { path: c.path })">{{ c.path }}</td>
              <td>
                <button class="ghost tiny" :disabled="gitBusy || !can('git.commit')" @click="gitDo('undo', { path: c.path })">撤回</button>
                <button class="ghost tiny" :disabled="gitBusy || !can('git.commit')" @click="gitDo('undo_sandbox_file', { path: c.path })">沙箱</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="row" style="flex-wrap:wrap;gap:6px">
        <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('init', { branch: 'main' })">初始化仓库</button>
        <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="refreshGit(); gitRead('status')">状态</button>
        <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('log', { n: 20 })">历史</button>
        <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('diff')">差异</button>
        <button class="ghost" :disabled="gitBusy || !can('git.read')" @click="gitRead('branches')">分支列表</button>
      </div>
      <div class="row" style="flex-wrap:wrap;gap:6px;margin-top:6px">
        <select v-model="commitType" class="commit-type" :title="COMMIT_TYPES.find(t => t.id === commitType)?.desc">
          <option v-for="t in COMMIT_TYPES" :key="t.id" :value="t.id">{{ t.label }}</option>
        </select>
        <input v-model="commitMsg" placeholder="提交描述" style="flex:1;min-width:140px" />
        <button class="ghost" :disabled="gitBusy || !can('git.commit')" @click="gitDo('stage')">暂存全部</button>
        <button :disabled="gitBusy || !can('git.commit')" @click="gitDo('commit', { message: commitFull })">提交</button>
      </div>
      <div class="dim mono" style="font-size:10px;margin:2px 0 6px;word-break:break-all">将提交：{{ commitFull }}</div>
      <div class="row" style="flex-wrap:wrap;gap:6px">
        <input v-model="remoteUrl" placeholder="远端仓库地址" style="flex:1;min-width:140px" />
        <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('set_remote', { name: 'origin', url: remoteUrl })">设置远端</button>
        <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('fetch')">获取</button>
        <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('pull')">拉取</button>
        <button class="ghost" :disabled="gitBusy || !can('git.remote')" @click="gitDo('push')">推送</button>
        <button :disabled="gitBusy || !can('git.remote')" @click="gitDo('sync')">一键同步</button>
      </div>
      <div class="row" style="flex-wrap:wrap;gap:6px;margin-top:6px">
        <input v-model="branchInput" placeholder="新分支名" style="max-width:120px" />
        <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('new_branch', { name: branchInput })">新建并切换</button>
        <input v-model="switchTarget" placeholder="切换到" style="max-width:110px" />
        <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('switch', { name: switchTarget })">切换</button>
        <input v-model="mergeSource" placeholder="合并源" style="max-width:110px" />
        <button class="ghost" :disabled="gitBusy || !can('git.branch')" @click="gitDo('merge', { source: mergeSource })">合并</button>
      </div>
      <pre v-if="gitOut" style="max-height:120px">{{ gitOut }}</pre>
    </div>

    <!-- ============ 工具链浮层 ============ -->
    <div v-if="showTcPanel" class="repo-pop repo-pop-tc" :style="{ left: dropAnchorLeft + 'px', right: 'auto' }" @click.self="showTcPanel = false">
      <div class="repo-pop-head">
        <strong>工具链</strong>
        <span class="badge" :class="toolchains.every(t => t.ok) ? 'b-ok' : 'b-warn'" style="margin-left:6px">
          {{ toolchains.filter(t => t.ok).length }}/{{ toolchains.length }} 可用
        </span>
        <span class="spacer"></span>
        <button class="ghost tiny" @click="loadToolchains()">刷新</button>
        <button class="ghost tiny" @click="showTcPanel = false">关闭 ✕</button>
      </div>
      <table>
        <tr v-for="t in toolchains" :key="t.id">
          <td class="mono" style="width:52px">{{ t.id }}</td>
                <td style="width:20px">
                  <span v-if="t.ok" class="badge b-ok">✓</span>
                  <span v-else-if="t.version" class="badge b-warn">!</span>
                  <span v-else class="badge b-bad">✗</span>
                </td>
                <td class="mono dim" style="width:60px">{{ t.version || '—' }}</td>
                <td class="mono dim wrap" style="font-size:9.5px">{{ t.path || '未找到' }}</td>
                <td style="width:34px">
                  <button v-if="isShell && can('toolchain.config')" class="ghost tiny"
                          :title="'指定 ' + t.id + ' 目录'" @click="pickToolchainDir(t)">…</button>
                </td>
              </tr>
            </table>
            <div v-if="!isShell" class="dim" style="font-size:10px;margin-top:4px">
              「指定目录」需桌面版外壳（Electron）；网页版可编辑 <code>data/toolchains.json</code>
            </div>
    </div>

    <div v-if="errMsg" class="alert-bar" @click="errMsg = ''" title="点击关闭">
      <span class="wrap">{{ errMsg }}</span>
      <span class="spacer"></span><span class="dim" style="font-size:10px">点击关闭 ✕</span>
    </div>

    <!-- VSCode 式：侧栏收起后，从屏幕左右边缘各滑出一条窄把手条，点它侧栏再滑出 -->
    <button v-if="leftCollapsed" class="edge-rail edge-left" @click="toggleLeft" title="展开左栏">›</button>
    <button v-if="rightCollapsed" class="edge-rail edge-right" @click="toggleRight" title="展开右栏">‹</button>

    <div class="cols" :class="{ 'left-collapsed': leftCollapsed, 'right-collapsed': rightCollapsed }">

      <!-- ============ 左栏 ============ -->
      <div class="col col-left" v-show="!leftCollapsed">
        <div class="panel repo-panel">
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
            文件
            <span class="spacer"></span>
            <span class="dim mono" style="font-size:10px">{{ treeRoot || '未选仓库' }}</span>
            <button class="ghost tiny" @click="loadTree('')">刷新</button>
          </div>
          <div v-if="treeError" class="alert-inline">{{ treeError }}</div>
          <div class="tree-root">
            <div v-if="!form.repo" class="empty">先在上方填仓库路径或点「浏览…」</div>
            <div v-else-if="treeLoading && !treeNodes['']" class="empty">正在读取…</div>
            <div v-else-if="!treeChildrenOf('').length" class="empty">（空目录）</div>
            <template v-else>
              <div v-for="n in treeChildrenOf('')" :key="n.path" class="tree-node" :style="{ paddingLeft: '2px' }">
                <div class="tree-row" :class="{ dir: n.is_dir }" @click="n.is_dir ? toggleDir(n) : openInEditor(n)">
                  <span class="tree-caret">{{ n.is_dir ? (treeExpanded[n.path] ? '▾' : '▸') : '' }}</span>
                  <span class="tree-icon">{{ n.is_dir ? '📁' : '📄' }}</span>
                  <span class="tree-name">{{ n.name }}</span>
                  <span v-if="!n.is_dir" class="dim" style="font-size:9.5px">{{ n.size }}</span>
                </div>
                <template v-if="n.is_dir && treeExpanded[n.path]">
                  <div v-for="c in treeChildrenOf(n.path)" :key="c.path" class="tree-node" :style="{ paddingLeft: '14px' }">
                    <div class="tree-row" :class="{ dir: c.is_dir }" @click="c.is_dir ? toggleDir(c) : openInEditor(c)">
                      <span class="tree-caret">{{ c.is_dir ? (treeExpanded[c.path] ? '▾' : '▸') : '' }}</span>
                      <span class="tree-icon">{{ c.is_dir ? '📁' : '📄' }}</span>
                      <span class="tree-name">{{ c.name }}</span>
                      <span v-if="!c.is_dir" class="dim" style="font-size:9.5px">{{ c.size }}</span>
                    </div>
                  </div>
                </template>
              </div>
            </template>
          </div>
          <div class="dim" style="font-size:10px;margin-top:5px">
            点目录展开，点文件用系统默认编辑器打开。
          </div>
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
          <!-- 甲方模式多一个开关：需求模式（我指定范围）/ 目标模式（系统自己规划切片） -->
          <div v-if="isClientMode" class="row" style="gap:8px;align-items:center;margin-bottom:6px">
            <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:4px"
                   title="需求模式：你指定范围与文件，系统只做给定契约｜目标模式：只给一句话，切片、验收对象、预算分配全部由系统自己规划（已通过的能力不会重做）">
              模式
              <select v-model="night.goalMode" style="width:168px">
                <option :value="false">需求（我指定范围）</option>
                <option :value="true">目标（全自主规划）</option>
              </select>
            </label>
            <span v-if="night.goalMode" class="dim" style="font-size:10px">
              全自主：自己规划能力表与切片，每轮跑完重新观测整个项目；缺口和降级会写进报告
            </span>
          </div>
          <textarea v-if="!(isClientMode && night.goalMode)" v-model="form.requirement" style="min-height:120px"
                    :placeholder="isClientMode
                      ? '写清要什么、写进哪个文件（如 clean.py）、行为与异常怎么处理｜点「开始夜班」'
                      : '写清改哪个文件、要什么行为、异常怎么处理｜Ctrl+Enter 发送'"
                    @keydown.ctrl.enter.prevent="onSendShortcut"></textarea>
          <textarea v-else v-model="night.goal" style="min-height:90px"
                    placeholder="一句话目标，例如「做一个 CSV 记账工具：能导入、按月汇总、导出报表」｜点「全自主开始」"></textarea>

          <!-- 甲方模式：只有「开始夜班」这一个动作（内部自己拍板、自己写） -->
          <div v-if="isClientMode" class="row" style="margin-top:8px;gap:10px;flex-wrap:wrap;align-items:flex-end">
            <button style="min-width:120px" :disabled="night.busy || !can('agent.run')"
                    :title="night.goalMode
                      ? '给一句话目标 → 系统自己规划能力表与切片 → 逐片生成 → 每轮重新观测 → 报告'
                      : '提需求 → 系统反复体检/派工/开发/机械验收 → 交付报告'"
                    @click="startNight">{{ night.busy ? '夜班进行中…'
                      : (night.goalMode ? '全自主开始' : '开始夜班') }}</button>
            <!-- 两步法第一步：先估算花费（构建蓝图 + 估算金额），用户据此充值，再开工。
                 不再有"上限/预算"输入框：唯一停机线 = 系统总预算 ×1.05，写在 .sixcoder/budget.json -->
            <button class="ghost tiny" :disabled="night.busy || night.estimateBusy || !can('agent.run')
                    || !form.repo || !(night.goalMode ? night.goal.trim() : form.requirement.trim())"
                    :title="night.goalMode ? '用目标模式构建蓝图并估算需要多少钱（不跑夜班）'
                            : '构建蓝图并估算需要多少钱（不跑夜班）'"
                    @click="estimateNight">{{ night.estimateBusy ? '估算中…' : '① 估算花费' }}</button>
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
          <!-- 两步法第一步的结果：预计需要多少钱（token × 用户填的单价）+ 唯一停机线说明 -->
          <div v-if="isClientMode && night.estimateText"
               style="margin-top:6px;border:1px solid var(--line);border-radius:6px;padding:8px;white-space:pre-wrap;font-size:11px">{{ night.estimateText }}</div>

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
              <!-- 钱的三件事：系统预算（唯一停机线的基准）、停机阈值、实际花费 -->
              <span v-if="night.sysBudget" class="badge b-acc">系统预算 ¥{{ night.sysBudget.toFixed(2) }}</span>
              <span v-if="night.stopThreshold" class="badge b-warn">停机线 ¥{{ night.stopThreshold.toFixed(2) }}</span>
              <span v-if="night.spent != null" class="badge b-ok">已花 ¥{{ night.spent.toFixed(2) }}</span>
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
      <div class="col col-right" v-show="!rightCollapsed">
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

    <!-- ============ 厂商配置（二级弹窗：点厂商矩形弹出来） ============ -->
    <div v-if="providerModal && providerDraft" class="modal-mask"
         style="z-index:120" @click.self="closeProviderModal()">
      <div class="modal" style="max-width:720px">
        <div class="modal-head">
          <strong>{{ providerDraft.label || providerDraft.id }} 配置</strong>
          <span class="badge" :class="providerById(providerDraft.id)?.key_configured ? 'b-ok' : 'b-dim'">
            {{ providerById(providerDraft.id)?.key_configured ? 'key 已配置' : '未配置 key' }}
          </span>
          <span class="spacer"></span>
          <button class="ghost tiny" @click="closeProviderModal()">关闭 ✕</button>
        </div>
        <div class="modal-body">
          <!-- 返回上一级：回到远端厂商列表（2026-11-18 用户口径） -->
          <div class="row" style="gap:8px;align-items:center;margin-bottom:6px">
            <button class="ghost tiny" style="font-size:12px;padding:3px 10px"
                    @click="closeProviderModal(); showRemotePanel = true">← 返回厂商列表</button>
            <span class="dim" style="font-size:10.5px">当前：{{ providerDraft.label || providerDraft.id }}</span>
          </div>
          <!-- 连接 -->
          <div class="mp-sec">
            <div class="mp-sec-t">连接</div>
            <div class="mp-grid">
              <label><span>显示名</span><input v-model="providerDraft.label" placeholder="厂商显示名" /></label>
              <label><span>请求网址</span><input v-model="providerDraft.base_url" class="mono" placeholder="https://…" /></label>
            </div>
            <div class="dim" style="font-size:10.5px;margin-top:4px;line-height:1.6">
              每家厂商的 key 各自独立，不会跨厂商兜底（把 A 家的 key 发到 B 家端点必然 401）。
              key 只存本机设置文件，界面只显示掩码。key 在下面<b>每一把的卡片里</b>分别填。
            </div>
          </div>

          <!-- 思考强度等级 -->
          <div class="mp-sec">
            <div class="mp-sec-t">拥有的思考强度等级</div>
            <div class="row" style="gap:8px;flex-wrap:wrap">
              <button v-for="e in EFFORT_LEVELS" :key="e" class="ghost tiny"
                      :class="{ 'sel': providerDraft.efforts.includes(e) }"
                      @click="draftToggleEffort(e)">{{ e }}</button>
            </div>
            <div class="dim" style="font-size:10.5px;margin-top:4px;line-height:1.6">
              勾选这家厂商**实际支持**的档位。GLM 官方只有 low / high / max（没有 none，
              "不思考"要用 thinking.type=disabled，而 GLM-5.3 起已不支持禁用）；
              DeepSeek 是 none / low / high / max 四档。
            </div>
          </div>


          <!-- ===== key 卡片列表（第一层，和厂商卡片一样） ===== -->
          <div class="mp-sec" v-if="!keyViewKey">
            <div class="mp-sec-t">key（{{ (providerDraft.keys || []).length }} 把）
              <span class="dim" style="font-weight:400;font-size:10.5px">｜点卡片进去配这把 key 的模型</span>
              <span class="spacer"></span>
              <button class="ghost tiny" @click="draftAddKey()">＋ 新增 key</button>
            </div>
            <div v-if="!(providerDraft.keys || []).length"
                 class="dim" style="font-size:11px;padding:12px 2px;text-align:center">
              这家厂商还没有 key。
              <button class="primary tiny" style="margin-left:8px" @click="draftAddKey()">＋ 新增第一把 key</button>
            </div>
            <div v-else class="pv-cards" style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
              <div v-for="(k, ki) in providerDraft.keys" :key="ki" class="pv-card"
                   style="cursor:pointer;position:relative" @click="keyViewKey = k"
                   :title="'点进去配置「' + (k.label || roleName(k.role)) + '」的模型'">
                <button class="ghost tiny danger"
                        style="position:absolute;top:4px;right:4px"
                        title="删除这把 key（同步写入配置文件；编号会被下一把复用）"
                        @click.stop="draftRemoveKey(k)">删除</button>
                <div class="pv-name">{{ k.label || roleName(k.role) }}</div>
                <div class="pv-sub dim">
                  {{ roleName(k.role) }} ·
                  key {{ k.key_configured ? (k.key_masked || '已配置') : '未配置' }} ·
                  {{ (k.models || []).length }} 个模型
                </div>
              </div>
            </div>
            <div class="dim" style="font-size:10.5px;margin-top:8px;line-height:1.6">
              新增 key 只需要 <b>起名字 + 选用途 + 填 key</b>（保存按钮在 key 详情页里），
              模型自动按厂商默认配好，点进具体 key 再改它的模型。
              用途 = 角色：写作者写码，审核者 1/2 死局时协商（多数决）。删除立即写进配置文件。
            </div>
          </div>

          <!-- ===== 单个 key 详情（第二层：模型配置在这里） ===== -->
          <div class="mp-sec" v-else>
            <div class="mp-sec-t" style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <button class="ghost tiny" style="font-size:12px;padding:3px 10px"
                      @click="keyViewKey = null">← 返回 key 列表</button>
              <strong>{{ keyViewKey.label || roleName(keyViewKey.role) }}</strong>
              <span class="dim" style="font-weight:400;font-size:10.5px">｜{{ roleName(keyViewKey.role) }}</span>
              <span class="spacer" style="flex:1"></span>
              <button class="ghost tiny danger"
                      @click="draftRemoveKey(keyViewKey); keyViewKey = null"
                      title="删除后它的编号（k2/k3…）会被下一把新 key 自动复用">删除这把 key</button>
            </div>
            <div class="mp-grid">
              <label><span>用途（角色）</span>
                <select v-model="keyViewKey.role" @change="draftSetRole(keyViewKey, keyViewKey.role)">
                  <option value="writer">写作者 ★</option>
                  <option value="reviewer1">审核者 1</option>
                  <option value="reviewer2">审核者 2</option>
                  <option value="">备用（不参与）</option>
                </select>
              </label>
              <label><span>名字</span>
                <input v-model="keyViewKey.label" placeholder="如：主账号 / 审核小号" /></label>
              <label style="grid-column:1/-1"><span>API key</span>
                <input v-model="keyViewKey.api_key" type="password"
                       :placeholder="keyViewKey.key_configured ? ('已配置：' + (keyViewKey.key_masked || '') + '（留空 = 不修改）') : 'sk-…（这把 key）'" /></label>
            </div>
            <div v-if="keyViewKey._draft" class="dim"
                 style="font-size:10.5px;margin-top:8px;line-height:1.6">
              新 key 的模型保存时会**自动按厂商默认配好**（deepseek：flash + pro）。
              保存后回到列表点进来，就能增删改这把 key 自己的模型。
            </div>
            <template v-else>
              <div class="dim" style="font-size:10px;margin:10px 0 2px 2px">
                这把 key 用的模型（{{ (keyViewKey.models || []).length }} 个）
                <button class="ghost tiny" style="margin-left:8px" @click="draftAddModelTo(keyViewKey)">＋ 添加模型</button>
              </div>
              <div v-if="!(keyViewKey.models || []).length" class="dim" style="font-size:11px;padding:6px 2px">
                这把 key 还没配模型（审核者会借写作者的模型但用本 key 发请求）。
              </div>
              <div v-for="(m, mi) in (keyViewKey.models || [])" :key="mi" class="pv-model">
                <div class="row" style="gap:8px;align-items:center">
                  <label style="flex:1;min-width:120px"><span class="dim" style="font-size:10px">模型名</span>
                    <input v-model="m.model" class="mono" placeholder="deepseek-flash / glm-5.3 / gpt-4o" /></label>
                  <label style="flex:1;min-width:100px"><span class="dim" style="font-size:10px">ID</span>
                    <input v-model="m.id" class="mono" placeholder="flash" /></label>
                  <label style="flex:1;min-width:110px"><span class="dim" style="font-size:10px">显示名</span>
                    <input v-model="m.label" placeholder="给这个模型起个名字" /></label>
                  <button class="ghost tiny danger" @click="draftRemoveModelOf(keyViewKey, m)">删除</button>
                </div>
                <div class="row" style="gap:8px;align-items:center;margin-top:5px">
                  <label style="width:130px"><span class="dim" style="font-size:10px">最大输出 token</span>
                    <input type="number" min="0" v-model.number="m.max_output_tokens" /></label>
                  <label style="width:100px"><span class="dim" style="font-size:10px">温度</span>
                    <input type="number" min="0" max="2" step="0.1" v-model.number="m.temperature" /></label>
                  <label style="width:100px"><span class="dim" style="font-size:10px">优先级</span>
                    <input type="number" v-model.number="m.priority" /></label>
                  <label style="width:150px" title="这个模型支不支持原生 function calling。选「不支持」时引擎不再往请求里挂 tools，改走文本协议；自动判断 = 查本地能力表，踩过坑会自动记住">
                    <span class="dim" style="font-size:10px">原生工具调用</span>
                    <select v-model="m.supports_tools">
                      <option value="">自动判断</option>
                      <option value="true">支持</option>
                      <option value="false">不支持</option>
                    </select>
                  </label>
                  <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:4px">
                    <input type="checkbox" v-model="m.enabled" style="width:auto" /> 启用
                  </label>
                  <span class="dim mono" style="font-size:10px">思考 {{ (m.efforts || []).join('/') || '继承厂商' }}</span>
                </div>
              </div>
            </template>
            <div class="row" style="gap:8px;margin-top:10px">
              <span class="spacer"></span>
              <button class="primary tiny" @click="saveProviderDraft()">保存这个 key</button>
            </div>
            <div class="dim" style="font-size:10.5px;margin-top:4px;line-height:1.6">
              模型名留空的那条会被忽略；ID 必须填（留空时用模型名当 ID）。「最大输出 token」填 0 = 由引擎按任务规模算。
              key 输入框留空保存 = 不修改已有。
            </div>
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
            <div class="panel-title" style="margin-top:12px">全局连接参数（兜底 / 兼容旧配置）</div>
            <div class="dim" style="font-size:11px;margin-bottom:6px;line-height:1.6">
              这里是<b>全局</b>配置，只作为档案没填时的兜底。DeepSeek 档案永远走官方地址、不继承这里；
              OpenAI 兼容档案才继承。要给某个模型单独配网址 / key，请到顶栏「远端模型」里点开它。
            </div>
            <div class="row" style="gap:8px;align-items:center">
              <input v-model="remoteForm.model" placeholder="deepseek-flash" style="width:180px" />
              <input v-model="remoteForm.base_url" placeholder="https://api.deepseek.com" style="flex:1" />
              <select v-model="remoteForm.compat" style="width:130px">
                <option value="deepseek">DeepSeek</option>
                <option value="openai">OpenAI 兼容</option>
              </select>
              <button class="ghost tiny" @click="saveRemote({model: remoteForm.model, base_url: remoteForm.base_url, compat: remoteForm.compat})">保存</button>
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
            <tr><td class="dim">高峰（全价）</td>
                <td class="mono">{{ remote.usage?.peak?.calls ?? 0 }} 次 · ¥{{ (remote.usage?.peak?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">闲时（半价）</td>
                <td class="mono">{{ remote.usage?.offpeak?.calls ?? 0 }} 次 · ¥{{ (remote.usage?.offpeak?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">总花费</td><td class="mono">¥{{ (remote.usage?.cost_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">缓存省下</td><td class="mono">¥{{ (remote.usage?.cache_saved_cny ?? 0).toFixed(4) }}</td></tr>
            <tr><td class="dim">高峰时段</td>
                <td class="mono">{{ (remote.usage?.window?.ranges || []).join('、') || '09:00-12:00、14:00-18:00' }}
                  （北京时间工作日；其余半价）</td></tr>
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
