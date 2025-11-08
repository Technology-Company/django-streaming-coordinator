# Issue #9: Configuration Issues

## Severity
**MEDIUM** ⚙️

## Affected Components
- `config/settings.py`

## Description

The Django settings file has multiple issues that make it unsafe for production:

1. **Hardcoded insecure SECRET_KEY**
2. **DEBUG = True** (should never be true in production)
3. **Empty ALLOWED_HOSTS** (won't work in production)
4. **No environment-based configuration**
5. **No separation of dev/staging/prod settings**

```python
# settings.py:24
SECRET_KEY = 'django-insecure-6x2*yr&n8qeq-qq1l+60_!!cmcy8kh91j&3(-(@3&o8@!k)s^4'  # ❌

# settings.py:27
DEBUG = True  # ❌ Exposes stack traces, SQL queries, etc.

# settings.py:29
ALLOWED_HOSTS = []  # ❌ Won't work in production
```

## How to Reproduce

### Issue 1: Hardcoded SECRET_KEY

```bash
# Anyone with access to the code knows the SECRET_KEY
git clone <repository>
cat config/settings.py | grep SECRET_KEY

# ❌ Key is exposed in version control
# ❌ All deployments use the same key
# ❌ Can't rotate key without code change
```

### Issue 2: DEBUG Mode Enabled

```python
# Visit a non-existent URL
curl http://127.0.0.1:8888/nonexistent

# With DEBUG=True:
# ❌ Returns full Django debug page
# ❌ Exposes:
#    - Full Python path
#    - Environment variables
#    - Installed packages
#    - Source code snippets
#    - Database queries
# ❌ Security vulnerability!
```

### Issue 3: Empty ALLOWED_HOSTS

```bash
# Try to deploy to production
export DJANGO_SETTINGS_MODULE=config.settings
python manage.py runserver 0.0.0.0:8000

# Access from remote IP:
curl http://your-server-ip:8000/

# ❌ Gets "DisallowedHost" error
# ❌ Because ALLOWED_HOSTS = []
```

### Issue 4: No Environment Variables

```python
# Try to configure for different environments
# ❌ No way to set different DB for dev/staging/prod
# ❌ No way to set different SECRET_KEY
# ❌ No way to enable/disable DEBUG
# ❌ Everything is hardcoded
```

## Impact

1. **Security Breach** - Exposed SECRET_KEY, debug info leaks
2. **Production Failure** - App won't work with ALLOWED_HOSTS = []
3. **No Environment Parity** - Can't have separate dev/staging/prod configs
4. **Key Rotation Impossible** - SECRET_KEY is in version control
5. **Compliance Issues** - Violates security best practices

## Proposed Fix

### Fix 9.1: Use Environment Variables

```python
# config/settings.py
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# ✓ Secret key from environment
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-dev-key-only-for-local-development'  # Default for dev only
)

# ✓ Debug from environment
DEBUG = os.environ.get('DJANGO_DEBUG', 'False') == 'True'

# ✓ Allowed hosts from environment
ALLOWED_HOSTS = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    'localhost,127.0.0.1'
).split(',')

# ✓ Database from environment
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ.get('DB_NAME', 'streaming_dev'),
        'USER': os.environ.get('DB_USER', 'postgres'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

# ✓ Redis from environment
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# ✓ Streaming configuration
STREAMING_MAX_CONNECTIONS = int(os.environ.get('STREAMING_MAX_CONNECTIONS', '1000'))
STREAMING_AUTH_REQUIRED = os.environ.get('STREAMING_AUTH_REQUIRED', 'True') == 'True'
```

### Fix 9.2: Separate Settings Files

```
config/
├── settings/
│   ├── __init__.py
│   ├── base.py          # Shared settings
│   ├── development.py   # Dev overrides
│   ├── staging.py       # Staging overrides
│   └── production.py    # Production overrides
```

```python
# config/settings/base.py
"""Base settings shared across all environments"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Security
SECRET_KEY = os.environ['DJANGO_SECRET_KEY']  # Required!

# Application definition
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'streaming',
]

# ... common settings ...


# config/settings/development.py
"""Development settings"""
from .base import *

DEBUG = True
ALLOWED_HOSTS = ['localhost', '127.0.0.1']

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Development-specific settings
STREAMING_AUTH_REQUIRED = False  # Easier local testing
STREAMING_MAX_CONNECTIONS = 10   # Lower limits for dev


# config/settings/production.py
"""Production settings"""
from .base import *

DEBUG = False
ALLOWED_HOSTS = os.environ.get('DJANGO_ALLOWED_HOSTS', '').split(',')

# Security settings
SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 31536000
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# Production database (PostgreSQL)
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': os.environ['DB_NAME'],
        'USER': os.environ['DB_USER'],
        'PASSWORD': os.environ['DB_PASSWORD'],
        'HOST': os.environ['DB_HOST'],
        'PORT': os.environ.get('DB_PORT', '5432'),
        'CONN_MAX_AGE': 600,
    }
}

# Production-specific settings
STREAMING_AUTH_REQUIRED = True
STREAMING_MAX_CONNECTIONS = 10000
STREAMING_RATE_LIMIT = 100

# Logging
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'INFO',
    },
}
```

### Fix 9.3: Add Environment Validation

```python
# config/settings/base.py
import os
import sys

def get_required_env(key):
    """Get required environment variable or exit"""
    value = os.environ.get(key)
    if value is None:
        print(f"ERROR: Required environment variable {key} is not set")
        sys.exit(1)
    return value

def get_env(key, default=None, required=False):
    """Get environment variable with optional default"""
    if required:
        return get_required_env(key)
    return os.environ.get(key, default)


# Use in production settings
# config/settings/production.py
SECRET_KEY = get_required_env('DJANGO_SECRET_KEY')
ALLOWED_HOSTS = get_required_env('DJANGO_ALLOWED_HOSTS').split(',')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': get_required_env('DB_NAME'),
        'USER': get_required_env('DB_USER'),
        'PASSWORD': get_required_env('DB_PASSWORD'),
        'HOST': get_required_env('DB_HOST'),
        'PORT': get_env('DB_PORT', '5432'),
    }
}
```

### Fix 9.4: Add .env File Support

```python
# Install python-decouple or python-dotenv
# pip install python-decouple

# config/settings/base.py
from decouple import config, Csv

SECRET_KEY = config('SECRET_KEY')
DEBUG = config('DEBUG', default=False, cast=bool)
ALLOWED_HOSTS = config('ALLOWED_HOSTS', cast=Csv())

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': config('DB_NAME'),
        'USER': config('DB_USER'),
        'PASSWORD': config('DB_PASSWORD'),
        'HOST': config('DB_HOST', default='localhost'),
        'PORT': config('DB_PORT', default=5432, cast=int),
    }
}


# .env (NOT in version control!)
SECRET_KEY=your-secret-key-here
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
DB_NAME=streaming_dev
DB_USER=postgres
DB_PASSWORD=yourpassword
DB_HOST=localhost
DB_PORT=5432


# .env.example (IN version control as template)
SECRET_KEY=generate-a-secret-key
DEBUG=False
ALLOWED_HOSTS=your-domain.com
DB_NAME=streaming_prod
DB_USER=postgres
DB_PASSWORD=
DB_HOST=db.example.com
DB_PORT=5432
```

### Fix 9.5: Update .gitignore

```
# .gitignore
*.pyc
__pycache__/
db.sqlite3
/media/
/static/

# Environment files
.env
.env.local
.env.*.local

# Secrets
secrets/
*.pem
*.key

# IDE
.vscode/
.idea/
*.swp
```

## Usage

```bash
# Development
export DJANGO_SETTINGS_MODULE=config.settings.development
python manage.py runserver

# Or with .env file
python manage.py runserver  # Automatically loads .env

# Staging
export DJANGO_SETTINGS_MODULE=config.settings.staging
export DJANGO_SECRET_KEY=...
export DJANGO_ALLOWED_HOSTS=staging.example.com
python manage.py runserver

# Production
export DJANGO_SETTINGS_MODULE=config.settings.production
export DJANGO_SECRET_KEY=...
export DJANGO_ALLOWED_HOSTS=example.com,www.example.com
gunicorn config.wsgi:application
```

## Secret Key Generation

```python
# Generate a new secret key
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

## Testing

```python
def test_settings_validation(self):
    """Test that production settings require environment variables"""
    import os
    import importlib

    # Clear environment
    for key in ['DJANGO_SECRET_KEY', 'DJANGO_ALLOWED_HOSTS', 'DB_NAME']:
        os.environ.pop(key, None)

    # Try to import production settings
    with self.assertRaises(SystemExit):
        importlib.import_module('config.settings.production')

def test_development_settings_have_defaults(self):
    """Test that development settings work without environment variables"""
    import importlib

    # Should work without any environment variables
    settings = importlib.import_module('config.settings.development')
    self.assertTrue(settings.DEBUG)
    self.assertIn('localhost', settings.ALLOWED_HOSTS)
```

## Deployment Checklist

- [ ] Generate unique SECRET_KEY for each environment
- [ ] Set DEBUG=False in production
- [ ] Configure ALLOWED_HOSTS with actual domain
- [ ] Set up database credentials
- [ ] Set up Redis URL
- [ ] Configure logging
- [ ] Set up SSL certificates
- [ ] Enable security headers
- [ ] Remove .env from version control
- [ ] Add .env.example as template

## Related Issues

- #6 No Authentication (settings need auth configuration)
- #8 Missing Observability (settings need logging/metrics config)
