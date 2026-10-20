# 持续部署（CD）

push 到 `master` → `CI` 跑单元测试 → 成功后 `Deploy` 自动部署到本机。

## 部署流程
1. `git fetch` + `git merge --ff-only origin/master`（部署目录 `C://Users//Lenovo//dailyreport`，是指向项目真实目录的 junction，用于规避中文路径编码问题）
2. `pip install -r web/requirements.txt`
3. 结束并重启计划任务 `DailyReportWeb`（常驻运行 `scripts/run_web.cmd`）
4. 健康检查：`127.0.0.1:5000/archive` 与 `5001/login` 均需返回 200

## 运行环境
- 自托管 runner：`C://Users//Lenovo//actions-runner`，计划任务 `GitHubActionsRunner`（登录时自启），runner 名 `Lenovo-Desktop`，标签 `self-hosted/windows/x64/local-deploy`
- 网站常驻：计划任务 `DailyReportWeb`（登录时自启）
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
计划任务 `DailyReportWebGuard` 每 5 分钟检查 5000/5001，未监听则重启 `DailyReportWeb`，日志写到 `.webguard.log`。
用于兜底 runner 任务结束时清理子进程、或网站进程意外退出的情况。

## 回滚
`git revert <问题提交>` 后 push，CI + CD 会重新部署到回滚后的版本。
