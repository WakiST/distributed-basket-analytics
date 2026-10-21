@echo off
cd /d "%~dp0"
python run_web.py
if errorlevel 1 pause
