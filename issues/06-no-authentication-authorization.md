# Issue #6: No Authentication/Authorization

## Severity
**HIGH** 🔓

## Affected Components
- `streaming/server.py:14-88` (SSE endpoint)
- `streaming/server.py:91-95` (health check)

## Description

The SSE streaming endpoint and all API endpoints have **zero authentication or authorization**. Anyone who can reach the server can:
- Connect to any task's event stream
- View all data being processed
- Enumerate task IDs
- DOS the server with unlimited connections

```python
# server.py:14
async def stream_handler(request):
    # ❌ No authentication check!
    # ❌ No authorization check!
    # ❌ No rate limiting!
    task_instance = await coordinator.get_task_instance(...)
```

## How to Reproduce

### Test 1: Unauthenticated Access

```bash
# Anyone can access any task
curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1
# ✓ Works! No authentication required

# Try another user's task
curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/999
# ✓ Works if task exists!

# Enumerate task IDs
for i in {1..1000}; do
    curl -s -o /dev/null -w "%{http_code}" \
        http://127.0.0.1:8888/stream/tests/ExampleTask/$i
done
# ❌ Can enumerate all existing tasks
```

### Test 2: DOS Attack

```bash
# Open 10,000 connections
for i in {1..10000}; do
    curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1 &
done

# ❌ No rate limiting
# ❌ No connection limits per IP
# ❌ No authentication to identify attacker
# ❌ Server becomes unresponsive
```

### Test 3: Data Exposure

```python
# Scenario: Task processes sensitive data
class PaymentTask(StreamTask):
    async def process(self):
        await self.send_event('processing', {
            'credit_card': '4111-1111-1111-1111',  # Sensitive!
            'amount': 1000,
            'user_email': 'user@example.com'
        })

# ❌ Anyone can connect and see this data!
```

## Root Cause

The server was built for internal use without considering security:

```python
# server.py:14-88
async def stream_handler(request):
    # Extract params from URL
    app_name = path_parts[1]
    model_name = path_parts[2]
    task_id = int(path_parts[3])

    # ❌ No check: "Is this user allowed to see this task?"
    # ❌ No check: "Is this user authenticated?"
    # ❌ No check: "Has this IP made too many requests?"

    # Just serve the data
    task_instance = await coordinator.get_task_instance(...)
```

## Impact

1. **Data Breach** - Sensitive task data exposed to unauthorized users
2. **Privacy Violation** - Anyone can monitor any user's tasks
3. **DOS Vulnerability** - Trivial to overwhelm the server
4. **No Audit Trail** - Can't track who accessed what
5. **Compliance Issues** - Violates GDPR, HIPAA, SOC2, etc.

## Proposed Fix

### Fix 6.1: Add Authentication

**Option A: Token-Based Authentication**

```python
# server.py
import jwt
from django.conf import settings

async def authenticate_request(request):
    """Verify the request has a valid auth token"""
    # Get token from header
    auth_header = request.headers.get('Authorization', '')

    if not auth_header.startswith('Bearer '):
        return None

    token = auth_header[7:]  # Remove 'Bearer ' prefix

    try:
        # Verify JWT token
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=['HS256']
        )
        return payload.get('user_id')
    except jwt.InvalidTokenError:
        return None


async def stream_handler(request):
    # ✓ Authenticate
    user_id = await authenticate_request(request)
    if not user_id:
        return 401, {}, "Unauthorized"

    # ... rest of implementation
```

**Option B: Django Session Authentication**

```python
# server.py
from django.contrib.sessions.models import Session
from django.contrib.auth import get_user_model

User = get_user_model()

async def get_user_from_session(request):
    """Get user from Django session cookie"""
    session_key = request.cookies.get('sessionid')
    if not session_key:
        return None

    try:
        session = await Session.objects.aget(session_key=session_key)
        user_id = session.get_decoded().get('_auth_user_id')
        if user_id:
            return await User.objects.aget(pk=user_id)
    except Session.DoesNotExist:
        return None

    return None


async def stream_handler(request):
    # ✓ Authenticate
    user = await get_user_from_session(request)
    if not user:
        return 401, {}, "Unauthorized"

    # ... rest of implementation
```

### Fix 6.2: Add Authorization

Add user ownership to tasks:

```python
# models.py
from django.contrib.auth import get_user_model

User = get_user_model()

class StreamTask(models.Model):
    # ... existing fields ...

    # Add owner field
    owner = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='%(app_label)s_%(class)s_tasks'
    )

    # Add permission checking
    def can_access(self, user):
        """Check if user can access this task"""
        return self.owner_id == user.id

    class Meta:
        abstract = True
        permissions = [
            ('view_any_task', 'Can view any task'),
        ]


# server.py
async def stream_handler(request):
    # Authenticate
    user = await authenticate_request(request)
    if not user:
        return 401, {}, "Unauthorized"

    # Get task
    task_instance = await coordinator.get_task_instance(app_name, model_name, task_id)
    if not task_instance:
        return 404, {}, "Task not found"

    # ✓ Authorize
    if not task_instance.can_access(user):
        # Check if user has admin permission
        if not user.has_perm('streaming.view_any_task'):
            return 403, {}, "Forbidden"

    # ... rest of implementation
```

### Fix 6.3: Add Rate Limiting

```python
# server.py
from collections import defaultdict
from time import time

# Simple in-memory rate limiter
rate_limits = defaultdict(list)
MAX_REQUESTS_PER_MINUTE = 60

def get_client_ip(request):
    """Extract client IP from request"""
    forwarded = request.headers.get('X-Forwarded-For')
    if forwarded:
        return forwarded.split(',')[0]
    return request.headers.get('X-Real-IP', 'unknown')

def check_rate_limit(client_ip):
    """Check if client has exceeded rate limit"""
    now = time()
    minute_ago = now - 60

    # Clean old entries
    rate_limits[client_ip] = [
        timestamp for timestamp in rate_limits[client_ip]
        if timestamp > minute_ago
    ]

    # Check limit
    if len(rate_limits[client_ip]) >= MAX_REQUESTS_PER_MINUTE:
        return False

    # Record this request
    rate_limits[client_ip].append(now)
    return True


async def stream_handler(request):
    # ✓ Rate limiting
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        return 429, {'Retry-After': '60'}, "Too Many Requests"

    # ... rest of implementation
```

### Fix 6.4: Add CORS Configuration

```python
# server.py
def get_cors_headers(request):
    """Get CORS headers based on configuration"""
    origin = request.headers.get('Origin', '')

    # Check if origin is allowed
    allowed_origins = settings.STREAMING_ALLOWED_ORIGINS
    if origin in allowed_origins or '*' in allowed_origins:
        return {
            'Access-Control-Allow-Origin': origin,
            'Access-Control-Allow-Credentials': 'true',
            'Access-Control-Allow-Methods': 'GET, OPTIONS',
            'Access-Control-Allow-Headers': 'Authorization, Content-Type',
        }
    return {}


async def stream_handler(request):
    # Handle preflight
    if request.method == 'OPTIONS':
        return 200, get_cors_headers(request), ''

    # ... rest of implementation

    # Add CORS headers to response
    headers.update(get_cors_headers(request))
    return 200, headers, event_generator()
```

### Fix 6.5: Add Audit Logging

```python
# server.py
import logging

audit_logger = logging.getLogger('streaming.audit')

async def stream_handler(request):
    # ... authentication and authorization ...

    # ✓ Audit log
    audit_logger.info(
        'SSE connection',
        extra={
            'user_id': user.id,
            'task_id': task_id,
            'app_name': app_name,
            'model_name': model_name,
            'client_ip': get_client_ip(request),
            'timestamp': timezone.now().isoformat(),
        }
    )

    # ... rest of implementation
```

## Complete Secure Implementation

```python
# server.py (complete rewrite with security)
async def stream_handler(request):
    """Secure SSE endpoint with auth, authz, rate limiting"""

    # 1. Parse request
    path_parts = request.path.strip('/').split('/')
    if len(path_parts) != 4 or path_parts[0] != 'stream':
        return 404, {}, "Not Found"

    app_name = path_parts[1]
    model_name = path_parts[2]
    try:
        task_id = int(path_parts[3])
    except ValueError:
        return 400, {}, "Invalid task ID"

    # 2. Rate limiting
    client_ip = get_client_ip(request)
    if not check_rate_limit(client_ip):
        return 429, {'Retry-After': '60'}, "Too Many Requests"

    # 3. Authentication
    user = await authenticate_request(request)
    if not user:
        return 401, {}, "Unauthorized"

    # 4. Get task
    task_instance = await coordinator.get_task_instance(app_name, model_name, task_id)
    if task_instance is None:
        return 404, {}, "Task not found"

    # 5. Authorization
    if not task_instance.can_access(user):
        if not user.has_perm('streaming.view_any_task'):
            audit_logger.warning(
                'Unauthorized access attempt',
                extra={
                    'user_id': user.id,
                    'task_id': task_id,
                    'client_ip': client_ip,
                }
            )
            return 403, {}, "Forbidden"

    # 6. Audit log
    audit_logger.info(
        'SSE connection established',
        extra={
            'user_id': user.id,
            'task_id': task_id,
            'app_name': app_name,
            'model_name': model_name,
            'client_ip': client_ip,
        }
    )

    # 7. Handle completed tasks
    if task_instance.completed_at:
        headers = {
            'Content-Type': 'application/json',
        }
        headers.update(get_cors_headers(request))
        response_data = {
            'status': 'completed',
            'final_value': task_instance.final_value,
            'completed_at': task_instance.completed_at.isoformat(),
        }
        return 200, headers, json.dumps(response_data)

    # 8. Stream events
    client_queue = asyncio.Queue(maxsize=100)  # Bounded queue
    await task_instance.add_client(client_queue)

    async def event_generator():
        try:
            while True:
                event_data = await client_queue.get()
                event_type = event_data.get('type', 'message')
                data = event_data.get('data', {})

                # Add metadata
                data['_task_id'] = task_id
                data['_app'] = app_name
                data['_model'] = model_name
                data['_timestamp'] = event_data.get('timestamp')

                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(data)}\n\n"

                if event_type == 'complete' or event_type == 'error':
                    break

        finally:
            await task_instance.remove_client(client_queue)
            audit_logger.info(
                'SSE connection closed',
                extra={'user_id': user.id, 'task_id': task_id}
            )

    headers = {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
    }
    headers.update(get_cors_headers(request))

    return 200, headers, event_generator()
```

## Configuration

```python
# settings.py
# Streaming security settings
STREAMING_AUTH_REQUIRED = True
STREAMING_ALLOWED_ORIGINS = [
    'https://app.example.com',
    'http://localhost:3000',  # Development
]
STREAMING_RATE_LIMIT_PER_MINUTE = 60
STREAMING_MAX_CONNECTIONS_PER_USER = 10

# Audit logging
LOGGING = {
    'loggers': {
        'streaming.audit': {
            'handlers': ['audit_file'],
            'level': 'INFO',
        },
    },
    'handlers': {
        'audit_file': {
            'class': 'logging.FileHandler',
            'filename': 'logs/streaming_audit.log',
            'formatter': 'json',
        },
    },
}
```

## Testing

```python
async def test_unauthenticated_rejected(self):
    """Test that unauthenticated requests are rejected"""
    async with httpx.AsyncClient() as client:
        response = await client.get(
            'http://127.0.0.1:8888/stream/tests/ExampleTask/1',
            # No auth header
        )
        self.assertEqual(response.status_code, 401)

async def test_unauthorized_rejected(self):
    """Test that users can't access others' tasks"""
    # Create task owned by user1
    user1 = await User.objects.acreate_user(username='user1')
    task = await ExampleTask.objects.acreate(owner=user1)

    # Try to access as user2
    user2 = await User.objects.acreate_user(username='user2')
    token = generate_token(user2.id)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f'http://127.0.0.1:8888/stream/tests/ExampleTask/{task.pk}',
            headers={'Authorization': f'Bearer {token}'}
        )
        self.assertEqual(response.status_code, 403)

async def test_rate_limiting(self):
    """Test that rate limiting works"""
    user = await User.objects.acreate_user(username='user')
    token = generate_token(user.id)
    task = await ExampleTask.objects.acreate(owner=user)

    # Make 61 requests (limit is 60/minute)
    async with httpx.AsyncClient() as client:
        for i in range(61):
            response = await client.get(
                f'http://127.0.0.1:8888/stream/tests/ExampleTask/{task.pk}',
                headers={'Authorization': f'Bearer {token}'}
            )
            if i < 60:
                self.assertNotEqual(response.status_code, 429)
            else:
                self.assertEqual(response.status_code, 429)
```

## Related Issues

- #2 Memory Leaks (connection limits help prevent DOS)
- #3 Single-Process Architecture (rate limiting needs to be distributed)
- #8 Missing Observability (audit logs provide security monitoring)
