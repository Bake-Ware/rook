@echo off
rem Run from the repository root (the directory containing this file).
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python -m rook %*
