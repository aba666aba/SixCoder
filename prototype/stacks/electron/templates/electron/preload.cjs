// 预加载脚本（CommonJS）：只暴露必要的受控 API，不开 nodeIntegration。
const { contextBridge } = require('electron')

contextBridge.exposeInMainWorld('sixcoder', {
  ping: () => 'pong',
})
