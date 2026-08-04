@echo off
setlocal

rem Always run from this script's own folder, regardless of where it's launched from.
cd /d "%~dp0"

where conda >nul 2>nul
if errorlevel 1 (
    echo Conda was not found on PATH.
    echo Install Miniconda/Anaconda, or open an "Anaconda Prompt" and run this script from there.
    pause
    exit /b 1
)

conda env list | findstr /b /c:"job-agent " >nul
if errorlevel 1 (
    echo Creating conda environment "job-agent" from environment.yml...
    call conda env create -f environment.yml
) else (
    echo Environment "job-agent" already exists -- syncing packages with environment.yml...
    call conda env update -f environment.yml --prune
)

if errorlevel 1 (
    echo Conda environment setup failed -- see the output above.
    pause
    exit /b 1
)

if not exist ".env" (
    echo No .env found -- copying .env.example. Edit .env with your real settings, then run this again.
    copy ".env.example" ".env" >nul
    notepad ".env"
    pause
    exit /b 0
)

echo Starting dashboard API on http://localhost:8000 ...
start "Job Agent - API" cmd /k "conda activate job-agent && python -m uvicorn api:app --host 127.0.0.1 --port 8000"

echo Starting scheduler (daily/weekly triggers) ...
start "Job Agent - Scheduler" cmd /k "conda activate job-agent && python scheduler.py"

rem If those windows immediately close or show "'conda' is not recognized" /
rem "CondaError: Run 'conda init'", conda hasn't been initialized for a plain
rem Command Prompt yet -- run `conda init cmd.exe` once from an Anaconda
rem Prompt, restart your terminal, then run this script again.

echo Waiting for the API to come up...
timeout /t 3 /nobreak >nul

echo Opening dashboard...
start "" "frontend\index.html"

echo.
echo Job Application Agent is running (conda env: job-agent).
echo   - API + Scheduler are in the two new console windows -- close them to stop.
echo   - Dashboard opened in your browser (reads from http://localhost:8000).
echo.
endlocal
