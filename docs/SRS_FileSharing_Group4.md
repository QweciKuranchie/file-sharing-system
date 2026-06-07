# Software Requirements Specification
## Distributed File-Sharing System with Replication

**Course:** Networking & Distributed Systems Programming  
**Group:** 4  
**Version:** 1.0  
**Date:** June 2026

---

## Table of Contents

1. [Introduction](#1-introduction)
2. [Overall System Description](#2-overall-system-description)
3. [Functional Requirements](#3-functional-requirements)
4. [Non-Functional Requirements](#4-non-functional-requirements)
5. [Constraints and Limitations](#5-constraints-and-limitations)
6. [Team Responsibilities](#6-team-responsibilities)
7. [Revision History](#7-revision-history)

---

## 1. Introduction

### 1.1 Purpose

This Software Requirements Specification (SRS) describes the functional and non-functional requirements for a Distributed File-Sharing System built using Python. The system enables authenticated users to upload, download, list, rename, and delete files from a central primary server that automatically replicates data to a replica server. This document serves as the reference for all design, development, testing, and demonstration activities undertaken by Group 4.

### 1.2 Project Scope

The system is a distributed file-sharing application that demonstrates key concepts from the course topic: **Data Consistency and Replication Strategies in Distributed Systems**. The core deliverable is a working Python application that runs across multiple machines on the same local network, with a web browser interface for client interaction.

The project covers:

- File upload from client to primary server
- Automatic synchronous replication from primary to replica server
- File download, listing, renaming, and deletion by authenticated users
- Automatic failover to replica server if primary server is unreachable
- JWT-based user authentication and session management
- User registration and storage quota enforcement
- Folder/directory creation and navigation
- File metadata storage and search
- Shareable links and file access control

### 1.3 Definitions and Acronyms

| Term | Definition |
|------|------------|
| Primary Server | The main server that receives all client requests and initiates replication |
| Replica Server | A secondary server that maintains a copy of all files from the primary |
| Replication | The process of copying uploaded files from primary to replica automatically |
| Failover | The automatic switching of client requests to the replica when primary is down |
| Flask | A lightweight Python web framework used to build the web interface |
| TCP | Transmission Control Protocol, used for reliable data transfer between components |
| JWT | JSON Web Token, used for stateless session-based authentication |
| SRS | Software Requirements Specification (this document) |
| Client | Any user accessing the system through a web browser |
| Quota | The maximum total storage space allocated to a single user |

### 1.4 References

- Course Topic: Data Consistency and Replication Strategies in Distributed Systems
- Python 3.x Standard Library Documentation — docs.python.org
- Flask Documentation — flask.palletsprojects.com
- JWT Standard — RFC 7519
- IEEE Std 830-1998: Recommended Practice for Software Requirements Specifications

### 1.5 Document Overview

Section 2 provides an overall system description. Section 3 specifies all functional requirements. Section 4 specifies non-functional requirements. Section 5 covers system constraints and assumptions.

---

## 2. Overall System Description

### 2.1 System Context

The system operates on a Local Area Network (LAN) shared by multiple laptops. It consists of three logical components: a Flask web application layer, a primary file server, and a replica file server. All three are implemented in Python. Clients interact exclusively through a web browser; they do not require any installed software beyond a browser.

### 2.2 System Architecture Summary

| Component | Technology | Responsibility |
|-----------|------------|---------------|
| Flask Web App | Python + Flask | Serves browser UI, handles JWT auth, routes client requests |
| Primary Server | Python socket + threading | Receives file operations, stores files, triggers replication |
| Replica Server | Python socket + threading | Receives replicated files, serves as failover for clients |
| Client | Web Browser | Upload, download, list, rename, delete files via web interface |

### 2.3 User Classes

There is one class of user:

- **Authenticated User** — any person who has registered and holds a valid JWT token. All users have equal access to their own files and any files shared with them.

### 2.4 Operating Environment

- Multiple laptops connected to the same local area network (Wi-Fi or Ethernet)
- Python 3.x installed on all server machines
- Flask and PyJWT installed via pip on the machine running the web application
- Modern web browser (Chrome, Firefox, Edge) on client machines
- Operating System: Windows, macOS, or Linux

### 2.5 Assumptions and Dependencies

- All machines are on the same local network during the demo
- IP addresses of the primary and replica servers are known before starting the system
- No internet connection is required for the system to function
- File size limit of 10MB is enforced by the application
- The replica server is started before the primary server

---

## 3. Functional Requirements

### 3.1 User Authentication

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-01 | The system shall require users to log in with a username and password before accessing any feature. | High |
| FR-02 | The system shall maintain a session for authenticated users until they log out or the session expires. | High |
| FR-03 | The system shall display an error message for invalid login credentials. | High |
| FR-04 | The system shall allow users to log out and terminate their session. | Medium |

### 3.2 File Upload

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-05 | The system shall allow authenticated users to upload a file of any type (text, image, PDF, etc.). | High |
| FR-06 | The system shall reject any file larger than 10MB and notify the user. | High |
| FR-07 | If a file with the same name already exists, the system shall automatically rename the new file by appending a number (e.g., `report_1.pdf`). | High |
| FR-08 | Upon successful upload to the primary server, the system shall automatically replicate the file to the replica server before confirming success to the user. | High |
| FR-09 | The system shall display a success or failure message after each upload attempt. | Medium |

### 3.3 File Download

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-10 | The system shall allow authenticated users to download any file they own or have been granted access to. | High |
| FR-11 | If the primary server is unreachable, the system shall automatically serve the download request from the replica server. | High |
| FR-12 | The system shall display an error message if the requested file does not exist on either server. | Medium |

### 3.4 File Listing

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-13 | The system shall display a list of all files available on the server to authenticated users. | High |
| FR-14 | The file list shall include the filename and file size for each entry. | Medium |
| FR-15 | The file list shall refresh automatically after every upload or delete operation. | Medium |

### 3.5 File Deletion

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-16 | The system shall allow authenticated users to delete files they own from the server. | High |
| FR-17 | When a file is deleted from the primary server, the system shall also delete the corresponding file from the replica server. | High |
| FR-18 | The system shall display a confirmation prompt before executing a delete operation. | Medium |
| FR-19 | The system shall display a success or failure message after each delete attempt. | Medium |

### 3.6 Replication

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-20 | Replication from primary to replica shall occur automatically and synchronously on every upload. | High |
| FR-21 | The system shall log every replication event with a timestamp and result (success/failure). | Medium |
| FR-22 | If replication fails, the system shall notify the administrator via a console log message. | Medium |

### 3.7 Failover

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-23 | The system shall detect an unreachable primary server within 5 seconds. | High |
| FR-24 | Upon detecting primary server failure, the system shall automatically route read requests (download, list) to the replica server. | High |
| FR-25 | The system shall display a notice to the user when operating in failover mode. | Medium |

### 3.8 User Management

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-26 | The system shall allow new users to register with a unique username, email address, and password. | High |
| FR-27 | The system shall authenticate users via a username and password during login. | High |
| FR-28 | The system shall issue a JSON Web Token (JWT) upon successful login to manage user sessions. | High |
| FR-29 | The system shall validate the JWT on every protected request and reject expired or invalid tokens. | High |
| FR-30 | Each user account shall have a storage quota limit that restricts the total size of files they can upload. | High |
| FR-31 | The system shall display the user's current storage usage and remaining quota on their profile page. | Medium |
| FR-32 | The system shall prevent uploads that would cause a user to exceed their storage quota and notify the user. | High |
| FR-33 | The system shall maintain a user profile page displaying username, email, quota usage, and account creation date. | Medium |
| FR-34 | JWT tokens shall expire after 30 minutes of inactivity and require the user to log in again. | Medium |

### 3.9 File Operations

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-35 | The system shall allow authenticated users to upload files of any type including documents, images, and videos. | High |
| FR-36 | The system shall allow authenticated users to download any file they own or have been granted access to. | High |
| FR-37 | The system shall allow authenticated users to delete files they own from the central server. | High |
| FR-38 | The system shall allow authenticated users to rename any file they own. | High |
| FR-39 | When a file is renamed, the system shall update all associated metadata and shared links accordingly. | High |
| FR-40 | The system shall support upload and storage of documents (PDF, DOCX, TXT), images (JPG, PNG, GIF), and videos (MP4, AVI, MOV). | High |

### 3.10 File Organisation

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-41 | The system shall allow authenticated users to create folders and organise their files into a directory structure. | High |
| FR-42 | The system shall allow users to navigate into and out of folders through the web interface. | High |
| FR-43 | The system shall store and display metadata for every file including: file name, file size, file type, upload date, and owner username. | High |
| FR-44 | The system shall allow users to search for files by name. | High |
| FR-45 | The system shall allow users to filter files by file type (e.g. show only images, only documents). | Medium |
| FR-46 | Search results shall be returned within 3 seconds for a library of up to 500 files. | Medium |

### 3.11 Sharing and Access Control

| ID | Requirement | Priority |
|----|-------------|----------|
| FR-47 | The system shall allow file owners to generate a unique shareable link for any file they own. | High |
| FR-48 | Shareable links shall allow the recipient to view or download the file without requiring an account, if the file is set to public. | High |
| FR-49 | The system shall allow file owners to set a file's visibility to one of three states: Private, Public, or Shared. | High |
| FR-50 | Private files shall only be accessible by their owner. | High |
| FR-51 | Public files shall be accessible by anyone with the shareable link. | High |
| FR-52 | Shared files shall only be accessible by specific users designated by the file owner. | High |
| FR-53 | The system shall allow file owners to add or remove specific users from the shared access list of a file. | High |
| FR-54 | The system shall provide each authenticated user with a "Shared with Me" section listing all files other users have shared with them. | Medium |
| FR-55 | The system shall notify a user when a file has been shared with them. | Medium |

---

## 4. Non-Functional Requirements

### 4.1 Performance

- The system shall support at least 5 simultaneous client connections without crashing.
- File upload and download operations for files under 5MB shall complete within 10 seconds on the local network.
- The web interface shall load within 3 seconds under normal operating conditions.

### 4.2 Reliability

- Replication shall succeed for 100% of successful uploads under normal network conditions.
- The system shall not lose any file data during a single server failure if the replica is operational.

### 4.3 Usability

- The web interface shall be usable without any prior training.
- All error messages shall be written in plain language and indicate the action the user should take.

### 4.4 Security

- All file operations shall require a valid JWT token.
- User passwords shall not be stored or transmitted in plain text.
- JWT tokens shall expire after 30 minutes of inactivity.

### 4.5 Maintainability

- The codebase shall be organised into separate modules: `app.py`, `primary_server.py`, `replica_server.py`, `replication.py`, and `auth.py`.
- Each module shall include inline comments explaining the logic.

### 4.6 Portability

- The system shall run on Windows, macOS, and Linux without code modification.
- The only dependencies are Python 3.x, Flask, and PyJWT — all freely available.

---

## 5. Constraints and Limitations

- The system is built entirely in Python. No other programming language may be used.
- The system is designed for demonstration on a local area network only; it is not intended for deployment over the internet.
- Maximum supported file size is 10MB per upload.
- The system supports a maximum of one primary server and one replica server.

---

## 6. Team Responsibilities

| Role | Responsibility | Team Size |
|------|---------------|-----------|
| Primary Server Dev | Implement `primary_server.py`: file receive, store, and serve logic | 2 members |
| Replication Dev | Implement `replication.py`: auto-sync from primary to replica, failover logic | 2 members |
| Client Dev | Implement `app.py` and Flask UI templates for all client operations | 2 members |
| Testing | Write and execute all test cases from the test plan | 1 member |
| Documentation | Produce all project documents: SRS, protocol spec, DFDs, user manual, report | 1 member |
| Presentation & Demo | Prepare slides, coordinate live demo setup across laptops | 2 members |

---

## 7. Revision History

| Version | Date | Author | Description |
|---------|------|--------|-------------|
| 1.0 | June 2026 | Group 4 | Initial version |
| 1.1 | June 2026 | Group 4 | Added FR-26 to FR-55: User Management, File Operations, File Organisation, Sharing & Access Control |
