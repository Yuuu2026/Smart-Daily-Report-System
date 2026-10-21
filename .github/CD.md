# 持续部署（CD）

push 到 `master` → `CI` 跑单元测试 → 成功后 `Deploy` 自动部署到本机。

## 部署流程
1. `git fetch` + `git merge --ff-only origin/master`（部署目录 `C://Users//Lenovo//dailyreport`，是指向项目真实目录的 junction，用于规避中文路径编码问题）
2. `pip install -r web/requirements.txt`
3. 结束占用 5000/5001 的进程，由常驻守护 `scripts/web_supervisor.py` 在约 30 秒内重新拉起两个 Flask 应用
4. 健康检查：`127.0.0.1:5000/archive` 与 `5001/login` 均需返回 200（最多轮询 120 秒）

## 运行环境
- 自托管 runner：`C://Users//Lenovo//actions-runner`，runner 名 `Lenovo-Desktop`，标签 `self-hosted/windows/x64/local-deploy`。
  **由 `scripts/web_supervisor.py` 直接以无窗口方式拉起**（`bin\Runner.Listener.exe run`，`CREATE_NO_WINDOW`，
  自动读取 `actions-runner\.env` 里的代理），掉线后最多每 2 分钟重试一次，日志写 `.runner.out.log`。
  不再使用计划任务 `GitHubActionsRunner`：那条链路会调 `run.cmd`，每次启动都弹一个控制台窗口并打印
  `1 file(s) copied.` / `Connected to GitHub`，用户能看到，已移除。
- 网站常驻：计划任务 `DailyReportWebGuard`（登录时自启，用 `pyw.exe` 运行 `scripts/web_supervisor.py`，全程无窗口）
- 电脑关机或睡眠时无法部署

## 手动触发
Actions → Deploy → Run workflow

## 静态归档站（Pages）
`Deploy` 成功后 `Pages` 自动接手：在本机 runner 上跑 `scripts/export_static_site.py`，把 `dist_site/` 推到 `gh-pages` 分支，
由 GitHub Pages 提供只读公开站点：

- 访问地址：https://yuuu2026.github.io/Smart-Daily-Report-System/
- 内容来源：本机 `web/daily_report.db` 的 `published_items`（+ `github_commits`）
- 默认不导出成员上传的图片，需要时本地执行 `py -3.12 scripts/export_static_site.py --with-attachments`
- 只发新日报、不改代码时，站点不会自动更新，可手动 Actions → Pages → Run workflow

## 服务守护
计划任务 `DailyReportWebGuard` 在登录时启动**一个**常驻守护进程（`pyw.exe` + `scripts/web_supervisor.py`），
每 30 秒检查 5000/5001 与 GitHub Actions runner：网站掉线就重新拉起对应的 Flask 应用，
runner 掉线就无窗口地重启 `Runner.Listener.exe`。日志写 `.webguard.log`、`.web.out.log`、`.runner.out.log`。

设计要点（都是踩过的坑）：
- `pyw.exe` 属 GUI 子系统，**完全不创建控制台窗口**。早期用每分钟触发的 `powershell.exe -WindowStyle Hidden`，
  会每分钟闪一次黑框；改用常驻单进程后彻底没有弹窗。
- 子进程以 `DETACHED_PROCESS` 启动，不挂在任何控制台上。旧方案用 `run_web.cmd` 起 python，进程会随控制台
  收到 `0xC000013A (STATUS_CONTROL_C_EXIT)` 而静默退出。
- 守护进程用回环端口 `5099` 做单例锁，重复启动会立即退出，因此 `--once` 与常驻守护可以共存。
- **不要用计划任务 + `run.cmd` 启动 runner**：那条链路每次都弹出控制台并打印
  `1 file(s) copied.` / `√ Connected to GitHub`，用户能看到；也更容易被控制台回收事件杀掉。
- 手动修复一次：`py -3.12 scripts/web_supervisor.py --once`

## 回滚
`git revert <问题提交>` 后 push，CI + CD 会重新部署到回滚后的版本。
