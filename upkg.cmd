@echo off
setlocal
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "%~dp0upkg.py" %*
) else (
    python "%~dp0upkg.py" %*
)
exit /b %errorlevel%
