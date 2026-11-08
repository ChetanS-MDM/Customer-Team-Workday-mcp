@echo off
REM Setup script for Windows
cd /d "%~dp0"

echo === Workday MCP Setup ===

if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

echo Installing dependencies...
.venv\Scripts\python.exe -m pip install -q -r requirements.txt

echo Installing Playwright Chromium browser...
.venv\Scripts\python.exe -m playwright install chromium

echo.
echo === Setup complete! ===
echo.
echo Add this to your Claude Desktop config
echo (Settings ^> Developer ^> Edit Config):
echo.
echo {
echo   "mcpServers": {
echo     "workday": {
echo       "command": "%CD%\\.venv\\Scripts\\python.exe",
echo       "args": ["%CD%\\server.py"],
echo       "env": {
echo         "WORKDAY_BASE_URL": "https://wd115.myworkday.com",
echo         "WORKDAY_TENANT": "lilly"
echo       }
echo     }
echo   }
echo }
echo.
echo Then restart Claude Desktop and say: "Sign me in to Workday"
pause
