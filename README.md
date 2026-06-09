# 📁 Distributed File-Sharing System

A distributed file-sharing application built with Python that demonstrates **data consistency and replication strategies** in distributed systems. Authenticated users can upload, download, list, rename, and delete files from a primary server that automatically replicates data to a replica server with transparent failover.

> **Course:** Networking & Distributed Systems Programming — Group 4

![Python](https://img.shields.io/badge/Python-3.12+-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-3.0-000000?logo=flask)
![License](https://img.shields.io/badge/License-MIT-green)

---

## ✨ Features

- **JWT Authentication** — Secure registration, login, and session management with 30-minute token expiry
- **File Operations** — Upload, download, rename, delete, and list files through a web interface
- **Automatic Replication** — Every upload is synchronously replicated from primary to replica server
- **Transparent Failover** — Read requests automatically reroute to the replica when the primary is down
- **Storage Quotas** — Per-user quota enforcement (50 MB default) prevents abuse
- **Shareable Links** — Generate public, private, or user-specific sharing links
- **Duplicate Handling** — Automatic rename (`report.pdf` → `report_1.pdf`) on filename conflicts

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                       CLIENT ZONE                           │
│   [Browser 1]         [Browser 2]         [Browser N]       │
└──────────┬────────────────┬────────────────┬────────────────┘
           └────────────────┴────────────────┘
                            │ HTTP (port 5000)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                      SERVER ZONE (LAN)                      │
│                                                             │
│   ┌──────────────────────────────────────────┐              │
│   │         Flask Web App (app.py)           │              │
│   │     auth.py  ·  templates/  ·  JWT       │              │
│   └───────┬──────────────────────────┬───────┘              │
│           │ TCP :9000                │ Failover TCP :9001    │
│           ▼                          ▼                      │
│   ┌────────────────┐        ┌────────────────┐              │
│   │ Primary Server │──────▶ │ Replica Server │              │
│   │  :9000         │ Repl.  │  :9001         │              │
│   └───────┬────────┘        └───────┬────────┘              │
│           ▼                         ▼                       │
│     /shared_files/            /shared_files/                │
└─────────────────────────────────────────────────────────────┘
```

---

## 🚀 Quickstart

### Prerequisites

- **Python 3.12+** installed on all server machines
- All machines on the **same Local Area Network** (Wi-Fi or Ethernet)
- A modern web browser (Chrome, Firefox, Edge)

### 1. Clone & Install

```bash
git clone https://github.com/QweciKuranchie/file-sharing-system.git
cd file-sharing-system
pip install -r requirements.txt
```

### 2. Configure (Optional)

Set environment variables to override defaults, or edit [`config.py`](config.py):

| Variable | Default | Description |
|----------|---------|-------------|
| `JWT_SECRET_KEY` | Random per-process | Secret for signing JWT tokens |
| `PRIMARY_HOST` | `127.0.0.1` | Primary server IP address |
| `PRIMARY_PORT` | `9000` | Primary server TCP port |
| `REPLICA_HOST` | `127.0.0.1` | Replica server IP address |
| `REPLICA_PORT` | `9001` | Replica server TCP port |
| `FLASK_PORT` | `5000` | Flask HTTP port |

### 3. Start the Servers

Start each component in a separate terminal. **Order matters** — start the replica first.

```bash
# Terminal 1 — Replica Server
python replica_server.py

# Terminal 2 — Primary Server
python primary_server.py

# Terminal 3 — Flask Web App
python app.py
```

### 4. Open the App

Navigate to `http://<flask-machine-ip>:5000` in your browser.

---

## 📂 Project Structure

```
file-sharing-system/
├── app.py                  ← Flask web application & HTTP routing
├── auth.py                 ← JWT authentication & user management
├── config.py               ← Centralised configuration
├── database.py             ← SQLite schema initialisation & connection helpers
├── primary_server.py       ← Primary TCP file server
├── replica_server.py       ← Replica TCP file server
├── replication.py          ← Replication & failover logic
├── requirements.txt        ← Python dependencies
├── test_auth.py            ← Auth module test suite
├── shared_files/           ← Uploaded file storage (gitignored)
├── templates/              ← HTML templates for the web UI
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   └── profile.html
└── docs/                   ← Project documentation
    ├── SRS_FileSharing_Group4.md
    ├── Protocol_Spec_Group4.md
    └── Architecture_Diagram_Group4.md
```

---

## 🗄️ Data Model

### `users`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | Primary key, auto-increment |
| `username` | TEXT | Unique |
| `email` | TEXT | Unique, stored lowercase |
| `password_hash` | TEXT | Werkzeug scrypt/pbkdf2 hash |
| `quota_limit_bytes` | INTEGER | Default 52 428 800 (50 MB) |
| `quota_used_bytes` | INTEGER | Default 0 |
| `created_at` | TEXT | ISO 8601 timestamp |

### `files`

| Column | Type | Notes |
|--------|------|-------|
| `id` | INTEGER | Primary key, auto-increment |
| `filename` | TEXT | Name on disk (may be renamed for duplicates) |
| `original_name` | TEXT | Name the user uploaded |
| `file_type` | TEXT | File extension / MIME category |
| `file_size_bytes` | INTEGER | Size in bytes |
| `uploaded_at` | TEXT | ISO 8601 timestamp |
| `owner_id` | INTEGER | FK → `users.id`, cascade delete |

---

## 🧪 Running Tests

```bash
pip install pytest
python -m pytest test_auth.py -v
```

Expected: **32 tests passed** covering database init, registration, password hashing, login, JWT validation, and quota management.

---

## 📡 API Overview

### Authentication

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/register` | Create a new user account |
| POST | `/login` | Authenticate and receive JWT session |
| GET | `/logout` | Clear session and redirect to login |

### File Operations

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/dashboard` | Main UI with file list |
| POST | `/upload` | Upload a file (max 10 MB) |
| GET | `/download/<filename>` | Download a file |
| POST | `/delete/<filename>` | Delete a file |
| POST | `/rename/<filename>` | Rename a file |
| GET | `/files` | JSON list of files |
| GET | `/profile` | User profile & quota usage |

### Sharing

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/share/<filename>` | Generate a shareable link |
| GET | `/shared/<token>` | Access a shared file |
| GET | `/shared-with-me` | List files shared with you |

> Full protocol specification: [`docs/Protocol_Spec_Group4.md`](docs/Protocol_Spec_Group4.md)

---

## ⚙️ Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Client | HTML + CSS + JS | Web UI in the browser |
| Web Framework | Flask | HTTP routing, templates, sessions |
| Authentication | PyJWT + Werkzeug | JWT tokens + password hashing |
| Database | SQLite | User accounts & file metadata |
| Server Comms | Python `socket` | TCP between Flask ↔ Primary ↔ Replica |
| Concurrency | Python `threading` | Handle multiple clients simultaneously |

---

## 📄 License

This project is licensed under the [MIT License](LICENSE).
