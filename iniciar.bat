@echo off
setlocal
cd /d "%~dp0"
title Painel TSE - Presidente 2026 (2o turno)

where py >nul 2>nul
if %errorlevel%==0 (
  py painel.py --host 0.0.0.0 %*
) else (
  python painel.py --host 0.0.0.0 %*
)

if errorlevel 1 (
  echo.
  echo Nao foi possivel iniciar o painel. Verifique se o Python 3.10+ esta instalado.
  pause
)
