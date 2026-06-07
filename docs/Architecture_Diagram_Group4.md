# System Architecture Diagram
## Distributed File-Sharing System

**Course:** Networking & Distributed Systems Programming  
**Group:** 4  
**Version:** 1.0  
**Date:** June 2026

---

## Architecture Overview

```
╔══════════════════════════════════════════════════════════════════════════╗
║                            CLIENT ZONE                                   ║
║                                                                          ║
║  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐         ║
║  │    Client 1     │  │    Client 2     │  │    Client N     │         ║
║  │   Web Browser   │  │   Web Browser   │  │   Web Browser   │         ║
║  │    Laptop A     │  │    Laptop B     │  │   Any Laptop    │         ║
║  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘         ║
║           │                   │                     │                   ║
╚═══════════╪═══════════════════╪═════════════════════╪═══════════════════╝
            │                   │                     │
            └───────────────────┴─────────────────────┘
                          │  HTTP Requests
                          │  (Login, Upload, Download, List, Delete)
                          ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                      SERVER ZONE  (Local Area Network)                   ║
║                                                                          ║
║  ┌────────────────────────────────────────────────────────────────────┐ ║
║  │                       Flask Web App                                │ ║
║  │              app.py  |  auth.py  |  templates/                     │ ║
║  │         Session Auth  |  HTTP Routing  |  JWT Validation            │ ║
║  └──────────┬─────────────────────────────────────────┬──────────────┘ ║
║             │ TCP Socket                               │ Failover        ║
║             │ (Bidirectional)                          │ (dashed)        ║
║             ▼                                          ▼                ║
║  ┌──────────────────────┐       ┌──────────────────────────────────┐   ║
║  │   Primary Server     │       │         Replica Server           │   ║
║  │  primary_server.py   │       │       replica_server.py          │   ║
║  │  TCP Socket          │       │       TCP Socket                  │   ║
║  │  threading           │       │       threading                   │   ║
║  └──────────┬───────────┘       └────────────────┬─────────────────┘   ║
║             │                                     ▲                     ║
║             │    ┌────────────────────────┐       │                     ║
║             └───►│  Replication Module    │───────┘                     ║
║                  │   replication.py       │  Auto-sync on every upload  ║
║                  └────────────────────────┘                             ║
╚══════════════════════════════════════════════════════════════════════════╝
             │                                         │
             ▼                                         ▼
╔══════════════════════════════════════════════════════════════════════════╗
║                           STORAGE ZONE                                   ║
║                                                                          ║
║  ┌────────────────────────────┐    ┌────────────────────────────────┐  ║
║  │     Primary Storage        │    │       Replica Storage          │  ║
║  │      /shared_files/        │    │        /shared_files/          │  ║
║  │  Filesystem on Primary     │    │   Filesystem on Replica        │  ║
║  │       Machine              │    │        Machine                 │  ║
║  └────────────────────────────┘    └────────────────────────────────┘  ║
╚══════════════════════════════════════════════════════════════════════════╝
```

---

## Component Descriptions

### 1. Client (Web Browser)
- Runs on any laptop connected to the LAN
- Interacts with the system via a standard web browser (Chrome, Firefox, Edge)
- Communicates with the Flask Web App using HTTP only
- Has no direct connection to the Primary or Replica servers
- Supports: Login, Register, Upload, Download, List, Delete, Rename, Search, Share

### 2. Flask Web App
- **File:** `app.py`, `auth.py`, `templates/`
- Acts as the gateway between the client browser and the backend servers
- Handles all HTTP routing and serves the HTML user interface
- Issues and validates JWT tokens for all authenticated requests
- Routes requests to the Primary Server via TCP; falls back to Replica on failure

### 3. Primary Server
- **File:** `primary_server.py`
- The main backend server that handles all file operations
- Listens for TCP connections on port **9000**
- Accepts commands: `UPLOAD`, `DOWNLOAD`, `LIST`, `DELETE`, `RENAME`, `PING`
- Triggers replication to the Replica Server after every successful upload
- Stores files in the local `/shared_files/` directory

### 4. Replica Server
- **File:** `replica_server.py`
- Maintains an identical copy of all files from the Primary Server
- Listens for TCP connections on port **9001**
- Accepts replication commands from the Replication Module
- Serves `DOWNLOAD` and `LIST` requests during failover
- Read-only from the client's perspective; write operations are rejected during primary downtime

### 5. Replication Module
- **File:** `replication.py`
- Called automatically by the Primary Server after every upload
- Opens a TCP connection to the Replica Server (port 9001)
- Sends the `REPLICATE` command with the filename and file data
- Logs the result (success/failure) with a timestamp
- Also handles propagating `DELETE` and `RENAME` operations to the replica

---

## Communication Flows

### Upload Flow
```
Browser ──POST /upload──► Flask ──UPLOAD cmd──► Primary Server
                                                      │
                                                      ▼
                                              Save to /shared_files/
                                                      │
                                                      ▼
                                            Replication Module
                                                      │
                                               REPLICATE cmd
                                                      │
                                                      ▼
                                              Replica Server
                                                      │
                                                      ▼
                                              Save to /shared_files/
                                                      │
                                              OK REPLICATED
                                                      │
                                   Flask ◄── OK FILE_SAVED ◄──────
                                      │
                              Browser ◄── 200 OK (success message)
```

### Download Flow (Normal)
```
Browser ──GET /download/<file>──► Flask ──DOWNLOAD cmd──► Primary Server
                                                                │
                                                          Read from /shared_files/
                                                                │
                                             Flask ◄── OK <filesize> + bytes
                                               │
                               Browser ◄── File binary stream
```

### Failover Flow
```
Browser ──GET /download/<file>──► Flask ──PING──► Primary Server
                                                        │
                                                  (no response — 5s timeout)
                                                        │
                                              PRIMARY_DOWN = True
                                                        │
                                   Flask ──DOWNLOAD cmd──► Replica Server
                                                                │
                                              Flask ◄── OK <filesize> + bytes
                                                │
                               Browser ◄── File + "Failover mode" notice
```

---

## Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Client Interface | HTML + CSS + JavaScript | Web UI rendered in browser |
| Web Framework | Python + Flask | HTTP server, routing, templates |
| Authentication | PyJWT | JWT token generation and validation |
| Server Communication | Python `socket` module | TCP connections between components |
| Concurrency | Python `threading` module | Handle multiple clients simultaneously |
| File Storage | OS Filesystem | Store files in `/shared_files/` directory |
| Replication | Custom TCP protocol | Sync files from primary to replica |

---

## Network Configuration

| Component | Machine | Default Port |
|-----------|---------|-------------|
| Flask Web App | Any machine on LAN | 5000 (HTTP) |
| Primary Server | Dedicated machine | 9000 (TCP) |
| Replica Server | Dedicated machine | 9001 (TCP) |

> All machines must be connected to the same Local Area Network (LAN) for the system to function.

---

## Folder Structure

```
project/
├── app.py                  ← Flask web application & HTTP routing
├── auth.py                 ← JWT authentication & user management
├── primary_server.py       ← Primary TCP server
├── replica_server.py       ← Replica TCP server
├── replication.py          ← Replication & failover logic
├── shared_files/           ← Uploaded files (Primary)
│   └── ...
├── templates/              ← HTML templates for browser UI
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   └── profile.html
└── requirements.txt        ← Python dependencies (Flask, PyJWT)
```

---

## Revision History

| Version | Date | Author | Description |
|---------|------|--------|-------------|
| 1.0 | June 2026 | Group 4 | Initial version |
