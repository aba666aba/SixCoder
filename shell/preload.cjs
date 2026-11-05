const { contextBridge, ipcRenderer } = require('electron')

// 只暴露原生能力，不暴露 Node
contextBridge.exposeInMainWorld('electronAPI', {
  isElectron: true,
  pickDirectory: (opts) => ipcRenderer.invoke('pick-directory', opts),
  pickFile: (opts) => ipcRenderer.invoke('pick-file', opts),
  openPath: (p) => ipcRenderer.invoke('open-path', p),
  showInFolder: (p) => ipcRenderer.invoke('show-in-folder', p),
  shellInfo: () => ipcRenderer.invoke('shell-info'),
  // 登录选项：开机自启
  getLoginItem: () => ipcRenderer.invoke('get-login-item'),
  setLoginItem: (enabled) => ipcRenderer.invoke('set-login-item', !!enabled),
})
