# Learning Portal

## Setup

1. Create virtual environment and install dependencies:
   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Provide database configuration via environment variables or `app.yaml`:
   ```bash
   export INSTANCE_CONNECTION_NAME="your-instance"
   export DB_USER="postgres_user"
   export DB_PASS="postgres_password"
   export DB_NAME="database_name"
   export ADMIN_EMAIL="admin@aiforimpact.local"  # optional
   export ADMIN_TOKEN="devtoken"                 # optional
   ```

   Instead of exports, you may create an `app.yaml` with an `env_variables` section containing the same keys.

3. Run the server:
   ```bash
   python main.py
   ```
   Visit `http://localhost:8080`.

The home page will still render even if the database is unreachable, showing an error message rather than failing.
