@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "gallery\server.pid" (
  echo 没找到 gallery\server.pid，可能不是用后台脚本启动的。
  echo 可以手动结束：taskkill /f /im pythonw.exe
  pause
  exit /b 1
)
set /p PID=<gallery\server.pid
echo 正在停止相册服务 PID=%PID% ...
taskkill /f /pid %PID% >nul 2>&1
del /q "gallery\server.pid" >nul 2>&1
echo 已停止。
timeout /t 2 >nul
