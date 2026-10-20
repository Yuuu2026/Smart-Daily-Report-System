@echo off
rem 常驻启动两个 Flask 网站（供计划任务 / 自动部署调用）
rem 看门狗与浏览器启动均关闭：进程常驻，不弹窗
setlocal
set "START_WEB_NO_WATCHDOG=1"
set "START_WEB_NO_BROWSER=1"
cd /d "%~dp0.."
py -3.12 start_web.py
