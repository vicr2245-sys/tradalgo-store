@echo off
title Tradalgo — Build Installer
color 0A

echo.
echo  ============================================
echo   Tradalgo — Building Installer
echo  ============================================
echo.

if not exist "tradalgo.exe" (
    echo  ERROR: tradalgo.exe not found. Run build.bat first.
    pause & exit /b 1
)

if not exist "LICENSE.txt" (
    echo Tradalgo Software Licence > LICENSE.txt
    echo This software requires a valid licence key. >> LICENSE.txt
)

if not exist "installer_output" mkdir installer_output

echo  Building TradalgoSetup.exe...
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" TradalgoSetup.iss

if errorlevel 1 (
    echo.
    echo  Build failed — see error above.
    pause & exit /b 1
)

echo.
echo  ============================================
echo   Done: installer_output\TradalgoSetup.exe
echo  ============================================
echo.
pause
