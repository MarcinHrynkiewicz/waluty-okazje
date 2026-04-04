@echo off
echo Instalowanie zaleznosci...
pip install -r requirements.txt

echo.
echo Uruchamianie aplikacji na http://localhost:5000
python app.py
pause
