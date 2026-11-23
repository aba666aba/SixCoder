// 最小复现：只测 requestSingleInstanceLock 在本机的行为
const { app } = require('electron')
const got = app.requestSingleInstanceLock()
console.log('[probe] requestSingleInstanceLock =', got)
console.log('[probe] isPackaged =', app.isPackaged)
console.log('[probe] userData =', app.getPath('userData'))
console.log('[probe] name =', app.getName())
if (got) {
  app.whenReady().then(() => {
    console.log('[probe] whenReady OK，退出')
    app.quit()
  })
} else {
  console.log('[probe] 拿不到锁 → 与安装版表现一致')
  app.quit()
}
