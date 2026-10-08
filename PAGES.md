# GitHub Pages 部署

寻微已支持发布到 GitHub Pages，包括 `https://账号.github.io/仓库名/` 这样的项目子路径。GitHub Pages 仅托管静态文件，搜索和 AI 功能需要连接另行运行的 Python 搜索服务。

本项目仓库是 [Cheng-xiu/xunwei-search](https://github.com/Cheng-xiu/xunwei-search)，Pages 地址为 `https://cheng-xiu.github.io/xunwei-search/`。公开搜索服务推荐使用 [Render 一键部署说明](HOSTING.md)。

| 功能 | Pages 网页 | 连接搜索服务后 |
| --- | --- | --- |
| 界面、平台列表、浏览器站内搜索入口 | 可用 | 可用 |
| 站主模型配置自动填充 | 开启公开预置后可用 | 可保存至当前连接或访客会话 |
| 多平台自动搜索、冷门线索追查 | 需要服务 | 可用 |
| AI 总结、逐轮进展、停止与继续 | 需要服务及 AI 配置 | 可用 |
| 搜索历史、导入资料 | 需要服务 | 存在所连接的服务上 |

未连接服务时，界面会明确显示未连接，不会把浏览器搜索入口当作程序已取得的结果。程序的 16 个平台来源及原有搜索范围限制见 [README](README.md)。

## 发布网页

1. 将本目录作为 GitHub 仓库根目录上传，包含 `.github/workflows/pages.yml`、`web/`、`scripts/`、`tests/` 和后端代码。不要上传 `.local/`、实际环境文件或用户数据目录。
2. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
3. 向 `main` 或 `master` 分支提交代码，或在 **Actions → Deploy public frontend to GitHub Pages → Run workflow** 手动启动。
4. 工作流运行 Python 和 JavaScript 离线回归检查，构建 `docs/`，仅上传静态文件。成功后，部署记录和 Pages 设置会显示网站地址。

默认网页不预设搜索服务。打开后点击「连接搜索服务」，填写服务地址。公开访客服务会自动创建隔离会话，无需填写管理员令牌；私人服务可填写自己的专用访问令牌。可选：在仓库 **Settings → Secrets and variables → Actions → Variables** 创建 `XUNWEI_PAGES_API_BASE`，填入公开的 HTTPS 搜索服务地址，例如 `https://search-api.example.com`，再运行部署。这个变量会写入公开网页，**只能填地址，不能填 AI 密钥或访问令牌**。

也可在本机手动生成静态文件：

```powershell
python scripts/build_pages.py
python -m http.server 8890 --directory docs
```

预览地址为 `http://127.0.0.1:8890/`。构建仅输出 `index.html`、`app.js`、`styles.css`、`connection.js`、`deployment-config.js`、`platforms.json`、`.nojekyll` 七个文件；不复制 Python、模型配置、历史或导入资料。若选择传统的分支发布方式，可提交构建好的 `docs/`，将 Pages Source 改为 **Deploy from a branch**，选择 `main /docs`；自动工作流方式无需手动提交构建产物。

## 直接填充站主 API

支持站主明确选择将模型 API 公开到网页。开启后，网页默认选择「使用站主 API」，自动填入接口地址、密钥和模型；「自己配置 API」仍可填写个人服务。离线页面仅预填配置，实际搜索仍需连接 Python 搜索服务。连接公开服务时，这组预置保存到当前访客的 `custom` 会话，不修改服务端站主模型或其他访客。

连接已有个人模型配置时保留原配置；明确选择站主预置后，才替换当前连接的 AI 地址、模型与密钥。随后切回个人 API 需重新填写并保存自己的密钥，页面只记住选择，不保存或读回个人密钥。

**该方式会公开 API 密钥，任何访客都可从网页文件中提取并使用额度。** GitHub Actions Secret 只避免密钥进入 Git 提交和工作流日志，无法保护最终网页中的密钥。需要保护额度时，应使用下一节的服务端站主 API。

在仓库 Actions 设置中配置以下项目，再运行部署：

- Secret `XUNWEI_PAGES_OWNER_API_KEY`：明确允许公开的模型密钥。
- Variable `XUNWEI_PAGES_PUBLIC_OWNER_API`：填 `1`，明确开启公开预置。
- Variable `XUNWEI_PAGES_OWNER_BASE_URL`：模型 HTTPS 接口，如 `https://api.a6api.com/v1`。
- Variable `XUNWEI_PAGES_OWNER_MODEL`：模型名称，如 `deepseek-v4.1-flash`。

取消 `XUNWEI_PAGES_PUBLIC_OWNER_API` 后重新部署可关闭网页预置；已公开密钥如需停止使用，应同时在模型供应商处撤销。默认本机构建与分发 ZIP 不读取这组 Secret，也不包含密钥。

## 运行 HTTPS 搜索服务

向普通访客开放服务时，推荐直接使用随附的 [Render 配置](HOSTING.md)。`XUNWEI_PUBLIC_MODE=1` 开启访客会话：可选“使用站主 API”或“自己配置 API”，每个会话的历史、导入资料与模型配置相互隔离。站主通过 `AI_API_KEY`、`AI_BASE_URL`、`AI_MODEL` 环境变量提供共享模型；未配置密钥时界面明确显示站主模式不可用。访客自配密钥仅保留在其服务端临时会话内存中。

公开会话默认最多存活 60 分钟，同时最多 20 个会话，所有访客共享 4 个并行任务名额，每次递进最多 3 轮，可以手动继续。每个访客资料库最多 20 篇，标题、链接与正文合计最多 2 MB，删除后释放容量。私有本机模式不受这些新增访客预算限制。会话过期、撤销或服务重启后，访客密钥和临时资料不再保留；需要保存结果请及时导出。

搜索服务需要能访问各平台和所配置的 AI 接口。可在自己的服务器运行；随附 Dockerfile 使用 Python 标准库，服务和数据分开保存。

在服务器上复制 `backend.env.example` 为 `backend.env`，修改以下值：

- `XUNWEI_ACCESS_TOKEN`：新生成的管理员访问令牌，与 AI 服务密钥分开，不能公开给访客或写入 Pages。可执行 `python -c "import secrets; print(secrets.token_urlsafe(32))"` 生成。
- `XUNWEI_ALLOWED_ORIGINS`：你的 Pages **来源**，如 `https://alice.github.io`，不包含 `/仓库名/` 或末尾斜线；自定义域名需填实际网站来源。多个来源用逗号分隔。
- `XUNWEI_PUBLIC_HOSTS`：反向代理传给 Python 的实际 Host，如 `search-api.example.com`。不包含协议或路径。
- `AI_API_KEY`：你的 AI 接口密钥；也可启动后在网页的模型设置中填写。默认接口地址与模型沿用 `https://api.a6api.com/v1`、`deepseek-v4.1-flash`。

```sh
docker build -t xunwei-search .
docker run -d --name xunwei-search --restart unless-stopped \
  --env-file backend.env \
  -p 127.0.0.1:8877:8877 \
  -v xunwei-data:/data \
  xunwei-search
```

在服务前配置 HTTPS 反向代理。例如使用 Caddy，将 DNS 指向服务器，再配置：

```caddyfile
search-api.example.com {
    reverse_proxy 127.0.0.1:8877
}
```

反向代理须传递 `Authorization`、`Origin` 和允许列表中的 `Host`，并保持 `/api/` 路径。浏览器支持和部署平台须允许访问该 HTTPS 服务。Docker 镜像默认监听 8877；只注入 `PORT` 的托管平台也可指定端口，显式 `XUNWEI_PORT` 或 `--port` 优先。

在 Pages 网页中输入 `https://search-api.example.com` 和访问令牌，程序会先检查健康状态，再验证带令牌的平台接口，通过后才显示已连接。随后在「搜索与模型设置」中检查 AI 配置，即可使用搜索、总结与逐轮报告。

管理员访问令牌授权使用原有管理服务，能够查看其历史、资料及修改配置，只应由站主保管。公开模式普通访客使用程序生成的临时会话令牌，只能访问自己的会话。关闭公开模式时，仍使用原有私人访问令牌机制，适合自己或受信任的小范围使用者。

## Pages 连接本机服务

自己使用时，也可以让 Pages 网页连接本机 Python 服务。单独开一个 PowerShell，替换账号和令牌后运行：

```powershell
$env:XUNWEI_ACCESS_TOKEN = '换成新生成的随机令牌'
$env:XUNWEI_ALLOWED_ORIGINS = 'https://YOUR-USERNAME.github.io'
$env:XUNWEI_PUBLIC_HOSTS = '127.0.0.1:8878,localhost:8878'
python run.py --no-browser --port 8878 --data-dir .local/pages
```

在网页中填写 `http://127.0.0.1:8878` 和同一访问令牌。本机 HTTP 仅支持 `localhost` 或 `127.0.0.1`；远程地址必须为 HTTPS。浏览器可能要求允许访问本地网络，或阻止 HTTPS 网页访问本地服务。此时可直接打开本机程序，或使用上面的 HTTPS 服务方案。

本机原用法 `python run.py` 仍可使用，无需 Pages、跨域配置或访问令牌。跨来源或公开监听模式必须同时明确设置访问令牌、允许来源和 Host，否则服务拒绝启动。

## 配置与数据

默认模式的 AI 密钥保存在搜索服务配置或服务器环境变量中，不会返回前端。明确开启「直接填充站主 API」时，仅授权公开的站主预置会出现在生成网页中；个人密钥不会加入预置或构建。连接地址、访问令牌和模型方式选择暂存在当前浏览器标签页的 `sessionStorage` 中，关闭标签页后清除；个人模型密钥不写入浏览器存储。

`backend.env`、`.env`、`.local/`、数据库均已加入 Git 忽略规则；Docker 上下文也排除实际环境文件及用户数据。发布前应保持这些规则，勿把私人凭据放进前端、仓库变量或提交历史。公开模型预置只通过上面的显式配置注入部署产物。`backend.env.example` 是空密钥示例，可以提交。

所有搜索历史和导入资料保存在**连接的搜索服务**；公开访客使用各自的临时会话，私人服务沿用原有资料库。切换服务后会显示另一服务或会话的数据。HTTPS 服务同样使用原程序的搜索和 AI 能力；网站未收录、登录限制或平台接口不可用的内容仍可能无法取得。
