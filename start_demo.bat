@echo off
cd /d "%~dp0"
python run_demo.py
if errorlevel 1 pause
