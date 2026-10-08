@echo off
REM workbuddy2api 本地原生启动（Windows）
cd /d "%~dp0..\.."
".venv\Scripts\python.exe" "deploy\local\serve.py"
