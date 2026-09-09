@echo off
REM Masseberegning - start script for Windows.
REM Creates a virtual environment on first run, then starts the app.
REM
REM This runs the app the way it always did: one user, on this machine, no
REM sign-in and no database. LOCAL_SINGLE_USER=1 turns access control off, and
REM the app refuses to accept that flag on anything but a loopback address, so
REM it cannot be reused by accident for a real deployment. For the deployed
REM setup with invitations, see README-DEPLOY.md.

cd /d "%~dp0"

if not exist ".venv" (
    echo Setter opp virtuelt miljo ^(forste gang, tar et par minutter^)...
    python -m venv .venv
    if errorlevel 1 goto :nopython
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip --quiet
    python -m pip install -r requirements.txt
    if errorlevel 1 goto :pipfail
) else (
    call .venv\Scripts\activate.bat
)

set LOCAL_SINGLE_USER=1
set SERVE_STATIC=1
set HOST=127.0.0.1
set PORT=8000

echo.
echo   Masseberegning kjorer paa http://127.0.0.1:8000
echo   Lokal enbrukermodus - ingen innlogging.
echo   Lukk dette vinduet for aa stoppe.
echo.

start "" http://127.0.0.1:8000
python app.py
goto :eof

:nopython
echo.
echo   Fant ikke Python. Installer Python 3.10 eller nyere fra python.org
echo   og huk av for "Add Python to PATH".
pause
goto :eof

:pipfail
echo.
echo   Installasjon av avhengigheter feilet. Se meldingen over.
pause
goto :eof
