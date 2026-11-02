"""Cash Count Application.

A small Flask web application that:
  * reads the receipt sent by a cash counting machine over RS232,
  * extracts the total amount from the "TOTAL" line,
  * stores every count (unique ID, customer, amount, date/time, admin) in SQLite,
  * previews and prints a 50 mm x 25 mm label for each count.

Run with:  python app.py   (then open http://localhost:5000)
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import sqlite3
import threading
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from typing import Iterator, Optional

import serial
import serial.tools.list_ports
from flask import Flask, Response, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("werkzeug").setLevel(logging.WARNING)  # hide the per-request polling noise
log = logging.getLogger("cashcount")


# =============================================================================
# Configuration
# =============================================================================
class Config:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    DB_PATH = os.path.join(BASE_DIR, "cashcount.db")
    SECRET_KEY_PATH = os.path.join(BASE_DIR, "secret.key")

    HOST = "0.0.0.0"
    PORT = 5000

    DEFAULT_ADMIN_USERNAME = "admin"
    DEFAULT_ADMIN_NAME = "Admin"
    DEFAULT_ADMIN_PASSWORD = "admin123"

    ID_PREFIX = "CC"
    MAX_CUSTOMER_NAME_LENGTH = 60
    RECORDS_LIMIT = 200

    DEFAULT_BAUD_RATE = 9600
    SERIAL_TIMEOUT_SECONDS = 0.5
    RAW_LOG_SIZE = 40
    RAW_LOG_VISIBLE_LINES = 8


def load_secret_key() -> str:
    """Return the session signing key, generating and persisting it on first run."""
    if not os.path.exists(Config.SECRET_KEY_PATH):
        with open(Config.SECRET_KEY_PATH, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_hex(32))
    with open(Config.SECRET_KEY_PATH, encoding="utf-8") as handle:
        return handle.read().strip()


app = Flask(__name__)
app.secret_key = load_secret_key()


# =============================================================================
# Database
# =============================================================================
@contextmanager
def get_db() -> Iterator[sqlite3.Connection]:
    """Yield a connection that commits on success, rolls back on error and always closes."""
    connection = sqlite3.connect(Config.DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db() -> None:
    """Create the schema, apply simple migrations and seed the default admin."""
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS admins (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                name     TEXT NOT NULL,
                pw       TEXT NOT NULL,
                role     TEXT NOT NULL DEFAULT 'admin'
            );
            CREATE TABLE IF NOT EXISTS counts (
                unique_id     TEXT PRIMARY KEY,
                total_amount  REAL NOT NULL,
                date_time     TEXT NOT NULL,
                admin_name    TEXT NOT NULL,
                customer_name TEXT NOT NULL DEFAULT ''
            );
            """
        )
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(counts)")}
        if "customer_name" not in columns:  # databases created by older versions
            conn.execute("ALTER TABLE counts ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")

        if conn.execute("SELECT 1 FROM admins").fetchone() is None:
            conn.execute(
                "INSERT INTO admins (username, name, pw, role) VALUES (?, ?, ?, 'owner')",
                (
                    Config.DEFAULT_ADMIN_USERNAME,
                    Config.DEFAULT_ADMIN_NAME,
                    generate_password_hash(Config.DEFAULT_ADMIN_PASSWORD),
                ),
            )
            log.info("Default admin created (username: %s)", Config.DEFAULT_ADMIN_USERNAME)


_count_lock = threading.Lock()


def create_count(amount: float, customer_name: str, admin_name: str) -> dict:
    """Insert a new count with the next unique ID of the day and return the stored row."""
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    prefix = f"{Config.ID_PREFIX}-{day}-"

    with _count_lock, get_db() as conn:
        last_id = conn.execute(
            "SELECT MAX(unique_id) FROM counts WHERE unique_id LIKE ?", (prefix + "%",)
        ).fetchone()[0]
        sequence = int(last_id.rsplit("-", 1)[1]) + 1 if last_id else 1
        record = {
            "unique_id": f"{prefix}{sequence:04d}",
            "total_amount": amount,
            "date_time": now.strftime("%d-%m-%Y %H:%M:%S"),
            "admin_name": admin_name,
            "customer_name": customer_name,
        }
        conn.execute(
            "INSERT INTO counts (unique_id, total_amount, date_time, admin_name, customer_name) "
            "VALUES (:unique_id, :total_amount, :date_time, :admin_name, :customer_name)",
            record,
        )
    log.info("Saved %s (%.2f) by %s", record["unique_id"], amount, admin_name)
    return record


# =============================================================================
# Counting machine (RS232)
# =============================================================================
class MachineError(Exception):
    """Raised when the serial connection cannot be established."""


class MachineReader:
    """Reads the receipt printed by the counting machine on a background thread.

    The machine sends a full receipt line by line. The amount is taken only from
    the "TOTAL  <pieces>  <amount>" line, so dates, times and denomination rows
    can never be mistaken for the total.
    """

    TOTAL_LINE = re.compile(r"TOTAL\s+\d+\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
    LINE_BREAK = re.compile(rb"\r\n|\r|\n")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._port: Optional[str] = None
        self._amount: Optional[float] = None
        self._sequence = 0
        self._status = "Machine is not connected"
        self._raw: deque[str] = deque(maxlen=Config.RAW_LOG_SIZE)

    # ---- parsing ---------------------------------------------------------
    @classmethod
    def parse_amount(cls, line: str) -> Optional[float]:
        """Return the amount from a TOTAL line, or None for any other line."""
        match = cls.TOTAL_LINE.search(line.replace(",", ""))
        return float(match.group(1)) if match else None

    # ---- public API ------------------------------------------------------
    def connect(self, port: str, baud_rate: int) -> None:
        self.disconnect()
        try:
            connection = serial.Serial(
                port,
                baud_rate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=Config.SERIAL_TIMEOUT_SECONDS,
            )
        except (serial.SerialException, OSError, ValueError) as exc:
            self._set_status(f"Connection failed: {exc}")
            raise MachineError(f"Connection failed: {exc}") from exc

        self._stop.clear()
        with self._lock:
            self._port = port
            self._amount = None
            self._status = f"Connected: {port}"
        self._thread = threading.Thread(
            target=self._run, args=(connection,), name="machine-reader", daemon=True
        )
        self._thread.start()
        log.info("Connected to %s at %d baud", port, baud_rate)

    def disconnect(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            self._port = None
            self._status = "Machine disconnected"

    def clear_amount(self) -> None:
        with self._lock:
            self._amount = None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "amount": self._amount,
                "seq": self._sequence,
                "port": self._port,
                "status": self._status,
                "raw": list(self._raw)[-Config.RAW_LOG_VISIBLE_LINES:],
            }

    # ---- internals -------------------------------------------------------
    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _run(self, connection: serial.Serial) -> None:
        buffer = b""
        try:
            while not self._stop.is_set():
                try:
                    chunk = connection.read(64)
                except (serial.SerialException, OSError) as exc:
                    log.warning("Serial read failed: %s", exc)
                    self._set_status(f"Port error: {exc}")
                    break
                if chunk:
                    buffer = self._consume(buffer + chunk)
        finally:
            try:
                connection.close()
            except (serial.SerialException, OSError):
                pass
            with self._lock:
                self._port = None

    def _consume(self, buffer: bytes) -> bytes:
        """Handle every complete line in the buffer and return the unfinished remainder."""
        while True:
            match = self.LINE_BREAK.search(buffer)
            if match is None:
                return buffer
            line, buffer = buffer[: match.start()], buffer[match.end():]
            self._handle_line(line)

    def _handle_line(self, raw_line: bytes) -> None:
        text = raw_line.decode("ascii", "ignore").strip()
        if not text:
            return
        amount = self.parse_amount(text)
        with self._lock:
            self._raw.append(text)
            if amount:
                self._amount = amount
                self._sequence += 1


reader = MachineReader()


# =============================================================================
# Helpers and authentication
# =============================================================================
def json_error(message: str, status: int):
    return jsonify(error=message), status


def login_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if "name" not in session:
            return json_error("Login required", 401)
        return view(*args, **kwargs)

    return wrapper


def owner_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if session.get("role") != "owner":
            return json_error("Only the owner can perform this action", 403)
        return view(*args, **kwargs)

    return login_required(wrapper)


def current_user() -> dict:
    return {"name": session.get("name"), "role": session.get("role")}


@app.post("/api/login")
def login():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    password = str(payload.get("password", ""))
    with get_db() as conn:
        user = conn.execute("SELECT * FROM admins WHERE username = ?", (username,)).fetchone()
    if user is None or not check_password_hash(user["pw"], password):
        return json_error("Invalid username or password", 401)
    session["name"], session["role"] = user["name"], user["role"]
    return jsonify(current_user())


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    return jsonify(current_user())


@app.post("/api/admins")
@owner_required
def create_admin():
    payload = request.get_json(silent=True) or {}
    username = str(payload.get("username", "")).strip()
    name = str(payload.get("name", "")).strip()
    password = str(payload.get("password", ""))
    if not (username and name and password):
        return json_error("All fields are required", 400)
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO admins (username, name, pw) VALUES (?, ?, ?)",
                (username, name, generate_password_hash(password)),
            )
    except sqlite3.IntegrityError:
        return json_error("This username already exists", 400)
    log.info("Admin '%s' created by %s", username, session["name"])
    return jsonify(ok=True)


# =============================================================================
# Machine API
# =============================================================================
@app.get("/api/ports")
@login_required
def list_ports():
    ports = serial.tools.list_ports.comports()
    return jsonify([{"device": port.device, "desc": port.description} for port in ports])


@app.post("/api/connect")
@login_required
def connect_machine():
    payload = request.get_json(silent=True) or {}
    port = str(payload.get("port", "")).strip()
    if not port:
        return json_error("Select a COM port first", 400)
    try:
        reader.connect(port, int(payload.get("baud", Config.DEFAULT_BAUD_RATE)))
    except (MachineError, ValueError) as exc:
        return json_error(str(exc), 400)
    return jsonify(ok=True)


@app.post("/api/disconnect")
@login_required
def disconnect_machine():
    reader.disconnect()
    return jsonify(ok=True)


@app.post("/api/clear")
@login_required
def clear_amount():
    reader.clear_amount()
    return jsonify(ok=True)


@app.get("/api/reading")
@login_required
def get_reading():
    return jsonify(reader.snapshot())


# =============================================================================
# Counts API
# =============================================================================
@app.post("/api/counts")
@login_required
def save_count():
    payload = request.get_json(silent=True) or {}
    amount = reader.snapshot()["amount"]  # the amount always comes from the machine, never from the browser
    if not amount or amount <= 0:
        return json_error("No amount received from the machine. Count the notes first", 400)
    customer_name = str(payload.get("customer", "")).strip()
    if not customer_name:
        return json_error("Enter the customer name", 400)

    record = create_count(
        amount, customer_name[: Config.MAX_CUSTOMER_NAME_LENGTH], session["name"]
    )
    reader.clear_amount()
    return jsonify(record)


@app.get("/api/counts/<unique_id>")
@login_required
def get_count(unique_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM counts WHERE unique_id = ?", (unique_id.strip().upper(),)
        ).fetchone()
    if row is None:
        return json_error("No record found for this ID", 404)
    return jsonify(dict(row))


@app.get("/api/counts")
@login_required
def list_counts():
    query = (request.args.get("q") or "").strip()
    sql, params = "SELECT * FROM counts", []
    if query:
        sql += " WHERE unique_id LIKE ? OR customer_name LIKE ?"
        params = [f"%{query}%", f"%{query}%"]
    sql += " ORDER BY rowid DESC LIMIT ?"
    params.append(Config.RECORDS_LIMIT)
    with get_db() as conn:
        rows = conn.execute(sql, params).fetchall()
    return jsonify([dict(row) for row in rows])


# =============================================================================
# Frontend (single page)
# =============================================================================
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cash Count</title>
<style>
:root {
  --bg: #eef2ef;
  --ink: #16231d;
  --muted: #5d6b64;
  --line: #d3dcd6;
  --primary: #0f6b4f;
  --primary-dark: #0a4d38;
  --danger: #b3261e;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font: 16px/1.5 "Segoe UI", system-ui, sans-serif;
  background: var(--bg);
  color: var(--ink);
}
header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  padding: 10px 20px;
  background: var(--primary-dark);
  color: #fff;
}
nav button {
  background: none;
  border: 0;
  color: #cfe5da;
  font: inherit;
  padding: 6px 12px;
  cursor: pointer;
  border-radius: 6px;
}
nav button.active { background: #fff; color: var(--primary-dark); }
main { max-width: 860px; margin: 20px auto; padding: 0 16px; }
.card {
  background: #fff;
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 20px;
  margin-bottom: 16px;
}
h2 { margin: 0 0 12px; font-size: 18px; }
label { display: block; font-size: 14px; color: var(--muted); margin: 10px 0 4px; }
input, select {
  width: 100%;
  padding: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font: inherit;
}
input[readonly] { background: #f3f6f4; }
.btn {
  padding: 10px 18px;
  border: 0;
  border-radius: 8px;
  background: var(--primary);
  color: #fff;
  font: inherit;
  cursor: pointer;
}
.btn.secondary { background: #e3eae6; color: var(--ink); }
.row { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
.row > * { flex: 1; min-width: 140px; }
.row > .fit { flex: 0 0 auto; }
.amount-view {
  font: 700 44px/1.1 Consolas, monospace;
  color: var(--primary-dark);
  margin: 6px 0;
}
.hint { font-size: 14px; color: var(--muted); }
.error { color: var(--danger); font-size: 14px; min-height: 20px; }
.success { color: var(--primary); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 8px; border-bottom: 1px solid var(--line); }
.hidden { display: none !important; }
pre {
  background: #101a15;
  color: #9fe3c4;
  padding: 10px;
  border-radius: 8px;
  min-height: 50px;
  font-size: 13px;
  margin: 8px 0 0;
}
.modal {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, .55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
  padding: 12px;
}
.modal .box {
  background: #fff;
  border-radius: 12px;
  padding: 20px;
  width: 100%;
  max-width: 360px;
  max-height: 92vh;
  overflow: auto;
}
.paper {
  display: flex;
  justify-content: center;
  background: #fff;
  border: 1px dashed #9aa7a0;
  padding: 10px;
  margin: 10px 0 14px;
}

/* 50 mm x 25 mm label. Sizes are in em so the preview and the print stay proportional. */
.label {
  box-sizing: border-box;
  overflow: hidden;
  line-height: 1.15;
  font-family: Arial, Helvetica, sans-serif;
  color: #000;
  text-align: center;
}
.label .id { font-weight: 700; font-size: 1.05em; white-space: nowrap; }
.label .customer { font-size: 1em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.label .amount { font-weight: 700; font-size: 1.9em; white-space: nowrap; margin: .05em 0; }
.label .amount.small { font-size: 1.5em; }
.label .meta { font-size: .85em; white-space: nowrap; }
.paper .label { width: 290px; height: 145px; font-size: 17px; border: 1px solid #ccc; padding: 6px; }

#slip { display: none; }
@page { size: 50mm 25mm; margin: 0; }
@media print {
  html, body { width: 50mm; height: 25mm; margin: 0; padding: 0; overflow: hidden; }
  body * { visibility: hidden; }
  .modal { display: none !important; }
  #slip, #slip * { visibility: visible; }
  #slip {
    display: block;
    position: absolute;
    left: 0;
    top: 0;
    width: 50mm;
    height: 25mm;
    padding: 1mm;
    box-sizing: border-box;
    overflow: hidden;
  }
  #slip .label { width: 100%; height: 100%; font-size: 2.9mm; }
}
</style>
</head>
<body>

<section id="login-view" class="card" style="max-width:380px;margin:60px auto">
  <h2>Admin login</h2>
  <label for="login-username">Username</label>
  <input id="login-username" autocomplete="username">
  <label for="login-password">Password</label>
  <input id="login-password" type="password" autocomplete="current-password">
  <p class="error" id="login-error"></p>
  <button class="btn" id="login-btn">Login</button>
</section>

<div id="app-view" class="hidden">
  <header>
    <b>Cash Count</b>
    <nav>
      <button data-tab="count" class="active">New count</button>
      <button data-tab="search">ID search</button>
      <button data-tab="records">Records</button>
      <button data-tab="admins" id="nav-admins" class="hidden">Admins</button>
      <button id="logout-btn">Logout (<span id="whoami"></span>)</button>
    </nav>
  </header>

  <main>
    <section id="tab-count">
      <div class="card">
        <h2>Machine</h2>
        <div class="row">
          <div>
            <label for="port-select">COM port</label>
            <select id="port-select"></select>
          </div>
          <div>
            <label for="baud-select">Baud rate</label>
            <select id="baud-select">
              <option>9600</option><option>4800</option><option>19200</option>
              <option>38400</option><option>115200</option>
            </select>
          </div>
          <button class="btn secondary fit" id="refresh-btn">Refresh ports</button>
          <button class="btn fit" id="connect-btn">Connect</button>
        </div>
        <p class="hint" id="status-text">...</p>
        <pre id="raw-output">Raw machine data will appear here</pre>
      </div>

      <div class="card">
        <h2>Total amount</h2>
        <div class="amount-view" id="amount-view">Rs. 0</div>
        <label for="customer-input">Customer name</label>
        <input id="customer-input" maxlength="60" placeholder="Enter the customer name" autocomplete="off">
        <label for="amount-input">Amount (read from the machine only)</label>
        <input id="amount-input" type="number" readonly placeholder="Waiting for the machine">
        <p class="error" id="save-error"></p>
        <div class="row">
          <button class="btn" id="save-btn">Save</button>
          <button class="btn secondary" id="clear-btn">Clear</button>
        </div>
        <p class="hint">Saving stores the record in the database only. A label preview opens and you can print from there.</p>
      </div>
    </section>

    <section id="tab-search" class="hidden">
      <div class="card">
        <h2>Search by unique ID</h2>
        <div class="row">
          <input id="search-input" placeholder="CC-20261009-0001">
          <button class="btn fit" id="search-btn">Search</button>
        </div>
        <p class="error" id="search-error"></p>
        <div id="search-result"></div>
      </div>
    </section>

    <section id="tab-records" class="hidden">
      <div class="card">
        <h2>Records</h2>
        <div class="row" style="margin-bottom:10px">
          <input id="records-query" placeholder="Search by ID or customer name">
          <button class="btn secondary fit" id="records-btn">Search</button>
        </div>
        <div style="overflow-x:auto">
          <table>
            <thead>
              <tr><th>ID</th><th>Customer</th><th>Amount</th><th>Date &amp; time</th><th>Admin</th><th></th></tr>
            </thead>
            <tbody id="records-body"></tbody>
          </table>
        </div>
      </div>
    </section>

    <section id="tab-admins" class="hidden">
      <div class="card">
        <h2>Create admin</h2>
        <label for="admin-name">Name (printed on the label)</label>
        <input id="admin-name">
        <label for="admin-username">Username</label>
        <input id="admin-username">
        <label for="admin-password">Password</label>
        <input id="admin-password" type="password">
        <p class="error" id="admin-message"></p>
        <button class="btn" id="create-admin-btn">Create admin</button>
      </div>
    </section>
  </main>
</div>

<div id="preview-modal" class="modal hidden">
  <div class="box">
    <h2>Label preview</h2>
    <div class="paper" id="preview-body"></div>
    <div class="row">
      <button class="btn" id="print-btn">Print</button>
      <button class="btn secondary" id="close-preview-btn">Close</button>
    </div>
    <p class="hint" style="margin-top:10px">
      Label size: 50 mm x 25 mm. In the print dialog choose the thermal printer, set Margins to None
      and Scale to 100. Choosing "Save as PDF" saves a file instead of printing.
    </p>
  </div>
</div>
<div id="slip"></div>

<script>
"use strict";

const POLL_INTERVAL_MS = 700;
const TABS = ["count", "search", "records", "admins"];
const $ = (id) => document.getElementById(id);

const state = { lastSequence: 0, pollTimer: null, previewRecord: null };

/* ---------- helpers ---------- */
const escapeHtml = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

const formatMoney = (value) => "Rs. " + Number(value || 0).toLocaleString("en-IN");

async function api(url, method = "GET", body) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

/* ---------- session ---------- */
function showApp(user) {
  $("login-view").classList.add("hidden");
  $("app-view").classList.remove("hidden");
  $("whoami").textContent = user.name;
  $("nav-admins").classList.toggle("hidden", user.role !== "owner");
  refreshPorts();
  if (!state.pollTimer) state.pollTimer = setInterval(pollMachine, POLL_INTERVAL_MS);
}

async function login() {
  try {
    const user = await api("/api/login", "POST", {
      username: $("login-username").value,
      password: $("login-password").value,
    });
    showApp(user);
  } catch (error) {
    $("login-error").textContent = error.message;
  }
}

async function logout() {
  await api("/api/logout", "POST");
  location.reload();
}

/* ---------- tabs ---------- */
function switchTab(name) {
  document.querySelectorAll("nav [data-tab]").forEach((button) =>
    button.classList.toggle("active", button.dataset.tab === name));
  TABS.forEach((tab) => $("tab-" + tab).classList.toggle("hidden", tab !== name));
  if (name === "records") loadRecords();
}

/* ---------- machine ---------- */
async function refreshPorts() {
  try {
    const ports = await api("/api/ports");
    $("port-select").innerHTML = ports.length
      ? ports.map((p) =>
          `<option value="${escapeHtml(p.device)}">${escapeHtml(p.device)} - ${escapeHtml(p.desc)}</option>`).join("")
      : '<option value="">No COM port found</option>';
  } catch (error) {
    $("status-text").textContent = error.message;
  }
}

async function toggleConnection() {
  try {
    if ($("connect-btn").dataset.connected) {
      await api("/api/disconnect", "POST");
    } else {
      await api("/api/connect", "POST", {
        port: $("port-select").value,
        baud: $("baud-select").value,
      });
    }
  } catch (error) {
    $("status-text").textContent = error.message;
  }
}

function setAmount(value) {
  $("amount-input").value = value;
  $("amount-view").textContent = formatMoney(value);
}

async function pollMachine() {
  try {
    const reading = await api("/api/reading");
    const connected = Boolean(reading.port);
    $("status-text").textContent = reading.status;
    $("connect-btn").textContent = connected ? "Disconnect" : "Connect";
    $("connect-btn").dataset.connected = connected ? "1" : "";
    if (reading.raw.length) $("raw-output").textContent = reading.raw.join("\n");
    if (reading.seq !== state.lastSequence && reading.amount) {
      state.lastSequence = reading.seq;
      setAmount(reading.amount);
    }
  } catch (error) {
    /* transient errors are ignored; the next poll retries */
  }
}

/* ---------- label preview and printing ---------- */
function buildLabelHtml(record) {
  const [date, time] = record.date_time.split(" ");
  const shortDate = date.slice(0, 6) + date.slice(8); // dd-mm-yy
  const amount = formatMoney(record.total_amount);
  const amountClass = amount.length > 12 ? "amount small" : "amount";
  return `<div class="label">
    <div class="id">${escapeHtml(record.unique_id)}</div>
    <div class="customer">${escapeHtml(record.customer_name)}</div>
    <div class="${amountClass}">${amount}</div>
    <div class="meta">${shortDate} ${time.slice(0, 5)} | ${escapeHtml(record.admin_name)}</div>
  </div>`;
}

function openPreview(record) {
  state.previewRecord = record;
  $("preview-body").innerHTML = buildLabelHtml(record);
  $("preview-modal").classList.remove("hidden");
}

function printLabel() {
  $("slip").innerHTML = buildLabelHtml(state.previewRecord);
  window.print();
}

/* ---------- counts ---------- */
async function saveCount() {
  $("save-error").textContent = "";
  try {
    const record = await api("/api/counts", "POST", { customer: $("customer-input").value });
    setAmount("");
    $("customer-input").value = "";
    $("amount-view").textContent = "Saved: " + record.unique_id;
    openPreview(record);
  } catch (error) {
    $("save-error").textContent = error.message;
  }
}

async function clearCount() {
  await api("/api/clear", "POST");
  setAmount("");
  $("customer-input").value = "";
}

async function searchById() {
  $("search-error").textContent = "";
  $("search-result").innerHTML = "";
  try {
    const record = await api("/api/counts/" + encodeURIComponent($("search-input").value));
    $("search-result").innerHTML = `
      <div class="amount-view">${formatMoney(record.total_amount)}</div>
      <p>ID: <b>${escapeHtml(record.unique_id)}</b><br>
         Customer: ${escapeHtml(record.customer_name)}<br>
         Date &amp; time: ${escapeHtml(record.date_time)}<br>
         Admin: ${escapeHtml(record.admin_name)}</p>
      <button class="btn secondary" id="result-preview-btn">Label preview</button>`;
    $("result-preview-btn").onclick = () => openPreview(record);
  } catch (error) {
    $("search-error").textContent = error.message;
  }
}

async function loadRecords() {
  try {
    const records = await api("/api/counts?q=" + encodeURIComponent($("records-query").value));
    $("records-body").innerHTML = records.length
      ? records.map((r, index) => `<tr>
          <td>${escapeHtml(r.unique_id)}</td><td>${escapeHtml(r.customer_name)}</td>
          <td>${formatMoney(r.total_amount)}</td><td>${escapeHtml(r.date_time)}</td>
          <td>${escapeHtml(r.admin_name)}</td>
          <td><button class="btn secondary" data-index="${index}">Preview</button></td></tr>`).join("")
      : '<tr><td colspan="6">No records found</td></tr>';
    $("records-body").querySelectorAll("button").forEach((button) => {
      button.onclick = () => openPreview(records[button.dataset.index]);
    });
  } catch (error) {
    $("records-body").innerHTML = `<tr><td colspan="6">${escapeHtml(error.message)}</td></tr>`;
  }
}

/* ---------- admins ---------- */
async function createAdmin() {
  const message = $("admin-message");
  try {
    await api("/api/admins", "POST", {
      name: $("admin-name").value,
      username: $("admin-username").value,
      password: $("admin-password").value,
    });
    message.className = "error success";
    message.textContent = "Admin created.";
    ["admin-name", "admin-username", "admin-password"].forEach((id) => ($(id).value = ""));
  } catch (error) {
    message.className = "error";
    message.textContent = error.message;
  }
}

/* ---------- wiring ---------- */
function bindEvents() {
  $("login-btn").onclick = login;
  $("login-password").onkeydown = (event) => { if (event.key === "Enter") login(); };
  $("logout-btn").onclick = logout;

  document.querySelectorAll("nav [data-tab]").forEach((button) => {
    button.onclick = () => switchTab(button.dataset.tab);
  });

  $("refresh-btn").onclick = refreshPorts;
  $("connect-btn").onclick = toggleConnection;
  $("save-btn").onclick = saveCount;
  $("clear-btn").onclick = clearCount;

  $("search-btn").onclick = searchById;
  $("search-input").onkeydown = (event) => { if (event.key === "Enter") searchById(); };
  $("records-btn").onclick = loadRecords;
  $("records-query").onkeydown = (event) => { if (event.key === "Enter") loadRecords(); };

  $("create-admin-btn").onclick = createAdmin;
  $("print-btn").onclick = printLabel;
  $("close-preview-btn").onclick = () => $("preview-modal").classList.add("hidden");
}

bindEvents();
api("/api/me").then((user) => { if (user.name) showApp(user); }).catch(() => {});
</script>
</body>
</html>"""


@app.get("/")
def index() -> Response:
    return Response(INDEX_HTML, mimetype="text/html")


# =============================================================================
# Entry point
# =============================================================================
def main() -> None:
    init_db()
    log.info("Cash Count is running at http://localhost:%d", Config.PORT)
    app.run(host=Config.HOST, port=Config.PORT, threaded=True)


if __name__ == "__main__":
    main()






























































"""Cash Count App - Flask + pyserial + SQLite. Ek hi file me poora app."""
import re, sqlite3, threading, time
from collections import deque
from datetime import datetime
from functools import wraps

import serial
import serial.tools.list_ports
from flask import Flask, jsonify, request, session, Response
from werkzeug.security import generate_password_hash, check_password_hash

DB = "cashcount.db"
app = Flask(__name__)
app.secret_key = "badal-dena-koi-lamba-random-text"   # <- apna random text daal dein


# ----------------------------------------------------------- DATABASE
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS admins(
            id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL, pw TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'admin');
        CREATE TABLE IF NOT EXISTS counts(
            unique_id TEXT PRIMARY KEY, total_amount REAL NOT NULL,
            date_time TEXT NOT NULL, admin_name TEXT NOT NULL, customer_name TEXT NOT NULL DEFAULT '');""")
        cols = [r[1] for r in c.execute("PRAGMA table_info(counts)")]
        if "customer_name" not in cols:
            c.execute("ALTER TABLE counts ADD COLUMN customer_name TEXT NOT NULL DEFAULT ''")
        if not c.execute("SELECT 1 FROM admins").fetchone():
            c.execute("INSERT INTO admins(username,name,pw,role) VALUES(?,?,?,?)",
                      ("admin", "Admin", generate_password_hash("admin123"), "owner"))


# ----------------------------------------------------------- RS232 READER
state = {"port": None, "amount": None, "seq": 0, "status": "Machine connected nahi hai"}
rawlog = deque(maxlen=40)
stop_flag = threading.Event()
reader_thread = None

def parse_line(line: str):
    """Machine ki line me se amount nikalta hai: line ka aakhri number = total.
    Machine ka format alag ho to sirf ye function badlein."""
    nums = re.findall(r"\d+(?:\.\d+)?", line.replace(",", ""))
    return float(nums[-1]) if nums else None

def read_loop(ser):
    buf = b""
    while not stop_flag.is_set():
        try:
            data = ser.read(64)
        except Exception as e:
            state["status"] = f"Port error: {e}"
            break
        if not data:
            continue
        buf += data
        while True:
            m = re.search(rb"\r\n|\r|\n", buf)
            if not m:
                break
            line, buf = buf[:m.start()], buf[m.end():]
            text = line.decode("ascii", "ignore").strip()
            if not text:
                continue
            rawlog.append(text)
            val = parse_line(text)
            if val:
                state["amount"] = val
                state["seq"] += 1
    try:
        ser.close()
    except Exception:
        pass
    state["port"] = None

def disconnect():
    global reader_thread
    stop_flag.set()
    if reader_thread:
        reader_thread.join(timeout=2)
    reader_thread = None
    state["port"] = None
    state["status"] = "Machine disconnect ho gayi"


# ----------------------------------------------------------- AUTH
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if "name" not in session:
            return jsonify(error="Login required"), 401
        return f(*a, **k)
    return w

@app.post("/api/login")
def login():
    d = request.get_json() or {}
    with db() as c:
        u = c.execute("SELECT * FROM admins WHERE username=?", (d.get("username", ""),)).fetchone()
    if not u or not check_password_hash(u["pw"], d.get("password", "")):
        return jsonify(error="Username ya password galat hai"), 401
    session["name"], session["role"] = u["name"], u["role"]
    return jsonify(name=u["name"], role=u["role"])

@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)

@app.get("/api/me")
def me():
    return jsonify(name=session.get("name"), role=session.get("role"))

@app.post("/api/admins")
@login_required
def add_admin():
    if session.get("role") != "owner":
        return jsonify(error="Sirf owner naya admin bana sakta hai"), 403
    d = request.get_json() or {}
    if not all(d.get(k) for k in ("username", "name", "password")):
        return jsonify(error="Sab fields bharein"), 400
    try:
        with db() as c:
            c.execute("INSERT INTO admins(username,name,pw) VALUES(?,?,?)",
                      (d["username"], d["name"], generate_password_hash(d["password"])))
    except sqlite3.IntegrityError:
        return jsonify(error="Ye username pehle se hai"), 400
    return jsonify(ok=True)


# ----------------------------------------------------------- MACHINE API
@app.get("/api/ports")
@login_required
def ports():
    return jsonify([{"device": p.device, "desc": p.description} for p in serial.tools.list_ports.comports()])

@app.post("/api/connect")
@login_required
def connect():
    global reader_thread
    d = request.get_json() or {}
    disconnect()
    try:
        ser = serial.Serial(d["port"], int(d.get("baud", 9600)), bytesize=8, parity="N", stopbits=1, timeout=0.5)
    except Exception as e:
        state["status"] = f"Connect nahi hua: {e}"
        return jsonify(error=state["status"]), 400
    stop_flag.clear()
    state.update(port=d["port"], amount=None, status=f"Connected: {d['port']}")
    reader_thread = threading.Thread(target=read_loop, args=(ser,), daemon=True)
    reader_thread.start()
    return jsonify(ok=True)

@app.post("/api/disconnect")
@login_required
def disc():
    disconnect()
    return jsonify(ok=True)

@app.get("/api/reading")
@login_required
def reading():
    return jsonify(amount=state["amount"], seq=state["seq"], port=state["port"],
                   status=state["status"], raw=list(rawlog)[-8:])


# ----------------------------------------------------------- COUNTS
@app.post("/api/counts")
@login_required
def save_count():
    try:
        d = request.get_json() or {}
        amount = float(d.get("amount"))
        assert amount > 0
    except Exception:
        return jsonify(error="Amount sahi nahi hai"), 400
    customer = (d.get("customer") or "").strip()
    if not customer:
        return jsonify(error="Customer ka naam likhein"), 400
    now = datetime.now()
    day = now.strftime("%Y%m%d")
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM counts WHERE unique_id LIKE ?", (f"CC-{day}-%",)).fetchone()[0] + 1
        row = dict(unique_id=f"CC-{day}-{n:04d}", total_amount=amount,
                   date_time=now.strftime("%d-%m-%Y %H:%M:%S"), admin_name=session["name"],
                   customer_name=customer[:60])
        c.execute("INSERT INTO counts(unique_id,total_amount,date_time,admin_name,customer_name) "
                  "VALUES(:unique_id,:total_amount,:date_time,:admin_name,:customer_name)", row)
    state["amount"] = None
    return jsonify(row)

@app.get("/api/counts/<uid>")
@login_required
def get_count(uid):
    with db() as c:
        r = c.execute("SELECT * FROM counts WHERE unique_id=?", (uid.strip().upper(),)).fetchone()
    return jsonify(dict(r)) if r else (jsonify(error="Is ID ka record nahi mila"), 404)

@app.get("/api/counts")
@login_required
def list_counts():
    with db() as c:
        return jsonify([dict(r) for r in c.execute("SELECT * FROM counts ORDER BY rowid DESC LIMIT 100")])


# ----------------------------------------------------------- FRONTEND
PAGE = r"""<!DOCTYPE html><html lang="hi"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Cash Count</title>
<style>
:root{--bg:#eef2ef;--ink:#16231d;--mute:#5d6b64;--line:#d3dcd6;--a:#0f6b4f;--a2:#0a4d38;--w:#b3261e}
*{box-sizing:border-box}body{margin:0;font:16px/1.5 "Segoe UI",system-ui,sans-serif;background:var(--bg);color:var(--ink)}
header{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;padding:10px 20px;background:var(--a2);color:#fff}
nav button{background:none;border:0;color:#cfe5da;font:inherit;padding:6px 12px;cursor:pointer;border-radius:6px}
nav button.on{background:#fff;color:var(--a2)}main{max-width:860px;margin:20px auto;padding:0 16px}
.card{background:#fff;border:1px solid var(--line);border-radius:10px;padding:20px;margin-bottom:16px}
h2{margin:0 0 12px;font-size:18px}label{display:block;font-size:14px;color:var(--mute);margin:10px 0 4px}
input,select{width:100%;padding:10px;border:1px solid var(--line);border-radius:8px;font:inherit}
.btn{padding:10px 18px;border:0;border-radius:8px;background:var(--a);color:#fff;font:inherit;cursor:pointer}
.btn.alt{background:#e3eae6;color:var(--ink)}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:end}.row>*{flex:1;min-width:140px}
.big{font:700 44px/1.1 Consolas,monospace;color:var(--a2);margin:6px 0}.st{font-size:14px;color:var(--mute)}
.err{color:var(--w);font-size:14px;min-height:20px}table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line)}.hide{display:none!important}
pre{background:#101a15;color:#9fe3c4;padding:10px;border-radius:8px;min-height:50px;font-size:13px;margin:8px 0 0}
#slip{display:none}
@media print{body *{visibility:hidden}#slip,#slip *{visibility:visible}
#slip{display:block;position:absolute;left:0;top:0;width:72mm;font:14px/1.4 "Courier New",monospace;color:#000}
#slip .c{text-align:center;font-weight:bold}@page{size:80mm auto;margin:3mm}}
</style></head><body>

<section id="loginV" class="card" style="max-width:380px;margin:60px auto"><h2>Admin login</h2>
<label>Username</label><input id="lu"><label>Password</label><input id="lp" type="password">
<p class="err" id="le"></p><button class="btn" id="lb">Login</button></section>

<div id="appV" class="hide"><header><b>Cash Count</b><nav>
<button data-t="count" class="on">Naya count</button><button data-t="search">ID search</button>
<button data-t="list">Records</button><button data-t="admins" id="adT" class="hide">Admins</button>
<button id="lo">Logout (<span id="who"></span>)</button></nav></header><main>

<section id="t-count"><div class="card"><h2>Machine</h2>
<div class="row"><div><label>COM port</label><select id="port"></select></div>
<div><label>Baud rate</label><select id="baud"><option>9600</option><option>4800</option><option>19200</option><option>38400</option><option>115200</option></select></div>
<button class="btn alt" id="rf" style="flex:0 0 auto">Ports refresh</button>
<button class="btn" id="cb" style="flex:0 0 auto">Connect</button></div>
<p class="st" id="ss">...</p><pre id="raw">Machine ka raw data yahan dikhega</pre></div>
<div class="card"><h2>Total amount</h2><div class="big" id="av">Rs. 0</div>
<label>Customer ka naam</label><input id="cn" maxlength="60" placeholder="Customer ka naam likhein" autocomplete="off">
<label>Amount (machine se auto aayega; galat ho to yahan sahi karein)</label>
<input id="amt" type="number" min="1" step="any"><p class="err" id="ce"></p>
<div class="row" style="margin-top:8px"><button class="btn" id="sb">Save aur Print</button>
<button class="btn alt" id="clr">Clear</button></div></div></section>

<section id="t-search" class="hide"><div class="card"><h2>Unique ID se search</h2>
<div class="row"><input id="sid" placeholder="CC-20261009-0001"><button class="btn" id="sbtn" style="flex:0 0 auto">Search</button></div>
<p class="err" id="se"></p><div id="sres"></div></div></section>

<section id="t-list" class="hide"><div class="card"><h2>Pichle 100 records</h2><div style="overflow-x:auto">
<table><thead><tr><th>ID</th><th>Customer</th><th>Amount</th><th>Date-Time</th><th>Admin</th><th></th></tr></thead><tbody id="rows"></tbody></table></div></div></section>

<section id="t-admins" class="hide"><div class="card"><h2>Naya admin</h2>
<label>Naam (slip par dikhega)</label><input id="an"><label>Username</label><input id="au">
<label>Password</label><input id="ap" type="password"><p class="err" id="ae"></p>
<button class="btn" id="aab">Admin banayein</button></div></section></main></div>
<div id="slip"></div>

<script>
const $=i=>document.getElementById(i);let lastSeq=0,poll=null;
const esc=t=>String(t||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const money=n=>'Rs. '+Number(n).toLocaleString('en-IN');
async function api(u,m='GET',b){const r=await fetch(u,{method:m,headers:{'Content-Type':'application/json'},body:b&&JSON.stringify(b)});
 const d=await r.json();if(!r.ok)throw new Error(d.error||'Error');return d}
function enter(u){$('loginV').classList.add('hide');$('appV').classList.remove('hide');$('who').textContent=u.name;
 $('adT').classList.toggle('hide',u.role!=='owner');loadPorts();poll=setInterval(tick,700)}
$('lb').onclick=async()=>{try{enter(await api('/api/login','POST',{username:$('lu').value,password:$('lp').value}))}catch(e){$('le').textContent=e.message}};
$('lp').onkeydown=e=>{if(e.key==='Enter')$('lb').click()};
$('lo').onclick=async()=>{await api('/api/logout','POST');location.reload()};
api('/api/me').then(u=>u.name&&enter(u));
document.querySelectorAll('nav [data-t]').forEach(b=>b.onclick=()=>{
 document.querySelectorAll('nav [data-t]').forEach(x=>x.classList.toggle('on',x===b));
 ['count','search','list','admins'].forEach(t=>$('t-'+t).classList.toggle('hide',t!==b.dataset.t));
 if(b.dataset.t==='list')loadList()});
async function loadPorts(){const p=await api('/api/ports');
 $('port').innerHTML=p.length?p.map(x=>`<option value="${x.device}">${x.device} - ${x.desc}</option>`).join(''):'<option value="">Koi COM port nahi mila</option>'}
$('rf').onclick=loadPorts;
$('cb').onclick=async()=>{try{
 if($('cb').dataset.on){await api('/api/disconnect','POST')}
 else await api('/api/connect','POST',{port:$('port').value,baud:$('baud').value})}catch(e){$('ss').textContent=e.message}};
async function tick(){try{const r=await api('/api/reading');
 $('ss').textContent=r.status;$('cb').textContent=r.port?'Disconnect':'Connect';$('cb').dataset.on=r.port?'1':'';
 if(r.raw.length)$('raw').textContent=r.raw.join('\n');
 if(r.seq!==lastSeq&&r.amount){lastSeq=r.seq;setAmt(r.amount)}}catch(e){}}
function setAmt(v){$('amt').value=v;$('av').textContent=money(v||0)}
$('amt').oninput=()=>$('av').textContent=money($('amt').value||0);
function slip(r){const [d,t]=r.date_time.split(' ');
 $('slip').innerHTML=`<div class="c">CASH COUNT SLIP</div><div>--------------------------------</div>
 <div>ID     : ${r.unique_id}</div><div>Cust.  : ${esc(r.customer_name)}</div><div>Amount : ${money(r.total_amount)}</div><div>Date   : ${d}</div>
 <div>Time   : ${t}</div><div>Admin  : ${r.admin_name}</div><div>--------------------------------</div><div class="c">Thank you</div>`;
 window.print()}
$('sb').onclick=async()=>{$('ce').textContent='';try{const r=await api('/api/counts','POST',{amount:$('amt').value,customer:$('cn').value});
 setAmt('');$('cn').value='';$('av').textContent='Saved: '+r.unique_id;slip(r)}catch(e){$('ce').textContent=e.message}};
$('clr').onclick=()=>{setAmt('');$('av').textContent='Rs. 0'};
$('sbtn').onclick=async()=>{$('se').textContent='';$('sres').innerHTML='';try{
 const r=await api('/api/counts/'+encodeURIComponent($('sid').value));
 $('sres').innerHTML=`<div class="big">${money(r.total_amount)}</div><p>ID: <b>${r.unique_id}</b><br>Customer: ${esc(r.customer_name)}<br>Date-Time: ${r.date_time}<br>Admin: ${r.admin_name}</p><button class="btn alt" id="rp">Dobara print</button>`;
 $('rp').onclick=()=>slip(r)}catch(e){$('se').textContent=e.message}};
$('sid').onkeydown=e=>{if(e.key==='Enter')$('sbtn').click()};
async function loadList(){const rs=await api('/api/counts');
 $('rows').innerHTML=rs.map((r,i)=>`<tr><td>${r.unique_id}</td><td>${esc(r.customer_name)}</td><td>${money(r.total_amount)}</td><td>${r.date_time}</td><td>${r.admin_name}</td><td><button class="btn alt" data-i="${i}">Print</button></td></tr>`).join('');
 $('rows').querySelectorAll('button').forEach(b=>b.onclick=()=>slip(rs[b.dataset.i]))}
$('aab').onclick=async()=>{try{await api('/api/admins','POST',{name:$('an').value,username:$('au').value,password:$('ap').value});
 $('ae').style.color='#0f6b4f';$('ae').textContent='Admin ban gaya.';$('an').value=$('au').value=$('ap').value=''}
 catch(e){$('ae').style.color='';$('ae').textContent=e.message}};
</script></body></html>"""

@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    init_db()
    print("Chalu hai: http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True)