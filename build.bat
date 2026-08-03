@echo off
title Tradalgo — Build
color 0A

echo.
echo  ============================================
echo   Tradalgo — Building tradalgo.exe
echo  ============================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.
    echo  Download from https://www.python.org/downloads/
    echo  Tick "Add Python to PATH" during install.
    pause & exit /b 1
)
for /f "tokens=*" %%i in ('python --version') do echo  [1/4] %%i found.

echo  [2/4] Installing dependencies...
pip install requests flask numpy pyinstaller pywebview --quiet --upgrade
if errorlevel 1 (
    echo  ERROR: pip install failed.
    pause & exit /b 1
)
echo        Done.

echo  [3/4] Building tradalgo.exe (60-120 seconds)...
echo.
pyinstaller ^
    --onefile ^
    --name tradalgo ^
    --console ^
    --hidden-import flask ^
    --hidden-import flask.templating ^
    --hidden-import jinja2 ^
    --hidden-import logging.handlers ^
    --hidden-import werkzeug ^
    --hidden-import werkzeug.serving ^
    --hidden-import requests ^
    --hidden-import numpy ^
    --hidden-import numpy.core ^
    --hidden-import numpy.core._multiarray_umath ^
    --hidden-import smtplib ^
    --hidden-import email.mime.multipart ^
    --hidden-import email.mime.text ^
    --hidden-import concurrent.futures ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.messagebox ^
    --hidden-import webview ^
    --hidden-import webview.platforms.winforms ^
    --collect-submodules numpy ^
    --collect-submodules flask ^
    --collect-submodules webview ^
    --clean ^
    --noconfirm ^
    tradalgo.py

if errorlevel 1 (
    echo.
    echo  ERROR: Build failed. See output above.
    pause & exit /b 1
)

echo  [4/4] Finalising...
copy /Y "dist\tradalgo.exe" "tradalgo.exe" >nul
rmdir /S /Q build        >nul 2>&1
rmdir /S /Q dist         >nul 2>&1
rmdir /S /Q __pycache__  >nul 2>&1
del   /Q  tradalgo.spec  >nul 2>&1

echo.
echo  ============================================
echo   SUCCESS — tradalgo.exe is ready
echo  ============================================
echo.
echo   Double-click tradalgo.exe to launch.
echo   Opens as a native desktop app window.
echo.
echo   If window does not appear, the app will
echo   open in your browser at localhost:5000
echo.
pause
