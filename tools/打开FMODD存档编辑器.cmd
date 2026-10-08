@echo off
setlocal
cd /d "%~dp0.."
pythonw tools\fmodd_save_editor.py
if errorlevel 1 python tools\fmodd_save_editor.py
