<div align="center">
  <h2>AI-PRIORI (Backend)</h2>
</div>
<div align="center">
  <h3>DATA-INTELLIGENCE-AUTONOMY</h3>
</div>

## Overview

The AI-PRIORI Backend operates as the core intelligence engine, managing distributed agentic workflows, autonomous email dispatch, and sovereign role-based access control (RBAC). Built with FastAPI and powered by advanced agent architectures, this high-performance system facilitates seamless multi-tenant campaign logistics and execution.

## Core Architecture

- **Sovereign Governance:** High-security JWT-based provisioning handling structured authorization for Tier 1 (Super Admin), Tier 2 (Admin), and automated operational boundaries for Standard Users.
- **Agentic Workflow Hub:** Deep integration points for coordinating dynamic outreach logic, researching corporate data dynamically, and assembling custom distributions.
- **Stateless Capability Vault:** AES-256 Vault-encrypted tokens that execute official Google API handshakes through OAuth2 to distribute high-fidelity HTML dispatch metrics effortlessly without storing live passwords.
- **System Stability:** Persistent SQLite or scalable PostgreSQL deployment structures mediated seamlessly through the SQLAlchemy ORM layer.

## Tech Stack

- **Framework:** [FastAPI](https://fastapi.tiangolo.com/) (Python 3.10+)
- **ORM Storage:** SQLAlchemy
- **Authentication Ecosystem:** JSON Web Tokens (JWT), Cryptographic Hashing algorithms, Google OAuth2 Identity protocols
- **Communication Systems:** Secure SMTP/Google client builds

---

## 🚀 Quick Setup & Installation

To initialize the intelligence routing server locally, proceed with the following steps.

### Prerequisites
- Python 3.10 or higher
- `pip` package manager

### 1. Initialize Sector Operations
Clone the active repository and maneuver into your local backend framework:
```bash
git clone https://github.com/sainth-stack/CRM-BACKEND.git
cd CRM-BACKEND
```

### 2. Configure Virtual Environment
Activate a fully isolated Python environment to completely prevent dependency collisions inside the active host:
```bash
# For Windows Operators:
python -m venv venv
venv\Scripts\activate

# For Mac/Linux Operators:
python3 -m venv venv
source venv/bin/activate
```

### 3. Resolve Payload Dependencies
Extract and install the required architectural processing systems:
```bash
pip install -r requirements.txt
```

### 4. Inject Vault Credentials
You must bind the backend instance to the system identity matrix and external API credentials. Create a `.env` file within the base backend directory:
```env
# Required cryptographic signing
SECRET_KEY=paste_secure_token_here
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=43200 

# Optional capability injection
GOOGLE_CLIENT_ID=your_id
GOOGLE_CLIENT_SECRET=your_secret
```

### 5. Ignite the AI-PRIORI Core
Spin up the hyper-fast `uvicorn` development instance allowing live operational feedback:
```bash
uvicorn main:app --reload --port 8000
```
Operational readiness signals should follow immediately. The active deployment UI mapping API functions rests at `http://localhost:8000/docs`.

---

<div align="center">
  <p><i>Precision Sector Intelligence | Protected Asset</i></p>
</div>
