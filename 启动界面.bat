@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 正在启动经营周报准入检查界面...
echo 启动后浏览器打开 http://127.0.0.1:8000
echo 关闭这个窗口即停止服务。
echo.
where python >nul 2>nul
if errorlevel 1 (
  py -3 web\server.py --port 8000
) else (
  python -u web\server.py --port 8000
)
pause
