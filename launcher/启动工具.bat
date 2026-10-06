@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "runtime\python\python.exe" (
  echo Runtime missing. Extract the complete portable package.
  pause
  exit /b 1
)
"runtime\python\python.exe" -X utf8 "launcher\start.py" %*
if errorlevel 1 pause
