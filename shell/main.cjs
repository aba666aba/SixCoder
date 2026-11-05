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
const { app, BrowserWindow, dialog, shell, Menu, ipcMain } = require('electron')
const { spawn } = require('child_process')
const path = require('path')
const net = require('net')
const fs = require('fs')

const PORT = Number(process.env.SIXCODER_PORT || 8760)
const BACKEND_DIR = process.env.SIXCODER_BACKEND
  || path.join(__dirname, '..', 'prototype')

// Python 解释器：显式环境变量 → 本机已知安装位置 → PATH 里的 python
function pickPython() {
  if (process.env.SIXCODER_PYTHON && fs.existsSync(process.env.SIXCODER_PYTHON)) {
    return process.env.SIXCODER_PYTHON
  }
  const guesses = ['D:\\Python\\python.exe', 'C:\\Python312\\python.exe', 'C:\\Python311\\python.exe']
  for (const g of guesses) { if (fs.existsSync(g)) return g }
  return 'python'
}
const PYTHON = pickPython()

let win = null
let backend = null
let backendLog = []
let lastError = ''

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
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  })
  const push = (d) => {
    const s = String(d)
    backendLog.push(s)
    if (backendLog.length > 200) backendLog.shift()
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
    console.log(`[shell] 后端退出 code=${code}`)
    backend = null
  })
}

function waitForPort(host, port, retries = 60) {
  return new Promise((resolve, reject) => {
    const probe = (left) => {
      const sock = new net.Socket()
      sock.setTimeout(800)
      sock.connect(port, host, () => { sock.destroy(); resolve(true) })
        .on('error', () => {
          sock.destroy()
          if (left <= 0) return reject(new Error('timeout'))
          setTimeout(() => probe(left - 1), 500)
        })
        .on('timeout', () => {
          sock.destroy()
          if (left <= 0) return reject(new Error('timeout'))
          setTimeout(() => probe(left - 1), 500)
        })
    }
    probe(retries)
  })
}

function errorPage(title, lines) {
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><style>
    body{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
         background:#0d1017;color:#d7dee9;font:14px/1.8 "Microsoft YaHei",system-ui}
    .box{max-width:620px;padding:28px 32px;background:#151a23;border:1px solid #262d3a;border-radius:12px}
    h1{font-size:18px;margin:0 0 6px}
    .bar{width:56px;height:3px;background:#4f8cff;margin:12px 0 16px}
    code{background:#1b212c;padding:2px 6px;border-radius:4px;font-family:Consolas,monospace}
    li{margin:4px 0;color:#8792a6}
    .hint{margin-top:16px;color:#8792a6;font-size:12.5px}
  </style></head><body><div class="box">
    <h1>${title}</h1><div class="bar"></div>
    <ul>${lines.map((l) => `<li>${l}</li>`).join('')}</ul>
    <div class="hint">排错顺序：① Ollama 是否运行（<code>ollama serve</code>）→ ② 端口 ${PORT} 是否被占用
    → ③ Python 是否可用。<br/>详细日志可在终端查看（本窗口关闭后进程会一并退出）。</div>
  </div></body></html>`
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(html)
}

async function createWindow() {
  win = new BrowserWindow({
    width: 1480, height: 940, minWidth: 1120, minHeight: 700,
    title: 'SixCoder',
    backgroundColor: '#0d1017',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  })

  try {
    await waitForPort('127.0.0.1', PORT)
    await win.loadURL(`http://127.0.0.1:${PORT}`)
    console.log('[shell] 已加载界面')
  } catch (e) {
    const lines = [`后端在 60 秒内没有在端口 ${PORT} 就绪`]
    if (lastError) lines.unshift(lastError)
    if (/EADDRINUSE/.test(backendLog.join(''))) lines.unshift(`端口 ${PORT} 已被占用`)
    if (backendLog.length) lines.push(`最近日志：<code>${backendLog.slice(-3).join(' ').slice(0, 240)}</code>`)
    await win.loadURL(errorPage('后端启动失败', lines))
  }
  win.on('closed', () => { win = null })
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
}))

function buildMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: '应用',
      submenu: [
        { label: '重新加载界面', accelerator: 'F5', click: () => win && win.reload() },
        { label: '开发者工具', accelerator: 'F12', click: () => win && win.webContents.toggleDevTools() },
        { type: 'separator' },
        { label: '退出', accelerator: 'Ctrl+Q', role: 'quit' },
      ],
    },
    {
      label: '帮助',
      submenu: [
        {
          label: '打开文档目录',
          click: () => shell.openPath(path.join(BACKEND_DIR, '..', 'docs')),
        },
        {
          label: '打开原型目录',
          click: () => shell.openPath(BACKEND_DIR),
        },
      ],
    },
  ]))
}

/* ---------------- 单实例 ---------------- */
if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus() }
  })

  app.whenReady().then(async () => {
    buildMenu()
    // 先探测端口：已有后端就复用（避免 EADDRINUSE 让窗口直接退出），
    // 并清掉 Chromium 缓存，保证每次打开都拿到最新前端（实测旧页面缓存导致"改了看不到"）
    try {
      await waitForPort('127.0.0.1', PORT, 2)
      global.__backendAlreadyUp = true
    } catch { global.__backendAlreadyUp = false }
    try {
      const { session } = require('electron')
      await session.defaultSession.clearCache()
    } catch (e) { console.warn('[shell] 清缓存失败:', e.message) }
    spawnBackend()
    await createWindow()
    app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow() })
  })
}

app.on('window-all-closed', () => app.quit())
app.on('before-quit', () => {
  if (backend && !backend.killed) {
    console.log('[shell] 关闭后端')
    backend.kill()
  }
})
