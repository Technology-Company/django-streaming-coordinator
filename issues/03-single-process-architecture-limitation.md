# Issue #3: Single-Process Architecture Limitation

## Severity
**HIGH** 🚫

## Affected Components
- `streaming/coordinator.py:6-23` (Singleton coordinator)
- Entire architecture design

## Description

The `TaskCoordinator` is a **process-local singleton** that stores task state in memory. This design fundamentally prevents horizontal scaling and multi-worker deployments, which are standard in production Django/ASGI applications.

Each worker process has:
- Its own coordinator instance
- Its own `_tasks` dictionary
- Its own `_task_instances` cache
- No shared state with other workers

This leads to multiple critical problems when running with multiple workers.

## How to Reproduce

### Step 1: Start server with multiple workers

```bash
# Standard production deployment pattern
uvicorn streaming.server:app --workers 4 --host 0.0.0.0 --port 8888
```

### Step 2: Create a test to demonstrate the issue

```python
# test_multi_worker.py
import asyncio
import httpx
import json
from tests.models import ExampleTask

async def test_multi_worker_race():
    """Demonstrate that multiple workers can run the same task simultaneously"""

    # Create a task
    async with httpx.AsyncClient() as client:
        # Create task in database
        task = await ExampleTask.objects.acreate(message="Multi-worker test")
        print(f"Created task {task.pk}")

        # Make 4 concurrent requests to the SSE endpoint
        # With 4 workers, likely hits different worker processes
        async def connect_client(client_id):
            print(f"Client {client_id} connecting...")
            events = []
            try:
                from httpx_sse import aconnect_sse
                async with aconnect_sse(
                    client,
                    'GET',
                    f'http://127.0.0.1:8888/stream/tests/ExampleTask/{task.pk}'
                ) as event_source:
                    async for event in event_source.aiter_sse():
                        data = json.loads(event.data)
                        events.append({
                            'type': event.event,
                            'data': data,
                            'worker': data.get('_worker', 'unknown')
                        })
                        print(f"Client {client_id} got event: {event.event}")
                        if event.event == 'complete':
                            break
            except Exception as e:
                print(f"Client {client_id} error: {e}")
            return client_id, events

        # Connect 4 clients simultaneously
        results = await asyncio.gather(*[connect_client(i) for i in range(4)])

        # Analyze results
        print("\n=== Analysis ===")
        for client_id, events in results:
            print(f"Client {client_id}: {len(events)} events")

        # Check if task ran multiple times
        # Look for multiple 'start' events or database writes
        task_runs = 0
        for client_id, events in results:
            start_events = [e for e in events if e['type'] == 'start']
            if start_events:
                task_runs += 1

        print(f"\n❌ Task may have run {task_runs} times (should be 1)!")

        # Check database
        await task.arefresh_from_db()
        print(f"Task completed_at: {task.completed_at}")
        print(f"Task final_value: {task.final_value}")

if __name__ == "__main__":
    import django
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_multi_worker_race())
```

### Expected Output (Buggy Behavior)

```
Created task 1

Client 0 connecting...
Client 1 connecting...
Client 2 connecting...
Client 3 connecting...

Client 0 got event: start  # Worker 1 started task
Client 1 got event: start  # Worker 2 started task (duplicate!)
Client 2 got event: start  # Worker 3 started task (duplicate!)
Client 3 got event: start  # Worker 4 started task (duplicate!)

...

=== Analysis ===
Client 0: 5 events
Client 1: 5 events
Client 2: 5 events
Client 3: 5 events

❌ Task may have run 4 times (should be 1)!
```

### Step 3: Demonstrate state inconsistency

```python
# test_worker_state_isolation.py
import requests
import time

# Create a task
task_id = 1  # Assume task exists

# Connect to worker 1 (first request)
response1 = requests.get(
    f'http://127.0.0.1:8888/stream/tests/ExampleTask/{task_id}',
    stream=True
)

# Connect to worker 2 (second request - different worker)
response2 = requests.get(
    f'http://127.0.0.1:8888/stream/tests/ExampleTask/{task_id}',
    stream=True
)

# ❌ Each worker loads the task independently
# ❌ Each worker has its own coordinator instance
# ❌ Events from worker 1 don't reach clients on worker 2
# ❌ No state sharing between workers
```

## Root Cause Analysis

### Cause 1: Process-Local Singleton

```python
# coordinator.py:6-13
class TaskCoordinator:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
```

This creates **one instance per Python process**, not per application. With 4 workers = 4 coordinators.

### Cause 2: In-Memory State Storage

```python
# coordinator.py:19-22
self._tasks: Dict[str, asyncio.Task] = {}
self._task_instances: Dict[str, 'StreamTask'] = {}
self._locks: Dict[str, asyncio.Lock] = {}
```

All state lives in process memory. Workers can't share this.

### Cause 3: No Distributed Locking

```python
# coordinator.py:43-44
if task_key in self._tasks and not self._tasks[task_key].done():
    return
```

This check only works within a single process. Worker 1 doesn't know Worker 2 is running the task.

## Impact

### 1. **Duplicate Task Execution** ❌

Multiple workers can start the same task simultaneously:
- Wasted compute resources
- Duplicate API calls (if task fetches data)
- Race conditions on database writes
- Incorrect billing (if tasks are billable)

### 2. **Inconsistent Client Experience** ❌

Clients connected to different workers see different events:
- Client A (Worker 1) sees events from Worker 1's task instance
- Client B (Worker 2) sees events from Worker 2's task instance
- Events don't match
- Impossible to guarantee ordering

### 3. **Database Corruption Risk** ❌

Multiple task instances updating the same database record:

```python
# Worker 1
await task.mark_completed(final_value="Result from worker 1")

# Worker 2 (simultaneously)
await task.mark_completed(final_value="Result from worker 2")

# ❌ Last write wins - data loss
```

### 4. **Cannot Use Standard Production Patterns** ❌

Modern ASGI deployment requires multiple workers:
- **Gunicorn + Uvicorn workers**: Standard pattern
- **Kubernetes with replicas**: Each pod is isolated
- **Load balancers**: Distribute requests across workers
- **Auto-scaling**: Add/remove workers dynamically

The current architecture breaks all of these.

### 5. **No High Availability** ❌

If a worker crashes:
- All its running tasks are lost
- No other worker can take over
- Clients must reconnect and hope they hit a different worker

## Proposed Fix

### Option 1: Distributed Coordinator with Redis (Recommended)

Use Redis for distributed state and locking:

```python
# coordinator.py
import redis.asyncio as redis
import json
from contextlib import asynccontextmanager

class DistributedTaskCoordinator:
    def __init__(self, redis_url='redis://localhost:6379'):
        self.redis = redis.from_url(redis_url)

    async def start_task(self, task_instance, app_name, model_name):
        task_key = self.get_task_key(app_name, model_name, task_instance.pk)

        # Distributed lock using Redis
        lock_key = f"lock:{task_key}"
        acquired = await self.redis.set(
            lock_key,
            "locked",
            nx=True,  # Only set if doesn't exist
            ex=300    # Expire after 5 minutes (safety)
        )

        if not acquired:
            # Another worker is running this task
            return False

        # Mark task as running in Redis
        await self.redis.setex(
            f"running:{task_key}",
            3600,  # 1 hour TTL
            json.dumps({
                'started_at': timezone.now().isoformat(),
                'worker_id': os.getpid()
            })
        )

        # Start the task
        try:
            final_value = await task_instance.process()
            await task_instance.mark_completed(final_value=final_value)
        finally:
            # Release lock
            await self.redis.delete(lock_key)
            await self.redis.delete(f"running:{task_key}")

        return True

    async def is_task_running(self, app_name, model_name, task_id):
        task_key = self.get_task_key(app_name, model_name, task_id)
        return await self.redis.exists(f"running:{task_key}")
```

### Option 2: Database-Based Locking

Use database advisory locks (works but slower):

```python
# coordinator.py
from django.db import connection

class DBLockedTaskCoordinator:
    async def start_task(self, task_instance, app_name, model_name):
        task_key_hash = hash(self.get_task_key(app_name, model_name, task_instance.pk))

        # Try to acquire lock
        async with connection.cursor() as cursor:
            # PostgreSQL advisory lock
            await cursor.execute(
                "SELECT pg_try_advisory_lock(%s)",
                [task_key_hash]
            )
            result = await cursor.fetchone()

            if not result[0]:  # Lock not acquired
                return False

            try:
                # Run task
                final_value = await task_instance.process()
                await task_instance.mark_completed(final_value=final_value)
            finally:
                # Release lock
                await cursor.execute(
                    "SELECT pg_advisory_unlock(%s)",
                    [task_key_hash]
                )
```

### Option 3: Single-Leader Architecture

Only one worker handles tasks, others just serve SSE:

```python
# coordinator.py
import os

class SingleLeaderCoordinator:
    def __init__(self):
        # Only first worker (based on env var) runs tasks
        self.is_leader = os.environ.get('WORKER_ID') == '0'

    async def start_task(self, task_instance, app_name, model_name):
        if not self.is_leader:
            # This worker doesn't run tasks, just serves clients
            # Wait for leader to start the task
            return

        # Leader runs the task
        # (existing implementation)
```

### Option 4: Redis Pub/Sub for Event Distribution

Share events across all workers:

```python
# models.py
import redis.asyncio as redis

class StreamTask(models.Model):
    # ... existing fields ...

    async def send_event(self, event_type: str, data: dict):
        event_data = {
            'type': event_type,
            'data': data,
            'timestamp': timezone.now().isoformat()
        }

        # Publish to Redis channel
        redis_client = redis.from_url('redis://localhost')
        channel = f"task:{self.pk}:events"
        await redis_client.publish(channel, json.dumps(event_data))

        # Also send to local clients
        for queue in self._clients:
            try:
                await queue.put(event_data)
            except Exception:
                self._clients.discard(queue)

# server.py
async def stream_handler(request):
    # ...

    # Subscribe to Redis channel for this task
    redis_client = redis.from_url('redis://localhost')
    pubsub = redis_client.pubsub()
    channel = f"task:{task_id}:events"
    await pubsub.subscribe(channel)

    async def event_generator():
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    event_data = json.loads(message['data'])
                    event_type = event_data.get('type', 'message')
                    data = event_data.get('data', {})

                    yield f"event: {event_type}\n"
                    yield f"data: {json.dumps(data)}\n\n"

                    if event_type == 'complete' or event_type == 'error':
                        break
        finally:
            await pubsub.unsubscribe(channel)

    return 200, headers, event_generator()
```

## Recommended Solution

**Hybrid Approach** combining Option 1 + Option 4:

1. **Redis-based distributed locking** (Option 1) - Ensures only one worker runs a task
2. **Redis Pub/Sub for events** (Option 4) - All workers can serve clients
3. **Database for persistence** - Existing approach, keep it
4. **Health checks** - Monitor which workers are alive

This provides:
- ✅ No duplicate task execution
- ✅ All clients get same events regardless of worker
- ✅ Can scale horizontally
- ✅ High availability (any worker can serve clients)
- ✅ Works with Kubernetes, load balancers, auto-scaling

## Migration Path

**Phase 1**: Add Redis (optional, graceful degradation)
```python
# Use Redis if available, fall back to single-process
try:
    coordinator = DistributedTaskCoordinator()
except redis.ConnectionError:
    coordinator = TaskCoordinator()  # Existing
```

**Phase 2**: Make Redis required
```python
# Require Redis in production
coordinator = DistributedTaskCoordinator()
```

**Phase 3**: Add pub/sub
```python
# Distribute events via Redis
```

## Testing

```python
# tests/test_distributed.py
import pytest
import asyncio
from tests.models import ExampleTask

@pytest.mark.skipif(not redis_available(), reason="Requires Redis")
async def test_distributed_locking(self):
    """Test that only one worker runs a task"""

    task = await ExampleTask.objects.acreate(message="Distributed test")

    # Simulate two workers trying to start the same task
    coord1 = DistributedTaskCoordinator()
    coord2 = DistributedTaskCoordinator()

    results = await asyncio.gather(
        coord1.start_task(task, 'tests', 'ExampleTask'),
        coord2.start_task(task, 'tests', 'ExampleTask')
    )

    # Exactly one should succeed
    self.assertEqual(sum(results), 1, "Only one worker should run the task")

async def test_cross_worker_events(self):
    """Test that clients on different workers see same events"""

    # This requires actually spawning multiple worker processes
    # and connecting clients to each
    # (Complex integration test - omitted for brevity)
    pass
```

## Configuration

```python
# settings.py
STREAMING_REDIS_URL = os.environ.get(
    'STREAMING_REDIS_URL',
    'redis://localhost:6379/0'
)

STREAMING_COORDINATOR_MODE = os.environ.get(
    'STREAMING_COORDINATOR_MODE',
    'distributed'  # or 'single-process' for development
)
```

## Related Issues

- #1 Lost State on Task Instance Reload (exacerbated in multi-process)
- #2 Memory Leaks (multiplied by number of workers)
- #7 Race Condition in Task Startup (happens across processes too)
- #8 Missing Observability (need to monitor all workers)
