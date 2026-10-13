@echo off
title CameraWeb
cd /d "%~dp0"
py servidor.py
if errorlevel 1 (
  echo.
  echo Nao consegui iniciar. O Python 3 esta instalado? ^(comando: py^)
  pause
)
