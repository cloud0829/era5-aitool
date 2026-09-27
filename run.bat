@echo off
setlocal

REM ============================================================
REM  ERA5-AItool one-click launcher (ASCII only, safe for cmd)
REM  Starts backend + frontend in MINIMIZED background windows,
REM  then auto-opens the website in the default browser.
REM ============================================================

set "PROJECT_DIR=D:\Desktop\era5-AItool"
set "PYTHON_EXE=C:\Users\lei\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "NPM_EXE=C:\Users\lei\.workbuddy\binaries\node\versions\22.22.2\npm.cmd"
set "BACKEND_DIR=%PROJECT_DIR%\backend"
set "WEB_DIR=%PROJECT_DIR%\web"
set "BACKEND_PORT=8000"
set "WEB_PORT=5173"

echo ============================================
echo  ERA5-AItool launcher
echo  backend : http://127.0.0.1:%BACKEND_PORT%
echo  frontend: http://localhost:%WEB_PORT%
echo ============================================
echo.

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python not found: %PYTHON_EXE%
    pause
    exit /b 1
)
if not exist "%NPM_EXE%" (
    echo [ERROR] npm not found: %NPM_EXE%
    pause
    exit /b 1
)

REM Start backend in a minimized (background) window
start /min "ERA5-Backend" cmd /k "cd /d %BACKEND_DIR% && echo [Backend] uvicorn on port %BACKEND_PORT% ... && %PYTHON_EXE% -m uvicorn era5tool.main:app --host 127.0.0.1 --port %BACKEND_PORT% --reload"

REM Give backend a moment to boot
timeout /t 3 /nobreak >nul

REM Start frontend in a minimized (background) window
start /min "ERA5-Frontend" cmd /k "cd /d %WEB_DIR% && echo [Frontend] vite dev on port %WEB_PORT% ... && %NPM_EXE% run dev -- --port %WEB_PORT%"

REM Wait until the frontend answers, then open the browser (max ~30s)
echo Waiting for frontend to be ready ...
set "TRIES=0"
:waitloop
set /a TRIES+=1
if %TRIES% gtr 30 goto :openbrowser
timeout /t 1 /nobreak >nul
curl -s -o nul "http://127.0.0.1:%WEB_PORT%/" >nul 2>&1
if %errorlevel%==0 goto :openbrowser
goto :waitloop
:openbrowser
start "" "http://localhost:%WEB_PORT%"

echo.
echo Services are running in the background (minimized in taskbar).
echo Browser should now be open at http://localhost:%WEB_PORT%
echo Close the minimized windows to stop the services.
echo.
pause
