# Cash Count App

A lightweight web application that reads the total from a cash counting machine over RS232, stores each count in a local database, and prints a compact label for it.

Built with **Python**, **Flask**, **pyserial** and **SQLite**. No external services or internet connection are required.

## Features

- Reads the machine receipt automatically over a serial (RS232) port. No button press in the app is needed.
- Extracts the total from the `TOTAL <pieces> <amount>` line, so dates, times and denomination rows are never mistaken for the total.
- Amount is taken from the machine only. It cannot be typed in by an admin.
- Generates a unique ID for every count (format `CC-YYYYMMDD-0001`, restarting daily).
- Stores the unique ID, customer name, amount, date and time, and the admin who saved it.
- Label preview before printing, formatted for 50 mm x 25 mm thermal labels.
- Search by unique ID, or search records by ID or customer name.
- Login system with an owner account that can create additional admins.

## Requirements

- Windows 10/11 (also works on Linux and macOS)
- Python 3.9 or newer
- A USB-to-RS232 adapter (or a built-in COM port) connected to the counting machine
- A thermal label printer (optional, for printing labels)

## Quick Start (Windows)

1. Install Python from [python.org](https://www.python.org/downloads/). During installation, tick **Add Python to PATH**.
2. Download or clone this repository.
3. Double-click `run.bat`. On the first run it creates a virtual environment and installs the dependencies automatically.
4. Open **http://localhost:5000** in your browser.
5. Sign in with the default account and change it as described in the Security section below.

## Manual Setup (Windows, Linux, macOS)

```bash
python -m venv venv

# Windows
venv\Scripts\activate
# Linux / macOS
source venv/bin/activate

pip install -r requirements.txt
python app.py
```

## Default Login

| Username | Password   | Role  |
|----------|------------|-------|
| `admin`  | `admin123` | Owner |

Only the owner can create new admins (Admins tab).

## Usage

1. **Connect the machine.** On the *New count* tab select the COM port and baud rate (usually `9600`), then click **Connect**. The status line shows the connection state and the raw data received.
2. **Count the notes.** The machine sends its receipt when it prints. Press the machine's **Print / Send** button if the amount does not appear.
3. **Enter the customer name** and click **Save**. The record is stored in the database only. Nothing is printed yet.
4. **Review the label** in the preview window, then click **Print** if you want a printed copy.
5. **Look up records later** with *ID search* or the *Records* tab.

## Label Printing (50 mm x 25 mm)

1. In Windows, open *Settings > Printers & scanners > your printer > Printing preferences* and create a custom paper size of **50 mm x 25 mm**.
2. In the browser print dialog choose the thermal printer, set **Margins: None**, **Scale: 100**, and turn **Headers and footers** off.
3. Do not choose *Save as PDF* or *Microsoft Print to PDF*. These create a file on the desktop instead of printing.

To use a different label size, edit the `@page` and `#slip` rules in the CSS inside `app.py`.

## Configuration

Settings are grouped in the `Config` class at the top of `app.py`:

| Setting | Purpose |
|---------|---------|
| `HOST`, `PORT` | Address and port of the web server (default `0.0.0.0:5000`) |
| `DEFAULT_ADMIN_*` | Credentials of the first admin, created only when the database is empty |
| `ID_PREFIX` | Prefix of generated IDs (default `CC`) |
| `MAX_CUSTOMER_NAME_LENGTH` | Maximum length of the customer name |
| `RECORDS_LIMIT` | Maximum rows returned by the Records tab |
| `DEFAULT_BAUD_RATE` | Baud rate used when none is supplied |

## Data and Backup

- All records are stored in `cashcount.db` in the project folder.
- To back up your data, copy `cashcount.db` while the app is not saving a count.
- `secret.key` is generated on the first run and signs login sessions. Keep it private.
- Both files are listed in `.gitignore` and must never be committed to a public repository.

## Security Notes

- **Change the default password** before using the app in production. Edit `DEFAULT_ADMIN_PASSWORD` in `Config` before the first run, or create a new admin and stop using the default account.
- The server listens on all network interfaces by default (`HOST = "0.0.0.0"`). Set `HOST = "127.0.0.1"` if only the local computer should have access.
- Do not expose the app directly to the internet.
- Keep `DEBUG` off in production. Debug mode allows code execution from the browser.

## Troubleshooting

| Problem | What to check |
|---------|---------------|
| No COM port in the list | Plug in the USB-to-RS232 adapter, install its driver (FTDI, Prolific, CH340 or CP210x), then click **Refresh ports**. |
| "Connection failed" or access denied | Another program is using the port. Close it and reconnect. |
| Connected, but no amount appears | Press **Print / Send** on the machine. Check the raw data box. If you see garbled characters, try another baud rate. |
| Wrong amount is read | The receipt format may differ. Adjust the pattern in `MachineReader.TOTAL_LINE` in `app.py`. |
| Label prints blank or splits across labels | Confirm the 50 x 25 mm paper size in the printer driver and calibrate the label gap using the printer's feed or calibrate button. |
| Print creates a file on the desktop | A PDF printer is selected. Choose the thermal printer in the print dialog. |
| Login page looks outdated after an update | Hard refresh the browser with `Ctrl + F5`. |

## Project Structure

```
.
├── app.py              # Application (backend, machine reader, frontend)
├── requirements.txt    # Python dependencies
├── run.bat             # One-click launcher for Windows
├── README.md           # This file
└── .gitignore          # Excludes the database, secret key and virtual environment
```

Files created at runtime: `cashcount.db`, `secret.key`, and optionally `machine_log.txt`.

## Tech Stack

- Python 3.9+
- Flask
- pyserial
- SQLite (built into Python)
- Plain HTML, CSS and JavaScript (no build step)
