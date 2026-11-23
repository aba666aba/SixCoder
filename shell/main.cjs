/**
 * SixCoder 桌面外壳（Electron）。
 *
 * 职责只有三件：
 *   1. 拉起 Python 后端（coder-core 的 server.py），并等它真的能连上
 *   2. 开一个窗口加载 http://127.0.0.1:8760
 *   3. 提供浏览器做不到的原生能力：选目录、选文件、在资源管理器里打开
 *
 * 它不含任何业务逻辑：所有能力都在 Python 侧（权限/闸门/沙箱/审查/Git）。
 */
const { app, BrowserWindow, dialog, shell, Menu, ipcMain, Tray, nativeImage } = require('electron')
const { spawn, spawnSync } = require('child_process')
const path = require('path')
const net = require('net')
const fs = require('fs')
const os = require('os')

// 端口不是写死的：8760 被别的程序占用时自动往后找空闲端口，保证双击就能起。
const BASE_PORT = Number(process.env.SIXCODER_PORT || 8760)
let PORT = BASE_PORT

// 打包后（安装版）资源在 process.resourcesPath 下：
//   resources/python/python.exe     ← 随包附带的便携式 Python（用户不需要自己装）
//   resources/prototype/            ← 后端源码
// 开发态仍然用仓库里的 ../prototype + 本机 Python。
const IS_PACKED = app.isPackaged
const RES = process.resourcesPath || path.join(__dirname, '..')
const BACKEND_DIR = process.env.SIXCODER_BACKEND
  || (IS_PACKED ? path.join(RES, 'prototype') : path.join(__dirname, '..', 'prototype'))

// 运行期可变数据（日志/数据库/账本）放这里，与安装目录分离：
// 卸载默认删程序本体 + 这个目录；勾选「保留仓库历史」时只删程序本体，数据原样留存。
// 开发态不设（沿用仓库根），零迁移。
const DATA_HOME = process.env.SIXCODER_HOME
  || (IS_PACKED ? path.join(process.env.LOCALAPPDATA || os.homedir(), 'SixCoder') : '')
if (DATA_HOME) process.env.SIXCODER_HOME = DATA_HOME

// Python 解释器：显式环境变量 → 包内自带（安装版）→ 本机已知位置 → PATH 里的 python
// 每一档都先 `-c "import sys"` 试一下：包内 Python 被杀软拦截/缺 DLL 时，
// 直接 spawn 只会看到"后端退出"，错误页却不知道原因。试不活的档位跳过，留好记录。
function pythonWorks(exe) {
  try {
    const r = spawnSync(exe, ['-c', 'import sys; print(sys.version.split()[0])'], {
      timeout: 5000, windowsHide: true, encoding: 'utf8',
    })
    return r.status === 0
  } catch { return false }
}

function pickPython() {
  const cands = []
  if (process.env.SIXCODER_PYTHON) cands.push(process.env.SIXCODER_PYTHON)
  cands.push(path.join(RES, 'python', 'python.exe'))
  cands.push('D:\\Python\\python.exe', 'C:\\Python312\\python.exe', 'C:\\Python311\\python.exe', 'python')
  for (const exe of cands) {
    if (exe.includes('\\') && !fs.existsSync(exe)) continue
    if (pythonWorks(exe)) return exe
  }
  return path.join(RES, 'python', 'python.exe')   // 都不可用：保留包内路径，错误页好指认
}
const PYTHON = pickPython()

let win = null
let backend = null
let backendLog = []
let lastError = ''
let backendExitCode = null
let portOccupiedBy = []

function spawnBackend() {
  const entry = path.join(BACKEND_DIR, 'server.py')
  if (!fs.existsSync(entry)) {
    lastError = `找不到后端入口：${entry}`
    return
  }
  // 端口上已经有后端在跑（例如手动 `python server.py`）→ 直接复用，不再起第二个
  if (global.__backendAlreadyUp) {
    console.log('[shell] 检测到已有后端在监听，直接复用')
    return
  }
  backend = spawn(PYTHON, [entry, '--port', String(PORT)], {
    cwd: BACKEND_DIR,
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
           ...(DATA_HOME ? { SIXCODER_HOME: DATA_HOME } : {}) },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const push = (d) => {
    const s = String(d)
    backendLog.push(s)
    if (backendLog.length > 200) backendLog.shift()
    writeBackendLog(s)
    process.stdout.write(`[backend] ${s}`)
  }
  backend.stdout.on('data', push)
  backend.stderr.on('data', push)
  backend.on('error', (err) => {
    lastError = err.code === 'ENOENT'
      ? `找不到 Python（${PYTHON}）。请安装 Python 3.10+ 或在环境变量 SIXCODER_PYTHON 指定解释器路径`
      : `${err.code || 'ERROR'}: ${err.message}`
    console.error('[shell] 后端启动失败:', lastError)
  })
  backend.on('close', (code) => {
    backendExitCode = code
    console.log(`[shell] 后端退出 code=${code}  日志：${BACKEND_LOG_FILE}`)
    writeBackendLog(`--- 后端进程退出 code=${code} ---\n`)
    backend = null
  })
}

/* 真健康检查：光用 TCP connect 判断"端口通了"是不够的——别的程序占了端口时
   TCP 也通，会被误当成后端已就绪，然后 loadURL 加载出别人的页面（全蓝、无按钮）。
   这里真发 HTTP GET /v1/health，并校验返回体里 app==='sixcoder'，
   确认端口上跑的确实是本软件的后端。返回三态：'ours' | 'foreign' | 'off'。 */
function probeHealth(port, timeoutMs = 1000) {
  return new Promise((resolve) => {
    let settled = false
    const done = (v) => { if (!settled) { settled = true; req.destroy(); resolve(v) } }
    const req = require('http').get(
      { host: '127.0.0.1', port, path: '/v1/health', timeout: timeoutMs },
      (res) => {
        let body = ''
        res.setEncoding('utf8')
        res.on('data', (chunk) => { body += chunk; if (body.length > 4096) done('foreign') })
        res.on('end', () => {
          try {
            const j = JSON.parse(body)
            done(j && j.ok === true && j.app === 'sixcoder' ? 'ours' : 'foreign')
          } catch { done('foreign') }
        })
      })
    req.on('timeout', () => done('off'))
    req.on('error', () => done('off'))
  })
}

// 等待某个端口上的**本软件后端**就绪（不是只要有东西监听就算）。
function waitForBackend(port, retries = 60) {
  return new Promise(async (resolve) => {
    for (let i = 0; i < retries; i++) {
      const st = await probeHealth(port, 800)
      if (st === 'ours') return resolve(true)
      if (st === 'foreign') return resolve(false)  // 被别的程序占了，别再等
      await new Promise((r) => setTimeout(r, 500))
    }
    resolve(false)
  })
}

// 选端口：优先用 BASE_PORT；被占用则依次 +1，找到第一个空闲端口给后端用。
// 若 BASE_PORT 上已经是我们自己的后端（重复双击 / 手动起过），直接复用。
async function choosePort() {
  const state = await probeHealth(BASE_PORT)
  if (state === 'ours') return { port: BASE_PORT, reuse: true, occupied: [] }
  const occupied = []
  for (let p = BASE_PORT; p < BASE_PORT + 20; p++) {
    const free = await new Promise((resolve) => {
      const sock = new net.Socket()
      sock.setTimeout(600)
      sock.on('error', () => { sock.destroy(); resolve(true) })
      sock.on('timeout', () => { sock.destroy(); resolve(false) })
      sock.connect(p, '127.0.0.1', () => { sock.destroy(); resolve(false) })
    })
    if (free) return { port: p, reuse: false, occupied }
    occupied.push(p)
    console.log(`[shell] 端口 ${p} 已被占用，尝试下一个`)
  }
  return { port: BASE_PORT, reuse: false, occupied, exhausted: true }
}

// 后端 stderr/stdout 落盘：远程机器出问题时能拿到真实原因（原来只在内存里，窗口一关就没了）。
const LOG_DIR = DATA_HOME ? path.join(DATA_HOME, '_logs')
  : path.join(BACKEND_DIR, '..', '_logs')
const BACKEND_LOG_FILE = path.join(LOG_DIR, 'backend-shell.log')
function writeBackendLog(s) {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    fs.appendFileSync(BACKEND_LOG_FILE, `[${new Date().toISOString()}] ${s}`)
  } catch (e) { /* 日志写不进去也不能挡住启动 */ }
}

/* 启动流程日志（2026-11-23）。

   为什么单独一个文件：安装版双击后"没反应"时，用户能拿到的只有这个文件。
   之前两个盲区让这类故障完全无法排查：
     1. `app.whenReady().then(...)` **没有 .catch** —— 里面任何异常都被静默吞掉，
        表现就是"进程活着/退出、但没窗口、没日志"；
     2. 拿不到单实例锁时直接 `app.quit()` —— 静默退出，连一行记录都没有，
        而上一实例如果是被强杀的僵尸，用户看到的就是"双击毫无反应"。
   每次启动都追加记录（含 argv / isPackaged / 锁结果），出问题直接发这个文件。 */
const BOOT_LOG_FILE = path.join(LOG_DIR, 'boot.log')
function writeBootLog(msg) {
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    fs.appendFileSync(BOOT_LOG_FILE, `[${new Date().toISOString()}] ${msg}\n`)
  } catch (e) { /* 日志写不进去也不能挡住启动 */ }
}

function logFatal(where, err) {
  const text = err && err.stack ? err.stack : String(err)
  writeBootLog(`!! ${where} 失败：${text}`)
  console.error(`[shell] ${where} 失败:`, text)
}

function esc(s) {
  return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

/* 后端起不来时，把"为什么"写清楚：别的机器上最常见的不是代码问题，而是
   内置 Python 被杀软拦截 / 缺系统运行库 / 依赖装不全。这里逐项真跑一遍，
   结果写进 startup-diagnose.txt，用户直接发回来就能定位。 */
function diagnoseBackend() {
  const out = []
  const entry = path.join(BACKEND_DIR, 'server.py')
  const webIndex = path.join(BACKEND_DIR, 'web', 'dist', 'index.html')
  out.push(`OS: ${process.getSystemVersion ? process.getSystemVersion() : process.version} ${process.arch}`)
  out.push(`Electron: ${process.versions.electron || '-'} / Node ${process.versions.node}`)
  out.push(`RES: ${RES}`)
  out.push(`Python: ${PYTHON}  exists=${fs.existsSync(PYTHON)}`)
  out.push(`server.py: ${entry}  exists=${fs.existsSync(entry)}`)
  out.push(`web/dist/index.html: ${webIndex}  exists=${fs.existsSync(webIndex)}`)
  out.push(`SIXCODER_HOME: ${DATA_HOME || '(repo root)'}`)

  const run = (code, timeout) => {
    const r = spawnSync(PYTHON, ['-c', code], {
      cwd: BACKEND_DIR, timeout, windowsHide: true, encoding: 'utf8',
      env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    })
    const err = r.error ? `${r.error.code || ''} ${r.error.message}` : ''
    return `code=${r.status} signal=${r.signal || '-'} ${err}\n${(r.stdout || '').trim()}\n${(r.stderr || '').trim()}`
  }
  out.push('\n[python -c import sys]\n' + run('import sys; print(sys.version); print(sys.executable)', 10000))
  out.push('\n[import runtime deps]\n' + run(
    'import fastapi, uvicorn, pydantic, starlette, libcst, httpx, tokenizers, coverage, psutil; print("deps OK")',
    30000))
  const text = out.join('\n')
  try {
    fs.mkdirSync(LOG_DIR, { recursive: true })
    fs.writeFileSync(path.join(LOG_DIR, 'startup-diagnose.txt'), text)
  } catch { /* 诊断写不进去也不能挡错误页 */ }
  return text
}

function errorPage(title, lines) {  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
    body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
         background:#0d1017;color:#d7dee9;font:14px/1.8 "Microsoft YaHei",system-ui}
    .box{max-width:680px;padding:28px 32px;background:#151a23;border:1px solid #262d3a;border-radius:12px}
    h1{font-size:18px;margin:0 0 6px}
    .bar{width:56px;height:3px;background:#4f8cff;margin:12px 0 16px}
    code{background:#1b212c;padding:2px 6px;border-radius:4px;font-family:Consolas,monospace}
    li{margin:4px 0;color:#aab4c6}
    .hint{margin-top:16px;color:#8792a6;font-size:12.5px}
    button{margin-top:14px;padding:8px 16px;background:#4f8cff;color:#fff;border:0;border-radius:6px;cursor:pointer;font-size:13px}
  </style></head><body><div class="box">
    <h1>${title}</h1><div class="bar"></div>
    <ul>${lines.map((l) => `<li>${l}</li>`).join('')}</ul>
    <div class="hint">
      <b>先看清原因再动手：</b>是否装了 Ollama <b>不影响启动</b>（只在用本地模型时才需要）。<br/>
      常见原因按概率：① 后端 Python 启动报错 → 看日志最后几行；② 内置 Python 被杀毒软件拦截；
      ③ 端口全被占用。<br/>
      完整日志：<code>${BACKEND_LOG_FILE}</code>（菜单「日志与数据 → 打开日志目录」可直接打开）。
    </div>
    <button onclick="location.reload()">重试启动</button>
  </div></body></html>`
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html)
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1480, height: 940, minWidth: 1120, minHeight: 700,
    title: 'SixCoder',
    backgroundColor: '#0d1017',
    autoHideMenuBar: true,
    icon: trayIconPath(),          // 任务栏/标题栏图标（托盘同一个）
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  try {
    const ready = await waitForBackend(PORT)
    if (!ready) throw new Error('后端未就绪')
    await win.loadURL(`http://127.0.0.1:${PORT}`)
    console.log(`[shell] 已加载界面（端口 ${PORT}）`)
  } catch (e) {
    const lines = [`后端未能在端口 ${PORT} 就绪（日志：<code>${BACKEND_LOG_FILE}</code>）`]
    if (lastError) lines.unshift(lastError)
    if (/EADDRINUSE/.test(backendLog.join(''))) lines.unshift('首选端口被占用且没有可用备用端口')
    if (portOccupiedBy.length) lines.push(`已被占用、自动跳过的端口：${portOccupiedBy.join('、')}`)
    if (backendExitCode !== null) lines.push(`后端进程退出码：<code>${backendExitCode}</code>`)
    if (backendLog.length) lines.push(`最近日志：<code>${backendLog.slice(-4).join(' ').slice(-320)}</code>`)
    const diag = diagnoseBackend()
    const diagFile = path.join(LOG_DIR, 'startup-diagnose.txt')
    lines.push(`启动诊断（已写入 <code>${diagFile}</code>）：<pre style="max-height:220px;overflow:auto;white-space:pre-wrap;background:#0b0e14;border:1px solid #262d3a;border-radius:6px;padding:8px;font:11px/1.6 Consolas,monospace;color:#aab4c6">${esc(diag)}</pre>`)
    writeBackendLog(`--- 错误页：${lines.join(' | ')} ---\n`)
    await win.loadURL(errorPage('后端启动失败', lines))
  }
  // 叉号 = 收进托盘（不是退出）。真退出只有两条：托盘菜单「退出 SixCoder」、系统关机。
  win.on('close', (e) => {
    if (isQuitting) return
    e.preventDefault()
    hideToTray(!trayHinted)
    trayHinted = true
  })
  win.on('closed', () => { win = null })
}

/* ---------------- 系统托盘：关闭 = 收进托盘，托盘里才真退出 ----------------
   为什么要这样（2026-11-21 用户口径："关闭自动到托盘，托盘退出"）：
   夜班是独立进程，关窗本来就不中断；但窗口一关就没法看进度、也没法点停止，
   只能去任务管理器找。收进托盘后：叉号只是隐藏，左键点图标就回来；
   真正退出走托盘菜单，退出时会明确提示夜班仍在跑（它不在应用进程树里）。 */
let tray = null
let isQuitting = false
let trayHinted = false

function trayIconPath() {
  const cands = [
    path.join(__dirname, 'build', 'icon.ico'),
    path.join(__dirname, '..', 'shell', 'build', 'icon.ico'),
    process.resourcesPath ? path.join(process.resourcesPath, 'icon.ico') : '',
    process.resourcesPath ? path.join(process.resourcesPath, 'app.asar.unpacked', 'build', 'icon.ico') : '',
  ].filter(Boolean)
  for (const p of cands) {
    try { if (fs.existsSync(p)) return p } catch { /* 忽略 */ }
  }
  return undefined
}

function showWindow() {
  if (!win) { createWindow(); return }
  if (win.isMinimized()) win.restore()
  win.show()
  win.focus()
}

function createTray() {
  if (tray) return tray
  try {
    const p = trayIconPath()
    tray = new Tray(p ? nativeImage.createFromPath(p) : nativeImage.createEmpty())
    tray.setToolTip('SixCoder（夜班在后台跑，点这里回来）')
    tray.setContextMenu(Menu.buildFromTemplate([
      { label: '显示主窗口', click: showWindow },
      { label: '打开日志目录', click: () => { try { shell.openPath(LOG_DIR) } catch { /* 忽略 */ } } },
      { type: 'separator' },
      { label: '退出 SixCoder', click: () => { isQuitting = true; app.quit() } },
    ]))
    tray.on('click', showWindow)
    tray.on('double-click', showWindow)
  } catch (e) {
    console.warn('[shell] 托盘创建失败（不影响主窗口）:', e.message)
    tray = null
  }
  return tray
}

function hideToTray(firstTime) {
  if (!win) return
  win.hide()
  const t = createTray()
  if (firstTime && t) {
    // 第一次收进托盘时提示一次：否则用户以为软件已经退了
    try {
      t.displayBalloon({
        title: 'SixCoder 仍在后台',
        content: '窗口已收进托盘；夜班继续跑。左键点托盘图标回来，右键可退出。',
      })
    } catch { /* 部分系统不支持气泡，静默 */ }
  }
}

/* ---------------- 原生能力（浏览器做不到的部分） ---------------- */
ipcMain.handle('pick-directory', async (_e, opts = {}) => {
  const r = await dialog.showOpenDialog(win, {
    title: opts.title || '选择目录',
    defaultPath: opts.defaultPath || undefined,
    properties: ['openDirectory', 'createDirectory'],
  })
  return r.canceled ? null : r.filePaths[0]
})

ipcMain.handle('pick-file', async (_e, opts = {}) => {
  const r = await dialog.showOpenDialog(win, {
    title: opts.title || '选择文件',
    defaultPath: opts.defaultPath || undefined,
    properties: ['openFile'],
  })
  return r.canceled ? null : r.filePaths[0]
})

ipcMain.handle('open-path', async (_e, p) => {
  if (!p) return false
  const err = await shell.openPath(p)
  return !err
})

ipcMain.handle('show-in-folder', (_e, p) => {
  if (!p) return false
  shell.showItemInFolder(p)
  return true
})

/* 登录选项：开机自启（Windows 走注册表 Run 项，由 Electron 封装） */
ipcMain.handle('get-login-item', () => {
  try {
    const st = app.getLoginItemSettings()
    return !!st.openAtLogin
  } catch (e) {
    console.error('[shell] 读取登录项失败:', e)
    return false
  }
})

ipcMain.handle('set-login-item', (_e, enabled) => {
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enabled,
      openAsHidden: false,
      // 开发态会用 electron.exe；打包后自动指向安装后的程序
      path: process.execPath,
      args: [],
    })
    return !!app.getLoginItemSettings().openAtLogin
  } catch (e) {
    console.error('[shell] 设置登录项失败:', e)
    return false
  }
})

ipcMain.handle('shell-info', () => ({
  version: app.getVersion(),
  electron: process.versions.electron,
  backendPort: PORT,
  backendDir: BACKEND_DIR,
  python: PYTHON,
  dataHome: DATA_HOME || '',
  packaged: IS_PACKED,
}))

function buildMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: '应用',
      submenu: [
        { label: '重新加载界面', accelerator: 'F5', click: () => win && win.reload() },
        { label: '开发者工具', accelerator: 'F12', click: () => win && win.webContents.toggleDevTools() },
        { type: 'separator' },
        {
          // 原 start.ps1 -Web 的等价物：把同一个界面交给系统浏览器
          label: '在浏览器中打开',
          accelerator: 'Ctrl+Shift+B',
          click: () => shell.openExternal(`http://127.0.0.1:${PORT}`),
        },
        { type: 'separator' },
        { label: '退出', accelerator: 'Ctrl+Q', role: 'quit' },
      ],
    },
    {
      label: '日志与数据',
      submenu: [
        {
          label: '打开日志目录',
          click: () => {
            const dir = DATA_HOME ? path.join(DATA_HOME, '_logs') : path.join(BACKEND_DIR, '..', '_logs')
            fs.mkdirSync(dir, { recursive: true })
            shell.openPath(dir)
          },
        },
        {
          label: '打开数据目录',
          click: () => {
            if (DATA_HOME) { fs.mkdirSync(DATA_HOME, { recursive: true }); shell.openPath(DATA_HOME) }
            else shell.openPath(path.join(BACKEND_DIR, 'data'))
          },
        },
        { label: '打开安装目录', click: () => shell.openPath(BACKEND_DIR) },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '打开文档目录',
          click: () => shell.openPath(path.join(BACKEND_DIR, '..', 'docs')),
        },
      ],
    },
  ]))
}

/* ---------------- 安装期环境准备（--env-setup） ----------------
   安装器的可选组件页调用 `SixCoder.exe --env-setup --ollama --model`。
   **不再有 env-setup.ps1**：逻辑全在 prototype/tools/env_setup.py，只有一份实现。
   这里只负责开一个新控制台窗口让用户看到下载进度，不建界面、不启后端。 */
const ENV_SETUP = process.argv.includes('--env-setup')
/* ---------------- 安装期：自带运行时下载（--install-runtime） ----------------
   安装器勾选了 Python/JDK/Go/MinGW 时调用：
       SixCoder.exe --install-runtime python@3.13 java@21
   逻辑全在 prototype/core/runtimes.py（清单/多源下载/解压/真跑一次校验），
   这里只负责"开一个新控制台让用户看到进度"。 */
const INSTALL_RUNTIME = process.argv.includes('--install-runtime')
if (INSTALL_RUNTIME) {
  const specs = process.argv.filter((a) => /^(python|java|go|mingw|git)@[\w.]+$/.test(a))
  app.whenReady().then(() => {
    if (!specs.length) { app.quit(); return }
    try {
      const child = spawn('cmd.exe',
        ['/c', 'start', 'SixCoder 运行时安装', '/D', BACKEND_DIR,
          PYTHON, '-m', 'core.runtimes', 'install', ...specs],
        { windowsHide: false, detached: true, stdio: 'ignore',
          env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
                 ...(DATA_HOME ? { SIXCODER_HOME: DATA_HOME } : {}) } })
      child.unref()
    } catch (e) {
      dialog.showErrorBox('运行时安装失败', String(e && e.message || e))
    }
    app.quit()
  })
} else if (ENV_SETUP) {
  const want = []
  if (process.argv.includes('--ollama')) want.push('--ollama')
  if (process.argv.includes('--model')) want.push('--model')
  app.whenReady().then(() => {
    const script = path.join(BACKEND_DIR, 'tools', 'env_setup.py')
    if (!want.length) { app.quit(); return }
    if (!fs.existsSync(script)) {
      dialog.showErrorBox('环境准备失败', `找不到脚本：${script}`)
      app.quit(); return
    }
    try {
      const child = spawn('cmd.exe',
        ['/c', 'start', 'SixCoder 环境准备', '/D', BACKEND_DIR, PYTHON, script, ...want],
        { windowsHide: false, detached: true, stdio: 'ignore',
          env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1',
                 ...(DATA_HOME ? { SIXCODER_HOME: DATA_HOME } : {}) } })
      child.unref()
    } catch (e) {
      dialog.showErrorBox('环境准备失败', String(e && e.message || e))
    }
    app.quit()
  })
} else {
  // 单实例锁：拿不到通常意味着"已有实例在跑"（应激活它然后退出）。
  // 但上一实例要是被强杀/崩溃留下的僵尸锁，直接 quit 会让用户看到"双击毫无反应"
  // ——所以这里区分「真有活实例（后端在线）」与「僵尸锁」两种情形（2026-11-23）。
  const gotLock = app.requestSingleInstanceLock()
  writeBootLog(`启动：isPackaged=${IS_PACKED} lock=${gotLock} argv=${JSON.stringify(process.argv.slice(1))}`)

  async function boot() {
    buildMenu()
    // 先选端口：8760 上若已是本软件后端就复用；被别的程序占了就自动 +1 找空闲端口。
    // 关键是"真健康检查"——不能只看 TCP 通不通，否则会把占端口的别人的页面加载进来。
    const choice = await choosePort()
    PORT = choice.port
    portOccupiedBy = choice.occupied || []
    global.__backendAlreadyUp = !!choice.reuse
    if (choice.exhausted) {
      lastError = `端口 ${BASE_PORT} 起连续 20 个端口都被占用，无法启动后端`
    }
    writeBootLog(`端口选择 ${PORT}  复用已有后端=${!!choice.reuse}  跳过=${portOccupiedBy.join(',') || '无'}`)
    // 清掉 Chromium 缓存，保证每次打开都拿到最新前端（实测旧页面缓存导致"改了看不到"）
    try {
      const { session } = require('electron')
      await session.defaultSession.clearCache()
    } catch (e) { console.warn('[shell] 清缓存失败:', e.message) }
    spawnBackend()
    writeBootLog('已 spawn 后端')
    await createWindow()
    createTray()                                  // 托盘常驻，随时能点回来
    writeBootLog('窗口与托盘就绪，启动完成')
    app.on('activate', () => { showWindow() })
  }

  if (gotLock) {
    app.on('second-instance', () => {
      showWindow()
    })
    // 必须 .catch：以前这里没有 catch，whenReady 里任何异常都被静默吞掉，
    // 表现就是"没窗口也没日志"，完全无法排查。
    app.whenReady().then(boot).catch((e) => logFatal('boot', e))
  } else {
    app.whenReady().then(async () => {
      const st = await probeHealth(BASE_PORT, 1500)
      writeBootLog(`拿不到单实例锁；BASE_PORT(${BASE_PORT}) health=${st}`)
      if (st === 'ours') {
        writeBootLog('已有活实例（后端在线）→ 本次启动退出，窗口应在任务栏/托盘')
        app.quit()
        return
      }
      writeBootLog('锁被占但端口没有活后端 → 判定为僵尸锁，强制启动')
      await boot()
    }).catch((e) => logFatal('boot(zombie-lock)', e))
  }
}

// 窗口全关也不退出（收进托盘时窗口只是隐藏；真退出走托盘菜单/关机）
app.on('window-all-closed', () => {
  if (isQuitting) app.quit()
})
app.on('before-quit', () => {
  isQuitting = true
  if (backend && !backend.killed) {
    console.log('[shell] 关闭后端')
    backend.kill()
  }
})
