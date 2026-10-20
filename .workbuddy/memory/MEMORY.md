# 项目长期记忆

## 仓库与推送
- 远端：`origin` → https://github.com/Yuuu2026/Smart-Daily-Report-System （**公开**仓库）
- 本地分支 `master`，仓库**默认分支已改为 `master`**（2026-10-17 对齐），日常只需 `git push`。
  历史的 `main` 分支仍存在且停在 d5a0bd3，若继续用需手动同步；建议删除以免混淆。
- **GitHub 推送必须走代理**（本机 Clash Verge，端口 `127.0.0.1:7897`），已配置：
  `git config --global http.https://github.com.proxy http://127.0.0.1:7897`
  代理未开时报 `Recv failure: Connection was reset`（非认证问题）。
- CI：`.github/workflows/ci.yml`，push/PR 到 main|master 时在 ubuntu-latest + Python 3.12 上跑
  `python -m unittest discover -v`，真实发现 36 个测试。公开仓库 Actions 免费无限额度。

## 本地运行
- 启动：`START_WEB_NO_WATCHDOG=1 py -3.12 start_web.py`（后台/常驻）
- 不设该变量时，start_web.py 的看门狗每 2s 检测站点，偶发超时即杀进程，后台跑会被误杀。
- 两个服务：5000 归档端、5001 发布端。模板改动需重启 Flask 才生效（debug 关闭时模板被缓存）。
- 样式缓存版本号在各模板 `style.css?v=N`，改样式后需同步提升并重启。

## 前端约定
- 新增样式统一追加到 `web/static/style.css` 末尾，用 `.dashboard-page` 作用域限定，不改原有布局。
- `dashboard_shell.html` 顶层 `{% set %}` 会遮蔽 render_template 传入的同名变量（Jinja extends），同类命名需避开（如 `workspace_display`）。
- 工作台接入卡片：飞书 `.feishu-setup-banner`、GitHub `.github-setup-banner`，由 `feishu_needs_setup` / `github_needs_setup` / `feishu_reselect` 控制。
