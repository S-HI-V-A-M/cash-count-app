@echo off
cd /d "%~dp0"
if not exist venv (
  echo Pehli baar setup ho raha hai...
  python -m venv venv
  call venv\Scripts\activate
  pip install -r requirements.txt
) else (
  call venv\Scripts\activate
)
start "" http://localhost:5000
python app.py
pause
