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
  `python -m unittest discover -v`，真实发现 40 个测试。公开仓库 Actions 免费无限额度。
- 想验证整条流水线是否还能跑通时，用 `git commit --allow-empty` 发空提交即可（会完整触发 CI → Deploy → Pages），
  **不要往公开仓库塞"测试用"的无用代码**。

## CD（2026-10-20 接通）
- 链路：push master → `CI` 成功 → `Deploy`（workflow_run 触发）在本机自托管 runner 上部署。
- runner：`C:\Users\Lenovo\actions-runner`，名称 `Lenovo-Desktop`，标签 `self-hosted/windows/x64/local-deploy`；
  **由 `web_supervisor.py` 直接 `CREATE_NO_WINDOW` 启动 `bin\Runner.Listener.exe run`**（读 `actions-runner\.env` 的代理），
  日志 `.runner.out.log`，掉线最多每 120s 重试一次。**计划任务 `GitHubActionsRunner` 已删除**：
  它走 `run.cmd`，每次启动弹控制台并打印 `1 file(s) copied.` / `√ Connected to GitHub`，用户可见。
- 部署步骤：`git fetch` + `merge --ff-only` → `pip install -r web/requirements.txt` → 重启 → 健康检查 → 汇总。
- 部署/运行路径：`C:\Users\Lenovo\dailyreport`（**指向项目真实目录的 junction**，规避中文路径在 Windows PowerShell 5.1 下的编码问题）。**项目目录若改名/移动，junction 会失效，需重建**。
- 网站常驻：计划任务 `DailyReportWebGuard` 用 `pyw.exe -3.12 scripts\web_supervisor.py` 拉起**单个常驻守护进程**
  （GUI 子系统，全程无窗口），守护每 30s 检查 5000/5001，掉线则用 `DETACHED_PROCESS` 重新拉起对应 Flask 应用。
  部署时结束端口占用进程即可，守护约 30s 内自动恢复（`deploy.yml` 已改为这种方式）。
- 旧的 `DailyReportWeb`（`run_web.cmd` + cmd 控制台）已删除：控制台被回收时 python 会收到
  `0xC000013A (STATUS_CONTROL_C_EXIT)` 静默退出，是站点反复掉线的根因。
- 架构注意：`deploy.yml` 里的 PowerShell **必须全 ASCII**（runner 以无 BOM UTF-8 写脚本，PS 5.1 会按 GBK 解析导致中文乱码）。
- 手动重部署：Actions → Deploy → Run workflow。回滚：`git revert` 后 push，会自动部署回滚版本。

## 代码结构（2026-10-21 规整后）
- `web/app.py`（归档端 + 数据库 schema/连接）→ `web/publisher_repo.py`（发布端数据层）
  → `web/publisher_app.py`（发布端路由，只做请求编排）。
- **发布端所有数据库读写、时间转换、视图模型都在 `publisher_repo.py`**，新增页面时先在这里加查询函数，
  不要把 SQL 写回路由文件。公共字段用 `_base_item()`，时间显示用 `to_local()` / `local_label()` / `relative_time()`。
- 路由统一两层装饰器：`@login_required`（未登录跳登录页）、`@csrf_protected`（表单 token 失败返回 400）；
  需要"闪提示 + 跳回某页"的用 `csrf_redirect("endpoint", **args)`。
- 测试用 `patch("web.publisher_app.xxx")` 打桩 `send_progress_email` / `smtp_settings_from_env` /
  `collect_feishu_recent_messages` / `store_feishu_app_secret` / `list_feishu_chats` / `send_test_email`，
  这些调用点必须留在 `publisher_app.py`，否则测试失效。

## 本地运行
- 手动启动：`START_WEB_NO_WATCHDOG=1 py -3.12 start_web.py`（日常不用管，计划任务 `DailyReportWebGuard` 已常驻）
- 手动修复一次：`py -3.12 scripts/web_supervisor.py --once`（端口 5099 单例锁，与常驻守护不冲突）
- 不设该变量时，start_web.py 的看门狗每 2s 检测站点，偶发超时即杀进程，后台跑会被误杀。
- `START_WEB_NO_BROWSER=1` 可阻止启动时自动弹浏览器（CD 部署时必设，否则每次部署弹窗）。
- 两个服务：5000 归档端、5001 发布端。模板改动需重启 Flask 才生效（debug 关闭时模板被缓存）。
- 样式缓存版本号在各模板 `style.css?v=N`，改样式后需同步提升并重启。

## 前端约定
- 新增样式统一追加到 `web/static/style.css` 末尾，用 `.dashboard-page` 作用域限定，不改原有布局。
- `dashboard_shell.html` 顶层 `{% set %}` 会遮蔽 render_template 传入的同名变量（Jinja extends），同类命名需避开（如 `workspace_display`）。
- 工作台接入卡片：飞书 `.feishu-setup-banner`、GitHub `.github-setup-banner`，由 `feishu_needs_setup` / `github_needs_setup` / `feishu_reselect` 控制。

## 最近代码改动（工作台可发送）
- 来源是 `.claude/recent_code_changes.json`，由 Claude Code 的 PostToolUse hook 写入；**只在 Claude Code 会话里更新**，
  WorkBuddy 改代码不会触发，记录会停在最后一次 Claude Code 编辑。
- `_recent_code_changes.html` 用 `code_changes_editable` 区分：工作台渲染「勾选框 + 可编辑 textarea」，
  归档 / 飞书 / GitHub 页只读。
- 发布走 `chosen_code_changes()`：表单带 `code_changes_present=1` 时看 `include_code_changes` 与 `code_changes_text`，
  不勾选或文本为空就不附加；缺该标记（旧表单、脚本调用）沿用「全部附加」以兼容测试。
- 邮件侧 `deliver_progress_email` / `send_progress_email` 有 `recent_changes_text`，用用户改过的措辞而不是结构体。
- 记录不会因提交而清理，可能显示已提交的旧条目。

## 日报表单字段与工作量柱状图
- 工作台正文只有两问：「解决了什么问题？」(`solved`) 与「下一步准备做什么？」(`next_steps`)；`finished` / `problems`
  字段仍保留在后台解析（历史数据兼容），表单不再渲染。
- 段落标签集中在 `report_core.PUBLISHED_SECTION_LABELS`，新增字段必须同步三处：
  `PUBLISHED_SECTION_LABELS`、`publisher_repo.published_item()` 的 labels、`report_quality.SECTION_LABELS`。
- 工作量口径：`publisher_repo.workload_series()` = 当天 `published_items + records(tasks) + records(collaborations)
  + github_commits + feishu_messages` 的条数，默认近 7 天；工作台与邮件共用同一个函数，不要各写一套统计。
- 邮件里的柱状图只能用表格拼（`td height` + `bgcolor`），邮件客户端不执行 JS；`_email_bar_chart` / `_email_stat_cards`
  是邮件专用渲染函数，改样式时保持内联样式、不要引入 `<style>` 或 class。
- 邮件 `ul` 用 `list-style:none`：正文行首本身带「- 」，否则会出现双项目符号。

## 静态归档站（GitHub Pages）
- 导出：`py -3.12 scripts/export_static_site.py`（可加 `--with-attachments`），产物在 `dist_site/`，已被忽略不入库。
- 模板：`scripts/static_templates/{index,report}.html`，复用 `web/static/style.css` 类名，不依赖 Flask 路由。
- 发布：`.github/workflows/pages.yml` 在 Deploy 成功后于本机 runner 导出并 `push --force` 到 `gh-pages` 分支（临时目录建仓，不污染工作区）。
- 站点：https://yuuu2026.github.io/Smart-Daily-Report-System/ ，仓库 Settings → Pages 需设为「分支 gh-pages / root」。
- 导出内容会公开（成员姓名 + 工作进展），默认不含成员上传图片。
- 服务守护：见上文「CD」小节 —— `scripts/web_supervisor.py` + 计划任务 `DailyReportWebGuard`（pythonw 无窗口常驻，30s 巡检）。
  日志 `.webguard.log`（守护事件）、`.web.out.log`（Flask 输出），两者都已忽略。
- 排查经验：用户反馈「每分钟弹终端窗口」= 计划任务用 `powershell.exe -WindowStyle Hidden` 每分钟触发，Hidden 只隐藏窗口自身、
  控制台仍会闪。改用 GUI 子系统的 `pyw.exe` 常驻单进程后彻底无窗口；`wscript` 被安全策略拦截、S4U 需管理员权限，都不可用。
