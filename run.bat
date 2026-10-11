@echo off
chcp 65001 >nul
rem ============================================
rem  团队智能日报 - 一键启动
rem  用法: 双击本文件, 或在命令行输入 run.bat
rem ============================================
cd /d "%~dp0"

rem 采集端(日报归档), 端口 5000
netstat -ano | findstr "LISTENING" | findstr ":5000 " >nul 2>&1
if errorlevel 1 (
    start "日报采集端 5000" python web\app.py
) else (
    echo [跳过] 5000 端口已在运行, 不重复启动
)

rem 发布端(成员登录发布), 端口 5001
netstat -ano | findstr "LISTENING" | findstr ":5001 " >nul 2>&1
if errorlevel 1 (
    start "成员发布端 5001" python web\publisher_app.py
) else (
    echo [跳过] 5001 端口已在运行, 不重复启动
)

rem 等 2 秒让服务起来
timeout /t 2 /nobreak >nul

rem 打开浏览器
start "" http://127.0.0.1:5000/archive

echo.
echo  日报归档:  http://127.0.0.1:5000/archive
echo  成员发布:  http://127.0.0.1:5001/
echo.
echo  关闭服务: 在对应的黑色窗口里按 Ctrl+C, 或直接关掉那个窗口。
echo.
pause
