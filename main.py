 (cd "$(git rev-parse --show-toplevel)" && git apply --3way <<'EOF' 
diff --git a/main.py b/main.py
index 2400e1b817ea20c50a9a07338769b9fc390ee55c..056e46684f556df955fd11066883aa83ba1012e5 100644
--- a/main.py
+++ b/main.py
@@ -1,49 +1,74 @@
 import os
 import json
 import uuid
 from contextlib import contextmanager
 from datetime import datetime
 
+import yaml
 from flask import (
     Flask, render_template, jsonify, abort,
     request, redirect, url_for
 )
 import psycopg2
 from psycopg2 import pool
 from psycopg2.extras import RealDictCursor
 
 app = Flask(__name__)
 
 # ---- Config from env (set in app.yaml) ----
 INSTANCE_CONNECTION_NAME = os.getenv("INSTANCE_CONNECTION_NAME")
 DB_USER = os.getenv("DB_USER")
 DB_PASS = os.getenv("DB_PASS")
 DB_NAME = os.getenv("DB_NAME")
 ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "admin@aiforimpact.local")
 ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")  # optional simple protection; leave unset to disable
 
+
+def load_env_from_app_yaml() -> None:
+    """Populate missing env vars from app.yaml for local dev."""
+    global INSTANCE_CONNECTION_NAME, DB_USER, DB_PASS, DB_NAME, ADMIN_EMAIL, ADMIN_TOKEN
+    if all([INSTANCE_CONNECTION_NAME, DB_USER, DB_PASS, DB_NAME]):
+        return
+    try:
+        with open("app.yaml", "r", encoding="utf-8") as f:
+            data = yaml.safe_load(f) or {}
+        envs = data.get("env_variables", {})
+        INSTANCE_CONNECTION_NAME = INSTANCE_CONNECTION_NAME or envs.get("INSTANCE_CONNECTION_NAME")
+        DB_USER = DB_USER or envs.get("DB_USER")
+        DB_PASS = DB_PASS or envs.get("DB_PASS")
+        DB_NAME = DB_NAME or envs.get("DB_NAME")
+        ADMIN_EMAIL = ADMIN_EMAIL or envs.get("ADMIN_EMAIL", "admin@aiforimpact.local")
+        ADMIN_TOKEN = ADMIN_TOKEN or envs.get("ADMIN_TOKEN")
+    except FileNotFoundError:
+        # app.yaml absent: ignore silently for compatibility
+        pass
+    except Exception as e:  # pragma: no cover
+        print(f"Warning: failed to load env vars from app.yaml: {e}")
+
+
+load_env_from_app_yaml()
 DB_HOST = f"/cloudsql/{INSTANCE_CONNECTION_NAME}" if INSTANCE_CONNECTION_NAME else None
 
 # ---- Connection Pool (lazy) ----
 _pg_pool: pool.SimpleConnectionPool | None = None
 
 def init_pool():
     global _pg_pool
     if _pg_pool is None:
         if not all([DB_HOST, DB_NAME, DB_USER, DB_PASS]):
             raise RuntimeError("Database env vars are missing. Check app.yaml env_variables.")
         _pg_pool = psycopg2.pool.SimpleConnectionPool(
             minconn=1,
             maxconn=5,
             host=DB_HOST,
             dbname=DB_NAME,
             user=DB_USER,
             password=DB_PASS,
             cursor_factory=RealDictCursor,
             connect_timeout=10,
             options="-c search_path=public"
         )
 
 @contextmanager
 def get_conn():
     """Borrow/return a connection from the pool (lazy-init on first use)."""
 
EOF
)
