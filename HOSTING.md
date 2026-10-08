# 托管寻微后端

[部署到 Render](https://render.com/deploy?repo=https://github.com/Cheng-xiu/xunwei-search)

GitHub Pages 发布网页界面，Render 运行 Python 搜索服务。上面的入口会打开 Render 的部署确认页面；需要你登录自己的 Render 账户并确认创建服务。仓库包含配置并不表示后端已经上线，实际服务地址以 Render 仪表板为准。

## 首次部署

1. 打开部署入口。如果 Render 要求关联 GitHub，授权访问 `Cheng-xiu/xunwei-search`；只需选择这个仓库。确认 Blueprint 使用仓库根目录的 `render.yaml`。[Render GitHub 集成说明](https://render.com/docs/github)
2. 检查服务 `xunwei-search-api` 的套餐为 **Free**。配置只创建一个 Docker Web Service，不创建付费磁盘或数据库；管理令牌由 Render 自动随机生成，无需手填密钥。[Blueprint 配置说明](https://render.com/docs/blueprint-spec)
3. 等待服务显示 `Live`，复制 Render 提供的完整 HTTPS 地址。打开该地址的 `/api/health`，应返回包含 `"ok": true` 的 JSON。首次唤醒可能需要等待约一分钟。
4. 在寻微网页中连接这个搜索服务，使用访客会话。管理令牌 `XUNWEI_ACCESS_TOKEN` 只用于后端管理，不向访客发放，也不放入 Pages 配置。
5. 如需让公开网页默认连接这个服务，在 GitHub 仓库 **Settings → Secrets and variables → Actions → Variables** 新增 `XUNWEI_PAGES_API_BASE`，值为第 3 步的 HTTPS 地址，不要带查询参数或末尾 `/api`。随后在 **Actions → Deploy public frontend to GitHub Pages → Run workflow** 重新构建网页。这个变量是公开服务地址，不能放 API Key。

一键部署入口使用 Render 官方的 Deploy Button 机制。本站模板设置 `autoDeployTrigger: commit`，推送到服务关联的分支后自动更新，便于站主维护。使用模板部署副本时，可在 Render 的自动部署设置中选择关闭，再用 **Manual Deploy** 手动更新。Render 官方对通用部署按钮建议关闭自动更新；这是一项建议，本站按站主的维护需求开启。[部署按钮说明](https://render.com/docs/deploy-to-render)

## 两种模型使用方式

| 方式 | 配置位置 | 密钥与费用 |
| --- | --- | --- |
| 访客自配 API | 网页中的当前访客会话 | 访客填写自己的兼容服务地址、模型与 Key，使用自己的额度。密钥通过 HTTPS 发给后端，仅在该访客会话内存中使用，不写入 Pages、仓库或持久配置。 |
| 站主提供 API | Render 服务的 **Environment** | 站主添加 `AI_API_KEY`，按需设置 `AI_BASE_URL`、`AI_MODEL`，保存并重新部署后启用共享模式。调用消耗站主额度，网页不会取得站主 Key。 |

模板没有预置 `AI_API_KEY`。未配置站主 Key 时，访客仍可选择自配 API；站主共享模式不会伪装成可用。不要把 Key 写进 `render.yaml`、`deployment-config.js`、GitHub 公开变量或聊天中的部署截图。Render 的环境配置用于保存部署秘密；这里不需要把密钥提交到 GitHub。[Render 环境变量与秘密说明](https://render.com/docs/configure-environment-variables)

每个访客会话使用独立的临时资料库、历史与模型配置。默认最多 20 个会话，单会话最长 60 分钟；退出、到期或后端重启后需要重新创建会话。需要保留的结果请在会话有效时导出。站主管理工作区与访客会话分开，给 `/data` 增加持久化存储也不会把临时访客会话变成永久账户。

## 部署参数与免费层边界

`render.yaml` 已设置这些公开参数：

| 参数 | 用途 |
| --- | --- |
| `XUNWEI_HOST=0.0.0.0` | 接受 Render 转发的连接；程序使用 Render 的 `PORT`。 |
| `XUNWEI_PUBLIC_MODE=1` | 开启访客隔离与自配/站主两种模型方式。 |
| `XUNWEI_ALLOWED_ORIGINS=https://cheng-xiu.github.io` | 允许此 Pages 来源跨域连接；Origin 不包含 `/xunwei-search/` 路径。 |
| `XUNWEI_PUBLIC_HOSTS` | 自动取当前服务的 `RENDER_EXTERNAL_HOSTNAME`，避免硬编码尚未分配的域名。 |
| `XUNWEI_ACCESS_TOKEN` | Render 生成的管理员秘密；保留在服务端。 |
| `XUNWEI_DATA_DIR=/data` | 管理工作区目录；默认免费服务上为临时存储。 |

如果改用另一个 GitHub 用户、Pages 自定义域名或后端自定义域名，要同步调整允许的 Origin / Host；按逗号分隔精确值，不使用 `*`。Render 给后端配置了自定义域名后，健康检查可能使用该域名作为 Host，需要一并加入允许列表。[健康检查说明](https://render.com/docs/health-checks)

Render 免费层适合低频体验，当前官方限制包括：

- 15 分钟没有传入请求后会休眠，重新唤醒通常约一分钟。页面提示超时时可等待服务恢复后再次连接。
- 重启、重新部署或休眠都会丢失本地文件，包括 SQLite 历史；免费 Web Service 不支持持久磁盘。
- 每个工作区每月共享 750 个免费实例小时，另有流量和构建额度。是否会产生额外费用取决于账户支付设置及用量，部署前以仪表板显示为准。
- 频繁主动访问外部 API 可能触发免费服务流量限制。多轮搜索会访问搜索与 AI 服务，先用较小轮数测试，并查看提供商状态和额度。

这些限制依据 [Render 免费层官方说明](https://render.com/docs/free)，会随平台政策变化。Blueprint 只选择 `plan: free`，不会自动添加磁盘、数据库或升级套餐；AI 服务费用仍按所选模型提供商规则计算。
