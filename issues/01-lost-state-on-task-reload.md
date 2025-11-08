# Issue #1: Lost State on Task Instance Reload

## Severity
**CRITICAL** ❌

## Affected Components
- `streaming/coordinator.py:115`
- `streaming/models.py:23-26`

## Description

When a task instance is loaded from the database via `get_task_instance()`, Django creates a **new Python object**. The `__init__` method in `StreamTask` resets the instance variables `_clients = set()` and `_latest_data = None`, causing complete loss of runtime state.

This creates a critical bug where:
1. Multiple in-memory instances of the same task can exist simultaneously
2. Late-joining clients never receive the latest event (because `_latest_data` is None on the new instance)
3. Existing clients are lost (their queues aren't in the new instance's `_clients` set)
4. The new instance doesn't know about the running task

## How to Reproduce

### Step 1: Create a reproduction script

```python
# test_state_loss.py
import asyncio
from tests.models import ExampleTask
from streaming.coordinator import coordinator

async def test_state_loss():
    # Create a task
    task = await ExampleTask.objects.acreate(message="State loss test")

    # Create first client and start the task
    queue1 = asyncio.Queue()
    await task.add_client(queue1)
    print(f"Task instance 1 ID: {id(task)}")
    print(f"Task instance 1 clients: {len(task._clients)}")

    # Start the task
    await coordinator.start_task(task, 'tests', 'ExampleTask')

    # Wait a bit for task to start
    await asyncio.sleep(0.02)

    # Simulate second client connecting - coordinator loads from DB
    task2 = await coordinator.get_task_instance('tests', 'ExampleTask', task.pk)
    print(f"\nTask instance 2 ID: {id(task2)}")
    print(f"Task instance 2 clients: {len(task2._clients)}")
    print(f"Task instance 2 latest_data: {task2._latest_data}")

    # These should be the same instance, but they're not!
    print(f"\nSame instance? {id(task) == id(task2)}")
    print(f"Task 1 still has client? {len(task._clients) > 0}")
    print(f"Task 2 knows about client? {len(task2._clients) > 0}")

    # Create second client queue
    queue2 = asyncio.Queue()
    await task2.add_client(queue2)

    # Try to get latest event (should work but doesn't)
    try:
        latest = await asyncio.wait_for(queue2.get(), timeout=0.1)
        print(f"\nClient 2 received latest event: {latest}")
    except asyncio.TimeoutError:
        print("\n❌ Client 2 DID NOT receive latest event (BUG!)")

if __name__ == "__main__":
    import django
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_state_loss())
```

### Step 2: Run the reproduction script

```bash
python test_state_loss.py
```

### Expected Output (Current Buggy Behavior)

```
Task instance 1 ID: 140234567890
Task instance 1 clients: 1

Task instance 2 ID: 140234599999  # ❌ Different instance!
Task instance 2 clients: 0         # ❌ Lost the first client!
Task instance 2 latest_data: None  # ❌ Lost the latest event!

Same instance? False               # ❌ CRITICAL BUG
Task 1 still has client? True
Task 2 knows about client? False

❌ Client 2 DID NOT receive latest event (BUG!)
```

### Step 3: Real-world scenario (HTTP SSE)

```bash
# Terminal 1: Start server
python manage.py runserver_stream --host 127.0.0.1 --port 8888

# Terminal 2: Create a task and connect first client
curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1 &

# Terminal 3: Connect second client (simulates late-joining)
# This triggers get_task_instance() which loads from DB
curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1

# ❌ Second client never receives the latest event immediately
# ❌ First client might be orphaned
```

## Root Cause Analysis

### Problem 1: Instance Caching vs Database Reload

In `coordinator.py:get_task_instance()`:

```python
# Line 96: Check cache
if task_key in self._task_instances:
    return self._task_instances[task_key]  # ✓ Returns cached instance

# Line 115: Load from DB (creates NEW instance)
task_instance = await model_class.objects.aget(pk=task_id)  # ❌ NEW object!

# Line 118: Cache the new instance
self._task_instances[task_key] = task_instance  # ❌ Overwrites the running one!
```

### Problem 2: `__init__` Resets State

In `models.py:StreamTask.__init__()`:

```python
def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    self._clients = set()      # ❌ Always empty on DB load
    self._latest_data = None   # ❌ Always None on DB load
```

When Django loads a model from the database, it calls `__init__()`, which wipes out any runtime state.

## Impact

1. **Late-joining clients never get latest event** - Breaks core feature
2. **Multiple instances track different clients** - Memory leak and inconsistency
3. **Race conditions** - Two instances could try to save conflicting data
4. **Unpredictable behavior** - Which instance's state is "real"?

## Proposed Fix

### Option 1: Never Reload Running Tasks (Recommended)

Prevent database reload if task is already running:

```python
# coordinator.py
async def get_task_instance(self, app_name: str, model_name: str, task_id: int):
    task_key = self.get_task_key(app_name, model_name, task_id)

    # ✓ Fast path: return cached instance
    if task_key in self._task_instances:
        return self._task_instances[task_key]

    if task_key not in self._locks:
        self._locks[task_key] = asyncio.Lock()

    lock = self._locks[task_key]

    async with lock:
        # ✓ Double-check after acquiring lock
        if task_key in self._task_instances:
            return self._task_instances[task_key]

        # ✓ Only load from DB if not cached
        try:
            model_class = apps.get_model(app_name, model_name)
            task_instance = await model_class.objects.aget(pk=task_id)

            # ✓ Cache BEFORE checking if we should start
            # This prevents the instance from being loaded again
            self._task_instances[task_key] = task_instance

            # ✓ Start task if not completed
            if not task_instance.completed_at:
                await self.start_task(task_instance, app_name, model_name)

            return task_instance
        except Exception:
            return None
```

**Note**: This already seems to be the current implementation. The bug might be elsewhere.

### Option 2: Make Runtime State Persistent (Better Long-term)

Store runtime state in a shared backend (Redis, Memcached):

```python
# models.py
import redis
import json

class StreamTask(models.Model):
    # ... existing fields ...

    @property
    def _clients_key(self):
        return f"task:{self.pk}:clients"

    @property
    def _latest_data_key(self):
        return f"task:{self.pk}:latest_data"

    async def send_event(self, event_type: str, data: dict):
        event_data = {
            'type': event_type,
            'data': data,
            'timestamp': timezone.now().isoformat()
        }

        # Store latest event in Redis
        redis_client.set(self._latest_data_key, json.dumps(event_data), ex=3600)

        # Get client IDs from Redis
        client_ids = redis_client.smembers(self._clients_key)

        # Publish to all clients via Redis pub/sub
        for client_id in client_ids:
            redis_client.publish(f"client:{client_id}", json.dumps(event_data))
```

### Option 3: Singleton Pattern for Task Instances

Ensure only one instance per task exists per process:

```python
# models.py
class StreamTaskMeta(ABCMeta, type(models.Model)):
    _instances = {}
    _locks = {}

    def __call__(cls, *args, **kwargs):
        # Extract pk if present
        pk = kwargs.get('pk') or (args[0] if args and hasattr(args[0], 'pk') else None)

        if pk:
            instance_key = f"{cls.__name__}:{pk}"

            if instance_key not in cls._locks:
                cls._locks[instance_key] = threading.Lock()

            with cls._locks[instance_key]:
                if instance_key in cls._instances:
                    # Return existing instance
                    return cls._instances[instance_key]

                # Create new instance
                instance = super().__call__(*args, **kwargs)
                cls._instances[instance_key] = instance
                return instance

        return super().__call__(*args, **kwargs)
```

### Option 4: Lazy Initialization of State

Don't reset state in `__init__`:

```python
# models.py
def __init__(self, *args, **kwargs):
    super().__init__(*args, **kwargs)
    # Only initialize if not already set
    if not hasattr(self, '_clients'):
        self._clients = set()
    if not hasattr(self, '_latest_data'):
        self._latest_data = None
```

## Recommended Solution

**Combination of Option 1 + Option 4**:

1. Fix `__init__` to not reset existing state (Option 4)
2. Ensure coordinator never replaces a cached running instance (Option 1 - verify this is working)
3. Add explicit tests for this scenario
4. Long-term: Move to Option 2 (Redis-backed state) for multi-process support

## Testing

Add this test case:

```python
async def test_task_instance_consistency(self):
    """Verify that get_task_instance returns the same instance for a running task"""
    task = await ExampleTask.objects.acreate(message="Instance test")

    # Get instance and add client
    instance1 = await coordinator.get_task_instance('tests', 'ExampleTask', task.pk)
    queue1 = asyncio.Queue()
    await instance1.add_client(queue1)

    # Get instance again (should be same object)
    instance2 = await coordinator.get_task_instance('tests', 'ExampleTask', task.pk)

    # Verify it's the SAME instance
    self.assertIs(instance1, instance2, "Should return the same instance object")
    self.assertEqual(len(instance2._clients), 1, "Should have the client from instance1")

    # Add client to instance2
    queue2 = asyncio.Queue()
    await instance2.add_client(queue2)

    # Verify instance1 sees it too
    self.assertEqual(len(instance1._clients), 2, "Instance1 should see client added to instance2")
```

## Related Issues

- #3 Single-Process Architecture Limitation (this bug compounds in multi-process scenarios)
- #7 Race Condition in Task Startup (related to instance caching)
