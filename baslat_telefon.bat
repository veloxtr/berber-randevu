@echo off
cd /d "%~dp0"
echo Telefonda  http://ADRES:5050  yazin. ADRES asagidaki IPv4 degeridir:
ipconfig | findstr /i "IPv4"
echo.
python app.py --ag
pause
