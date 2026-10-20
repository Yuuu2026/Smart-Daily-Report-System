# 项目长期记忆

## 仓库与推送
- 远端：`origin` → https://github.com/Yuuu2026/Smart-Daily-Report-System （**公开**仓库）
- 本地分支 `master`，仓库**默认分支已改为 `master`**（2026-10-17 对齐），日常只需 `git push`。
  `main` 与 `master` 已于 2026-10-20 对齐到同一提交（23c2b05），推送时注意两条分支不会自动同步，
  单用 `master` 即可；如需保持 main 最新，用 `git push origin master:main` 快进。
- **GitHub 推送必须走代理**（本机 Clash Verge，端口 `127.0.0.1:7897`），已配置：
  `git config --global http.https://github.com.proxy http://127.0.0.1:7897`
  代理未开时报 `Recv failure: Connection was reset`（非认证问题）。
- CI：`.github/workflows/ci.yml`，push/PR 到 main|master 时在 ubuntu-latest + Python 3.12 上跑
  `python -m unittest discover -v`，真实发现 36 个测试。公开仓库 Actions 免费无限额度。

## CD（2026-10-20 接通）
- 链路：push master → `CI` 成功 → `Deploy`（workflow_run 触发）在本机自托管 runner 上部署。
- runner：`C:\Users\Lenovo\actions-runner`，名称 `Lenovo-Desktop`，标签 `self-hosted/windows/x64/local-deploy`；
  用计划任务 `GitHubActionsRunner`（登录自启）拉起，无管理员权限故未装系统服务。`.env` 里配了代理。
- 部署步骤：`git fetch` + `merge --ff-only` → `pip install -r web/requirements.txt` → 重启 → 健康检查 → 汇总。
- 部署/运行路径：`C:\Users\Lenovo\dailyreport`（**指向项目真实目录的 junction**，规避中文路径在 Windows PowerShell 5.1 下的编码问题）。**项目目录若改名/移动，junction 会失效，需重建**。
- 网站常驻：计划任务 `DailyReportWeb` 运行 `scripts/run_web.cmd`（内部设 `START_WEB_NO_WATCHDOG=1`、`START_WEB_NO_BROWSER=1`）。
  部署时用 `schtasks /End` + `/Run` 重启，这样进程不被 runner 的 job 结束清理杀掉。
- 架构注意：`deploy.yml` 里的 PowerShell **必须全 ASCII**（runner 以无 BOM UTF-8 写脚本，PS 5.1 会按 GBK 解析导致中文乱码）。
- 手动重部署：Actions → Deploy → Run workflow。回滚：`git revert` 后 push，会自动部署回滚版本。

## 本地运行
- 手动启动：`START_WEB_NO_WATCHDOG=1 py -3.12 start_web.py`（日常更推荐用计划任务 `DailyReportWeb` 常驻）
- 不设该变量时，start_web.py 的看门狗每 2s 检测站点，偶发超时即杀进程，后台跑会被误杀。
- `START_WEB_NO_BROWSER=1` 可阻止启动时自动弹浏览器（CD 部署时必设，否则每次部署弹窗）。
- 两个服务：5000 归档端、5001 发布端。模板改动需重启 Flask 才生效（debug 关闭时模板被缓存）。
- 样式缓存版本号在各模板 `style.css?v=N`，改样式后需同步提升并重启。

## 前端约定
- 新增样式统一追加到 `web/static/style.css` 末尾，用 `.dashboard-page` 作用域限定，不改原有布局。
- `dashboard_shell.html` 顶层 `{% set %}` 会遮蔽 render_template 传入的同名变量（Jinja extends），同类命名需避开（如 `workspace_display`）。
- 工作台接入卡片：飞书 `.feishu-setup-banner`、GitHub `.github-setup-banner`，由 `feishu_needs_setup` / `github_needs_setup` / `feishu_reselect` 控制。

## 静态归档站（GitHub Pages）
- 导出：`py -3.12 scripts/export_static_site.py`（可加 `--with-attachments`），产物在 `dist_site/`，已被忽略不入库。
- 模板：`scripts/static_templates/{index,report}.html`，复用 `web/static/style.css` 类名，不依赖 Flask 路由。
- 发布：`.github/workflows/pages.yml` 在 Deploy 成功后于本机 runner 导出并 `push --force` 到 `gh-pages` 分支（临时目录建仓，不污染工作区）。
- 站点：https://yuuu2026.github.io/Smart-Daily-Report-System/ ，仓库 Settings → Pages 需设为「分支 gh-pages / root」。
- 导出内容会公开（成员姓名 + 工作进展），默认不含成员上传图片。
- 服务守护：计划任务 `DailyReportWebGuard` 每 1 分钟巡检 5000/5001，掉线则强杀残留监听进程并重启 `DailyReportWeb`（日志 `.webguard.log`）。
  **已知现象**：每次 Deploy/Pages 任务结束后 python 子进程会被清掉、任务状态变 Ready，守护会在 1 分钟内自动拉起。
