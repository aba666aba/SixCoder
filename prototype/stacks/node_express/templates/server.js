// Express 服务入口（**由引擎生成，不要改**）。
//
// 为什么不让远端写这个文件：路由是分模块长的（routes/users.js、routes/items.js …），
// 每加一组路由就重写入口 → 之前的挂载被抹掉。
//
// 你只需要在 routes/*.js 里 `module.exports = router`，入口会自动挂上去。
const express = require('express');

const app = express();
app.use(express.json());

app.get('/health', (_req, res) => res.json({ ok: true }));

// 自动挂载 routes/ 下的路由模块。
// 单个模块坏了不拖垮整个服务（与 Web 入口的"坏模块跳过"同一原则）：
// 它自己的路径 404，别的路由照常工作。
const fs = require('fs');
const path = require('path');
const routesDir = path.join(__dirname, 'routes');
try {
  for (const file of fs.readdirSync(routesDir)) {
    if (!file.endsWith('.js') || file.startsWith('_')) continue;
    try {
      const router = require(path.join(routesDir, file));
      const mount = '/' + path.basename(file, '.js');
      app.use(mount, router);
    } catch (err) {
      console.error('[routes] 挂载失败 ' + file + ': ' + err.message);
    }
  }
} catch (err) {
  if (err.code !== 'ENOENT') console.error('[routes] 读取失败: ' + err.message);
}

if (require.main === module) {
  const port = Number(process.env.PORT) || 3000;
  app.listen(port, () => console.log('listening on ' + port));
}

module.exports = app;
