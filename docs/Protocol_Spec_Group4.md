# Communication Protocol Specification
## Distributed File-Sharing System

**Course:** Networking & Distributed Systems Programming  
**Group:** 4  
**Version:** 1.0  
**Date:** June 2026

---

## Table of Contents

1. [Overview](#1-overview)
2. [System Architecture](#2-system-architecture)
3. [Layer 1 — HTTP Protocol (Client ↔ Flask)](#3-layer-1--http-protocol-client--flask)
4. [Layer 2 — TCP Socket Protocol (Flask ↔ Servers)](#4-layer-2--tcp-socket-protocol-flask--servers)
5. [Error Code Reference](#5-error-code-reference)
6. [Failover Flow](#6-failover-flow)
7. [Duplicate Filename Handling](#7-duplicate-filename-handling)
8. [Revision History](#8-revision-history)

---

## 1. Overview

This document defines the communication protocol used between all components of the Distributed File-Sharing System. It specifies the message formats, command structures, request/response flows, and error codes for every interaction in the system.

The system has two communication layers:

- **HTTP Layer** — between the client browser and the Flask web application
- **TCP Socket Layer** — between the Flask application, the Primary Server, the Replica Server, and the Replication Module

---

## 2. System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        CLIENT ZONE                          │
│                                                             │
│  [Browser: Client 1]  [Browser: Client 2]  [Browser: N]    │
│        Laptop A              Laptop B          Any          │
└──────────────────┬──────────────────────────────────────────┘
                   │  HTTP Requests
                   │  (Login, Upload, Download, List, Delete)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│                        SERVER ZONE (LAN)                    │
│                                                             │
│  ┌─────────────────┐    TCP     ┌─────────────────────┐    │
│  │  Primary Server │◄──────────►│   Flask Web App     │    │
│  │ primary_server  │            │  app.py | auth.py   │    │
│  │    .py          │            │  Session | Routing  │    │
│  └────────┬────────┘            └──────────┬──────────┘    │
│           │ Replication                    │ Failover       │
│           │ (auto on upload)               │ (dashed)       │
│           ▼                                ▼                │
│  ┌─────────────────┐                ┌──────────────────┐   │
│  │ Replication     │───────────────►│  Replica Server  │   │
│  │ Module          │                │ replica_server   │   │
│  │ replication.py  │                │    .py           │   │
│  └─────────────────┘                └──────────────────┘   │
└──────────┬──────────────────────────────────┬──────────────┘
           │                                  │
           ▼                                  ▼
┌─────────────────────────────────────────────────────────────┐
│                       STORAGE ZONE                          │
│                                                             │
│   [Primary /shared_files/]        [Replica /shared_files/]  │
│   Filesystem on Primary Machine   Filesystem on Replica     │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Layer 1 — HTTP Protocol (Client ↔ Flask)

All client interactions go through the Flask web application via standard HTTP. The client browser never communicates directly with the Primary or Replica servers.

### 3.1 Base URL

```
http://<flask-server-ip>:5000
```

---

### 3.2 Authentication Endpoints

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/login` | Renders the login page | No |
| POST | `/login` | Submits credentials; creates JWT session on success | No |
| GET | `/logout` | Clears the session and redirects to login page | Yes |
| POST | `/register` | Creates a new user account | No |

#### POST `/login` — Request Body

```
Content-Type: application/x-www-form-urlencoded

username=<string>
password=<string>
```

#### POST `/login` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 302 Redirect | Redirects to `/dashboard`; JWT set in session |
| Invalid credentials | 200 OK | Re-renders login page with error: `Invalid username or password` |
| Missing fields | 400 Bad Request | Re-renders login page with error: `All fields are required` |

#### POST `/register` — Request Body

```
Content-Type: application/x-www-form-urlencoded

username=<string>
email=<string>
password=<string>
```

#### POST `/register` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 302 Redirect | Redirects to `/login` with success message |
| Username taken | 409 Conflict | Error: `Username already exists` |
| Email taken | 409 Conflict | Error: `Email already registered` |
| Missing fields | 400 Bad Request | Error: `All fields are required` |

---

### 3.3 File Operation Endpoints

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| GET | `/dashboard` | Renders main UI with file list | Yes |
| POST | `/upload` | Uploads a file to the primary server | Yes |
| GET | `/download/<filename>` | Downloads a file by name | Yes |
| POST | `/delete/<filename>` | Deletes a file by name | Yes |
| POST | `/rename/<filename>` | Renames a file | Yes |
| GET | `/files` | Returns JSON list of available files | Yes |
| GET | `/profile` | Returns the user's profile and quota usage | Yes |

#### POST `/upload` — Request

```
Content-Type: multipart/form-data

file=<binary file data>
```

#### POST `/upload` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 200 OK | `{ "status": "success", "message": "File uploaded and replicated", "filename": "<saved_name>" }` |
| File too large (>10MB) | 413 Request Entity Too Large | `{ "status": "error", "message": "File exceeds 10MB limit" }` |
| Quota exceeded | 403 Forbidden | `{ "status": "error", "message": "Storage quota exceeded" }` |
| No file selected | 400 Bad Request | `{ "status": "error", "message": "No file selected" }` |
| Replication failed | 207 Multi-Status | `{ "status": "warning", "message": "Uploaded but replication failed" }` |
| Server error | 500 Internal Server Error | `{ "status": "error", "message": "Upload failed. Try again." }` |

#### GET `/download/<filename>` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success (primary up) | 200 OK | File binary stream with correct `Content-Disposition` header |
| Success (failover to replica) | 200 OK | File binary stream from replica; banner shown in UI |
| File not found | 404 Not Found | `{ "status": "error", "message": "File not found on any server" }` |
| Access denied | 403 Forbidden | `{ "status": "error", "message": "You do not have access to this file" }` |

#### GET `/files` — Response

```json
{
  "files": [
    { "name": "report.pdf", "size": "2.1 MB", "type": "pdf", "uploaded": "2026-06-01", "owner": "kwame" },
    { "name": "notes.txt", "size": "12 KB", "type": "txt", "uploaded": "2026-06-02", "owner": "ama" }
  ]
}
```

#### POST `/rename/<filename>` — Request Body

```
Content-Type: application/x-www-form-urlencoded

new_name=<string>
```

#### POST `/rename/<filename>` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 200 OK | `{ "status": "success", "message": "File renamed to <new_name>" }` |
| File not found | 404 Not Found | `{ "status": "error", "message": "File not found" }` |
| Name conflict | 409 Conflict | `{ "status": "error", "message": "A file with that name already exists" }` |
| Access denied | 403 Forbidden | `{ "status": "error", "message": "You can only rename files you own" }` |

#### POST `/delete/<filename>` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 200 OK | `{ "status": "success", "message": "File deleted from all servers" }` |
| File not found | 404 Not Found | `{ "status": "error", "message": "File not found" }` |
| Server error | 500 Internal Server Error | `{ "status": "error", "message": "Delete failed" }` |

---

### 3.4 Sharing Endpoints

| Method | Endpoint | Description | Auth Required |
|--------|----------|-------------|---------------|
| POST | `/share/<filename>` | Generate a shareable link or set permissions | Yes |
| GET | `/shared/<token>` | Access a file via a public shareable link | No |
| GET | `/shared-with-me` | List all files shared with the current user | Yes |

#### POST `/share/<filename>` — Request Body

```json
{
  "visibility": "public | private | shared",
  "shared_with": ["username1", "username2"]
}
```

#### POST `/share/<filename>` — Responses

| Outcome | HTTP Status | Response |
|---------|-------------|----------|
| Success | 200 OK | `{ "status": "success", "link": "http://<server>/shared/<token>" }` |
| File not found | 404 Not Found | `{ "status": "error", "message": "File not found" }` |
| Access denied | 403 Forbidden | `{ "status": "error", "message": "Only the file owner can change sharing settings" }` |

---

## 4. Layer 2 — TCP Socket Protocol (Flask ↔ Servers)

The Flask web app communicates with the Primary Server (and Replica Server during failover) using a custom TCP socket protocol. All messages are plain text strings encoded in UTF-8, terminated by a newline character (`\n`).

### 4.1 Connection Details

| Parameter | Primary Server | Replica Server |
|-----------|---------------|----------------|
| Default Port | `9000` | `9001` |
| Protocol | TCP | TCP |
| Encoding | UTF-8 | UTF-8 |
| Message Terminator | Newline (`\n`) | Newline (`\n`) |
| Connection Type | Per-request (short-lived) | Per-request (short-lived) |

---

### 4.2 General Message Format

**Request:**
```
COMMAND <argument1> <argument2>\n
```

**Success Response:**
```
OK <optional_data>\n
```

**Failure Response:**
```
ERROR <error_message>\n
```

---

### 4.3 Commands

#### UPLOAD

| Field | Value |
|-------|-------|
| Purpose | Send a file from Flask to the Primary Server |
| Request | `UPLOAD <filename> <filesize_in_bytes>` |
| Flow | 1. Send command line → 2. Wait for `READY` → 3. Send file bytes → 4. Wait for response |
| Success Response | `OK FILE_SAVED <saved_filename>` |
| Failure Response | `ERROR <reason>` |
| Example | `UPLOAD report.pdf 204800` |

---

#### DOWNLOAD

| Field | Value |
|-------|-------|
| Purpose | Request a file from Primary (or Replica on failover) |
| Request | `DOWNLOAD <filename>` |
| Flow | 1. Send command → 2. Receive file size in response → 3. Read file bytes |
| Success Response | `OK <filesize_in_bytes>` (then file bytes follow) |
| Failure Response | `ERROR FILE_NOT_FOUND` |
| Example | `DOWNLOAD report.pdf` |

---

#### LIST

| Field | Value |
|-------|-------|
| Purpose | Retrieve list of all files stored on the server |
| Request | `LIST` |
| Flow | 1. Send command → 2. Receive JSON array of file objects |
| Success Response | `OK [{"name":"report.pdf","size":"2.1 MB"},{"name":"notes.txt","size":"12 KB"}]` |
| Failure Response | `ERROR CANNOT_LIST` |

---

#### DELETE

| Field | Value |
|-------|-------|
| Purpose | Delete a file from the server and trigger deletion on replica |
| Request | `DELETE <filename>` |
| Flow | 1. Send command → 2. Primary deletes file → 3. Primary sends DELETE to replica → 4. Response sent |
| Success Response | `OK FILE_DELETED` |
| Failure Response | `ERROR FILE_NOT_FOUND` or `ERROR DELETE_FAILED` |
| Example | `DELETE report.pdf` |

---

#### RENAME

| Field | Value |
|-------|-------|
| Purpose | Rename a file on both primary and replica servers |
| Request | `RENAME <old_filename> <new_filename>` |
| Flow | 1. Send command → 2. Primary renames file → 3. Primary sends RENAME to replica → 4. Response sent |
| Success Response | `OK FILE_RENAMED <new_filename>` |
| Failure Response | `ERROR FILE_NOT_FOUND` or `ERROR NAME_CONFLICT` |
| Example | `RENAME draft.pdf final_report.pdf` |

---

#### REPLICATE

| Field | Value |
|-------|-------|
| Purpose | Primary server sends a file to the Replica Server (internal use only) |
| Request | `REPLICATE <filename> <filesize_in_bytes>` |
| Flow | 1. Primary sends command to replica → 2. Waits for `READY` → 3. Sends file bytes → 4. Waits for response |
| Success Response | `OK REPLICATED` |
| Failure Response | `ERROR REPLICATION_FAILED` |
| Triggered By | Automatically after every successful `UPLOAD` |

---

#### PING

| Field | Value |
|-------|-------|
| Purpose | Check if a server is reachable (used for failover detection) |
| Request | `PING` |
| Success Response | `OK PONG` |
| Timeout | If no response within 5 seconds, server is considered down |

---

## 5. Error Code Reference

| Error Code | Layer | Meaning | Client Action |
|------------|-------|---------|---------------|
| `ERROR FILE_NOT_FOUND` | TCP | Requested file does not exist on the server | Show error message to user |
| `ERROR FILE_TOO_LARGE` | HTTP | Uploaded file exceeds 10MB | Prompt user to choose a smaller file |
| `ERROR QUOTA_EXCEEDED` | HTTP | Upload would exceed user's storage quota | Notify user; show current usage |
| `ERROR REPLICATION_FAILED` | TCP | Primary could not sync file to replica | Log warning; file still saved on primary |
| `ERROR DELETE_FAILED` | TCP | File could not be deleted from disk | Show error; advise retry |
| `ERROR RENAME_FAILED` | TCP | File could not be renamed | Show error; check for name conflicts |
| `ERROR CANNOT_LIST` | TCP | Server could not read the storage directory | Show error; advise server check |
| `ERROR AUTH_REQUIRED` | HTTP | Request made without valid JWT token | Redirect to login page |
| `ERROR TOKEN_EXPIRED` | HTTP | JWT token has expired | Redirect to login page |
| `ERROR INVALID_FILENAME` | TCP | Filename contains illegal characters | Notify user to rename file |
| `ERROR ACCESS_DENIED` | HTTP | User does not have permission for this file | Show access denied message |
| `ERROR CONNECTION_REFUSED` | TCP | Target server is not running | Attempt failover to replica |

---

## 6. Failover Flow

When the Primary Server is unreachable, the Flask app automatically reroutes read requests (`DOWNLOAD` and `LIST`) to the Replica Server. Write requests (`UPLOAD`, `DELETE`, `RENAME`) return an error during failover since the replica is read-only when the primary is down.

| Step | Action | Component |
|------|--------|-----------|
| 1 | Flask sends `PING` to Primary Server | Flask |
| 2 | No response within 5 seconds — timeout | Flask |
| 3 | Flask sets internal flag: `PRIMARY_DOWN = True` | Flask |
| 4 | Flask sends request to Replica Server instead | Flask → Replica |
| 5 | Replica responds normally to `DOWNLOAD` and `LIST` | Replica |
| 6 | Flask shows `"Operating in failover mode"` notice in UI | Flask → Browser |
| 7 | On next request, Flask pings Primary again to detect recovery | Flask |

---

## 7. Duplicate Filename Handling

When a file is uploaded with a name that already exists on the server, the Primary Server automatically renames the incoming file by appending an incrementing number before the file extension.

```
report.pdf already exists
First duplicate  →  report_1.pdf
Second duplicate →  report_2.pdf
Third duplicate  →  report_3.pdf
```

The renamed filename is returned to Flask in the `UPLOAD` success response (`OK FILE_SAVED <saved_filename>`) and displayed to the user.

---

## 8. Revision History

| Version | Date | Author | Description |
|---------|------|--------|-------------|
| 1.0 | June 2026 | Group 4 | Initial version |
