@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
rem Prefer the real Python 3.13 install; fall back to whatever "python" is on PATH.
set PYEXE=python
set PYEXEW=pythonw
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set PYEXE=%LOCALAPPDATA%\Programs\Python\Python313\python.exe
if exist "%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe" set PYEXEW=%LOCALAPPDATA%\Programs\Python\Python313\pythonw.exe
rem No arguments = double-clicked = start the web UI in the background and open
rem the browser. pythonw.exe has no console, so no black window ever appears;
rem "start" lets this script exit immediately instead of staying alive.
if "%~1"=="" start "" "%PYEXEW%" -m study_coach.web
rem With arguments = used from a terminal = run that one subcommand and exit.
if not "%~1"=="" "%PYEXE%" -m study_coach.cli %*
