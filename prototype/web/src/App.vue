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

/* PATH 绑定：安装包下载的运行时默认不写 PATH，由用户自己点（初始一律"未绑定"）。
   「系统 PATH」= 用户自己原本就加过的，我们不动也不给解绑。 */
async function togglePathBind(tc) {
  try {
    // 解绑按钮只在 added=true 时出现；此时 bind=false。其余情况都是"绑定"。
    const doBind = !tc.added
    const d = await api('/v1/pathbind', {
      method: 'POST', body: JSON.stringify({ id: tc.id, bind: doBind })
    })
    await loadToolchains()
    errMsg.value = `${tc.label}：${d.detail || (doBind ? '已绑定' : '已解绑')}`
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
    // health 只做快速启动探针；模型列表另外异步补（Ollama 没开时 /v1/models 会慢）
    models.value = health.value.models || []
    modelPolicy.value = health.value.model_policy || null
    if (!form.model) form.model = health.value.default_model
    loadModels()
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
    if (d.policy) modelPolicy.value = d.policy
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
const remoteTest = reactive({ busy: false, ok: null, detail: '' })

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
const keyTest = reactive({ busy: false, ok: null, detail: '' })
watch(keyViewKey, () => { keyTest.busy = false; keyTest.ok = null; keyTest.detail = '' })

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

async function testProviderKey() {
  const d = providerDraft.value, k = keyViewKey.value
  if (!d || !k || keyTest.busy) return
  keyTest.busy = true; keyTest.ok = null; keyTest.detail = '正在连通测试…'
  try {
    const body = {
      provider: d.id, key_id: k.id || '',
      model: (k.models || [])[0]?.model || '',
      api_key: (k.api_key || '').trim(),   // 未保存的新 key 也能直接测
    }
    const r = await api('/v1/settings/remote/key-test', { method: 'POST', body: JSON.stringify(body) })
    keyTest.ok = !!r.ok
    keyTest.detail = r.detail || (r.ok ? '连通' : '调用失败')
    if (r.ok) await loadRemote()      // 测试通过后刷新状态，顶栏红点要变绿
  } catch (e) {
    keyTest.ok = false
    keyTest.detail = '测试失败：' + e.message
  } finally { keyTest.busy = false }
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
    // 保存后马上刷新远端可用状态（厂商 key 也算配置，顶栏红点变绿）
    await loadRemote()
    // **保存后不关窗、原地刷新列表**（2026-11-18 用户口径）：草稿从服务端回读，
    // 新 key 的自动编号（k2/k3…）与掩码立即出现在列表里，不用重开窗口。
    if (providerModal.value === d.id) {
      const keepKeyId = keyViewKey.value?.id
      openProviderModal(d.id)
      // 保存后留在当前 key 详情页：用户刚填完 key，下一步就是点「测试连接」，
      // 不该被踢回列表再点一遍（2026-11-21 用户反馈）。
      if (keepKeyId) {
        keyViewKey.value = (providerDraft.value?.keys || []).find((k) => k.id === keepKeyId) || null
      }
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

// 真打一次 1-token 请求验证 key 可用：填完/保存后立刻能看到"通/不通"，不用等任务跑起来。
async function testRemote() {
  if (remoteTest.busy) return
  remoteTest.busy = true; remoteTest.ok = null; remoteTest.detail = '正在连通测试…'
  try {
    const d = await api('/v1/settings/remote/test')
    remoteTest.ok = !!d.ok
    remoteTest.detail = d.detail || (d.ok ? '连通' : '调用失败')
  } catch (e) {
    remoteTest.ok = false
    remoteTest.detail = '测试失败：' + e.message
  } finally { remoteTest.busy = false }
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

/* ---------------- UI 审美样式库（类型枚举 + 用户自填配色） ----------------
   审美判据在人眼里：引擎不自检"好不好看"，只把有限类型列举给用户挑，
   颜色全部是用户自填插槽。选出来的约定注入生成链路，DOM/CSS 落地由浏览器探针验。 */
const uiStyles = ref([])          // 全部预设
const uiTypes = ref({})           // 类型清单 {page:[...],background:[...],...}
const uiSlots = ref([])           // 颜色插槽 [{key,label,hint}]
const uiSelected = ref('')        // 当前选中的预设 id
const uiColors = reactive({})     // 用户自填颜色 {key: value}
const uiLoaded = ref(false)

async function loadUIStyles() {
  if (uiLoaded.value) return
  try {
    const d = await api('/v1/uistyles')
    uiStyles.value = d.presets || []
    uiTypes.value = d.types || {}
    uiSlots.value = d.color_slots || []
    const sel = d.selected || {}
    uiSelected.value = sel.selected || ''
    for (const s of uiSlots.value) uiColors[s.key] = (sel.colors || {})[s.key] || ''
    uiLoaded.value = true
  } catch (e) { errMsg.value = '[FAIL] 读取样式库：' + e.message }
}

function pickUIStyle(id) {
  uiSelected.value = id
  // 切换预设时保留用户已填的颜色（颜色是用户的，不随预设变）
}

async function saveUIStyle() {
  if (!uiSelected.value) { errMsg.value = '请先选一个风格预设'; return }
  try {
    const d = await api('/v1/uistyles/select', {
      method: 'POST', body: JSON.stringify({ id: uiSelected.value, colors: { ...uiColors } }),
    })
    errMsg.value = '已保存风格「' + (uiStyles.value.find(p => p.id === uiSelected.value)?.label || uiSelected.value)
      + '」；生成时会按此约定落地，配色用你填的值。'
    return d
  } catch (e) { errMsg.value = '[FAIL] 保存风格：' + e.message }
}

// 闭环验收：把四块能力的验收路径收口成一张可跑单（审美仍标人在环）
const verifyBusy = ref(false)
const verifyResult = ref(null)
async function runVerify() {
  verifyBusy.value = true
  verifyResult.value = null
  try {
    verifyResult.value = await api('/v1/verify', {
      method: 'POST', body: JSON.stringify({ pid: uiSelected.value || '' }),
    })
  } catch (e) { errMsg.value = '[FAIL] 闭环验收：' + e.message }
  finally { verifyBusy.value = false }
}

/* ---------------- 受控部署通道（预留目标 + 一次性密码） ----------------
   密码只随执行请求体传一次，经后端 secret_fn 注入 ssh 子进程，不落盘/不回显。
   env:/file: 凭据的目标不需要输入密码；credential_kind='prompt' 才要。 */
const deployCfg = ref({ enabled: false, targets: [], capability: {} })
const deployForm = reactive({
  id: '', host: '', port: 22, user: '', auth: 'agent', key_ref: '',
  credential: '', allow_commands: '', workdir: '', note: '', enabled: true,
})
const deployExec = reactive({ target: '', command: '', reason: '', password: '', busy: false, result: null })
const deployLoaded = ref(false)

async function loadDeploy() {
  try {
    deployCfg.value = await api('/v1/deploy')
    deployLoaded.value = true
    if (!deployExec.target && deployCfg.value.targets?.length) deployExec.target = deployCfg.value.targets[0].id
  } catch (e) { errMsg.value = '[FAIL] 读部署配置：' + e.message }
}

function editDeployTarget(t) {
  Object.assign(deployForm, {
    id: t.id || '', host: t.host || '', port: t.port || 22, user: t.user || '',
    auth: t.auth || 'agent', key_ref: t.key_ref || '',
    credential: t.credential || '', // reveal=false 时为空；留空=不改动已有引用
    allow_commands: (t.allow_commands || []).join('\n'),
    workdir: t.workdir || '', note: t.note || '', enabled: t.enabled !== false,
  })
}

function resetDeployForm() {
  Object.assign(deployForm, {
    id: '', host: '', port: 22, user: '', auth: 'agent', key_ref: '',
    credential: '', allow_commands: '', workdir: '', note: '', enabled: true,
  })
}

async function saveDeployTargets() {
  if (!deployForm.id.trim() || !deployForm.host.trim() || !deployForm.user.trim()) {
    errMsg.value = '目标 id / host / user 必填'
    return
  }
  const t = {
    id: deployForm.id.trim(), host: deployForm.host.trim(), port: Number(deployForm.port) || 22,
    user: deployForm.user.trim(), auth: deployForm.auth, enabled: deployForm.enabled,
    allow_commands: String(deployForm.allow_commands || '').split('\n').map(s => s.trim()).filter(Boolean),
    workdir: deployForm.workdir.trim(), note: deployForm.note.trim(),
  }
  if (deployForm.auth === 'key_ref' && deployForm.key_ref.trim()) t.key_ref = deployForm.key_ref.trim()
  // credential 只在用户填了时带上；留空意味着保留已配置的引用（后端整表覆盖，所以要先并回旧值）
  const old = (deployCfg.value.targets || []).find(x => x.id === t.id)
  const cred = deployForm.credential.trim()
  t.credential = cred || (old?.credential || '')
  // 整表覆盖：先 reveal 读回所有目标的真实引用，替换/追加当前目标，避免清掉别人的引用。
  try {
    const full = await api('/v1/deploy?reveal=true')
    const rest = (full.targets || []).filter(x => x.id !== t.id).map(deployPayload)
    deployCfg.value = await api('/v1/deploy', {
      method: 'POST', body: JSON.stringify({ targets: [...rest, t], enabled: deployCfg.value.enabled }),
    })
    resetDeployForm()
    errMsg.value = '部署目标已保存（凭据引用不明文回显）'
    if (!deployExec.target) deployExec.target = t.id
  } catch (e) { errMsg.value = '[FAIL] 保存部署目标：' + e.message }
}

function deployPayload(x) {
  return {
    id: x.id, host: x.host, port: x.port, user: x.user, auth: x.auth, enabled: x.enabled,
    allow_commands: x.allow_commands || [], workdir: x.workdir || '', note: x.note || '',
    key_ref: x.key_ref || '', credential: x.credential || '',
  }
}

async function removeDeployTarget(id) {
  try {
    const full = await api('/v1/deploy?reveal=true')
    const targets = (full.targets || []).filter(x => x.id !== id).map(deployPayload)
    deployCfg.value = await api('/v1/deploy', {
      method: 'POST', body: JSON.stringify({ targets, enabled: deployCfg.value.enabled }),
    })
    if (deployExec.target === id) deployExec.target = ''
    errMsg.value = '已删除目标 ' + id
  } catch (e) { errMsg.value = '[FAIL] 删除目标：' + e.message }
}

async function toggleDeployEnabled() {
  try {
    const full = await api('/v1/deploy?reveal=true')
    const targets = (full.targets || []).map(deployPayload)
    deployCfg.value = await api('/v1/deploy', {
      method: 'POST', body: JSON.stringify({ targets, enabled: !deployCfg.value.enabled }),
    })
  } catch (e) { errMsg.value = '[FAIL] 切换部署通道：' + e.message }
}

function deployNeedsPassword() {
  const t = (deployCfg.value.targets || []).find(x => x.id === deployExec.target)
  return t?.auth === 'password' && t?.credential_kind === 'prompt'
}

async function runDeployExec() {
  if (!deployExec.target || !deployExec.command.trim()) {
    errMsg.value = '选目标并填命令'
    return
  }
  deployExec.busy = true
  deployExec.result = null
  const body = {
    target: deployExec.target, command: deployExec.command.trim(),
    reason: deployExec.reason.trim(),
  }
  if (deployNeedsPassword() && deployExec.password) body.password = deployExec.password
  try {
    deployExec.result = await api('/v1/deploy/exec', { method: 'POST', body: JSON.stringify(body) })
    deployExec.password = '' // 用完即清，不留在界面内存里
  } catch (e) {
    deployExec.result = { ok: false, why: e.message }
    deployExec.password = ''
  } finally { deployExec.busy = false }
}

/* ---------------- 半自动回填（外部真链路：人填一次 → 引擎代跑） ----------------
   值只进内存会话缓存，不回显、不落盘；服务重启后清空（boot_id 变了即需重填）。 */
const hoState = ref({ pending: [], capability: {} })
const hoForm = reactive({ namespace: 'flow', key: 'otp', value: '', ttl: 900, uses: 1 })
const hoBusy = ref(false)
const hoDemo = ref(null)

async function loadHumanOnce() {
  try { hoState.value = await api('/v1/human-once') }
  catch (e) { errMsg.value = '[FAIL] 读半自动状态：' + e.message }
}

async function putHumanOnce() {
  if (!hoForm.namespace.trim() || !hoForm.key.trim() || !hoForm.value.trim()) {
    errMsg.value = '命名空间 / 键 / 值 都要填'
    return
  }
  hoBusy.value = true
  try {
    await api('/v1/human-once/put', {
      method: 'POST',
      body: JSON.stringify({
        namespace: hoForm.namespace.trim(), key: hoForm.key.trim(),
        value: hoForm.value, ttl: Number(hoForm.ttl) || 900, uses: Number(hoForm.uses) || 1,
      }),
    })
    hoForm.value = ''            // 用完即清，不留在界面
    await loadHumanOnce()
    errMsg.value = '已存入会话缓存（值不回显、不落盘）'
  } catch (e) { errMsg.value = '[FAIL] 存入：' + e.message }
  finally { hoBusy.value = false }
}

async function clearHumanOnce(ns, key) {
  try {
    await api('/v1/human-once/clear?namespace=' + encodeURIComponent(ns) +
              '&key=' + encodeURIComponent(key), { method: 'POST' })
    await loadHumanOnce()
  } catch (e) { errMsg.value = '[FAIL] 清除：' + e.message }
}

async function runHumanOnceDemo() {
  hoBusy.value = true
  hoDemo.value = null
  const steps = [
    { kind: 'human', key: 'otp', hint: '短信验证码（示例）' },
    { kind: 'auto', action: '提交表单' },
  ]
  try {
    hoDemo.value = await api('/v1/human-once/run', {
      method: 'POST', body: JSON.stringify({ steps, namespace: 'demo' }),
    })
  } catch (e) { hoDemo.value = { ok: false, error: e.message } }
  finally { hoBusy.value = false }
}

function hoRestartHint() {
  const c = hoState.value.capability || {}
  if (!c.boot_id) return ''
  const at = c.booted_at ? new Date(c.booted_at * 1000).toLocaleTimeString() : ''
  return '本次进程 ' + String(c.boot_id).slice(0, 6) + (at ? '（启动于 ' + at + '）' : '') +
    '；服务重启后已填值会清空，需要重填一次——这是不落盘的安全取舍。'
}

/* ---------------- 多机编排（一次协商 + 一次人审覆盖整批主机） ---------------- */
const orchCfg = ref({ enabled: false, plans: [], capability: {} })
const orchForm = reactive({
  id: '', targets: [], stepsText: '', health_check: '', rollbackText: '',
  strategy: 'sequential', fail_fast: false, timeout: 120, note: '',
})
const orchExec = reactive({ plan: '', password: '', busy: false, result: null })

async function loadOrch() {
  try {
    orchCfg.value = await api('/v1/orchestration')
    if (!deployCfg.value.targets?.length) await loadDeploy()
    if (!orchExec.plan && orchCfg.value.plans?.length) orchExec.plan = orchCfg.value.plans[0].id
  } catch (e) { errMsg.value = '[FAIL] 读编排计划：' + e.message }
}

function orchPlanPayload(x) {
  return {
    id: x.id, targets: x.targets || [], steps: x.steps || [],
    health_check: x.health_check || '', rollback: x.rollback || [],
    strategy: x.strategy || 'sequential', fail_fast: !!x.fail_fast,
    enabled: x.enabled !== false, note: x.note || '', timeout: x.timeout || 120,
  }
}

function editOrchPlan(p) {
  orchForm.id = p.id || ''
  orchForm.targets = [...(p.targets || [])]
  orchForm.stepsText = (p.steps || []).map(s =>
    s.reason ? s.command + ' || ' + s.reason : s.command).join('\n')
  orchForm.health_check = p.health_check || ''
  orchForm.rollbackText = (p.rollback || []).join('\n')
  orchForm.strategy = p.strategy || 'sequential'
  orchForm.fail_fast = !!p.fail_fast
  orchForm.timeout = p.timeout || 120
  orchForm.note = p.note || ''
}

function resetOrchForm() {
  Object.assign(orchForm, {
    id: '', targets: [], stepsText: '', health_check: '', rollbackText: '',
    strategy: 'sequential', fail_fast: false, timeout: 120, note: '',
  })
}

function parseOrchSteps() {
  return String(orchForm.stepsText || '').split('\n')
    .map(l => l.trim()).filter(Boolean).map(l => {
      const parts = l.split('||').map(s => s.trim())
      return { command: parts[0], reason: parts[1] || '',
               on_fail: orchForm.fail_fast ? 'continue' : 'abort' }
    })
}

async function saveOrchPlan() {
  if (!orchForm.id.trim()) { errMsg.value = '计划 id 必填'; return }
  if (!orchForm.targets.length) { errMsg.value = '至少选一台目标（先在「部署通道」登记）'; return }
  const p = {
    id: orchForm.id.trim(), targets: [...orchForm.targets],
    steps: parseOrchSteps(),
    health_check: orchForm.health_check.trim(),
    rollback: String(orchForm.rollbackText || '').split('\n').map(s => s.trim()).filter(Boolean),
    strategy: orchForm.strategy, fail_fast: orchForm.fail_fast,
    enabled: true, note: orchForm.note.trim(), timeout: Number(orchForm.timeout) || 120,
  }
  try {
    const rest = (orchCfg.value.plans || []).filter(x => x.id !== p.id).map(orchPlanPayload)
    orchCfg.value = await api('/v1/orchestration', {
      method: 'POST', body: JSON.stringify({ plans: [...rest, p], enabled: orchCfg.value.enabled }),
    })
    resetOrchForm()
    errMsg.value = '编排计划已保存'
    if (!orchExec.plan) orchExec.plan = p.id
  } catch (e) { errMsg.value = '[FAIL] 保存编排：' + e.message }
}

async function removeOrchPlan(id) {
  try {
    const plans = (orchCfg.value.plans || []).filter(x => x.id !== id).map(orchPlanPayload)
    orchCfg.value = await api('/v1/orchestration', {
      method: 'POST', body: JSON.stringify({ plans, enabled: orchCfg.value.enabled }),
    })
    if (orchExec.plan === id) orchExec.plan = ''
  } catch (e) { errMsg.value = '[FAIL] 删除编排：' + e.message }
}

async function toggleOrchEnabled() {
  try {
    const plans = (orchCfg.value.plans || []).map(orchPlanPayload)
    orchCfg.value = await api('/v1/orchestration', {
      method: 'POST', body: JSON.stringify({ plans, enabled: !orchCfg.value.enabled }),
    })
  } catch (e) { errMsg.value = '[FAIL] 切换编排通道：' + e.message }
}

async function runOrchExec() {
  if (!orchExec.plan) { errMsg.value = '先选一个编排计划'; return }
  orchExec.busy = true
  orchExec.result = null
  const body = { plan: orchExec.plan, timeout: 600 }
  if (orchExec.password) body.password = orchExec.password
  try {
    orchExec.result = await api('/v1/orchestration/exec', {
      method: 'POST', body: JSON.stringify(body) })
    orchExec.password = ''
  } catch (e) {
    orchExec.result = { ok: false, why: e.message, hosts: [] }
    orchExec.password = ''
  } finally { orchExec.busy = false }
}

/* ---------------- 能力边界（插件适配 + 判据开放，只读） ---------------- */
const capCfg = ref({ adapters: [], sources: [], loaded: false })
async function loadCapabilities() {
  try {
    const [hp, js] = await Promise.all([
      api('/v1/host-plugins'), api('/v1/judgment-sources')])
    capCfg.value = { adapters: hp.adapters || [], sources: js.sources || [], loaded: true }
  } catch (e) { errMsg.value = '[FAIL] 读能力边界：' + e.message }
}

/* ---------------- 引擎体检（链路走查 / 闭环验收 / 运维单元） ----------------
   这三块此前都只有 API：装好软件的用户既没法自证"引擎健康"，也用不上运维单元。
   体检面板把它们收口成一个入口（2026-11-23）。 */
const wcResult = ref(null)
const wcBusy = ref(false)
async function runWalkthrough() {
  wcBusy.value = true
  wcResult.value = null
  try { wcResult.value = await api('/v1/walkthrough') }
  catch (e) { errMsg.value = '[FAIL] 链路走查：' + e.message }
  finally { wcBusy.value = false }
}

const opsCfg = ref({ enabled: false, units: [], capability: {} })
const opsExec = reactive({ unit: '', password: '', busy: false, result: null })
async function loadOps() {
  try {
    opsCfg.value = await api('/v1/ops')
    if (!opsExec.unit && opsCfg.value.units?.length) opsExec.unit = opsCfg.value.units[0].id
  } catch (e) { errMsg.value = '[FAIL] 读运维单元：' + e.message }
}
async function toggleOpsEnabled() {
  try {
    const units = (opsCfg.value.units || []).map(u => ({
      id: u.id, target: u.target, steps: u.steps || [],
      health_check: u.health_check || '', rollback: u.rollback || [],
      enabled: u.enabled !== false, note: u.note || '',
    }))
    opsCfg.value = await api('/v1/ops', {
      method: 'POST',
      body: JSON.stringify({ units, enabled: !opsCfg.value.enabled }),
    })
  } catch (e) { errMsg.value = '[FAIL] 切换运维通道：' + e.message }
}
async function runOpsExec() {
  if (!opsExec.unit) { errMsg.value = '先选一个运维单元'; return }
  opsExec.busy = true
  opsExec.result = null
  const body = { unit: opsExec.unit, timeout: 600 }
  if (opsExec.password) body.password = opsExec.password
  try {
    opsExec.result = await api('/v1/ops/exec', {
      method: 'POST', body: JSON.stringify(body) })
    opsExec.password = ''
  } catch (e) {
    opsExec.result = { ok: false, why: e.message }
    opsExec.password = ''
  } finally { opsExec.busy = false }
}

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
  if (settingsTab.value === 'deploy') loadDeploy()
  if (settingsTab.value === 'uistyle') loadUIStyles()
  if (settingsTab.value === 'human') loadHumanOnce()
  if (settingsTab.value === 'orch') loadOrch()
  if (settingsTab.value === 'caps') loadCapabilities()
  if (settingsTab.value === 'health') loadOps()
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
  if (form.repo) { loadTree(''); loadClarify(); adoptDetached() }
})

/* 打开界面/切仓库时接管"已经在跑"的后台夜班：
   以前只有点过「开始夜班」才会轮询，重开软件后进度区一片空白，用户以为没跑。 */
async function adoptDetached() {
  await pollNightOnce()
  const s = night.detStatus
  if (!s) return
  const running = !!s.night?.running || !!s.watchdog?.alive
  night.busy = running
  if (running) {
    night.detNote = `已接管正在跑的夜班（PID ${s.night?.procs?.[0]?.pid || s.watchdog?.pid || '?'}）· 关掉窗口也会继续跑`
    if (!night.detTimer) pollDetached()
  }
}

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

/* ---------------- 内置编辑器（2026-11-20 用户口径：别再来回切外部编辑器） ----------------
   以前点文件树是 `shell.openPath` → 交给系统默认编辑器，改一行代码要切出去再切回来，
   用户实测"很麻烦"。现在改成在窗口内直接打开、直接改、Ctrl+S 保存：
     · 读/写都走 `/v1/fs/read` `/v1/fs/write`，与远端改代码同一条沙箱写盘路径（有备份）
     · 覆盖中栏区域（不是新窗口），关掉就回到工作台，状态不丢
     · 保留「用系统编辑器打开」作为备选（偶尔要看 diff 插件/大文件） */
const ed = reactive({
  open: false, path: '', text: '', original: '',
  loading: false, saving: false, err: '', binary: false,
  info: '', savedAt: '', wrap: false, sysOnly: false,
  // 本地模型自动填充：busy 期间禁按钮（7B 在 CPU 上可能要十几秒，重复点会排队烧 CPU）
  fillBusy: false, fillMsg: '',
})
const edRef = ref(null)                 // textarea 元素（用于聚焦/滚动同步）
const gutterRef = ref(null)             // 行号列

/* ---------- 中栏：工作台 / 代码 IDE 双视图（2026-11-20 用户口径）----------
   用户的原话："左右栏都是可以收缩的嘛，把中栏变成 IDE 就可以有很大页面"。
   所以编辑器不再 fixed 全屏盖住三栏，而是 Teleport 进 .col-mid：
     · 左栏文件树常驻（可收起）；中栏在「工作台」与「代码」之间切换；
     · 打开的文件是上方多标签，点标签切换，关标签不影响其它文件；
     · 模式切换（开发者 ↔ 其它）自动切视图，标签与未保存内容不丢。 */
const midView = ref('work')             // work | code
const edTabs = ref([])                  // [{path,text,original,binary,info,savedAt,err,wrap}]

function tabLabel(p) { return String(p || '').split(/[\\/]/).pop() || String(p || '') }
function tabDirty(t) { return !!t && t.text !== t.original }
function stashActiveTab() {
  if (!ed.open || !ed.path) return
  const t = edTabs.value.find((x) => x.path === ed.path)
  if (!t) return
  Object.assign(t, {
    text: ed.text, original: ed.original, binary: ed.binary,
    info: ed.info, savedAt: ed.savedAt, err: ed.err, wrap: ed.wrap,
  })
}
function hydrateTab(t) {
  ed.path = t.path; ed.text = t.text; ed.original = t.original
  ed.binary = !!t.binary; ed.info = t.info || ''; ed.savedAt = t.savedAt || ''
  ed.err = t.err || ''; ed.wrap = !!t.wrap; ed.loading = false; ed.open = true
}
async function switchTab(path) {
  stashActiveTab(); stashBreakpoints()
  if (run.session || run.busy) await stopRunDebug()
  const t = edTabs.value.find((x) => x.path === path)
  if (!t) return
  hydrateTab(t)
  loadBreakpoints()
  midView.value = 'code'
  await nextTick()
  if (edRef.value && !ed.binary) { edRef.value.focus(); syncGutter({ target: edRef.value }) }
  await loadRunner()
  loadSymbolHints()
}
async function closeTab(path, force = false) {
  const idx = edTabs.value.findIndex((x) => x.path === path)
  if (idx < 0) return
  const t = edTabs.value[idx]
  if (path === ed.path) stashActiveTab()
  if (!force && tabDirty(t) && !confirm(`「${tabLabel(path)}」有未保存的改动，确定关闭吗？`)) return
  if (path === ed.path) await stopRunDebug()
  edTabs.value.splice(idx, 1)
  delete run.bpByPath[path]
  if (!edTabs.value.length) {
    ed.open = false; ed.path = ''; ed.text = ''; ed.original = ''
    ed.err = ''; ed.binary = false; ed.savedAt = ''; ed.info = ''
    midView.value = 'work'
    return
  }
  if (path === ed.path) {
    await switchTab(edTabs.value[Math.min(idx, edTabs.value.length - 1)].path)
  }
}

/* 编辑器里的悬浮对话框（开发者模式唯一的输入口，2026-11-20 用户口径）。
   模型源开关的记忆放 localStorage：开发者一半时间用本地省钱、一半用远端啃硬骨头，
   每次打开都要重选一遍会很烦。 */
const edChat = reactive({
  open: false,
  input: '',
  source: localStorage.getItem('sc_fill_source') || 'local',   // local | remote
  // 回答显示（用户口径：对话框上面要有显示，不然不知道进度）
  answer: '',          // 最近一次模型返回的代码
  answerMeta: '',      // 模型 / 耗时 / 动作（插入/替换/续写）
  answerErr: '',       // 失败原因
  elapsed: 0,          // 生成中已用秒数
})

/* AI 代码导航（2026-11-20）：让 AI 也能按中文意图跳到文件/函数、调试指定函数。
   路由优先级：先本地规则识别「打开/定位/调试/找」意图 → 命中就走导航，不花 token；
   识别不出（普通改写/续写）才回退到 fillWithAI() 走模型。 */
const nav = reactive({
  busy: false,
  action: '',          // navigate | debug | search
  targets: [],         // [{path,line,name,kind,signature}]
  reason: '',
  symbolHints: [],     // 当前文件的符号表（下拉选函数，不用先选中文字）
  symbolPick: '',      // 下拉里选中的函数（定位/调试按钮直接用它）
  showSymbols: false,  // 符号搜索结果是否展开
  lastQuery: '',
})

/* 选中即改（2026-11-20）：选中一段代码 → 解释 / 改 / 补测试 / 找引用。
   改走 diff 预览 + 接受/拒绝，不直接写盘；这是"指一下，AI 改完并证明没改坏"的第一步。 */
const assist = reactive({
  open: false, action: '', busy: false,
  hasSel: false, selStart: 0, selEnd: 0, selected: '',
  instruction: '', result: '', error: '', model: '', ms: 0,
  baseText: '', refs: [],
})
const ASSIST_LABEL = { explain: '解释', edit: '改代码', test: '补测试', refs: '引用' }

function refreshSelection() {
  const el = edRef.value
  if (!el) { assist.hasSel = false; return }
  const s = el.selectionStart, t = el.selectionEnd
  assist.selStart = s; assist.selEnd = t
  assist.selected = ed.text.slice(s, t)
  assist.hasSel = t > s && !!assist.selected.trim()
}

function copyAssist() {
  if (!assist.result) return
  navigator.clipboard?.writeText(assist.result).catch(() => {})
}

function closeAssist() {
  assist.open = false; assist.busy = false
  assist.error = ''; assist.result = ''; assist.refs = []
}

async function openAssist(action) {
  refreshSelection()
  if (!assist.hasSel) { errMsg.value = '先在编辑器里选中一段代码'; return }
  assist.open = true; assist.action = action
  assist.result = ''; assist.error = ''; assist.refs = []
  assist.instruction = ''
  if (action === 'edit') {
    await nextTick()
    document.querySelector('.assist-input')?.focus()
    return
  }
  if (action === 'refs') { await findAssistRefs(); return }
  await runAssist()
}

async function runAssist() {
  if (assist.busy || !assist.selected.trim()) return
  if (assist.action === 'edit' && !assist.instruction.trim()) {
    assist.error = '先写一句想怎么改，例如「先校验再入库」'; return
  }
  assist.busy = true; assist.error = ''; assist.result = ''
  assist.baseText = ed.text
  try {
    const d = await api('/v1/editor/assist', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path: ed.path, action: assist.action,
      selected: assist.selected,
      before: ed.text.slice(Math.max(0, assist.selStart - 6000), assist.selStart),
      after: ed.text.slice(assist.selEnd, assist.selEnd + 800),
      instruction: assist.instruction, source: edChat.source, model: form.model || '',
    }) })
    if (!d.ok) { assist.error = d.reason || '模型没有返回内容'; return }
    assist.result = d.text || ''
    assist.model = d.model || ''; assist.ms = d.ms || 0
  } catch (e) {
    assist.error = '调用失败：' + e.message
  } finally { assist.busy = false }
}

async function findAssistRefs() {
  const sym = (assist.selected.match(/[A-Za-z_$][\w$]*/) || [])[0] || activeSymbol()
  if (!sym) { assist.error = '选中里没有可识别的符号名'; return }
  assist.busy = true; assist.error = ''; assist.refs = []
  try {
    const q = new URLSearchParams({ repo: form.repo, q: sym })
    const d = await api('/v1/editor/symbols?' + q.toString())
    assist.refs = d.hits || []
    if (!assist.refs.length) assist.error = `没找到「${sym}」的引用`
  } catch (e) { assist.error = '查引用失败：' + e.message }
  finally { assist.busy = false }
}

// 行级 diff（LCS）：选区一般只有几十行，800×800 以内足够；太大就退回整段替换视图
function assistDiff(a, b) {
  const A = String(a || '').split('\n'), B = String(b || '').split('\n')
  if (!A.length || !B.length) {
    return [...A.map((x) => ({ t: 'del', x })), ...B.map((x) => ({ t: 'add', x }))]
  }
  if (A.length * B.length > 640000) {
    return [...A.map((x) => ({ t: 'del', x })), ...B.map((x) => ({ t: 'add', x }))]
  }
  const m = A.length, n = B.length
  const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1))
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = A[i] === B[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out = []
  let i = 0, j = 0
  while (i < m && j < n) {
    if (A[i] === B[j]) { out.push({ t: 'same', x: A[i] }); i++; j++ }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: 'del', x: A[i] }); i++ }
    else { out.push({ t: 'add', x: B[j] }); j++ }
  }
  while (i < m) out.push({ t: 'del', x: A[i++] })
  while (j < n) out.push({ t: 'add', x: B[j++] })
  return out
}

async function acceptAssist() {
  if (!assist.result || assist.busy) return
  if (!canWriteFile()) { assist.error = '当前模式只读，不能改文件'; return }
  if (assist.action === 'explain') { copyAssist(); return }
  if (ed.text !== assist.baseText) {
    assist.error = '生成后文件又被改过，为避免覆盖请重新生成一次'
    return
  }
  if (assist.action === 'edit') {
    const s = assist.selStart, t = assist.selEnd
    ed.text = ed.text.slice(0, s) + assist.result + ed.text.slice(t)
    stashActiveTab()
    assist.open = false
    await nextTick()
    if (edRef.value) {
      edRef.value.focus()
      edRef.value.selectionStart = edRef.value.selectionEnd = s + assist.result.length
    }
  } else if (assist.action === 'test') {
    ed.text = ed.text.replace(/\s*$/, '') + '\n\n' + assist.result + '\n'
    stashActiveTab()
    assist.open = false
    await nextTick()
    edRef.value?.focus()
  }
  assist.result = ''; assist.error = ''
}
let edChatTimer = null
function startChatTick() {
  if (edChatTimer) return
  const t0 = Date.now()
  edChat.elapsed = 0
  edChatTimer = setInterval(() => { edChat.elapsed = Math.round((Date.now() - t0) / 1000) }, 1000)
}
function stopChatTick() {
  if (edChatTimer) { clearInterval(edChatTimer); edChatTimer = null }
}
async function copyAnswer() {
  try { await navigator.clipboard.writeText(edChat.answer); edChat.answerMeta += ' · 已复制' } catch { }
}
function setFillSource(s) {
  edChat.source = s === 'remote' ? 'remote' : 'local'
  try { localStorage.setItem('sc_fill_source', edChat.source) } catch { /* 隐私模式忽略 */ }
}
function openEditorChat() {
  edChat.open = true
  nextTick(() => {
    const el = document.querySelector('.ed-chat textarea')
    if (el) el.focus()
  })
}
/* ---------------- 开发者模式：运行与调试（2026-11-20） ----------------
   运行：按文件语言自动选工具链真跑，输出/诊断在底部面板；诊断行可点跳到编辑器。
   调试：Python 用真断点（bdb）单步/看变量；其他语言明确标注不支持，不误导。 */
const run = reactive({
  open: false,          // 输出面板是否展开
  busy: false,
  lang: '', tool: '', toolVersion: '', cmd: [], cwd: '', debug: false, reason: '',
  args: '', timeout: 30,
  stdout: '', stderr: '', rc: null, ms: 0, ok: null, phase: '',
  diag: [],             // [{file,line,col,message}]
  session: null,        // 调试会话 id（必须 null，不能 ''：空串会被 Vue 渲染成 disabled）
  breakpoints: [],      // 当前文件已下断点行号（点击编辑器行号列切换）
  bpByPath: {},         // 断点按文件分开存，切标签不串味（VSCode 口径）
  paused: null,         // 当前暂停 {line, locals, stack, func}
  events: [],           // 调试输出事件流
  expr: '',             // 变量观察表达式
  watch: {},            // 表达式求值结果
})
let dbgTimer = null
function stopDebugPoll() { if (dbgTimer) { clearInterval(dbgTimer); dbgTimer = null } }
function startDebugPoll() {
  stopDebugPoll()
  dbgTimer = setInterval(async () => {
    if (!run.session) return
    try {
      const d = await api('/v1/editor/debug/events?id=' + encodeURIComponent(run.session))
      for (const ev of (d.events || [])) applyDebugEvent(ev)
      if (d.status && d.status.ended) { run.session = null; run.busy = false; stopDebugPoll() }
    } catch (e) { /* 轮询失败不刷屏，等下轮 */ }
  }, 700)
}
function applyDebugEvent(ev) {
  if (ev.event === 'output') {
    if (ev.stream === 'stderr') run.stderr += (ev.text || '') + '\n'
    else run.stdout += (ev.text || '') + '\n'
  } else if (ev.event === 'paused') {
    run.paused = { line: ev.line, locals: ev.locals || {}, stack: ev.stack || [], func: ev.func }
    run.breakpoints = ev.breakpoints || run.breakpoints
    run.busy = false
  } else if (ev.event === 'locals') {
    Object.assign(run.watch, ev.exprs || {}, ev.locals || {})
  } else if (ev.event === 'breakpoints') {
    run.breakpoints = ev.breakpoints || []
  } else if (ev.event === 'ended') {
    run.stdout += ev.error ? '[调试异常] ' + ev.error + '\n' : ''
    run.ok = !ev.error; run.session = null; run.busy = false; run.paused = null
    stopDebugPoll()
  } else if (ev.event === 'exception') {
    run.stderr += `[${ev.type}] ${ev.message}\n`
  }
}
async function loadRunner() {
  if (!ed.open || ed.binary || !form.repo) { run.reason = ed.binary ? '二进制文件不能运行' : ''; return }
  try {
    const d = await api('/v1/editor/runner?repo=' + encodeURIComponent(form.repo)
      + '&path=' + encodeURIComponent(ed.path))
    run.lang = d.lang || ''; run.tool = d.tool || ''; run.toolVersion = d.toolVersion || ''
    run.cmd = d.cmd || []; run.cwd = d.cwd || ''; run.debug = !!d.debug; run.reason = d.reason || ''
  } catch (e) { run.reason = e.message }
}
async function runCurrent() {
  if (!ed.open || ed.binary || !form.repo) return
  run.open = true; run.busy = true; run.ok = null
  run.stdout = ''; run.stderr = ''; run.diag = []; run.rc = null
  try {
    const d = await api('/v1/editor/run', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path: ed.path, args: run.args, timeout: run.timeout }) })
    run.stdout = d.stdout || ''; run.stderr = d.stderr || ''
    run.rc = d.returncode; run.ms = d.ms || 0; run.ok = !!d.ok
    run.diag = d.diagnostics || []; run.lang = d.lang || run.lang
    run.reason = d.reason || ''; run.phase = d.phase || ''
  } catch (e) { run.ok = false; run.reason = e.message }
  finally { run.busy = false }
}
function jumpToDiag(d) {
  const ref = (d.file || '').replace(/\\/g, '/')
  if (!ref.endsWith(ed.path.replace(/\\/g, '/'))) { errMsg.value = '这个错误不在当前文件：' + d.file; return }
  const n = Math.max(1, Number(d.line) || 1)
  nextTick(() => {
    if (!edRef.value) return
    const lines = ed.text.split('\n')
    const pos = lines.slice(0, n - 1).join('\n').length + (n > 1 ? 1 : 0)
    edRef.value.focus(); edRef.value.setSelectionRange(pos, pos)
    const lineH = edRef.value.scrollHeight / Math.max(1, lines.length)
    edRef.value.scrollTop = Math.max(0, (n - 4) * lineH)
    syncGutter({ target: edRef.value })
  })
}
async function startDebug() {
  if (!run.debug) { errMsg.value = run.reason || '当前文件不支持断点调试'; return }
  run.open = true; run.busy = true
  run.stdout = ''; run.stderr = ''; run.paused = null; run.watch = {}; run.ok = null
  try {
    const d = await api('/v1/editor/debug/start', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path: ed.path, breakpoints: run.breakpoints }) })
    if (!d.ok) { run.reason = d.reason; run.busy = false; return }
    run.session = d.id; run.reason = ''
    startDebugPoll()
  } catch (e) { run.reason = e.message; run.busy = false }
}
async function dbgCmd(op, extra = {}) {
  if (!run.session) return
  if (op === 'continue') { run.paused = null; run.busy = true }
  if (op === 'step' || op === 'next') run.busy = true
  try {
    const d = await api('/v1/editor/debug/cmd', { method: 'POST', body: JSON.stringify({
      id: run.session, op, ...extra }) })
    for (const ev of (d.events || [])) applyDebugEvent(ev)
    if (d.status && d.status.ended) { run.session = null; stopDebugPoll() }
  } catch (e) { run.reason = e.message; run.busy = false }
}
/* 断点：直接点行号列（VSCode 手感），命中处画红点。
   旧版是编辑器头部的「断点」按钮 + 光标所在行，用户说不方便，已改成点行号。 */
function stashBreakpoints() {
  if (ed.path) run.bpByPath[ed.path] = [...run.breakpoints]
}
function loadBreakpoints() {
  run.breakpoints = [...(run.bpByPath[ed.path] || [])]
}
function toggleBreakpointAt(line) {
  const n = Math.max(1, Number(line) || 1)
  const has = run.breakpoints.includes(n)
  const next = has ? run.breakpoints.filter(x => x !== n) : [...run.breakpoints, n].sort((a, b) => a - b)
  run.breakpoints = next
  if (ed.path) run.bpByPath[ed.path] = [...next]
  if (run.session) dbgCmd(has ? 'unbreak' : 'break', { line: n })
}
function watchExpr() {
  const e = (run.expr || '').trim()
  if (!e || !run.session) return
  dbgCmd('locals', { exprs: [e] })
  run.expr = ''
}
async function stopRunDebug() {
  stopDebugPoll()
  if (run.session) { try { await api('/v1/editor/debug/stop', { method: 'POST', body: JSON.stringify({ id: run.session, op: 'quit' }) }) } catch { } }
  run.session = null; run.busy = false; run.paused = null
}

const edDirty = computed(() => ed.open && ed.text !== ed.original)
const edLines = computed(() => {
  const n = ed.text ? ed.text.split('\n').length : 0
  return Array.from({ length: n }, (_, i) => i + 1)
})

// 当前打开的文件是否就是这一项（文件树高亮用）
function isEditing(node) {
  return ed.open && !!node && (node.path || '') === ed.path && !node.is_dir
}

function canReadFile() { return can('file.read') }
function canWriteFile() { return can('file.write') }

async function openFile(node, force = false) {
  if (!node || node.is_dir) return
  if (!form.repo) { errMsg.value = '先选仓库'; return }
  const path = node.path
  // 已打开的标签：直接切过去（保留未保存内容），除非显式要求从磁盘重读（回滚）
  const exists = edTabs.value.find((t) => t.path === path)
  if (exists && !force) {
    stashActiveTab()
    await switchTab(path)
    return
  }
  if (ed.open && !force && edDirty.value && !confirm('当前文件有未保存的改动，确定切换吗？')) return
  if (ed.open && !force) stashActiveTab()
  stashBreakpoints()
  if (run.session || run.busy) await stopRunDebug()
  const loadingTab = exists || {
    path, text: '', original: '', binary: false,
    info: '', savedAt: '', err: '', wrap: ed.wrap,
  }
  if (!exists) edTabs.value.push(loadingTab)
  ed.path = path
  loadBreakpoints()
  ed.loading = true; ed.err = ''; ed.binary = false; ed.savedAt = ''
  ed.open = true; midView.value = 'code'
  try {
    const q = new URLSearchParams({ repo: form.repo, path })
    const d = await api('/v1/fs/read?' + q.toString())
    ed.text = d.text || ''
    ed.original = ed.text
    ed.binary = !!d.binary
    ed.info = `${(d.size || 0).toLocaleString()} 字节 · ${d.lines || 0} 行`
    ed.sysOnly = ed.binary
    Object.assign(loadingTab, {
      text: ed.text, original: ed.original, binary: ed.binary,
      info: ed.info, savedAt: '', err: '', wrap: ed.wrap,
    })
    await nextTick()
    if (edRef.value && !ed.binary) { edRef.value.focus(); syncGutter({ target: edRef.value }) }
    await loadRunner()
    loadSymbolHints()
  } catch (e) {
    ed.err = e.message; loadingTab.err = e.message
    ed.text = ''; ed.original = ''
    loadingTab.text = ''; loadingTab.original = ''
  } finally { ed.loading = false; loadingTab.err = ed.err }
}

async function saveFile() {
  if (!ed.open || ed.binary || !form.repo) return
  if (!canWriteFile()) { ed.err = '当前模式不允许写文件（只读模式下只能看）'; return }
  if (ed.text === ed.original) { ed.savedAt = '没有改动'; return }
  ed.saving = true; ed.err = ''
  try {
    const d = await api('/v1/fs/write', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path: ed.path, content: ed.text, backup: true }) })
    ed.original = ed.text
    ed.savedAt = `已保存 ${d.bytes} 字节${d.backup ? '（已备份原文件）' : ''}`
    const t = edTabs.value.find((x) => x.path === ed.path)
    if (t) Object.assign(t, { text: ed.text, original: ed.original, savedAt: ed.savedAt, err: '' })
    await refreshGit()
  } catch (e) {
    ed.err = e.message
  } finally { ed.saving = false }
}

function closeEditor(force = false) {
  // 「关闭 Esc」只关当前标签；其它标签保留（2026-11-20 用户口径：上方多标签共存）
  return closeTab(ed.path, force)
}

// Ctrl+S 保存 / Ctrl+W 关闭：编辑器打开时接管，避免触发浏览器默认的"保存网页"
function onEditorKey(e) {
  if (!ed.open) return
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
    e.preventDefault(); saveFile()
  } else if (e.key === 'Escape') {
    e.preventDefault(); closeEditor()
  }
}

/* 行号列跟随 textarea 滚动（两个独立滚动容器，不同步会错位） */
function syncGutter(e) {
  if (gutterRef.value) gutterRef.value.scrollTop = e.target.scrollTop
}

/* ---------------- 自动缩进（编辑器里最影响手感的一件事） ----------------
   规则（对齐 VSCode 的默认行为，不引入额外插件）：
     1) 回车：新行沿用上一行缩进；
     2) 上一行以 `:` `{` `[` `(` 结尾（去掉行尾空白后）→ 多缩一级；
     3) 光标右边紧跟着 `}` `]` `)` → 这一行先退一级（少按一次 Shift+Tab）；
     4) Tab/Shift+Tab：有选区时整块缩进/反缩进，无选区时插两个空格；
   缩进单位用**空格**（两个），不用 tab —— 模型生成的代码、Git diff 都按空格对齐，
   混用 tab 会让同一份文件里出现两种缩进宽度。 */
const INDENT = '  '

function _lineStart(text, pos) { return text.lastIndexOf('\n', Math.max(0, pos - 1)) + 1 }

function autoIndentNewline(e) {
  const el = e.target
  const s = el.selectionStart, t = el.selectionEnd
  const text = ed.text
  if (s !== t) {                                        // 选区被回车替换：保持默认（删掉选区）
    e.preventDefault()
    const lineStart = _lineStart(text, s)
    const head = text.slice(lineStart, s)
    const base = (head.match(/^[ \t]*/) || [''])[0]
    const insert = '\n' + base
    ed.text = text.slice(0, s) + insert + text.slice(t)
    nextTick(() => { el.selectionStart = el.selectionEnd = s + insert.length })
    return
  }
  const lineStart = _lineStart(text, s)
  const line = text.slice(lineStart, s)
  let indent = (line.match(/^[ \t]*/) || [''])[0].replace(/\t/g, INDENT)
  const trimmed = line.replace(/[ \t]+$/, '')
  if (/[:{[(]\s*$/.test(trimmed)) indent += INDENT
  const nextChar = text.slice(t, t + 1)
  if (/[}\])]/.test(nextChar) && indent.endsWith(INDENT)) {
    indent = indent.slice(0, -INDENT.length)
  }
  e.preventDefault()
  const insert = '\n' + indent
  ed.text = text.slice(0, s) + insert + text.slice(t)
  nextTick(() => {
    el.selectionStart = el.selectionEnd = s + insert.length
    el.scrollTop = el.scrollHeight > 0 ? el.scrollTop : 0
  })
}

function indentBlock(e, outdent) {
  const el = e.target
  const s = el.selectionStart, t = el.selectionEnd
  const text = ed.text
  const startLine = _lineStart(text, s)
  let endPos = text.indexOf('\n', t)
  if (endPos === -1) endPos = text.length
  const block = text.slice(startLine, endPos)
  const lines = block.split('\n')
  const done = lines.map((ln) => {
    if (outdent) {
      if (ln.startsWith(INDENT)) return ln.slice(INDENT.length)
      if (ln.startsWith('\t')) return ln.slice(1)
      return ln
    }
    return ln.trim() ? INDENT + ln : ln
  })
  const replaced = done.join('\n')
  ed.text = text.slice(0, startLine) + replaced + text.slice(endPos)
  const delta = replaced.length - block.length
  nextTick(() => {
    // 保持整块选中：反缩进时选区左端可能被顶到行首，用 Math.max 夹一下
    el.selectionStart = Math.max(startLine, s + (outdent ? Math.min(0, delta) : INDENT.length))
    el.selectionEnd = Math.max(el.selectionStart, t + delta)
  })
}

function onEditorKeydown(e) {
  if (!ed.open) return
  // 本地模型填充：Ctrl+Space（业界惯例）
  if ((e.ctrlKey || e.metaKey) && (e.key === ' ' || e.code === 'Space')) {
    e.preventDefault(); fillWithAI(); return
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
    autoIndentNewline(e); return
  }
  if (e.key === 'Tab') {
    e.preventDefault()
    const el = e.target
    if (el.selectionStart !== el.selectionEnd) { indentBlock(e, e.shiftKey); return }
    if (e.shiftKey) { indentBlock(e, true); return }
    const s = el.selectionStart
    ed.text = ed.text.slice(0, s) + INDENT + ed.text.slice(el.selectionEnd)
    nextTick(() => { el.selectionStart = el.selectionEnd = s + INDENT.length })
  }
}

/* 把模型返回的代码对齐到光标处（"自动填充"最容易毁手感的一步）。
   实测：光标在 `def divide(a, b):\n    ` 时，7B 会返回整段以 4 空格开头的代码，
   直接粘在光标后 → 变成 8 空格，看起来像缩进错了。做法：
     1) 先按"最小公共缩进"整体 dedent（模型习惯性地把整段再缩一级）；
     2) 第一行不加前缀（它接在光标已有的缩进后面）；
     3) 其余行统一补上光标处的缩进，保持相对层级。 */
function alignInsertion(ins, textBeforeCursor) {
  if (!ins) return ins
  const lineStart = textBeforeCursor.lastIndexOf('\n') + 1
  const head = textBeforeCursor.slice(lineStart)
  if (head.trim() !== '') return ins               // 光标在行中间：原样插入，别自作主张
  const curIndent = (head.match(/^[ \t]*/) || [''])[0].replace(/\t/g, INDENT)
  const raw = ins.split('\n')
  const filled = raw.filter((l) => l.trim())
  if (!filled.length) return ins
  const base = Math.min(...filled.map((l) => (l.match(/^[ \t]*/) || [''])[0].length))
  const dedented = raw.map((l) => {
    if (!l.trim()) return ''
    const ind = (l.match(/^[ \t]*/) || [''])[0]
    return l.slice(Math.min(base, ind.length))
  })
  // 模型偶尔会把光标前那一行又抄一遍 —— 一模一样就去掉
  const beforeLast = (textBeforeCursor.replace(/[ \t]+$/, '').split('\n').pop() || '').trim()
  if (dedented.length && dedented[0].trim() && dedented[0].trim() === beforeLast) {
    dedented.shift()
  }
  return dedented
    .map((l, i) => (i === 0 || !l ? l : curIndent + l))
    .join('\n')
}

/* ---------------- AI 代码导航：跳到文件/函数、调试指定函数 ---------------- */

// 把光标移到编辑器第 n 行并滚动到可见（诊断跳转 / 符号跳转共用）
function gotoLine(n) {
  const line = Math.max(1, Number(n) || 1)
  nextTick(() => {
    if (!edRef.value) return
    const lines = ed.text.split('\n')
    const pos = lines.slice(0, line - 1).join('\n').length + (line > 1 ? 1 : 0)
    edRef.value.focus(); edRef.value.setSelectionRange(pos, pos)
    const lineH = edRef.value.scrollHeight / Math.max(1, lines.length)
    edRef.value.scrollTop = Math.max(0, (line - 4) * lineH)
    syncGutter({ target: edRef.value })
  })
}

// 打开目标文件（可能不是当前文件）并跳到指定行；then 可选自动调试
async function openTarget(t, { thenDebug = false } = {}) {
  if (!t || !t.path) return
  if (t.path !== ed.path) {
    await openFile({ path: t.path, is_dir: false })
  }
  gotoLine(t.line || 1)
  if (thenDebug) {
    await nextTick()
    await debugSymbol(t.symbol || t.name, t.path)
  }
}

// 读编辑器当前选区里的标识符（没选就取光标所在单词），给"选中函数→定位/调试"按钮用
function selectedSymbol() {
  const el = edRef.value
  if (!el || !ed.text) return ''
  let s = el.selectionStart, t = el.selectionEnd
  if (s === t) {
    // 光标没选：向两边扩到标识符边界
    const word = /[A-Za-z_$][\w$]*/
    const left = ed.text.slice(0, s).match(word)?.[0] || ''
    const right = ed.text.slice(s).match(word)?.[0] || ''
    const name = (left + right).match(word)?.[0] || ''
    return name
  }
  const sel = ed.text.slice(s, t).trim()
  const m = sel.match(/[A-Za-z_$][\w$]*/)
  return m ? m[0] : ''
}

// 下拉优先，没选下拉才退回"选中/光标处的词" —— 调试/定位不用再先选文字
function activeSymbol() { return nav.symbolPick || selectedSymbol() }

// 选中函数名点按钮：在当前文件精确定位 + 直接启动调试
async function debugSymbol(symbol, path = ed.path) {
  if (!ed.open || !form.repo || !symbol) { errMsg.value = '先打开文件并选中一个函数名'; return }
  if (canReadFile() === false) { errMsg.value = '只读模式不能运行/调试'; return }
  nav.busy = true
  try {
    const d = await api('/v1/editor/debug/symbol', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path, symbol }) })
    if (!d.ok) { errMsg.value = d.reason || '定位失败'; return }
    const t = d.target || {}
    // 跳到函数体首行（断点落点）
    if (path === ed.path) gotoLine(d.breakpoint || t.line || 1)
    if (!d.id) { errMsg.value = d.reason || '这个函数无法启动调试'; return }
    run.session = d.id; run.open = true; run.busy = false
    run.stdout = ''; run.stderr = ''; run.paused = null; run.watch = {}; run.ok = null
    run.breakpoints = [d.breakpoint || t.line || 1]
    run.bpByPath[path] = [...run.breakpoints]
    startDebugPoll()
    edChat.answerMeta = `已在 ${t.name || symbol} 第 ${d.breakpoint || t.line} 行下断点并启动调试`
    edChat.answer = ''
  } catch (e) { errMsg.value = '调试失败：' + e.message }
  finally { nav.busy = false }
}

// 选中函数名点按钮：只定位不调试
async function locateSymbol(symbol, path = ed.path) {
  if (!ed.open || !form.repo || !symbol) { errMsg.value = '先打开文件并选中一个函数名'; return }
  nav.busy = true
  try {
    const d = await api('/v1/editor/symbol/locate', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path, symbol }) })
    if (!d.ok) { errMsg.value = d.reason || '没找到'; return }
    await openTarget({ ...d, symbol })
    edChat.answerMeta = `已定位到 ${d.name}（第 ${d.line} 行）`
    edChat.answer = ''
  } catch (e) { errMsg.value = '定位失败：' + e.message }
  finally { nav.busy = false }
}

// 取当前文件的符号表（供选中按钮旁的候选提示）
async function loadSymbolHints() {
  if (!ed.open || !form.repo || !ed.path) return
  try {
    const q = new URLSearchParams({ repo: form.repo, q: ed.path })
    const d = await api('/v1/editor/symbols?' + q.toString())
    nav.symbolHints = (d.hits || []).filter(h => h.path === ed.path)
    if (!nav.symbolHints.some(h => h.name === nav.symbolPick)) {
      nav.symbolPick = nav.symbolHints[0]?.name || ''
    }
  } catch { nav.symbolHints = []; nav.symbolPick = '' }
}

// 对话里的一句话：先本地识别意图，命中导航/调试就直接做，否则回退代码填充
async function chatOrNavigate() {
  const text = (edChat.input || '').trim()
  if (!ed.open || ed.binary || ed.fillBusy || nav.busy) return
  // 轻量本地预判：只有出现明确的导航动词才走 navigate，避免每个普通改写到后端白跑一趟
  if (!/打开|跳到|跳转|定位|调试|断点|单步|跑到|看看|找到|查看|转到|带我去|哪个文件|在哪|哪里|搜索|搜一下|找一下|找找|全仓/.test(text)) {
    return fillWithAI()
  }
  nav.busy = true; nav.reason = ''; nav.targets = []
  try {
    const d = await api('/v1/editor/navigate', { method: 'POST', body: JSON.stringify({
      repo: form.repo, text }) })
    if (!d.ok || !d.targets || !d.targets.length) {
      // 识别了动作但没找到目标：如实提示，不瞎跳，也不当成代码改写
      nav.reason = d.reason || '没找到目标'; nav.action = d.action || ''
      edChat.answerErr = nav.reason; edChat.answerMeta = ''; edChat.answer = ''
      return
    }
    nav.action = d.action; nav.targets = d.targets; nav.lastQuery = text
    if (d.action === 'debug') {
      await openTarget(d.targets[0], { thenDebug: true })
    } else if (d.action === 'navigate') {
      await openTarget(d.targets[0])
      edChat.answerMeta = `已定位到 ${d.targets[0].name}（${d.targets[0].path}:${d.targets[0].line}）`
      edChat.answer = ''
    } else {
      // search：列候选，展示可点击面板
      nav.showSymbols = true
      edChat.answerMeta = `找到 ${d.targets.length} 个候选，点下面的结果跳转`
      edChat.answer = ''
    }
  } catch (e) { edChat.answerErr = '导航失败：' + e.message }
  finally { nav.busy = false }
}

// 点搜索结果：跳转（debug 动作则同时启动调试）
async function pickNavTarget(t) {
  await openTarget(t, { thenDebug: nav.action === 'debug' })
}

/* ---------------- 本地模型自动填充 ----------------
   光标前 6000 字符 + 光标后 800 字符发给本地 Ollama，返回的代码直接插在光标处。
   为什么走"插入"而不是"幽灵文本预览"：textarea 做不了内联预览，而插入 + Ctrl+Z
   撤销是最不容易让人误操作的做法（用户已经习惯 Ctrl+Z）。 */
async function fillWithAI() {
  if (!ed.open || ed.binary || ed.fillBusy) return
  const el = edRef.value
  const s = el ? el.selectionStart : ed.text.length
  const t = el ? el.selectionEnd : ed.text.length
  ed.fillBusy = true; ed.err = ''
  edChat.answer = ''; edChat.answerErr = ''; edChat.answerMeta = ''
  ed.fillMsg = ''
  startChatTick()
  const started = Date.now()
  const hasSel = t > s
  try {
    const d = await api('/v1/editor/fill', { method: 'POST', body: JSON.stringify({
      repo: form.repo, path: ed.path,
      before: ed.text.slice(Math.max(0, s - 6000), s),
      after: ed.text.slice(t, t + 800),
      selected: ed.text.slice(s, t),
      model: form.model || '',
      instruction: (edChat.input || '').trim(),   // 悬浮对话框里写的意图
      source: edChat.source,                      // 本地 / 远端（用户自己选）
    }) })
    if (!d.ok) {
      edChat.answerErr = d.reason || '模型没有返回内容'
      edChat.answerMeta = ''
      return
    }
    const raw = d.insertion || ''
    // 改写选区时原样替换；续写时做缩进对齐（否则双重缩进）
    const ins = hasSel ? raw : alignInsertion(raw, ed.text.slice(0, s))
    ed.text = ed.text.slice(0, s) + ins + ed.text.slice(t)
    const secs = ((Date.now() - started) / 1000).toFixed(1)
    const how = hasSel ? '替换选区' : (edChat.input.trim() ? '插入光标处' : '续写')
    edChat.answer = ins
    edChat.answerMeta = `${how} ${ins.length} 字符 · ${d.model} · ${secs}s · Ctrl+Z 可撤销`
    ed.fillMsg = ''
    await nextTick()
    if (el) { el.selectionStart = el.selectionEnd = s + ins.length; el.focus() }
  } catch (e) {
    edChat.answerErr = '调用失败：' + e.message
  } finally { ed.fillBusy = false; stopChatTick() }
}

/* 在编辑器里回滚当前文件（用户口径：编辑器里也要能回滚，不用切到顶栏）。
   走沙箱备份（不是 git）：只影响这一个文件，且沙箱只保留最近 3 份历史，
   所以最多能这样回滚 3 次；更早的历史交给 Git 提交。 */
const edRollbackBusy = ref(false)

async function rollbackCurrentFile() {
  if (!form.repo || !ed.path) { edChat.answerErr = '先选仓库并打开一个文件'; return }
  if (!confirm(`把 ${ed.path} 回滚到上一次保存前的状态？\n\n`
    + `（用的是沙箱备份，每个文件只保留最近 3 份，所以最多回滚 3 次）`)) return
  edRollbackBusy.value = true
  edChat.answerErr = ''
  try {
    const d = await api('/v1/git/do', { method: 'POST', body: JSON.stringify({
      op: 'undo_sandbox_file', repo: form.repo, args: { path: ed.path } }) })
    if (d && d.ok === false) {
      edChat.answerErr = d.detail || '回滚失败：没有可用的备份（可能已经回滚过 3 次）'
      return
    }
    await openFile({ path: ed.path, is_dir: false }, true)
    edChat.answerMeta = '已回滚到上次保存前'
    await refreshGit()
  } catch (e) {
    edChat.answerErr = '回滚失败：' + e.message
  } finally { edRollbackBusy.value = false }
}

// 直接交给系统默认编辑器（备选路径：需要 IDE 插件、大文件、看 diff 时用）
async function openFileExternally() {
  if (!ed.open) return
  const abs = (treeRoot.value || form.repo || '').replace(/[\\/]+$/, '') + '/' + ed.path
  if (shellApi?.openPath) { shellApi.openPath(abs); return }
  errMsg.value = '调用系统编辑器需要桌面版外壳；浏览器版请直接用仓库路径'
}

// 复制当前文件路径（贴到别的工具里很方便）
async function copyFilePath() {
  try {
    await navigator.clipboard.writeText(
      (treeRoot.value || form.repo || '').replace(/[\\/]+$/, '') + '/' + ed.path)
    ed.savedAt = '路径已复制'
  } catch { ed.savedAt = '复制失败' }
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
                         // 后台长跑（2026-11-20 用户口径）：不占界面进程，关掉窗口也继续跑。
                         // 走 /v1/night/detached → spawn 独立 watchdog；状态从磁盘轮询。
                         detached: true, detStatus: null, detTimer: null, detNote: '',
                         // 后台夜班的增量日志（喂「执行时间线」）：offset 按字节推进，
                         // 日志换文件（新的一夜/换仓库）就重置，避免重复或漏行。
                         logOffset: 0, logPath: '', logTimer: null,
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
const isReadonlyMode = computed(() => currentMode.value?.id === 'readonly')

/* ---------------- 夜班模式：需求澄清对话框（2026-11-20 用户口径）----------------
   夜班的交互就三样：一个文本框、一个开始按钮、一段对话。
   点「开始分析」→ 远端把需求拆成"要做什么 + 技术栈 + 还没定的几件事（多选）"，
   人点完再让它自评"这些细节够不够开工"；它说够了，按钮才变成「开始夜班」。
   为什么要这一层：直接开跑的代价是整夜的钱都押在"它猜的算不算数"上。

   状态跟仓库绑定并落盘（<repo>/.sixcoder/clarify.json），关掉界面再打开能接着看。 */
const clarify = reactive({
  started: false, busy: false, ready: false, round: 0,
  input: '', requirement: '', narrative: '', render: '', specPath: '',
  questions: [], answers: {}, answered: [], msgs: [], note: '', elapsedS: 0,
})
let clarifyTimer = null

function clarifyMsg(role, text) {
  if (!text || !String(text).trim()) return
  clarify.msgs.push({ role, text: String(text) })
}

async function loadClarify() {
  if (!form.repo) return
  try {
    const d = await api(`/v1/night/clarify/status?repo=${encodeURIComponent(form.repo)}`)
    if (!d.exists) {
      Object.assign(clarify, { started: false, ready: false, round: 0,
        narrative: '', questions: [], answers: {}, answered: [], msgs: [], render: '' })
      return
    }
    clarify.started = true
    clarify.ready = !!d.ready
    clarify.round = d.round || 0
    clarify.requirement = d.requirement || ''
    // 协商定稿也要写回表单：否则重启后「开始夜班」看到的 form.requirement 还是空的
    // （2026-11-21 用户报"夜班总是显示需要仓库和需求"的直接原因）
    if (clarify.requirement) form.requirement = clarify.requirement
    clarify.narrative = d.narrative || ''
    clarify.render = d.render || ''
    clarify.questions = d.questions || []
    clarify.answered = d.answered || []
    clarify.answers = {}
    clarify.msgs = []
    clarifyMsg('user', d.requirement)
    if (d.narrative) clarifyMsg('assistant', d.narrative)
    if (clarify.ready) clarify.note = '远端已确认这些细节足以开工 → 点「开始夜班」'
    else if (clarify.questions.length) clarify.note = `还有 ${clarify.questions.length} 项待确认`
  } catch { /* 后端刚起时忽略 */ }
}

function startClarifyTick() {
  if (clarifyTimer) return
  const t0 = Date.now()
  clarify.elapsedS = 0
  clarifyTimer = setInterval(() => { clarify.elapsedS = Math.round((Date.now() - t0) / 1000) }, 1000)
}
function stopClarifyTick() {
  if (clarifyTimer) { clearInterval(clarifyTimer); clarifyTimer = null }
}

async function startClarify() {
  const text = clarify.input.trim() || clarify.requirement
  if (!form.repo) { errMsg.value = '先选仓库'; return }
  if (!text) { errMsg.value = '先写需求：要做什么、给谁用'; return }
  clarify.busy = true; errMsg.value = ''
  clarifyMsg('user', clarify.input.trim() || text)
  startClarifyTick()
  try {
    const d = await api('/v1/night/clarify', { method: 'POST', body: JSON.stringify({
      repo: form.repo, requirement: text, effort: 'max' }) })
    applyClarifyResult(d)
    clarify.input = ''
  } catch (e) {
    clarifyMsg('system', '分析失败：' + e.message)
    errMsg.value = e.message
  } finally { clarify.busy = false; stopClarifyTick() }
}

async function answerClarify(useRecommended = false) {
  if (!form.repo) return
  const answers = {}
  for (const q of clarify.questions) {
    const picked = useRecommended ? (q.options || [])[0] : clarify.answers[q.key]
    if (picked) answers[q.key] = picked
  }
  if (!Object.keys(answers).length) { errMsg.value = '至少选一项（或点「都按推荐来」）'; return }
  const human = Object.entries(answers).map(([k, v]) => {
    const q = clarify.questions.find((x) => x.key === k)
    return `${q ? q.question : k} → ${v}`
  }).join('；')
  clarify.busy = true; errMsg.value = ''
  clarifyMsg('user', (useRecommended ? '都按你推荐的来：' : '我的选择：') + human)
  startClarifyTick()
  try {
    const d = await api('/v1/night/clarify', { method: 'POST', body: JSON.stringify({
      repo: form.repo, requirement: clarify.requirement, answers, effort: 'max' }) })
    applyClarifyResult(d)
  } catch (e) {
    clarifyMsg('system', '继续协商失败：' + e.message)
    errMsg.value = e.message
  } finally { clarify.busy = false; stopClarifyTick() }
}

function applyClarifyResult(d) {
  clarify.started = true
  clarify.ready = !!d.ready
  clarify.round = d.round || 0
  clarify.requirement = d.requirement || clarify.requirement
  clarify.narrative = d.narrative || ''
  clarify.render = d.render || ''
  clarify.questions = d.questions || []
  clarify.answered = d.answered || []
  clarify.answers = {}
  clarify.specPath = d.spec_path || ''
  clarify.note = d.note || ''
  if (d.narrative) clarifyMsg('assistant', d.narrative)
  if (clarify.ready) {
    clarifyMsg('assistant', '我确认这些细节足够开工了。点「开始夜班」，我按这份规格做完再叫你。')
  } else if (clarify.questions.length) {
    clarifyMsg('assistant', `还有 ${clarify.questions.length} 项需要你定一下（下面选完点「回答并继续」）。`)
  }
  // 协商定了规格，夜班就用这份规格开工（覆盖输入框里的原始需求）
  if (clarify.requirement) form.requirement = clarify.requirement
}

async function resetClarify() {
  if (!form.repo) return
  if (!confirm('重新开始协商？已经选过的待定项会丢掉。')) return
  clarify.busy = true
  try {
    await api('/v1/night/clarify', { method: 'POST', body: JSON.stringify({
      repo: form.repo, reset: true, requirement: clarify.input.trim() || clarify.requirement }) })
  } catch { /* 清空失败也把界面清掉，别卡住 */ }
  Object.assign(clarify, { started: false, ready: false, round: 0, narrative: '', render: '',
    questions: [], answers: {}, answered: [], msgs: [], note: '', specPath: '' })
  clarify.busy = false
}

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

/* 夜班真正要交上去的需求：协商定稿 > 表单里写的 > 输入框里刚打的。
   为什么需要这一处兜底（2026-11-21 用户报"夜班总是显示需要仓库和需求"）：
   协商结果落在 <repo>/.sixcoder/clarify.json，界面重启/切仓库回来时由 loadClarify()
   恢复成 clarify.requirement，但 form.requirement 是内存字段、一直是空的；
   旧代码只认 form.requirement，于是按钮明明叫「开始夜班」却永远说没需求。 */
const nightRequirement = computed(() => {
  if (night.goalMode) return (night.goal || '').trim()
  return (clarify.requirement || form.requirement || clarify.input || '').trim()
})

async function startNight() {
  const auto = !!night.goalMode
  if (!form.repo) { errMsg.value = '夜班需要先选仓库：点顶部「仓库」选一个目录'; return }
  if (auto) {
    if (!night.goal.trim()) {
      errMsg.value = '全自主模式需要一句话目标：在「高级选项 → 模式」下面的框里写目标'; return
    }
  } else {
    const need = nightRequirement.value
    if (!need) {
      errMsg.value = '还缺需求：在输入框写清要做什么 → 点「开始分析」；远端确认细节后按钮才变成「开始夜班」'
      return
    }
    // 协商定稿写回表单：后面估算/接口/重开界面都用同一份，不再各说各话
    if (need !== form.requirement.trim()) form.requirement = need
  }
  if (!auto && !night.scale) {
    errMsg.value = '请选择目标代码规模（S/M/L/XL）——约束按规模分级，选错等于没约束'; return
  }
  night.busy = true; errMsg.value = ''; night.lines = []; night.delivery = ''
  night.logOffset = 0; night.logPath = ''      // 新一轮 → 从头读日志
  night.info = null; night.estimateText = ''; night.sysBudget = 0
  night.stopThreshold = 0; night.spent = null
  // ── 后台长跑：交给独立 watchdog 进程，关掉界面也不中断 ──
  // 为什么单独一条路：/v1/night 是在 server.py 线程里跑的，而 Electron 关窗会
  // backend.kill() → 夜班随之死；这里改成 spawn 独立进程 + 看门狗托管。
  if (night.detached) {
    try {
      const d = await api('/v1/night/detached', { method: 'POST', body: JSON.stringify({
        repo: form.repo,
        requirement: auto ? '' : nightRequirement.value,
        goal: auto ? night.goal : '' }) })
      night.taskId = ''
      night.detNote = d.already
        ? '已有夜班在跑（同一仓库），没有重复启动'
        : `已后台启动（PID ${d.pid}）· 任务来源：${d.goal_source} · 关掉窗口也会继续跑`
      night.info = { state: 'running', mode: d.already ? 'running' : 'detached',
                     policy: '', verified: 0, total: 0 }
      pollDetached()
      return
    } catch (e) {
      night.busy = false; errMsg.value = e.message; return
    }
  }
  try {
    const d = await api('/v1/night', { method: 'POST', body: JSON.stringify({
      repo: form.repo,
      requirement: auto ? '' : nightRequirement.value,
      goal: auto ? night.goal : '',
      model: form.model,
      plan: night.plan, scale: night.scale,
      remote_profile: night.remoteProfile }) })
    night.taskId = d.task_id
    night.info = { state: 'running', mode: d.mode, policy: d.policy, verified: 0, total: 0 }
    connectNight(d.task_id)
  } catch (e) { night.busy = false; errMsg.value = e.message }
}

/* 后台长跑的状态轮询：进度从磁盘读（coverage.jsonl / budget.json），
   所以关掉界面再打开也能看到刚才那一夜跑到哪了——不依赖 SSE。 */
/* 后台夜班没有 SSE：增量读日志喂「执行时间线」。
   2026-11-21 用户报"执行时间线不显示远端的回复了"——detached 路径原来根本没接
   `events`，所以整晚空白。这里每 2 秒取一次新行（按字节 offset，不重复不遗漏）。 */
async function pullNightLog() {
  if (!form.repo) return
  try {
    const q = new URLSearchParams({ repo: form.repo, offset: String(night.logOffset || 0) })
    const d = await api('/v1/night/detached/log?' + q.toString())
    if (!d.ok) return
    if (d.log && night.logPath && d.log !== night.logPath) {
      night.logOffset = 0                 // 换了一夜 → 从头读
      night.logPath = d.log
      pushEvent({ kind: 'notice', ts: '', payload: { message: '（换到新一轮夜班日志）' } })
      return
    }
    if (!night.logPath && d.log) night.logPath = d.log
    night.logOffset = d.offset || night.logOffset
    const now = new Date().toTimeString().slice(0, 8)
    for (const line of (d.lines || [])) {
      const m = String(line).match(/^\[(\d{2}:\d{2}:\d{2})\]/)
      pushEvent({ kind: 'night_log', ts: m ? m[1] : now, payload: { text: line } })
    }
    if (events.value.length > 800) events.value.splice(0, events.value.length - 800)
  } catch { /* 后端刚起/刚停，下轮再试 */ }
}

function stopNightLogTimer() {
  if (night.logTimer) { clearInterval(night.logTimer); night.logTimer = null }
}

async function pollDetached() {
  if (night.detTimer) { clearInterval(night.detTimer); night.detTimer = null }
  const tick = async () => {
    try {
      const wasRunning = night.busy
      const s = await api(`/v1/night/detached/status?repo=${encodeURIComponent(form.repo)}`)
      night.detStatus = s
      const running = !!(s.night && s.night.running)
      const wd = !!(s.watchdog && s.watchdog.alive)
      night.busy = running || wd
      if (s.budget) {
        night.sysBudget = s.budget.threshold || 0
        night.spent = s.budget.spent == null ? null : s.budget.spent
      }
      if (!running && !wd) {
        clearInterval(night.detTimer); night.detTimer = null
        stopNightLogTimer()
        night.busy = false
        if (wasRunning) maybeAutoPack()      // 收工：需求里要 exe 就自动打一个
      } else if (!night.logTimer) {
        pullNightLog()                       // 先补一次历史尾巴，再按 2 秒增量
        night.logTimer = setInterval(pullNightLog, 2000)
      }
    } catch (e) { /* 后端刚重启时忽略，下次轮询再试 */ }
  }
  await tick()
  night.detTimer = setInterval(tick, 15000)
}

/* 后台长跑的一行摘要（放函数里，模板里只插值，避免模板语法兼容问题） */
function detLine() {
  const s = night.detStatus
  if (!s) return ''
  const c = s.coverage || {}; const b = s.budget || {}; const n = s.night || {}
  const parts = []
  if (c.total) parts.push(`进度 ${c.done || 0}/${c.total}（${c.pct || 0}%）`)
  if (c.absent) parts.push(`红 ${c.absent}`)
  if (b.spent != null) parts.push(`花费 ¥${b.spent}`)
  if (b.threshold) parts.push(`停机线 ¥${b.threshold}`)
  parts.push(n.running ? '夜班在跑' : '夜班已停')
  return parts.join(' · ')
}

/* ---------------- 运维（原 START_NIGHT.bat / STOP_NIGHT.bat 的功能，2026-11-20 并进界面） ----------------
   检测 / 修复 / 生成契约都是"脱离界面跑"的独立进程：日志落磁盘，界面轮询尾巴。
   为什么不在请求线程里跑：full --repair 实测 10-15 分钟，占住 SSE 且关窗即死 ——
   和夜班踩的是同一个坑，所以走同一套「独立进程 + 轮询」模型。 */
const ops = reactive({ op: '', running: false, busy: false, log: '', elapsed: 0,
                       note: '', desc: '', timer: null })

/* ---------------- 打包 exe（甲方模式交付的最后一步，2026-11-22 用户口径）----------------
   "甲方模式能不能根据需要直接生成 exe"：夜班产出的是源码目录，这里把它打成一个
   双击就能跑的可执行文件。PyInstaller 要 1-3 分钟，所以走独立进程 + 状态轮询。 */
const pack = reactive({ busy: false, state: 'idle', exe: '', entry: '', kind: '',
                        log: '', logTail: '', timer: null, auto: true, note: '' })
const PACK_HINT = /exe|可执行|安装包|桌面(程序|软件|应用)|客户端|绿色版|双击/i

async function startPack(manual = true) {
  if (!form.repo) { if (manual) errMsg.value = '先选仓库'; return }
  if (pack.busy) { if (manual) errMsg.value = '正在打包，等它跑完'; return }
  pack.busy = true; pack.note = manual ? '正在准备打包…' : '夜班收工，自动打包 exe…'
  pack.log = ''; pack.logTail = ''; pack.exe = ''
  try {
    const d = await api('/v1/night/package', { method: 'POST',
      body: JSON.stringify({ repo: form.repo }) })
    if (d.already) { pack.note = d.note || '已经在打包了'; }
    else if (!d.ok) { pack.busy = false; pack.note = d.reason || '不能打包'; errMsg.value = pack.note; return }
    else { pack.entry = d.entry; pack.kind = d.kind; pack.note = d.note || '' }
    pollPack()
    if (!pack.timer) pack.timer = setInterval(pollPack, 2000)
  } catch (e) { pack.busy = false; pack.note = '[FAIL] ' + e.message }
}

async function pollPack() {
  if (!form.repo) return
  try {
    const d = await api(`/v1/night/package/status?repo=${encodeURIComponent(form.repo)}`)
    pack.state = d.state || 'idle'
    pack.logTail = d.log_tail || pack.logTail
    if (d.entry) pack.entry = d.entry
    if (d.kind) pack.kind = d.kind
    if (d.state === 'done') {
      pack.busy = false; pack.exe = d.exe || ''
      pack.note = `打包完成：${d.exe}（${((d.size || 0) / 1048576).toFixed(1)} MB）`
      pushEvent({ kind: 'notice', ts: '', payload: { message: pack.note } })
      stopPackTimer()
    } else if (d.state === 'failed') {
      pack.busy = false
      pack.note = '打包失败：' + (d.reason || '见日志')
      pushEvent({ kind: 'notice', ts: '', payload: { message: pack.note } })
      stopPackTimer()
    } else if (d.running) {
      pack.busy = true
      pack.note = '正在打包（PyInstaller，1-3 分钟）…'
    } else if (pack.busy) {
      pack.busy = false; stopPackTimer()
    }
  } catch { /* 后端重启时忽略 */ }
}

function stopPackTimer() {
  if (pack.timer) { clearInterval(pack.timer); pack.timer = null }
}

function openPackDir() {
  if (!pack.exe) return
  try { shellApi?.showInFolder?.(pack.exe) } catch { /* 浏览器里没有原生桥 */ }
}

/* 夜班收工且需求里提到"exe/程序/安装包"→ 自动打包（只触发一次） */
function maybeAutoPack() {
  if (!pack.auto || pack.busy || pack.state === 'done') return
  const text = `${nightRequirement.value || ''} ${clarify.render || ''}`
  if (!PACK_HINT.test(text)) return
  startPack(false)
}
const OPS_LABEL = { report: '检测', repair: '检测+修复', contract: '生成契约' }

async function startOps(op) {
  if (!form.repo) { errMsg.value = '运维任务需要先选仓库'; return }
  if (ops.running) { errMsg.value = '已有运维任务在跑，等它结束再起下一个'; return }
  ops.busy = true; ops.note = ''; ops.log = ''
  try {
    const d = await api('/v1/ops/start', { method: 'POST',
      body: JSON.stringify({ repo: form.repo, op }) })
    ops.op = d.op || op
    ops.running = !!d.pid && !d.already
    ops.desc = d.desc || ''
    ops.note = d.already ? `已有运维任务在跑（${OPS_LABEL[d.op] || d.op}）` : `已启动：${OPS_LABEL[op] || op}`
    if (ops.running) startOpsTimer()
  } catch (e) { errMsg.value = e.message } finally { ops.busy = false }
}

async function pollOps() {
  try {
    const d = await api('/v1/ops/status')
    ops.running = !!d.running
    ops.op = d.op || ops.op
    ops.desc = d.desc || ops.desc
    ops.elapsed = d.elapsed_s || 0
    ops.log = d.log_tail || ''
    if (!ops.running && ops.timer) { stopOpsTimer(); ops.note = '运维任务已结束' }
  } catch { /* 后端刚重启时忽略，下次轮询再试 */ }
}

function startOpsTimer() {
  if (ops.timer) return
  pollOps()
  ops.timer = setInterval(pollOps, 3000)
}
function stopOpsTimer() {
  if (ops.timer) { clearInterval(ops.timer); ops.timer = null }
}

/* 停夜班：替代 STOP_NIGHT.bat。先停看门狗再停夜班进程（后端保证顺序），
   否则看门狗会把你刚停掉的夜班又拉起来。 */
async function stopNight() {
  ops.busy = true; night.detNote = ''
  try {
    const d = await api('/v1/night/stop', { method: 'POST',
      body: JSON.stringify({ repo: form.repo || '' }) })
    night.detNote = d.count ? `已停止 ${d.count} 个进程` : (d.note || '没有在跑的夜班')
    if (night.detTimer) { clearInterval(night.detTimer); night.detTimer = null }
    stopNightLogTimer()
    night.detStatus = null
    night.busy = false
    night.info = null
    await pollNightOnce()
  } catch (e) { errMsg.value = e.message } finally { ops.busy = false }
}

async function pollNightOnce() {
  if (!form.repo) return
  try {
    night.detStatus = await api(`/v1/night/detached/status?repo=${encodeURIComponent(form.repo)}`)
  } catch { }
}

/* 在系统浏览器里打开当前界面（原 start.ps1 -Web 的等价物） */
function openInBrowser() {
  const url = location.origin
  try { window.open(url, '_blank') } catch { errMsg.value = '浏览器被拦截，请手动复制地址：' + url }
}

function opsElapsed() {
  const s = Number(ops.elapsed) || 0
  return s >= 60 ? `${Math.floor(s / 60)} 分 ${s % 60} 秒` : `${s} 秒`
}


/* 两步法第一步：只构建蓝图 + 估算花费，不跑夜班；单价与系统预算都在 .sixcoder/budget.json */async function estimateNight() {
  const text = nightRequirement.value
  if (!form.repo || !text) {
    errMsg.value = form.repo
      ? '还缺目标/需求：先在输入框写清要做什么（或写一句话目标）再估算'
      : '请先选择仓库再估算花费'; return
  }
  night.estimateBusy = true; night.estimateText = ''
  try {
    const d = await api('/v1/night/estimate', { method: 'POST', body: JSON.stringify({
      repo: form.repo,
      requirement: night.goalMode ? '' : text,
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
    // 同步进「执行时间线」：夜班的每一步也要能在主时间线里看到（不只是小面板）
    pushEvent({ kind: ev.kind, ts: ev.ts, payload: ev.payload })
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
    // 模式切换自动换中栏：开发者模式有打开文件 → 代码 IDE；其余模式 → 工作台。
    // 只切视图，标签和未保存内容都留在 edTabs 里。
    stashActiveTab()
    midView.value = (currentMode.value?.id === 'developer' && edTabs.value.length) ? 'code' : 'work'
  } catch (e) { errMsg.value = e.message }
}

/* ---------------- 代码审查 / 改进意见（只读模式：只有这一个动作） ----------------
   2026-11-20 用户口径："审查模式只有一个审查按钮即可不需要文本框，只是实现审查功能"。
   所以这里不再要求"预期功能"：没有预期就走确定性规则 + 与现有规格比对；
   有预期（例如从历史任务带过来的需求）才拿它当验收口径。 */
const reviewUseModel = ref(true)
const reviewBusy = ref(false)
const reviewResult = ref(null)

async function runReview() {
  if (!form.repo && !taskInfo.value?.id) { errMsg.value = '请先选择仓库（或用历史任务载入一个工作区）'; return }
  reviewBusy.value = true
  reviewResult.value = null
  errMsg.value = ''
  try {
    reviewResult.value = await api('/v1/review', {
      method: 'POST',
      body: JSON.stringify({
        repo: taskInfo.value?.id ? '' : form.repo,
        task_id: taskInfo.value?.id || '',
        // 空串 = 没有预期口径，只做确定性规则（后端按空处理，不报错）
        expectation: (form.requirement || '').trim(),
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
    case 'night_log': return p.text
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
  // 编辑器快捷键（Ctrl+S 保存 / Esc 关闭）：只在编辑器打开时接管
  window.addEventListener('keydown', onEditorKey)
  // 运维任务可能在关掉界面前就启动了：打开界面立刻读一次状态，别显示成"没在跑"
  startOpsTimer()
})

let modelsTimer = null
function onWindowFocus() { loadModels() }
function onVisibilityChange() { if (!document.hidden) loadModels() }

onUnmounted(() => {
  if (modelsTimer) clearInterval(modelsTimer)
  if (approvalTimer) clearInterval(approvalTimer)
  stopOpsTimer()
  if (es) { es.close(); es = null }
  saveSession()
  window.removeEventListener('focus', onWindowFocus)
  window.removeEventListener('beforeunload', saveSession)
  window.removeEventListener('pagehide', saveSession)
  document.removeEventListener('visibilitychange', onVisibilityChange)
  window.removeEventListener('keydown', onEditorKey)
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
          <td class="mono" style="width:74px">{{ t.label || t.id }}</td>
          <td style="width:20px">
            <span v-if="t.ok" class="badge b-ok" :title="t.source || ''">✓</span>
            <span v-else-if="t.installed" class="badge b-warn" :title="t.source || ''">!</span>
            <span v-else class="badge b-bad">✗</span>
          </td>
          <td class="mono dim" style="width:52px">{{ t.version || '未安装' }}</td>
          <td class="mono dim wrap" style="font-size:9.5px">{{ t.path || '未找到' }}</td>
          <td style="width:82px">
            <span class="badge" :class="t.added ? 'b-ok' : (t.bound ? 'b-warn' : 'b-bad')">
              {{ t.bind_label || (t.bound ? '系统 PATH' : '未绑定') }}
            </span>
          </td>
          <td style="width:44px">
            <button v-if="isShell && can('toolchain.config') && t.bindable && !t.bound" class="ghost tiny"
                    :title="'加入当前用户 PATH：' + (t.bind_dir || t.id)" @click="togglePathBind(t)">绑定</button>
            <button v-else-if="isShell && can('toolchain.config') && t.added" class="ghost tiny"
                    :title="'从当前用户 PATH 移除 ' + (t.bind_dir || t.id)" @click="togglePathBind(t)">解绑</button>
            <button v-else-if="isShell && can('toolchain.config')" class="ghost tiny"
                    :title="'指定 ' + t.id + ' 目录'" @click="pickToolchainDir(t)">…</button>
          </td>
        </tr>
      </table>
      <div v-if="!isShell" class="dim" style="font-size:10px;margin-top:4px">
        「指定目录」需桌面版外壳（Electron）；网页版可编辑 <code>data/toolchains.json</code>
      </div>
      <div class="dim" style="font-size:10px;margin-top:4px">
        PATH 默认不绑定（安装包下载的运行时也如此）；点「绑定」只改当前用户环境变量，卸载时会自动移除。
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
            <!-- 后端返回异常/旧缓存时 signals 可能整个缺失；直接读 project_venv 会抛
                 TypeError 把整棵树渲染打断（页面看起来"只有背景"）。这里先挡一层。 -->
            <template v-if="probeInfo.signals">
            <span v-if="probeInfo.signals.project_venv" class="badge b-ok" style="margin-left:6px"
                  :title="'测试用项目自带解释器：' + probeInfo.signals.python">项目 .venv</span>
            <span v-else class="badge b-dim" style="margin-left:6px"
                  :title="'测试用 SixCoder 自带 Python；项目依赖缺失时请建 .venv 或装 requirements'">引擎 Python</span>
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
            <!-- 收集失败别只给一句"失败"：把 pytest 最后几行和怎么办直接摊开 -->
            <div v-if="probeInfo.signals.baseline === 'collect-failed'" class="probe-diag">
              <div v-if="probeInfo.signals.collect_reason" class="probe-reason">
                {{ probeInfo.signals.collect_reason }}</div>
              <pre v-if="probeInfo.signals.collect_tail" class="probe-tail">{{ probeInfo.signals.collect_tail }}</pre>
              <div class="dim" style="font-size:10px;line-height:1.6">
                在仓库目录跑 <b>python -m pytest --collect-only -q</b> 可看完整报错；
                一般是 import 不到依赖、conftest 报错或测试文件语法错。
              </div>
            </div>
            </template>
            <span v-else class="dim" style="font-size:10.5px">
              探测结果缺少细节（signals 为空），点「探测」重试
            </span>
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
                <div class="tree-row" :class="{ dir: n.is_dir, 'is-editing': isEditing(n) }"
                     @click="n.is_dir ? toggleDir(n) : openFile(n)">
                  <span class="tree-caret">{{ n.is_dir ? (treeExpanded[n.path] ? '▾' : '▸') : '' }}</span>
                  <span class="tree-icon">{{ n.is_dir ? '📁' : '📄' }}</span>
                  <span class="tree-name">{{ n.name }}</span>
                  <span v-if="!n.is_dir" class="dim" style="font-size:9.5px">{{ n.size }}</span>
                </div>
                <template v-if="n.is_dir && treeExpanded[n.path]">
                  <div v-for="c in treeChildrenOf(n.path)" :key="c.path" class="tree-node" :style="{ paddingLeft: '14px' }">
                    <div class="tree-row" :class="{ dir: c.is_dir, 'is-editing': isEditing(c) }"
                         @click="c.is_dir ? toggleDir(c) : openFile(c)">
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
            点目录展开，点文件在这里直接打开（可改，Ctrl+S 保存）。
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
      <div v-show="midView === 'work'" class="panel" style="display:flex;align-items:center;gap:8px">
        <div class="panel-title" style="margin:0">执行时间线</div>
          <span class="spacer"></span>
          <span class="pill" v-if="testSpecs.length">测试规约 {{ testSpecs.length }}</span>
        </div>
        <div v-show="midView === 'work'" class="timeline" ref="timelineEl">
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
        <div v-show="midView === 'work'" class="panel">
          <div class="panel-title" style="margin-bottom:6px">
            需求
            <span class="spacer"></span>
            <span class="badge b-acc">{{ currentMode?.name || '—' }}</span>
          </div>

          <!-- ============ 夜班模式：一个文本框 + 一个按钮 + 一段对话 ============
               2026-11-20 用户口径：夜班不是"填个需求就开跑"，而是远端先把实现细节与
               技术栈问清楚，它自己确认"能完全做完"了才开工。下面就是这段协商。
               消息流常驻（看得见它问了什么、你答了什么），待定项渲染成多选。 -->
          <template v-if="isClientMode">
            <div v-if="clarify.msgs.length" class="chat-flow">
              <div v-for="(m, i) in clarify.msgs" :key="i" class="chat-msg" :class="m.role">
                <span class="chat-role">{{ m.role === 'user' ? '你' : (m.role === 'system' ? '系统' : '远端') }}</span>
                <div class="chat-text">{{ m.text }}</div>
              </div>
            </div>
            <div v-else class="chat-empty">
              写下你要做的东西（要做成什么样、给谁用、必须有哪些功能）。<br />
              点「开始分析」后远端会读需求、定技术栈、把还没定的几件事列成选项问你 ——
              它说"够了"之前不会开工，避免整夜的钱押在猜错上。
            </div>

            <div v-if="clarify.busy" class="chat-wait">
              远端分析中… {{ clarify.elapsedS }}s
              <span class="dim">（首轮约 1 分钟：它在拆需求、选技术栈、列待定项）</span>
            </div>

            <!-- 待定项：每项单选 + 一键按推荐 -->
            <div v-if="clarify.questions.length && !clarify.busy" class="qbox">
              <div class="qbox-head">
                远端还想确认这几件事
                <span class="dim" style="font-size:10.5px">（不选就按它的推荐来）</span>
              </div>
              <div v-for="q in clarify.questions" :key="q.key" class="qitem">
                <div class="qtitle">{{ q.question }}</div>
                <label v-for="(o, oi) in q.options" :key="oi" class="qopt">
                  <input type="radio" :name="'cq-' + q.key" v-model="clarify.answers[q.key]" :value="o" />
                  <span>{{ o }}</span>
                </label>
              </div>
              <div class="row" style="gap:8px;margin-top:8px">
                <button :disabled="clarify.busy || !can('agent.run')" @click="answerClarify(false)">回答并继续</button>
                <button class="ghost tiny" :disabled="clarify.busy || !can('agent.run')" @click="answerClarify(true)">都按推荐来</button>
              </div>
            </div>

            <!-- 输入区：只有它一个文本框 -->
            <textarea v-model="clarify.input" class="clarify-input" :disabled="clarify.busy"
                      :placeholder="clarify.started
                        ? '补充或修改要求（改主意也在这里说）；想换一件事就点「重新协商」｜Ctrl+Enter'
                        : '要做什么、给谁用、必须有哪些功能。不用写技术栈 —— 远端会问你｜Ctrl+Enter 发送'"
                      @keydown.ctrl.enter.prevent="clarify.ready ? startNight() : startClarify()"></textarea>

            <div class="row" style="margin-top:8px;gap:10px;flex-wrap:wrap;align-items:center">
              <button v-if="clarify.ready" class="primary-lg" :disabled="night.busy || !can('agent.run')"
                      title="远端已确认可以做完，按协商出来的规格开工"
                      @click="startNight">{{ night.busy ? '夜班进行中…' : '开始夜班' }}</button>
              <button v-else class="primary-lg"
                      :disabled="clarify.busy || !can('agent.run') || !(clarify.input.trim() || clarify.requirement)"
                      :title="clarify.started ? '用新描述重新分析（已选的待定项会保留）' : '让远端读需求并列出待定项'"
                      @click="startClarify">{{ clarify.busy ? '远端分析中…' : (clarify.started ? '重新描述需求' : '开始分析') }}</button>
              <span v-if="clarify.note" class="dim" style="font-size:10.5px">{{ clarify.note }}</span>
              <span class="spacer"></span>
              <button v-if="clarify.started" class="ghost tiny" :disabled="clarify.busy"
                      @click="resetClarify">重新协商</button>
            </div>

            <!-- 高级：不抢戏，但都在（后台长跑 / 规模 / 全自主 / 估算 / 帮助） -->
            <details class="adv">
              <summary>高级选项（后台长跑、代码规模、全自主规划、估算花费）</summary>
              <div class="row" style="gap:14px;align-items:center;flex-wrap:wrap;margin-top:8px">
                <label class="dim" style="font-size:11px;display:flex;align-items:center;gap:4px"
                       title="勾上后夜班跑在独立进程里（看门狗 + 巡检托管）：关掉界面不中断、崩溃自动重启">
                  <input type="checkbox" v-model="night.detached" :disabled="night.busy" />
                  后台长跑（关界面不中断）
                </label>
                <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px">
                  规模
                  <select v-model="night.scale" style="width:180px">
                    <option v-for="t in night.scaleTiers" :key="t.id" :value="t.id">{{ t.label }}</option>
                  </select>
                </label>
                <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px"
                       title="需求模式：你指定范围，系统只做给定契约｜目标模式：一句话目标，切片与预算全由系统规划">
                  模式
                  <select v-model="night.goalMode" style="width:150px">
                    <option :value="false">需求（我指定范围）</option>
                    <option :value="true">目标（全自主规划）</option>
                  </select>
                </label>
                <button class="ghost tiny" :disabled="night.busy || night.estimateBusy || !can('agent.run')
                        || !form.repo || !nightRequirement"
                        title="先构建蓝图并估算要花多少钱（不跑夜班）"
                        @click="estimateNight">{{ night.estimateBusy ? '估算中…' : '估算花费' }}</button>
                <button class="ghost tiny" :disabled="night.busy" @click="loadNight()">刷新进度</button>
                <button class="ghost tiny" :disabled="night.busy"
                        @click="settingsTab = 'help'; showSettings = true">帮助</button>
              </div>
              <textarea v-if="night.goalMode" v-model="night.goal" style="min-height:70px;margin-top:8px"
                        placeholder="一句话目标，例如「做一个 CSV 记账工具：能导入、按月汇总、导出报表」"></textarea>
              <div v-if="night.detNote" class="dim" style="font-size:11px;margin-top:6px">{{ night.detNote }}</div>
              <div v-if="night.detStatus" class="dim" style="font-size:11px;margin-top:4px;line-height:1.5">
                {{ detLine() }}
              </div>
              <div v-if="night.estimateText"
                   style="margin-top:6px;border:1px solid var(--line);border-radius:6px;padding:8px;white-space:pre-wrap;font-size:11px">{{ night.estimateText }}</div>
            </details>
          </template>

          <!-- ============ 开发者模式：主界面不放需求框 ============
               用户口径（2026-11-20）：开发者模式的输入只有一个地方 —— 用自带编辑器打开文件后
               浮出来的那个对话框。主界面再放一个文本框会让人不知道"到底在跟谁说话"。 -->
          <div v-else-if="!isReadonlyMode" class="dev-hint">
            <div class="dev-hint-title">在编辑器里对话</div>
            <div class="dev-hint-body">
              左侧「文件」点开任意文件 → 编辑器右上角浮出对话框：
              写你要它做什么（例如「给这个函数加参数校验」「把这段改成异步」），
              选 <b>本地</b> 或 <b>远端</b>，点发送。它会按你的选区或光标位置生成代码，
              插进去后可以继续改，Ctrl+S 保存。
            </div>
            <details class="adv">
              <summary>按需求整任务生成（旧的单任务入口，仍可用）</summary>
              <textarea v-model="form.requirement" style="min-height:90px;margin-top:8px"
                        placeholder="写清改哪个文件、要什么行为、异常怎么处理｜Ctrl+Enter 发送"
                        @keydown.ctrl.enter.prevent="onSendShortcut"></textarea>
              <div class="row" style="margin-top:8px;gap:10px">
                <button :disabled="busy || !can('agent.run')" @click="startTask">{{ busy ? '执行中…' : '发送' }}</button>
              </div>
            </details>
          </div>

          <!-- ============ 只读模式：只有一个审查按钮 ============ -->
          <div v-else class="row" style="gap:10px;align-items:center">
            <button class="primary-lg" :disabled="reviewBusy || !can('review.run')"
                    title="确定性规则 + 与现有规格比对，只出意见，不改任何文件"
                    @click="runReview">{{ reviewBusy ? '审查中…' : '审查' }}</button>
            <span class="dim" style="font-size:10.5px">
              只做审查：不给输入框，也不改任何文件；结果在下方
            </span>
          </div>

          <!-- 运维（原 START_NIGHT.bat / STOP_NIGHT.bat / start.ps1 -Web 的功能，全部并进界面）：
               检测=零 token 只出报告；检测+修复=只改有错处；契约=生成全项目契约。
               三个都是脱离界面的独立进程，日志落磁盘，这里轮询尾巴。 -->
          <div v-if="isClientMode" class="row"
               style="margin-top:8px;gap:8px;flex-wrap:wrap;align-items:center">
            <span class="dim" style="font-size:10.5px">运维</span>
            <button class="ghost tiny"
                    :disabled="ops.running || ops.busy || !form.repo || !can('agent.run')"
                    title="只跑检测器出 .sixcoder/errors_report.md，零 token、不改任何代码"
                    @click="startOps('report')">检测（零 token）</button>
            <button class="ghost tiny"
                    :disabled="ops.running || ops.busy || !form.repo || !can('agent.run')"
                    title="检测 + 只改有错处（会花 token，实测 10-15 分钟）"
                    @click="startOps('repair')">检测 + 修复</button>
            <button class="ghost tiny"
                    :disabled="ops.running || ops.busy || !form.repo || !can('agent.run')"
                    title="从现有代码反推全项目契约并锁定（慢，可续跑；会丢旧缓存）"
                    @click="startOps('contract')">生成契约</button>
            <button class="ghost tiny" :disabled="pack.busy || !form.repo || !can('agent.run')"
                    title="把当前仓库打成可双击运行的 exe（PyInstaller onefile，1-3 分钟）"
                    @click="startPack()">{{ pack.busy ? '打包中…' : '打包 exe' }}</button>
            <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:3px"
                   title="夜班收工后，若需求里提到 exe/程序/安装包，就自动打包一次">
              <input type="checkbox" v-model="pack.auto" />收工后自动打包
            </label>
            <button class="ghost tiny" :disabled="ops.busy || !can('agent.run')"
                    title="停掉夜班与看门狗：先停看门狗再停夜班进程，不会杀了又自己起来"
                    @click="stopNight">停止夜班</button>
            <button class="ghost tiny" title="用系统浏览器打开当前界面"
                    @click="openInBrowser">在浏览器打开</button>
            <span v-if="ops.running" class="badge b-warn">
              {{ OPS_LABEL[ops.op] || ops.op }} 运行中 · {{ opsElapsed() }}</span>
            <span v-else-if="ops.note" class="dim" style="font-size:10.5px">{{ ops.note }}</span>
          </div>
          <div v-if="isClientMode && (ops.log || ops.running)"
               style="margin-top:6px;border:1px solid var(--line);border-radius:6px;padding:8px;
                      max-height:170px;overflow:auto;font-family:Consolas,monospace;
                      font-size:11px;white-space:pre-wrap;background:var(--panel2)">{{ ops.log || '（等待输出…）' }}</div>

          <!-- 打包 exe：状态 + 产物路径 + 日志尾巴 -->
          <div v-if="isClientMode && (pack.note || pack.busy || pack.exe)" style="margin-top:6px">
            <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
              <span class="badge" :class="pack.state === 'done' ? 'b-ok'
                    : (pack.state === 'failed' ? 'b-bad' : 'b-warn')">
                {{ pack.state === 'done' ? 'exe 已生成' : (pack.state === 'failed' ? '打包失败' : '打包中') }}</span>
              <span v-if="pack.entry" class="dim mono" style="font-size:10.5px">
                入口 {{ pack.entry }}（{{ pack.kind }}）</span>
              <span class="dim" style="font-size:10.5px">{{ pack.note }}</span>
              <button v-if="pack.exe" class="ghost tiny" @click="openPackDir">打开所在目录</button>
            </div>
            <pre v-if="pack.logTail"
                 style="margin-top:6px;max-height:140px;overflow:auto;border:1px solid var(--line);
                        border-radius:6px;padding:8px;font-family:Consolas,monospace;
                        font-size:10.5px;white-space:pre-wrap;background:var(--panel2)">{{ pack.logTail }}</pre>
          </div>

          <!-- 夜班进度 + 交付报告（甲方模式的主舞台） -->
          <div v-if="isClientMode && (night.lines.length || night.delivery || night.info || night.detStatus)" style="margin-top:8px">
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
            <!-- 后台长跑没有 SSE：进度从磁盘读（夜班自己的日志尾巴），
                 重开软件也能看见"它刚才在干什么"，而不是一片空白。 -->
            <pre v-else-if="night.detStatus?.night_log_tail"
                 style="max-height:180px;overflow:auto;margin-top:6px;border:1px solid var(--line);
                        border-radius:6px;padding:8px;font-family:Consolas,monospace;
                        font-size:10.5px;white-space:pre-wrap;background:var(--panel2)">{{ night.detStatus.night_log_tail }}</pre>
            <div v-if="night.detStatus?.recent_files?.length" class="dim"
                 style="font-size:10.5px;margin-top:6px;line-height:1.7">
              <b>最近写出的文件</b>（1 小时内）：
              <span v-for="(f, i) in night.detStatus.recent_files" :key="i" class="mono"
                    style="margin-right:10px">{{ f.mtime }} {{ f.path }} ({{ f.size }}B)</span>
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
      <!-- ============ 内置编辑器：直接嵌在中栏（不再 fixed 全屏盖住文件树） ============ -->
      <div v-if="ed.open && midView === 'code'" class="editor-overlay">
        <!-- 多标签：点标签切换文件，✕ 只关当前标签；未保存的点标记提示 -->
        <div class="editor-tabs">
          <div v-for="t in edTabs" :key="t.path" class="editor-tab"
               :class="{ active: t.path === ed.path }" @click="switchTab(t.path)"
               :title="t.path">
            <span class="tab-name">{{ tabLabel(t.path) }}</span>
            <span v-if="tabDirty(t)" class="tab-dirty" title="未保存">●</span>
            <button class="tab-close" title="关闭这个标签" @click.stop="closeTab(t.path)">✕</button>
          </div>
          <span class="spacer"></span>
          <button class="ghost tiny" title="回到工作台（标签和未保存内容都保留）"
                  @click="stashActiveTab(); midView = 'work'">工作台</button>
        </div>
        <div class="editor-head">
          <span class="badge b-acc mono">{{ ed.path }}</span>
          <span v-if="edDirty" class="badge b-warn">未保存</span>
          <span v-else-if="ed.savedAt" class="badge b-ok">{{ ed.savedAt }}</span>
          <span class="dim" style="font-size:10.5px">{{ ed.info }}</span>
          <span class="spacer"></span>
          <label class="dim" style="font-size:10.5px;display:flex;align-items:center;gap:4px">
            <input type="checkbox" v-model="ed.wrap" style="width:auto" />自动换行
          </label>
          <button v-if="!ed.binary" class="ghost tiny" data-testid="editor-run" :disabled="run.busy || !!run.session"
                  :title="run.lang ? `用 ${run.tool}${run.toolVersion ? ' ' + run.toolVersion : ''} 运行（自动识别语言/工具链）` : '识别中…'"
                  @click="runCurrent()">{{ run.busy && !run.session ? '运行中…' : '▶ 运行' }}</button>
          <!-- 调试/定位：下拉选当前文件的函数，不用先选中文字（用户：不方便） -->
          <select v-if="!ed.binary && nav.symbolHints.length" v-model="nav.symbolPick"
                  class="sym-select" title="选一个函数，再点定位或调试；断点直接点左边行号">
            <option v-for="h in nav.symbolHints" :key="h.name" :value="h.name">
              {{ h.name }} · {{ h.line }} 行
            </option>
          </select>
          <button v-if="!ed.binary" class="ghost tiny" data-testid="editor-locate-symbol"
                  :disabled="nav.busy || !activeSymbol()" :title="'定位函数：' + (activeSymbol() || '选个函数或选中函数名')"
                  @click="locateSymbol(activeSymbol())">⌖ 定位</button>
          <button v-if="!ed.binary" class="ghost tiny" data-testid="editor-debug-symbol"
                  :disabled="nav.busy || !!run.session || !activeSymbol()"
                  :title="'在该函数入口下断点并启动调试（Python）：' + (activeSymbol() || '选个函数或选中函数名')"
                  @click="debugSymbol(activeSymbol())">⚑ 调试</button>
          <button v-if="!ed.binary" class="ghost tiny" data-testid="editor-debug" :class="{ active: !!run.session }"
                  :disabled="!!(run.busy && !run.session)"
                  :title="run.debug ? '断点调试：下断点后继续，或单步/看变量（Python）' : (run.reason || '当前语言不支持断点，仅能运行看诊断')"
                  @click="run.session ? stopRunDebug() : startDebug()">
            {{ run.session ? '■ 停止调试' : (run.debug ? '调试' : '调试(仅Python)') }}</button>
          <button class="ghost tiny" @click="copyFilePath" title="复制文件绝对路径">复制路径</button>
          <button class="ghost tiny" @click="openFileExternally"
                  title="交给系统默认编辑器（需要 IDE 插件或看 diff 时用）">系统编辑器</button>
          <button class="ghost tiny" :disabled="edRollbackBusy || ed.binary"
                  title="把当前文件回滚到上一次保存前（沙箱备份，每文件最多 3 份）"
                  @click="rollbackCurrentFile">{{ edRollbackBusy ? '回滚中…' : '回滚此文件' }}</button>
          <button class="ghost tiny" :class="{ active: edChat.open }" :disabled="ed.binary"
                  title="打开对话面板：写你要它做什么，选本地或远端，点发送（Ctrl+Space 直达）"
                  @click="edChat.open ? (edChat.open = false) : openEditorChat()">
            {{ edChat.open ? '收起对话' : 'AI 对话' }}</button>
          <button class="ghost tiny" :disabled="!edDirty || ed.saving || ed.binary" @click="saveFile">
            {{ ed.saving ? '保存中…' : '保存 Ctrl+S' }}</button>
          <button class="ghost tiny" @click="closeEditor()">关闭 Esc</button>
        </div>
        <!-- 选中即改：选中代码后浮出动作条；结果面板给 diff/解释/引用 -->
        <div v-if="!ed.binary && (assist.hasSel || assist.open)" class="assist-bar">
          <span class="dim" style="font-size:10.5px">选中 {{ assist.selected.length }} 字符</span>
          <button class="ghost tiny" @click="openAssist('explain')">💡 解释</button>
          <button class="ghost tiny" @click="openAssist('edit')">✎ 改</button>
          <button class="ghost tiny" @click="openAssist('test')">🧪 补测试</button>
          <button class="ghost tiny" @click="openAssist('refs')">🔗 找引用</button>
          <span class="spacer"></span>
          <div v-if="assist.open" class="seg">
            <button :class="{ on: edChat.source === 'local' }" @click="setFillSource('local')">本地</button>
            <button :class="{ on: edChat.source === 'remote' }" @click="setFillSource('remote')">远端</button>
          </div>
          <button v-if="assist.open" class="ghost tiny" @click="closeAssist()">收起</button>
        </div>
        <div v-if="!ed.binary && assist.open" class="assist-panel">
          <div class="assist-head">
            <span class="badge b-acc">{{ ASSIST_LABEL[assist.action] || assist.action }}</span>
            <span v-if="assist.busy" class="badge b-warn">生成中…</span>
            <span v-else-if="assist.model" class="dim" style="font-size:10px">{{ assist.model }} · {{ assist.ms }}ms</span>
            <span class="spacer"></span>
            <button v-if="assist.result" class="ghost tiny" @click="copyAssist()">复制</button>
            <button v-if="assist.action === 'edit' && assist.result" class="ghost tiny" @click="acceptAssist()">接受改动</button>
            <button v-if="assist.action === 'test' && assist.result" class="ghost tiny" @click="acceptAssist()">插入到文件末尾</button>
            <button v-if="assist.result" class="ghost tiny" @click="assist.result = ''; assist.error = ''">重来</button>
          </div>
          <div v-if="assist.action === 'edit' && !assist.result" class="assist-prompt">
            <input class="assist-input" v-model="assist.instruction"
                   placeholder="想怎么改？例如：先校验再入库；保持函数签名不变"
                   @keydown.enter.prevent="runAssist()" />
            <button class="ghost tiny" :disabled="assist.busy" @click="runAssist()">生成改动</button>
          </div>
          <div v-if="assist.error" class="assist-err">{{ assist.error }}</div>
          <div v-if="assist.action === 'refs'" class="assist-refs">
            <div v-for="(r, i) in assist.refs" :key="i" class="nav-result" @click="pickNavTarget(r)">
              <span class="mono b-acc">{{ r.name }}</span>
              <span class="dim">{{ r.path }}:{{ r.line }}</span>
              <span class="badge b-dim">{{ r.kind }}</span>
            </div>
            <div v-if="!assist.refs.length && !assist.busy" class="empty">没有引用</div>
          </div>
          <template v-else-if="assist.result">
            <div v-if="assist.action === 'edit'" class="assist-diff">
              <div v-for="(l, i) in assistDiff(assist.selected, assist.result)" :key="i" :class="'d-' + l.t">
                <span class="d-sign">{{ l.t === 'add' ? '+' : (l.t === 'del' ? '-' : ' ') }}</span>{{ l.x || ' ' }}
              </div>
            </div>
            <pre v-else class="assist-text">{{ assist.result }}</pre>
          </template>
          <div v-else-if="assist.busy" class="empty" style="padding:10px">模型生成中…</div>
        </div>
        <div v-if="ed.err" class="alert-inline" style="margin:6px 10px">{{ ed.err }}</div>
        <div v-if="ed.loading" class="empty" style="padding:24px">正在读取…</div>
        <div v-else-if="ed.binary" class="empty" style="padding:24px">
          这是二进制文件，界面里不能编辑。点右上角「系统编辑器」打开。
        </div>
        <div v-else class="editor-body">
          <!-- 断点直接点行号（VSCode 手感）：红点=已下断点，黄三角=当前暂停 -->
          <div ref="gutterRef" class="editor-gutter" data-testid="editor-breakpoint"
               title="点行号下/取消断点（模拟 VSCode）">
            <div v-for="n in edLines" :key="n" class="gutter-line"
                 :class="{ bp: run.breakpoints.includes(n), paused: run.paused?.line === n }"
                 :title="'第 ' + n + ' 行：点击切换断点'"
                 @click="toggleBreakpointAt(n)">
              <span class="bp-dot"></span><span class="ln">{{ n }}</span>
            </div>
          </div>
          <textarea ref="edRef" v-model="ed.text" class="editor-area"
                    :class="{ wrap: ed.wrap }" spellcheck="false"
                    @select="refreshSelection" @mouseup="refreshSelection" @keyup="refreshSelection"
                    @scroll="syncGutter" @keydown="onEditorKeydown"></textarea>
        </div>
        <!-- 运行/调试输出面板：底部浮层，可折叠。诊断行点击跳到编辑器对应行。 -->
        <div v-if="!ed.binary && run.open" class="run-panel">
          <div class="run-bar">
            <button class="ghost tiny" @click="run.open = false">收起输出 ✕</button>
            <span class="dim mono" style="font-size:10px">{{ run.lang || '—' }}
              <template v-if="run.tool"> · {{ run.tool }}{{ run.toolVersion ? ' ' + run.toolVersion : '' }}</template>
            </span>
            <span v-if="run.busy" class="badge b-warn">{{ run.session ? '调试中' : '运行中' }}…</span>
            <span v-else-if="run.ok === true" class="badge b-ok">成功</span>
            <span v-else-if="run.ok === false" class="badge b-bad">失败</span>
            <span v-if="run.cwd" class="dim" style="font-size:10px" :title="run.cwd">cwd: {{ run.cwd.split(/[\\/]/).pop() }}</span>
            <span class="spacer"></span>
            <span v-if="run.ms" class="dim mono" style="font-size:10px">{{ run.ms }}ms</span>
            <span v-if="run.rc !== null" class="dim mono" style="font-size:10px">rc={{ run.rc }}</span>
          </div>
          <!-- 运行参数（运行前填）；调试会话中也显示，方便看用了什么命令 -->
          <div v-if="!run.session" class="run-args">
            <label class="dim" style="font-size:10.5px">参数</label>
            <input v-model="run.args" class="run-args-input" placeholder="命令行参数，如 --verbose x=1（空格分隔）"
                   @keydown.enter.prevent="runCurrent()" />
            <label class="dim" style="font-size:10.5px">超时</label>
            <input v-model.number="run.timeout" type="number" min="1" max="300" class="run-args-num" />
            <span class="dim">s</span>
          </div>
          <!-- 调试控制条 -->
          <div v-if="run.session" class="dbg-bar">
            <span class="badge b-acc">会话 {{ run.session.slice(0, 8) }}</span>
            <button class="ghost tiny" :disabled="!run.paused" @click="dbgCmd('continue')">▶ 继续 F5</button>
            <button class="ghost tiny" :disabled="!run.paused" @click="dbgCmd('step')">↓ 单步 F10</button>
            <button class="ghost tiny" :disabled="!run.paused" @click="dbgCmd('next')">→ 跳过 F11</button>
            <span class="dim" style="font-size:10px">断点 {{ run.breakpoints.join(',') || '无' }}</span>
            <span class="spacer"></span>
            <button class="ghost tiny" @click="stopRunDebug()">停止</button>
          </div>
          <!-- 暂停处的局部变量 -->
          <div v-if="run.paused" class="dbg-locals">
            <div class="run-sec-title">暂停：{{ run.paused.func }} · 第 {{ run.paused.line }} 行</div>
            <div class="dbg-vars">
              <span v-for="(v, k) in run.paused.locals" :key="k" class="dbg-var">
                <b>{{ k }}</b>=<code>{{ v }}</code></span>
              <span v-if="!Object.keys(run.paused.locals).length" class="dim">（无局部变量）</span>
            </div>
            <div class="dbg-watch">
              <input v-model="run.expr" class="run-args-input" placeholder="观察表达式，如 x+y"
                     @keydown.enter.prevent="watchExpr()" />
              <button class="ghost tiny" @click="watchExpr()">求值</button>
              <span v-for="(v, k) in run.watch" :key="k" class="dbg-var"><b>{{ k }}</b>=<code>{{ v }}</code></span>
            </div>
          </div>
          <!-- 诊断（可点跳转） -->
          <div v-if="run.diag.length" class="run-diag">
            <div class="run-sec-title">诊断（点击跳到该行）· {{ run.diag.length }} 条</div>
            <div v-for="(d, i) in run.diag" :key="i" class="run-diag-line" @click="jumpToDiag(d)">
              <span class="mono b-acc">{{ d.file.split(/[\\/]/).pop() }}:{{ d.line }}</span>
              <span class="dim">{{ d.message }}</span>
            </div>
          </div>
          <div v-if="run.reason" class="alert-inline" style="margin:4px 8px">{{ run.reason }}</div>
          <!-- 输出区 -->
          <div class="run-out">
            <div class="run-sec-title">标准输出</div>
            <pre class="run-pre">{{ run.stdout || '（无输出）' }}</pre>
            <div v-if="run.stderr" class="run-sec-title" style="color:var(--fail)">错误输出</div>
            <pre v-if="run.stderr" class="run-pre err">{{ run.stderr }}</pre>
          </div>
        </div>
        <!-- 悬浮对话框：只有用编辑器打开文件才会出现（开发者模式唯一的输入口） -->
        <div v-if="!ed.binary" class="ed-chat" :class="{ open: edChat.open }">
          <div class="ed-chat-bar">
            <button class="ghost tiny" @click="edChat.open = !edChat.open">
              {{ edChat.open ? '收起' : 'AI 对话' }}</button>
            <span class="dim" style="font-size:10.5px">模型</span>
            <div class="seg">
              <button :class="{ on: edChat.source === 'local' }" @click="setFillSource('local')"
                      title="本地 Ollama：快、免费、离线可用；短续写最合适">本地</button>
              <button :class="{ on: edChat.source === 'remote' }" @click="setFillSource('remote')"
                      title="远端最强档：能读懂‘把这个函数改成 X 语义’，慢一点、要花钱">远端</button>
            </div>
            <span class="dim" style="font-size:10px">
              {{ edChat.source === 'local' ? (form.model || '本地最强模型') : '远端最强档' }}
            </span>
            <span class="spacer"></span>
            <span v-if="ed.fillBusy" class="badge b-warn">生成中…</span>
          </div>
          <template v-if="edChat.open">
            <!-- 回答显示：生成中显示进度与已用秒数，完成后显示返回的代码（可复制） -->
            <div v-if="ed.fillBusy || edChat.answer || edChat.answerErr" class="ed-chat-answer">
              <div v-if="ed.fillBusy" class="ed-chat-wait">
                {{ edChat.source === 'remote' ? '远端生成中' : '本地模型生成中' }}… {{ edChat.elapsed }}s
                <span class="dim" style="font-size:10px">
                  {{ edChat.source === 'remote' ? '（远端约 20–60 秒）' : '（本地 7B 约 10–30 秒）' }}
                </span>
              </div>
              <template v-else>
                <div v-if="edChat.answerErr" class="ed-chat-err">{{ edChat.answerErr }}</div>
                <pre v-else class="ed-chat-code">{{ edChat.answer }}</pre>
                <div class="ed-chat-meta">
                  <span>{{ edChat.answerMeta }}</span>
                  <span class="spacer"></span>
                  <button v-if="edChat.answer" class="ghost tiny" @click="copyAnswer">复制</button>
                  <button v-if="edChat.answer" class="ghost tiny" @click="edChat.answer = ''">清空</button>
                </div>
              </template>
            </div>
            <textarea v-model="edChat.input" class="ed-chat-input"
                      placeholder="改写代码，或说「打开 payment.py 的 refund 函数」「调试 calc.py 的 add 函数」「find_symbols 在哪」。
  留空发送 = 光标处续写；选中一段发送 = 改写这段。"
                      @keydown.ctrl.enter.prevent="chatOrNavigate()"
                      @keydown.meta.enter.prevent="chatOrNavigate()"></textarea>
            <div class="row" style="gap:8px;align-items:center;margin-top:6px">
              <button :disabled="ed.fillBusy || nav.busy" @click="chatOrNavigate()">
                {{ ed.fillBusy ? '生成中…' : (nav.busy ? '导航中…' : '发送 Ctrl+Enter') }}</button>
              <span class="dim" style="font-size:10px">
                说「调试 X 函数」直接跑断点；其余当代码改写
              </span>
            </div>
            <!-- AI 导航到的候选/结果：可点击直接跳（调试动作会同时启动断点） -->
            <div v-if="nav.targets.length" class="nav-results">
              <div class="run-sec-title">
                {{ nav.action === 'debug' ? '调试目标' : (nav.action === 'search' ? '找到的符号' : '定位结果') }}
                <button class="ghost tiny" style="float:right" @click="nav.targets = []; nav.showSymbols = false">关闭</button>
              </div>
              <div v-for="(t, i) in nav.targets" :key="i" class="nav-result" @click="pickNavTarget(t)">
                <span class="mono b-acc">{{ t.name }}</span>
                <span class="dim">{{ t.path }}:{{ t.line }}</span>
                <span class="badge b-dim">{{ t.kind }}</span>
              </div>
            </div>
            <div v-if="nav.reason && !nav.targets.length" class="ed-chat-err">{{ nav.reason }}</div>
          </template>
        </div>

        <div class="editor-foot">
          <span class="mono">{{ ed.path }}</span>
          <span v-if="!ed.binary">· 改完按 <b>Ctrl+S</b> 保存（自动备份原文件）· <b>Esc</b> 关闭当前标签</span>
          <span v-if="ed.fillMsg" class="badge b-acc">{{ ed.fillMsg }}</span>
          <span v-if="!canWriteFile()" style="color:var(--warn,#e0a30a)">· 当前模式只读，不能保存</span>
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
              <button class="ghost tiny" :disabled="keyTest.busy" @click="testProviderKey()">
                {{ keyTest.busy ? '测试中…' : '测试连接' }}</button>
              <button class="primary tiny" @click="saveProviderDraft()">保存这个 key</button>
            </div>
            <div v-if="keyTest.detail" style="margin-top:6px;font-size:11.5px"
                 :style="{ color: keyTest.ok === false ? '#ffb4ae' : (keyTest.ok ? '#9be9a8' : 'var(--dim)') }">
              {{ keyTest.ok === true ? '✓ ' : (keyTest.ok === false ? '✗ ' : '') }}{{ keyTest.detail }}
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
          <span class="tab" :class="{ active: settingsTab === 'deploy' }" @click="settingsTab = 'deploy'; loadDeploy()">部署通道</span>
          <span class="tab" :class="{ active: settingsTab === 'uistyle' }" @click="settingsTab = 'uistyle'; loadUIStyles()">UI 审美</span>
          <span class="tab" :class="{ active: settingsTab === 'human' }" @click="settingsTab = 'human'; loadHumanOnce()">半自动回填</span>
          <span class="tab" :class="{ active: settingsTab === 'orch' }" @click="settingsTab = 'orch'; loadOrch()">多机编排</span>
          <span class="tab" :class="{ active: settingsTab === 'caps' }" @click="settingsTab = 'caps'; loadCapabilities()">能力边界</span>
          <span class="tab" :class="{ active: settingsTab === 'health' }" @click="settingsTab = 'health'; loadOps()">体检</span>
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
              <button class="ghost tiny" :disabled="remoteTest.busy" @click="testRemote()">
                {{ remoteTest.busy ? '测试中…' : '测试连接' }}</button>
              <button class="ghost tiny" @click="revealRemoteKey()">{{ remoteForm.api_key ? '隐藏' : '显示' }}</button>
            <button class="ghost tiny" @click="clearRemoteKey()">清除</button>
            </div>
            <div v-if="remoteTest.detail" style="margin-top:6px;font-size:11.5px"
                 :style="{ color: remoteTest.ok === false ? '#ffb4ae' : (remoteTest.ok ? '#9be9a8' : 'var(--dim)') }">
              {{ remoteTest.ok === true ? '✓ ' : (remoteTest.ok === false ? '✗ ' : '') }}{{ remoteTest.detail }}
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

          <!-- 部署通道：预留服务器 + 一次性密码（密码不落盘、不回显） -->
          <template v-else-if="settingsTab === 'deploy'">
            <div class="panel-title">受控部署通道</div>
            <div class="dim" style="font-size:12px;margin-bottom:10px;line-height:1.6">
              人预先登记服务器、认证方式和命令白名单，远端只能在这里面"提议"命令。
              每次执行要过<b>三方协商 ≥2</b> + <b>强制人审</b>，全程审计。
              密码只随执行请求传一次，走 SSH_ASKPASS 注入子进程，<b>不落盘、不进日志、界面不回显</b>。
            </div>
            <div class="row" style="gap:8px;align-items:center;margin-bottom:8px">
              <span class="badge" :class="deployCfg.enabled ? 'b-ok' : 'b-dim'">
                {{ deployCfg.enabled ? '已启用' : '未启用（总开关关闭）' }}</span>
              <button class="ghost tiny" @click="toggleDeployEnabled()">
                {{ deployCfg.enabled ? '关闭通道' : '启用通道' }}</button>
              <button class="ghost tiny" @click="loadDeploy()">刷新</button>
            </div>

            <div class="panel-title" style="margin-top:12px">已预留目标</div>
            <table v-if="deployCfg.targets?.length">
              <thead><tr><th>id</th><th>host:port</th><th>user</th><th>认证</th><th>凭据</th><th>命令白名单</th><th></th></tr></thead>
              <tbody>
                <tr v-for="t in deployCfg.targets" :key="t.id">
                  <td class="mono">{{ t.id }}</td>
                  <td class="mono">{{ t.host }}:{{ t.port }}</td>
                  <td class="mono">{{ t.user }}</td>
                  <td>{{ t.auth }}</td>
                  <td class="dim" style="font-size:11px">
                    {{ t.auth === 'password' ? (t.credential_kind || '未配置') + ' ' + (t.credential_masked || '') : (t.key_ref_masked || '—') }}
                  </td>
                  <td class="dim" style="font-size:11px">{{ (t.allow_commands || []).join(' / ') || '—' }}</td>
                  <td><button class="ghost tiny" @click="editDeployTarget(t)">编辑</button>
                      <button class="ghost tiny" @click="removeDeployTarget(t.id)">删</button></td>
                </tr>
              </tbody>
            </table>
            <div v-else class="dim" style="font-size:12px">还没有目标。先在下面填一个。</div>

            <div class="panel-title" style="margin-top:14px">{{ deployForm.id ? '编辑目标：' + deployForm.id : '新增目标' }}</div>
            <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
              <input v-model="deployForm.id" placeholder="id（如 prod）" style="width:130px" />
              <input v-model="deployForm.host" placeholder="host / IP" style="width:150px" />
              <input v-model="deployForm.port" placeholder="端口" style="width:70px" />
              <input v-model="deployForm.user" placeholder="用户名" style="width:120px" />
              <select v-model="deployForm.auth" style="width:130px">
                <option value="agent">SSH agent / 默认密钥</option>
                <option value="key_ref">私钥路径引用</option>
                <option value="password">用户名 + 密码</option>
              </select>
              <label class="dim" style="font-size:11.5px">
                <input type="checkbox" v-model="deployForm.enabled" /> 启用此目标
              </label>
            </div>
            <div v-if="deployForm.auth === 'key_ref'" class="row" style="gap:8px;margin-top:6px">
              <input v-model="deployForm.key_ref" placeholder="本机私钥路径（只存引用，不回显）" style="flex:1" />
            </div>
            <div v-if="deployForm.auth === 'password'" class="row" style="gap:8px;margin-top:6px;align-items:center;flex-wrap:wrap">
              <input v-model="deployForm.credential" placeholder="凭据引用：env:VAR / file:PATH / prompt" style="flex:1" />
              <span class="dim" style="font-size:11px">明文会被拒绝；prompt=执行时临时输入</span>
            </div>
            <div class="row" style="gap:8px;margin-top:6px;align-items:center">
              <input v-model="deployForm.workdir" placeholder="远端工作目录（可选）" style="width:220px" />
              <input v-model="deployForm.note" placeholder="备注（可选）" style="width:180px" />
            </div>
            <div style="margin-top:6px">
              <div class="dim" style="font-size:11.5px;margin-bottom:4px">命令白名单（每行一个前缀，远端命令必须整条命中）</div>
              <textarea v-model="deployForm.allow_commands" rows="3" placeholder="git pull&#10;systemctl restart myapp&#10;docker ps" style="width:100%"></textarea>
            </div>
            <div class="row" style="gap:8px;margin-top:8px">
              <button class="primary tiny" @click="saveDeployTargets()">保存目标</button>
              <button class="ghost tiny" @click="resetDeployForm()">清空表单</button>
            </div>

            <div class="panel-title" style="margin-top:16px">执行一条命令</div>
            <div class="dim" style="font-size:11px;margin-bottom:6px">
              这里发命令后会进入协商 + 人审队列；到「权限模式」Tab 通过审批才真正执行。
            </div>
            <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
              <select v-model="deployExec.target" style="width:150px">
                <option v-for="t in deployCfg.targets" :key="t.id" :value="t.id">{{ t.id }}</option>
              </select>
              <input v-model="deployExec.command" placeholder="白名单内的命令" style="flex:1" />
            </div>
            <div class="row" style="gap:8px;margin-top:6px;align-items:center;flex-wrap:wrap">
              <input v-model="deployExec.reason" placeholder="原因（给人审看）" style="flex:1" />
              <input v-if="deployNeedsPassword()" v-model="deployExec.password" type="password"
                     placeholder="一次性密码（用完即焚）" style="width:200px" />
              <button class="primary tiny" :disabled="deployExec.busy" @click="runDeployExec()">
                {{ deployExec.busy ? '提交中…' : '提交执行' }}</button>
            </div>
            <div v-if="deployExec.result" style="margin-top:8px;font-size:12px"
                 :style="{ color: deployExec.result.ok ? '#9be9a8' : '#ffb4ae' }">
              <div>{{ deployExec.result.ok ? '✓ 执行成功' : '✗ ' + (deployExec.result.why || '失败') }}
                <span v-if="deployExec.result.rc !== undefined" class="dim">（rc={{ deployExec.result.rc }}）</span></div>
              <pre v-if="deployExec.result.output" class="approval-diff" style="margin-top:6px">{{ deployExec.result.output }}</pre>
            </div>
          </template>

          <!-- UI 审美样式库：审美判据在人眼里，引擎只列举类型让用户挑，颜色自己填 -->
          <template v-else-if="settingsTab === 'uistyle'">
            <div class="panel-title">UI 审美 · 挑一个风格</div>
            <div class="dim" style="font-size:12px;margin-bottom:10px;line-height:1.6">
              引擎判不了"好不好看"，所以不替你拍板。这里把页面/背景/菜单/按钮的类型全部列出来，
              组合成 10 个预设供你挑；颜色由你自己填。选中后生成时会按此约定落地，
              落地是否到位由浏览器探针查 DOM/CSS，审美本身仍由你判断。
            </div>

            <div class="panel-title" style="font-size:13px">① 选风格预设</div>
            <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:12px">
              <div v-for="p in uiStyles" :key="p.id" class="panel"
                   :style="{ cursor:'pointer', border:'1px solid ' + (uiSelected === p.id ? 'var(--accent)' : 'var(--line)'),
                             background: uiSelected === p.id ? 'rgba(79,140,255,.08)' : 'var(--panel2)' }"
                   @click="pickUIStyle(p.id)">
                <div style="font-weight:600;font-size:13px">
                  {{ p.label }} <span class="dim" style="font-weight:400">· {{ p.category }}</span>
                  <span v-if="uiSelected === p.id" style="color:var(--accent)">✓</span>
                </div>
                <div class="dim" style="font-size:11px;line-height:1.5;margin-top:4px">{{ p.description }}</div>
                <div class="dim" style="font-size:10px;margin-top:6px">
                  页面={{ p.dimensions?.page }} · 背景={{ p.dimensions?.background }} ·
                  菜单={{ p.dimensions?.menu }} · 按钮={{ p.dimensions?.button }}
                </div>
              </div>
            </div>

            <div class="panel-title" style="font-size:13px">② 自填配色（插槽，不填则生成时留占位）</div>
            <div class="panel" style="margin-bottom:12px">
              <div v-for="s in uiSlots" :key="s.key" style="display:flex;align-items:center;gap:10px;margin:6px 0">
                <label style="width:64px;font-size:12px">{{ s.label }}</label>
                <input v-model="uiColors[s.key]" :placeholder="s.hint"
                       style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--line);padding:5px 8px;border-radius:4px" />
                <span v-if="uiColors[s.key]" :style="{ width:'18px',height:'18px',borderRadius:'4px',border:'1px solid var(--line)',background:uiColors[s.key] }"></span>
              </div>
            </div>

            <details style="margin-bottom:12px">
              <summary class="dim" style="cursor:pointer;font-size:12px">查看全部类型（4 类 × 10 种）</summary>
              <div v-for="(list, dim) in uiTypes" :key="dim" style="margin-top:8px;font-size:11px;line-height:1.7">
                <b>{{ dim }}</b>：
                <span v-for="t in list" :key="t.id" class="dim" style="margin-right:8px">{{ t.label }}({{ t.id }})</span>
              </div>
            </details>

            <button @click="saveUIStyle" class="btn primary">保存风格并用于生成</button>

            <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
              <div class="panel-title" style="font-size:13px">闭环验收（最后落地）</div>
              <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:8px">
                把四块能力的验收路径收口成一张单：风格约定、半自动闭环、插件桥清单、判据开放。
                机验只覆盖结构/产物/DOM/CSS；审美与真链路仍标人在环，不谎报全自动。
              </div>
              <button @click="runVerify" class="btn" :disabled="verifyBusy">
                {{ verifyBusy ? '验收中…' : '跑闭环验收' }}
              </button>
              <div v-if="verifyResult" style="margin-top:10px;font-size:12px">
                <div :style="{ color: verifyResult.ok ? '#9be9a8' : '#ffb4ae', fontWeight: 600 }">
                  {{ verifyResult.ok ? '✓ 验收通过' : '✗ 有未通过项' }}
                  （{{ verifyResult.passed }}/{{ verifyResult.total }}）
                </div>
                <div v-for="it in verifyResult.items" :key="it.name"
                     class="panel" style="margin-top:6px;padding:6px 8px">
                  <span :style="{ color: it.ok ? '#9be9a8' : '#ffb4ae' }">{{ it.ok ? '✓' : '✗' }}</span>
                  <b>{{ it.name }}</b> <span class="dim">{{ it.detail }}</span>
                  <div v-if="it.human_review" class="dim" style="font-size:11px;margin-top:3px">
                    人在环：{{ it.human_review }}</div>
                </div>
              </div>
            </div>
          </template>

          <!-- 半自动回填：人填一次 → 引擎代跑（值不落盘/不回显） -->
          <template v-else-if="settingsTab === 'human'">
            <div class="panel-title">半自动回填 · 外部真链路</div>
            <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:10px">
              验证码 / 短信 OTP / 第三方登录这类判据在别人服务器上，引擎拿不到，全人工又太碎。
              这里做半自动：<b>你填一次</b>，同一会话内引擎后续代跑；<b>不是全自动</b>，第一次人工不可避免。
              值只进内存、不回显、不落盘。
            </div>
            <div v-if="hoRestartHint()" class="panel"
                 style="padding:8px 10px;margin-bottom:12px;font-size:11px;line-height:1.6">
              {{ hoRestartHint() }}
            </div>

            <div class="panel-title" style="font-size:13px">① 存入一次（人填）</div>
            <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
              <input v-model="hoForm.namespace" placeholder="命名空间（如 flow）" style="width:130px" />
              <input v-model="hoForm.key" placeholder="键（如 otp）" style="width:110px" />
              <input v-model="hoForm.value" type="password" placeholder="值（验证码/口令，不回显）"
                     style="flex:1;min-width:160px" />
              <input v-model.number="hoForm.ttl" type="number" placeholder="TTL 秒" style="width:90px" />
              <input v-model.number="hoForm.uses" type="number" placeholder="可用次数" style="width:90px" />
              <button class="primary tiny" :disabled="hoBusy" @click="putHumanOnce()">存入</button>
            </div>

            <div class="panel-title" style="font-size:13px">② 会话内已填（不含值）</div>
            <table v-if="hoState.pending && hoState.pending.length" style="margin-bottom:10px">
              <tr><th>命名空间</th><th>键</th><th>剩余次数</th><th>剩余秒</th><th></th></tr>
              <tr v-for="it in hoState.pending" :key="it.namespace + '/' + it.key">
                <td class="mono" style="font-size:11px">{{ it.namespace }}</td>
                <td class="mono" style="font-size:11px">{{ it.key }}</td>
                <td>{{ it.uses_left }}</td>
                <td>{{ it.ttl_left }}</td>
                <td><button class="ghost tiny" @click="clearHumanOnce(it.namespace, it.key)">清除</button></td>
              </tr>
            </table>
            <div v-else class="dim" style="font-size:12px;margin-bottom:10px">暂无已填项。</div>

            <div class="panel-title" style="font-size:13px">③ 跑一次步骤表（演示暂停 → 续跑）</div>
            <div class="dim" style="font-size:11px;line-height:1.6;margin-bottom:6px">
              缺值会停在需要人的那一步并回报 need；去 ① 填入后重跑即可从该步继续。
            </div>
            <button class="btn" :disabled="hoBusy" @click="runHumanOnceDemo()">
              {{ hoBusy ? '执行中…' : '跑演示步骤' }}</button>
            <div v-if="hoDemo" class="panel" style="margin-top:8px;padding:8px;font-size:12px">
              <div :style="{ color: hoDemo.ok ? '#9be9a8' : '#ffb4ae' }">
                {{ hoDemo.ok ? '✓ 全部步骤完成'
                   : (hoDemo.need ? '⏸ 停在需要人填的一步' : '✗ 执行失败') }}
              </div>
              <div v-if="hoDemo.need" class="dim" style="margin-top:4px;font-size:11px">
                需要：{{ hoDemo.need.namespace }}/{{ hoDemo.need.key }} —
                {{ hoDemo.need.hint || '（无说明）' }}
              </div>
              <div v-if="hoDemo.error" class="dim" style="margin-top:4px;font-size:11px">
                {{ hoDemo.error }}
              </div>
              <div v-for="s in (hoDemo.steps || [])" :key="s.i"
                   class="dim" style="font-size:11px;margin-top:2px">
                第 {{ s.i + 1 }} 步 · {{ s.kind }} · {{ s.ok ? '完成' : (s.error || '未完成') }}
              </div>
            </div>
          </template>

          <!-- 多机编排：一次协商 + 一次人审覆盖整批主机 -->
          <template v-else-if="settingsTab === 'orch'">
            <div class="panel-title">多机编排</div>
            <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:10px">
              一个计划绑定多台预留服务器，共用一组步骤：<b>一次三方协商 + 一次人审覆盖整批</b>
              （不是每台审一次）。每台每条命令都过各自目标的白名单；单机失败隔离，故障机自动回滚。
              先到「部署通道」登记目标与命令白名单。
            </div>
            <div class="row" style="gap:8px;margin-bottom:10px;align-items:center">
              <span class="dim" style="font-size:12px">通道：{{ orchCfg.enabled ? '已启用' : '未启用' }}</span>
              <button class="ghost tiny" @click="toggleOrchEnabled()">切换</button>
              <span class="dim" style="font-size:11px">（目标 {{ (deployCfg.targets || []).length }} 台）</span>
            </div>

            <div class="panel-title" style="font-size:13px">① 已有计划</div>
            <table v-if="orchCfg.plans && orchCfg.plans.length" style="margin-bottom:10px">
              <tr><th>id</th><th>目标</th><th>步骤</th><th>策略</th><th></th></tr>
              <tr v-for="p in orchCfg.plans" :key="p.id">
                <td class="mono" style="font-size:11px">{{ p.id }}</td>
                <td style="font-size:11px">{{ (p.targets || []).join(', ') }}</td>
                <td>{{ (p.steps || []).length }}</td>
                <td style="font-size:11px">{{ p.strategy }}<span v-if="p.fail_fast"> · 首败即停</span></td>
                <td style="white-space:nowrap">
                  <button class="ghost tiny" @click="editOrchPlan(p)">编辑</button>
                  <button class="ghost tiny" @click="removeOrchPlan(p.id)">删除</button>
                </td>
              </tr>
            </table>
            <div v-else class="dim" style="font-size:12px;margin-bottom:10px">还没有编排计划。</div>

            <div class="panel-title" style="font-size:13px">② 新建 / 编辑计划</div>
            <div class="panel" style="margin-bottom:12px">
              <div class="row" style="gap:8px;margin-bottom:6px;align-items:center;flex-wrap:wrap">
                <input v-model="orchForm.id" placeholder="计划 id（如 deploy_api）" style="width:180px" />
                <select v-model="orchForm.strategy" style="width:120px">
                  <option value="sequential">sequential</option>
                  <option value="parallel">parallel</option>
                </select>
                <label style="font-size:12px"><input type="checkbox" v-model="orchForm.fail_fast" /> 首台失败即停</label>
                <input v-model.number="orchForm.timeout" type="number" placeholder="每台超时秒" style="width:110px" />
              </div>
              <div class="dim" style="font-size:11px;margin:6px 0 4px">目标（多选，来自部署通道）</div>
              <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:8px">
                <label v-for="t in (deployCfg.targets || [])" :key="t.id" style="font-size:12px">
                  <input type="checkbox" :value="t.id" v-model="orchForm.targets" /> {{ t.id }}
                  <span class="dim" style="font-size:10px">({{ t.host }})</span>
                </label>
                <span v-if="!(deployCfg.targets || []).length" class="dim" style="font-size:11px">
                  还没有目标，先去「部署通道」登记。</span>
              </div>
              <div class="dim" style="font-size:11px;margin-bottom:4px">
                步骤（每行一条；用 <code>||</code> 分隔原因，如 <code>systemctl restart app || 重启服务</code>）
              </div>
              <textarea v-model="orchForm.stepsText" rows="4"
                        style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);padding:6px 8px;border-radius:4px;font-family:var(--mono,monospace);font-size:12px"></textarea>
              <div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">
                <input v-model="orchForm.health_check" placeholder="健康检查命令（可选）" style="flex:1" />
              </div>
              <div class="dim" style="font-size:11px;margin:6px 0 4px">失败回滚（每行一条，可选）</div>
              <textarea v-model="orchForm.rollbackText" rows="2"
                        style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line);padding:6px 8px;border-radius:4px;font-family:var(--mono,monospace);font-size:12px"></textarea>
              <div class="row" style="gap:8px;margin-top:8px">
                <button class="primary tiny" @click="saveOrchPlan()">保存计划</button>
                <button class="ghost tiny" @click="resetOrchForm()">清空表单</button>
              </div>
            </div>

            <div class="panel-title" style="font-size:13px">③ 执行</div>
            <div class="dim" style="font-size:11px;margin-bottom:6px">
              提交后进入协商 + 人审队列（到「权限模式」通过审批才真正下发），可能要等一会儿。
            </div>
            <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
              <select v-model="orchExec.plan" style="width:180px">
                <option v-for="p in orchCfg.plans" :key="p.id" :value="p.id">{{ p.id }}</option>
              </select>
              <input v-model="orchExec.password" type="password" placeholder="一次性密码（需要时填）"
                     style="width:200px" />
              <button class="primary tiny" :disabled="orchExec.busy" @click="runOrchExec()">
                {{ orchExec.busy ? '执行中…' : '提交执行' }}</button>
            </div>
            <div v-if="orchExec.result" style="margin-top:8px;font-size:12px">
              <div :style="{ color: orchExec.result.ok ? '#9be9a8' : '#ffb4ae' }">
                {{ orchExec.result.ok ? '✓ ' : '✗ ' }}{{ orchExec.result.why }}
              </div>
              <div v-for="h in (orchExec.result.hosts || [])" :key="h.target"
                   class="panel" style="margin-top:6px;padding:6px 8px;font-size:11px">
                <span :style="{ color: h.ok ? '#9be9a8' : '#ffb4ae' }">{{ h.ok ? '✓' : '✗' }}</span>
                <b>{{ h.target }}</b>
                <span v-for="s in (h.steps || [])" :key="s.command" class="dim" style="margin-left:8px">
                  {{ s.command }} → rc={{ s.rc }}
                </span>
                <span v-if="h.health" class="dim" style="margin-left:8px">
                  健康检查 {{ h.health.ok ? '通过' : '失败' }}</span>
              </div>
            </div>
          </template>

          <!-- 能力边界：插件适配 + 判据开放（只读清单，诚实标人在环） -->
          <template v-else-if="settingsTab === 'caps'">
            <div class="panel-title">能力边界 · 哪些能机判、哪些必须人管</div>
            <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:12px">
              下面两份清单来自公开开发文档：上面是常用软件的插件桥（能验什么产物），
              下面是主动开放了判据接口的平台（哪些原本判不了的变成可机读）。
              <b>只搬读取权，不等于能自动操作平台</b>；标「人在环」的部分机器不判。
            </div>

            <div class="panel-title" style="font-size:13px">① 插件适配（闭源宿主：插件桥 + 验产物）</div>
            <div v-if="!capCfg.loaded" class="dim" style="font-size:12px">加载中…</div>
            <div v-for="a in capCfg.adapters" :key="a.id" class="panel"
                 style="margin-bottom:8px;padding:8px 10px">
              <div style="font-size:13px;font-weight:600">
                {{ a.label }}
                <span class="dim" style="font-weight:400;font-size:11px">· {{ a.category }}</span>
                <span style="margin-left:6px;font-size:10px;padding:1px 6px;border-radius:8px"
                      :style="a.human_in_loop
                        ? { background:'rgba(255,180,110,.15)', color:'#ffb46e' }
                        : { background:'rgba(155,233,168,.15)', color:'#9be9a8' }">
                  {{ a.human_in_loop ? '部分人在环' : '可机验' }}</span>
              </div>
              <div class="dim" style="font-size:11px;margin-top:3px">桥接：{{ a.bridge }}</div>
              <div style="font-size:11px;margin-top:4px">
                <b style="color:#9be9a8">可机验</b>：
                <span v-for="(c, i) in a.can_verify" :key="'c' + i" class="dim">· {{ c }} </span>
              </div>
              <div style="font-size:11px;margin-top:2px">
                <b style="color:#ffb4ae">人在环</b>：
                <span v-for="(c, i) in a.cannot_verify" :key="'n' + i" class="dim">· {{ c }} </span>
              </div>
              <div class="dim" style="font-size:10px;margin-top:4px">
                <a v-for="(d, i) in a.docs" :key="'d' + i" :href="d" target="_blank"
                   style="color:var(--accent);margin-right:8px">公开文档{{ i + 1 }}</a>
              </div>
            </div>

            <div class="panel-title" style="font-size:13px;margin-top:14px">② 判据开放（平台开放接口，判据可机读）</div>
            <div v-for="s in capCfg.sources" :key="s.id" class="panel"
                 style="margin-bottom:8px;padding:8px 10px">
              <div style="font-size:13px;font-weight:600">
                {{ s.platform }}
                <span class="dim" style="font-weight:400;font-size:11px">· {{ s.category }}</span>
                <span style="margin-left:6px;font-size:10px;padding:1px 6px;border-radius:8px;background:rgba(155,233,168,.15);color:#9be9a8">判据开放</span>
              </div>
              <div style="font-size:11px;margin-top:4px">
                <b>可机读</b>：<span v-for="(x, i) in s.interfaces" :key="'i' + i" class="dim">· {{ x }} </span>
              </div>
              <div style="font-size:11px;margin-top:2px">
                <b>可机判</b>：<span v-for="(x, i) in s.verdicts" :key="'v' + i" class="dim">· {{ x }} </span>
              </div>
              <div style="font-size:11px;margin-top:2px">
                <b style="color:#ffb4ae">仍需人判</b>：<span class="dim">{{ s.still_human }}</span>
              </div>
              <div class="dim" style="font-size:10px;margin-top:4px">
                凭据：{{ s.auth }} ·
                <a :href="s.docs" target="_blank" style="color:var(--accent)">公开文档</a>
              </div>
            </div>
          </template>

          <!-- 体检：链路走查 / 闭环验收 / 运维单元（此前都只有 API，用户点不到） -->
          <template v-else-if="settingsTab === 'health'">
            <div class="panel-title">引擎体检</div>
            <div class="dim" style="font-size:12px;line-height:1.6;margin-bottom:12px">
              装好软件后你可以自己确认引擎是否健康：链路走查看各模块是否连通，
              闭环验收看能力是否真的在工作，运维单元让"重启 / 健康检查 / 失败回滚"一次审完。
              前两项只读或内存态，不改你的配置、不连真实服务器。
            </div>

            <div class="panel-title" style="font-size:13px">① 链路走查（只读，不连服务器）</div>
            <button class="btn" :disabled="wcBusy" @click="runWalkthrough()">
              {{ wcBusy ? '走查中…' : '跑链路走查' }}</button>
            <div v-if="wcResult" style="margin-top:10px;font-size:12px">
              <div :style="{ color: wcResult.ok ? '#9be9a8' : '#ffb4ae', fontWeight: 600 }">
                {{ wcResult.ok ? '✓ 链路通' : '✗ 有断点' }}
                （通过 {{ wcResult.passed }}/{{ wcResult.total }}<span v-if="wcResult.skipped">，跳过 {{ wcResult.skipped }}</span>）
              </div>
              <div v-for="s in wcResult.steps" :key="s.name" class="panel"
                   style="margin-top:6px;padding:6px 8px">
                <span v-if="s.status === 'skip'" class="dim">－</span>
                <span v-else :style="{ color: s.status === 'fail' ? '#ffb4ae' : '#9be9a8' }">
                  {{ s.status === 'fail' ? '✗' : '✓' }}</span>
                <b>{{ s.name }}</b> <span class="dim">{{ s.detail }}</span>
              </div>
            </div>

            <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
              <div class="panel-title" style="font-size:13px">② 闭环验收</div>
              <div class="dim" style="font-size:11px;line-height:1.6;margin-bottom:6px">
                机验只覆盖结构/产物/DOM/CSS；审美与真链路仍人在环，结果里会明确列出。
              </div>
              <button class="btn" :disabled="verifyBusy" @click="runVerify()">
                {{ verifyBusy ? '验收中…' : '跑闭环验收' }}</button>
              <div v-if="verifyResult" style="margin-top:10px;font-size:12px">
                <div :style="{ color: verifyResult.ok ? '#9be9a8' : '#ffb4ae', fontWeight: 600 }">
                  {{ verifyResult.ok ? '✓ 验收通过' : '✗ 有未通过项' }}
                  （{{ verifyResult.passed }}/{{ verifyResult.total }}）
                </div>
                <div v-for="it in verifyResult.items" :key="it.name" class="panel"
                     style="margin-top:6px;padding:6px 8px">
                  <span :style="{ color: it.ok ? '#9be9a8' : '#ffb4ae' }">{{ it.ok ? '✓' : '✗' }}</span>
                  <b>{{ it.name }}</b> <span class="dim">{{ it.detail }}</span>
                  <div v-if="it.human_review" class="dim" style="font-size:11px;margin-top:3px">
                    人在环：{{ it.human_review }}</div>
                </div>
              </div>
            </div>

            <div style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
              <div class="panel-title" style="font-size:13px">③ 运维单元（一次协商 + 一次人审覆盖整组）</div>
              <div class="row" style="gap:8px;margin-bottom:8px;align-items:center">
                <span class="dim" style="font-size:12px">通道：{{ opsCfg.enabled ? '已启用' : '未启用' }}</span>
                <button class="ghost tiny" @click="toggleOpsEnabled()">切换</button>
                <span class="dim" style="font-size:11px">（单元 {{ (opsCfg.units || []).length }} 个）</span>
              </div>
              <table v-if="opsCfg.units && opsCfg.units.length" style="margin-bottom:8px">
                <tr><th>单元</th><th>目标</th><th>步骤</th><th>健康检查</th></tr>
                <tr v-for="u in opsCfg.units" :key="u.id">
                  <td class="mono" style="font-size:11px">{{ u.id }}</td>
                  <td style="font-size:11px">{{ u.target }}<span v-if="u.target_host" class="dim"> ({{ u.target_host }})</span></td>
                  <td>{{ (u.steps || []).length }}</td>
                  <td style="font-size:11px">{{ u.health_check || '—' }}</td>
                </tr>
              </table>
              <div v-else class="dim" style="font-size:11px;margin-bottom:8px">
                还没有运维单元。单元通过 API/CLI 配置（目标须先在「部署通道」登记）。
              </div>
              <div class="row" style="gap:8px;align-items:center;flex-wrap:wrap">
                <select v-model="opsExec.unit" style="width:180px">
                  <option v-for="u in opsCfg.units" :key="u.id" :value="u.id">{{ u.id }}</option>
                </select>
                <input v-model="opsExec.password" type="password" placeholder="一次性密码（需要时填）"
                       style="width:200px" />
                <button class="primary tiny" :disabled="opsExec.busy" @click="runOpsExec()">
                  {{ opsExec.busy ? '执行中…' : '提交执行' }}</button>
              </div>
              <div v-if="opsExec.result" style="margin-top:8px;font-size:12px">
                <div :style="{ color: opsExec.result.ok ? '#9be9a8' : '#ffb4ae' }">
                  {{ opsExec.result.ok ? '✓ ' : '✗ ' }}{{ opsExec.result.why }}
                </div>
                <div v-for="s in (opsExec.result.steps || [])" :key="s.command"
                     class="dim" style="font-size:11px;margin-top:2px">
                  {{ s.command }} → rc={{ s.rc }} {{ s.ok ? '' : '（失败）' }}
                </div>
                <div v-if="opsExec.result.health" class="dim" style="font-size:11px;margin-top:2px">
                  健康检查：{{ opsExec.result.health.ok ? '通过' : '失败' }}
                </div>
              </div>
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
            <div class="panel-title">常用工具（默认未绑定 PATH；装没装都列在这里）</div>
            <div v-if="!isShell" class="dim" style="font-size:11.5px;margin-bottom:8px">
              网页版不能弹目录选择器，也不能改本机 PATH；桌面版（Electron）可「浏览…」指定目录、点「绑定」写入当前用户环境变量。
            </div>
            <table>
              <tr><th>工具</th><th>状态</th><th>版本</th><th>路径</th><th>PATH</th><th></th></tr>
              <tr v-for="t in toolchains" :key="t.id">
                <td class="mono">{{ t.label || t.id }}</td>
                <td>
                  <span v-if="t.ok" class="badge b-ok" :title="t.source || ''">✓</span>
                  <span v-else-if="t.installed" class="badge b-warn" :title="t.source || ''">!</span>
                  <span v-else class="badge b-bad">✗</span>
                </td>
                <td class="mono dim">{{ t.version || '未安装' }}</td>
                <td class="mono dim wrap" style="font-size:10.5px">{{ t.path || '未找到' }}</td>
                <td>
                  <span class="badge" :class="t.added ? 'b-ok' : (t.bound ? 'b-warn' : 'b-bad')">
                    {{ t.bind_label || (t.bound ? '系统 PATH' : '未绑定') }}
                  </span>
                </td>
                <td>
                  <button v-if="isShell && can('toolchain.config') && t.bindable && !t.bound" class="ghost tiny"
                          :title="'加入当前用户 PATH：' + (t.bind_dir || t.id)"
                          @click="togglePathBind(t)">绑定</button>
                  <button v-else-if="isShell && can('toolchain.config') && t.added" class="ghost tiny"
                          :title="'从当前用户 PATH 移除 ' + (t.bind_dir || t.id)"
                          @click="togglePathBind(t)">解绑</button>
                  <button v-else-if="isShell && can('toolchain.config')" class="ghost tiny" @click="pickToolchainDir(t)">浏览…</button>
                </td>
              </tr>
            </table>
            <div class="dim" style="font-size:10.5px;margin-top:8px">
              探测顺序：设置覆盖 &gt; 环境变量（JAVA_HOME 等）&gt; PATH &gt; 常见安装目录。
              「绑定」只写当前用户 PATH（不动系统 PATH），卸载时按清单精确移除，不碰你自己加的条目。
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
            你的原始仓库不会被改动；引擎本地对应的临时区归档（`data/scratch/`）会一并清掉。
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
            工作区副本（`workspaces/&lt;id&gt;/`，含产出的代码）可以一起删，也可以留着继续看。<b>你的原始仓库不会被改动。</b><br />
            引擎本地的该仓库临时区归档（`data/scratch/&lt;指纹&gt;/`，收尾时搬过去备用的探针现场）也会一并删除。
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
