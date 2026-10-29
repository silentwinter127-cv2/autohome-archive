@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo 正在后台启动相册服务（关掉这个黑窗口也不会停）...
start "" /b pythonw "gallery\server.py" --port 8765
timeout /t 2 >nul
start "" "http://127.0.0.1:8765/"
