# gitnote：GitHub 项目发现

Python 标准库驱动的 GitHub 项目发现页，从公开仓库搜索结果生成个性化推荐。

## 获取与启动

需要 Python 3.10 或更新版本，无第三方 Python 依赖。

```text
git clone https://github.com/cells114514/gitnote.git
cd gitnote
python app.py
```

macOS 或 Linux 如使用 `python3` 命令，运行 `python3 app.py`。

打开 <http://127.0.0.1:8765/> 后，页面会自动获取一批真实项目；点击“换一批”会替换当前推荐批次，旧批次中的收藏仍可在“我的收藏”找到。有会话缓存时会先显示缓存，再尝试获取新批次；限流或网络失败时保留缓存。一次发现流程可能发起多次 GitHub Search 请求。顶部输入关键词后按 Enter 或点“搜索 GitHub”，后端会查询 GitHub 的公开仓库索引，并提供分页；输入过程中不会逐字请求。左侧的 Stars 总榜使用 GitHub 累计星标排序，近期 Trending 保留 GitHub Trending 网页的原榜顺序，可切换今天、本周、本月。GitHub Search 每个查询最多可访问前 1,000 条结果。匿名搜索也可用，但额度较低。

点击任意卡片主体可打开站内详情。真实仓库在打开后按需读取公开仓库元数据和 GitHub 渲染后的 README HTML，再使用本地 DOMPurify 净化后展示。超大或不可用的 README 会显示原仓库链接；第三方图片需主动点击才能加载。卡片上的箭头仍可直接打开 GitHub。顶栏按钮可以切换明亮与黑暗模式；深色背景参考 VS Code 经典 Dark+ 的黑灰色，手动选择会保存在浏览器。

## GitHub 登录

登录用于让 **Python 后端**以你的 GitHub 身份调用公开仓库搜索。按 [GitHub 官方步骤](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)打开 GitHub → Settings → Developer settings → **OAuth apps** → **New OAuth App**，注册一个自己使用的 OAuth App。当前注册页面在 **Redirect URIs** 区域提供 **Redirect URI** 输入框；GitHub 文档也将这个授权后回跳地址称为 Authorization callback URL。各项填写如下：

- Application name：例如 `gitnote local`。
- Homepage URL：`http://127.0.0.1:8765/`。
- Redirect URI：`http://127.0.0.1:8765/auth/callback`。

**Allow wildcard matching** 和 **Enable Device Flow** 不需要开启；可保留默认启用的 **Expire user access tokens**，程序支持自动续期。注册后在应用设置页取得 Client ID，并生成 Client Secret。此程序使用 `127.0.0.1` 回环地址；按 [GitHub 的回环重定向规则](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps#loopback-redirect-urls)，`python app.py --port 其他端口` 时 GitHub 允许授权请求携带实际监听端口，因此通常无需修改注册的回调地址。页面仍应从 `http://127.0.0.1:端口/` 打开。

启动 Python 前设置本机环境变量（不要把值写进代码或提交到仓库）：

```powershell
$env:GH_CLIENT_ID = "你的 OAuth App Client ID"
$env:GH_CLIENT_SECRET = "你的 OAuth App Client Secret"
python app.py
```

macOS 或 Linux 可使用：

```sh
export GH_CLIENT_ID="你的 OAuth App Client ID"
export GH_CLIENT_SECRET="你的 OAuth App Client Secret"
python3 app.py
```

打开页面并点“连接 GitHub”，在 GitHub 授权页确认后即可使用认证搜索。程序使用 OAuth 授权码、`state` 与 PKCE S256，不主动申请仓库或账户写入权限。access token 和可能返回的 refresh token 仅放在 Python 进程内存，浏览器只保存随机、HttpOnly 的会话 cookie；短期令牌会在到期前自动续期，重启服务后需重新登录。点击“断开”会清除内存会话；如需撤销 GitHub 端授权，可在 GitHub 设置的 Applications 中撤销该 OAuth App。

[GitHub Search 官方限额](https://docs.github.com/en/rest/search/search#rate-limit)对公开仓库搜索为匿名 10 次/分钟、认证 30 次/分钟；本项目另设保守的本地预算，分别为 6 次/分钟和 20 次/分钟。因此登录后本项目的实际搜索预算由每分钟 6 次提高到 20 次。普通 REST API 的主限额则从匿名每 IP 60 次/小时提高到认证用户通常 5,000 次/小时，搜索仍受独立限额约束。页面显示 GitHub 最近一次 Search 响应给出的额度；重复查询命中公开缓存时不会消耗新请求，也不会刷新显示值。

登录后可主动点击“导入我 Star 过的仓库”，最多读取前 200 个 Star 仓库的 topics 和语言，用于建立本地兴趣画像。导入不会在服务器保存完整仓库列表。浏览器保存的是汇总画像，可随时重置。

## 推荐逻辑

推荐逻辑按 GitTok.dev 当前公开的 `taste` 与 `feed` 实现移植到 Python，并固定对照 commit `565fbd4ddd0e03a5a969e7becbbec12d7aa2f834`：

- 喜欢、打开、分享、停留和“不感兴趣”形成带权重的 topic/语言偏好；罕见 topic 得到更多信号，少量行为受到置信度折减。收藏只管理本地列表。
- 搜索前对话题做探索/利用抽样，再抽取 GitTok 的 Stars 档和 GitHub 排序方式；记录查询页码以减少重复。为适配瀑布流，一次发现最多合并 3 次同规则抽样，每次取最多 12 张，并在达到 12 张且覆盖至少 3 种语言时停止；深页为空时可能额外补查第一页。
- 搜索返回后按 topic 与语言有效偏好排序，并尽量避免滚动 10 张卡片中出现超过 2 张同语言的项目。Stars 和更新时间用于候选搜索或明确选择的榜单与最近更新视图，不混入“为你推荐”的偏好分数。

推荐卡片只显示 GitHub Search 提供的公开元数据；打开详情时额外读取仓库信息和 README。README 不执行仓库内的脚本；普通外链在新标签页打开，GitHub 自有图片懒加载，其他图片由用户主动决定是否加载。推荐画像和喜欢/收藏/隐藏列表保存在浏览器 `localStorage`；当前推荐批次及已见公开卡片保存在 `sessionStorage`。旧版 `open-wander-*` 和 `giterest-*` 数据会迁移到 `gitnote-*`。浏览器只将画像发给本机 Python 服务计算，不向第三方上报停留时长。

GitTok.dev 的代码和话题池采用[非商业许可](GITTOK_LICENSE.txt)，来源详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。商用前需要单独核对并取得所需授权。

## 文件与验证

- `app.py`：本地 HTTP 接口、OAuth 回调、榜单和详情 API。
- `github_auth.py`、`github_client.py`、`cache.py`：认证、GitHub API、Search 配额与公开结果缓存。
- `taste_profile.py`、`taste_ranking.py`、`discovery.py`、`topics.json`：GitTok 推荐画像、抽样、排序与候选搜索；`trending.py`：GitHub Trending 页面解析和缓存回退。
- `web/`：页面和交互；`web/vendor/purify.min.js`：本地固定版本的 README HTML 净化库；`tests/`：算法及 API 行为测试。
- `VERSION`：当前项目版本；`CHANGELOG.md`：版本变更记录。

```powershell
python -m unittest discover -s tests -v
```

如本机安装了 Playwright 浏览器环境，服务运行时可执行 `python tests/browser_smoke.py` 检查首次自动发现、换批、搜索、榜单、详情和主题交互；测试默认连接 8765 端口，可用 `GITNOTE_TEST_URL` 指定其他地址。设置 `GITNOTE_REAL_README=1` 后还会检查真实公开仓库的 README 渲染。

真实 OAuth 端到端验证需要本机环境变量中的有效 OAuth App 凭据。无需凭据即可运行匿名搜索和模拟响应测试。

Trending 来自 GitHub 公共网页；网页结构变化时会展示上次成功的缓存并提供原榜链接。Search/Core 限额和网络状态以 GitHub 实际响应为准。
