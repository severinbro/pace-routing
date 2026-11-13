#!/usr/bin/env python3
import hashlib
import json
from pathlib import Path
import secrets
import sys

password = sys.argv[1] if len(sys.argv) > 1 else secrets.token_urlsafe(18)
salt = secrets.token_bytes(16)
value = {'salt': salt.hex(), 'password_hash': hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 300000).hex(), 'secret': secrets.token_bytes(32).hex()}
path = Path('config/dashboard-auth.json')
path.write_text(json.dumps(value) + '\n')
path.chmod(0o600)
print(password)
