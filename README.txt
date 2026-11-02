CASH COUNT APP (Python + Flask + SQLite)

CHALANE KA TARIKA
1. Python 3.9+ install karein (python.org). Install karte waqt "Add Python to PATH" tick karein.
2. Is folder me run.bat par double-click karein.
   (Pehli baar libraries khud install hongi, phir browser khul jayega.)
3. Login: username  admin   password  admin123   (login ke baad Admins tab me naya admin banayein)
4. "Machine connect" me COM port chunein aur baud rate (aam taur par 9600) daalein.
5. Thermal printer ko Windows ka default printer rakhein.
   Print dialog me Margins: None, Headers/footers: off.

Linux/Mac par:  pip install -r requirements.txt  &&  python app.py

DATA: sab cashcount.db file me save hota hai (isi folder me). Backup ke liye bas ye file copy karein.
AGAR AMOUNT GALAT AAYE: app.py me parse_line() function badlein (machine ka format alag ho sakta hai).
