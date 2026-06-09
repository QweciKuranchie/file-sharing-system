# Contributing to Distributed File-Sharing System

Thank you for contributing! This guide helps keep the codebase consistent and the team productive.

---

## 📋 Table of Contents

- [Getting Started](#-getting-started)
- [Branch Strategy](#-branch-strategy)
- [Commit Messages](#-commit-messages)
- [Code Style](#-code-style)
- [Pull Request Process](#-pull-request-process)
- [Running Tests](#-running-tests)
- [Project Structure](#-project-structure)

---

## 🚀 Getting Started

1. **Clone the repo**

   ```bash
   git clone https://github.com/QweciKuranchie/file-sharing-system.git
   cd file-sharing-system
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   pip install pytest flake8   # dev dependencies
   ```

3. **Verify your setup**

   ```bash
   python -m pytest test_auth.py -v
   ```

   All 32 tests should pass before you start making changes.

---

## 🌿 Branch Strategy

We use a simplified Git Flow:

```
main              ← stable, production-ready code
  └── develop     ← integration branch for ongoing work
       ├── feat/BE-1-auth-module
       ├── feat/FE-2-upload-page
       └── fix/token-expiry-bug
```

| Branch | Purpose | Merges into |
|--------|---------|-------------|
| `main` | Stable releases and demo-ready code | — |
| `develop` | Integration of all in-progress features | `main` |
| `feat/<ticket>-<description>` | New features | `develop` |
| `fix/<description>` | Bug fixes | `develop` |

### Rules

- **Never push directly to `main`** — always go through a pull request.
- Create feature branches from `develop`.
- Keep branches short-lived — merge within 1–2 days.

---

## 💬 Commit Messages

Follow the [Conventional Commits](https://www.conventionalcommits.org/) format:

```
<type>(<scope>): <short summary>

<optional body>
```

### Types

| Type | When to use |
|------|------------|
| `feat` | New feature or functionality |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `test` | Adding or updating tests |
| `ci` | CI/CD workflow changes |
| `refactor` | Code change that neither fixes a bug nor adds a feature |
| `style` | Formatting, whitespace, missing semicolons (no logic change) |
| `chore` | Build tooling, dependency updates, housekeeping |

### Scope

Use the ticket ID or module name: `auth`, `BE-1`, `FE-2`, `replication`, `ci`, etc.

### Examples

```
feat(BE-1): add SQLite schema and auth module
fix(auth): handle expired JWT gracefully on logout
docs: add contributing guide
ci: add develop branch to GitHub Actions workflow
test(auth): add edge case for empty email registration
```

---

## 🎨 Code Style

### Python

- **Follow [PEP 8](https://peps.python.org/pep-0008/)** — enforced by flake8 in CI.
- **Max line length:** 127 characters.
- **Use type hints** on all public function signatures.
- **Docstrings:** Use [NumPy-style](https://numpydoc.readthedocs.io/en/latest/format.html) docstrings for public functions.
- **Imports:** Group in this order, separated by blank lines:
  1. Standard library
  2. Third-party packages
  3. Local modules

### Example

```python
"""Module docstring — one-line summary."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import jwt
from flask import Flask, request

from config import JWT_SECRET_KEY
from database import get_connection


def my_function(user_id: int, name: str) -> dict:
    """Short description of what the function does.

    Parameters
    ----------
    user_id : int
        The user's primary key.
    name : str
        The user's display name.

    Returns
    -------
    dict
        The created resource.
    """
    ...
```

### HTML / CSS / JavaScript

- Use **semantic HTML5** elements.
- Keep inline styles to a minimum — use CSS classes.
- Use `const` / `let` instead of `var`.

---

## 🔀 Pull Request Process

1. **Create a feature branch** from `develop`:

   ```bash
   git checkout develop
   git pull origin develop
   git checkout -b feat/BE-3-file-download
   ```

2. **Make your changes** — commit often with clear messages.

3. **Run tests and lint locally** before pushing:

   ```bash
   python -m pytest test_auth.py -v
   flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
   ```

4. **Push and open a PR** against `develop`:

   ```bash
   git push origin feat/BE-3-file-download
   ```

5. **PR checklist:**

   - [ ] Tests pass locally and in CI
   - [ ] No flake8 errors on critical rules (E9, F63, F7, F82)
   - [ ] New functions have type hints and docstrings
   - [ ] Existing tests are not broken
   - [ ] PR title follows commit message convention
   - [ ] At least 1 teammate has reviewed the code

6. **After approval**, the PR author merges via **Squash & Merge**.

---

## 🧪 Running Tests

```bash
# Run all auth tests (verbose)
python -m pytest test_auth.py -v

# Run a specific test class
python -m pytest test_auth.py::TestLogin -v

# Run a single test
python -m pytest test_auth.py::TestLogin::test_login_success_returns_jwt -v

# Run with flake8 lint check
flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
```

CI runs these automatically on every push to `develop` and `main`, and on all pull requests.

---

## 📁 Project Structure

| File / Directory | Owner | Purpose |
|-----------------|-------|---------|
| `app.py` | Client Dev | Flask routes, templates, HTTP layer |
| `auth.py` | Backend Dev | Registration, login, JWT, quotas |
| `config.py` | Backend Dev | Centralised configuration |
| `database.py` | Backend Dev | SQLite schema & connection helpers |
| `primary_server.py` | Primary Server Dev | TCP file server (port 9000) |
| `replica_server.py` | Replication Dev | Replica TCP server (port 9001) |
| `replication.py` | Replication Dev | Auto-sync & failover logic |
| `templates/` | Client Dev | HTML templates |
| `test_*.py` | Testing | Pytest test suites |
| `docs/` | Documentation | SRS, protocol spec, architecture |

---

## ❓ Questions?

Open an issue or reach out to any team member. When in doubt, check the project docs:

- [Software Requirements Specification](docs/SRS_FileSharing_Group4.md)
- [Communication Protocol Specification](docs/Protocol_Spec_Group4.md)
- [Architecture Diagram](docs/Architecture_Diagram_Group4.md)
