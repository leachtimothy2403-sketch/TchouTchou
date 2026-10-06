@echo off
REM Starts the TchouTchou API on localhost:8000 (the tunnel is what exposes it).
REM Listens on 127.0.0.1 only, so the VPS needs NO inbound firewall rule.
cd /d "%~dp0.."
if not exist "%~dp0api_env.bat" (
  echo Missing deploy\api_env.bat -- copy deploy\api_env.example.bat to api_env.bat and fill it in.
  exit /b 1
)
call "%~dp0api_env.bat"
set TCHOUTCHOU_API_HOST=127.0.0.1
set TCHOUTCHOU_API_PORT=8000
python main.py
