# Issue #5: Database Polling Anti-Pattern

## Severity
**MEDIUM** 📉

## Affected Components
- `tests/models.py:52-66` (ContinueTask polling loop)

## Description

The `ContinueTask` example demonstrates a database polling pattern where tasks repeatedly query the database every 50ms to check for field updates. This anti-pattern doesn't scale and wastes database resources.

```python
# Current implementation
while iteration < max_iterations:
    await self.arefresh_from_db()  # ❌ DB query every 50ms!
    if self.continue_field:
        break
    await asyncio.sleep(0.05)
```

This creates **20 database queries per second per task**.

## How to Reproduce

### Step 1: Create Multiple Polling Tasks

```python
# test_polling_load.py
import asyncio
from tests.models import ContinueTask
from streaming.coordinator import coordinator
from django.db import connection
from django.test.utils import override_settings

@override_settings(DEBUG=True)  # Enable query logging
async def test_polling_load():
    """Demonstrate database load from polling"""

    # Create 10 tasks that will poll
    tasks = []
    for i in range(10):
        task = await ContinueTask.objects.acreate(
            message=f"Task {i}",
            continue_field=False
        )
        tasks.append(task)
        await coordinator.start_task(task, 'tests', 'ContinueTask')

    # Let them poll for 1 second
    await asyncio.sleep(1.0)

    # Count queries
    queries = len(connection.queries)
    print(f"\nTotal queries in 1 second: {queries}")
    print(f"Expected: ~200 (10 tasks * 20 queries/sec)")
    print(f"Actual: {queries}")

    # Check query types
    refresh_queries = [q for q in connection.queries if 'SELECT' in q['sql']]
    print(f"SELECT queries: {len(refresh_queries)}")

    print("\n❌ Database is being hammered with polling queries!")

    # Clean up - stop the tasks
    for task in tasks:
        await ContinueTask.objects.filter(pk=task.pk).aupdate(continue_field=True)

    await asyncio.sleep(0.1)

if __name__ == "__main__":
    import django, os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_polling_load())
```

### Expected Output

```
Total queries in 1 second: 197
Expected: ~200 (10 tasks * 20 queries/sec)
Actual: 197
SELECT queries: 197

❌ Database is being hammered with polling queries!
```

### Step 2: Scale Test

```bash
# With 50 concurrent polling tasks
# = 50 * 20 = 1000 queries/second

# With 100 concurrent polling tasks
# = 100 * 20 = 2000 queries/second

# Most databases can't sustain this for long!
# Connection pool exhaustion
# Query queue buildup
# Slow response times
```

### Step 3: Monitor Database

```sql
-- PostgreSQL: Check for polling queries
SELECT
    count(*),
    query
FROM pg_stat_activity
WHERE query LIKE '%tests_continuetask%'
GROUP BY query;

-- You'll see hundreds of identical SELECT queries
-- All just checking if continue_field changed
```

## Root Cause Analysis

The pattern exists because there's no event-driven mechanism for external updates to notify the task. The only way for the task to know the field changed is to repeatedly check.

```python
# ContinueTask demonstrates this pattern
while iteration < max_iterations:
    await self.arefresh_from_db()  # The only way to check!
    if self.continue_field:
        break
    await asyncio.sleep(0.05)  # Arbitrary delay
```

## Impact

1. **Database Load** - Wastes significant database resources
2. **Connection Pool Exhaustion** - Each task holds a connection while polling
3. **Latency** - Polling interval (50ms) adds latency to detecting changes
4. **Doesn't Scale** - 100 tasks = 2000 queries/second
5. **Battery Drain** - On mobile/edge devices, constant queries waste power

## Proposed Fix

### Option 1: Django Signals (Simple, Same Process)

Use Django signals to notify tasks when fields change:

```python
# models.py
from django.db.models.signals import post_save
from django.dispatch import receiver
import asyncio

class ContinueTask(StreamTask):
    continue_field = models.BooleanField(default=False)
    _continue_event = None  # asyncio.Event for this instance

    async def process(self):
        # Create an event for this task
        self._continue_event = asyncio.Event()

        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })

        # Wait for signal instead of polling ✓
        try:
            await asyncio.wait_for(
                self._continue_event.wait(),
                timeout=5.0  # Max wait time
            )
            await self.send_event('final', {
                'message': 'Continue signal received',
                'continue': True
            })
        except asyncio.TimeoutError:
            await self.send_event('timeout', {
                'message': 'Timeout waiting for continue signal'
            })
            return "Timeout"

        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
        return "Completed"


# Signal handler
@receiver(post_save, sender=ContinueTask)
def notify_continue_task(sender, instance, **kwargs):
    """Notify task when continue_field changes"""
    if instance.continue_field and hasattr(instance, '_continue_event'):
        if instance._continue_event:
            # Set the event to wake up the waiting task
            instance._continue_event.set()
```

**Limitation**: Only works within same process (see Issue #3).

### Option 2: Redis Pub/Sub (Multi-Process)

Use Redis for cross-process notifications:

```python
# models.py
import redis.asyncio as redis

class ContinueTask(StreamTask):
    continue_field = models.BooleanField(default=False)

    async def process(self):
        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })

        # Subscribe to Redis channel for this task
        redis_client = redis.from_url('redis://localhost')
        pubsub = redis_client.pubsub()
        channel = f"task:{self.pk}:continue"
        await pubsub.subscribe(channel)

        # Wait for notification
        try:
            async for message in pubsub.listen():
                if message['type'] == 'message':
                    # Got the signal!
                    await self.send_event('final', {
                        'message': 'Continue signal received',
                        'continue': True
                    })
                    break
        finally:
            await pubsub.unsubscribe(channel)

        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
        return "Completed"


# Update method (called by external code)
async def set_continue(self):
    """Set continue_field and notify waiting task"""
    self.continue_field = True
    await self.asave(update_fields=['continue_field'])

    # Publish notification
    redis_client = redis.from_url('redis://localhost')
    await redis_client.publish(f"task:{self.pk}:continue", "go")
```

### Option 3: Database LISTEN/NOTIFY (PostgreSQL)

Use PostgreSQL's native pub/sub:

```python
# models.py
from django.db import connection

class ContinueTask(StreamTask):
    continue_field = models.BooleanField(default=False)

    async def process(self):
        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })

        # Listen for PostgreSQL notification
        channel = f"task_{self.pk}_continue"

        async with connection.cursor() as cursor:
            await cursor.execute(f"LISTEN {channel}")

            # Wait for notification
            import select
            while True:
                # Check for notification (with timeout)
                if select.select([connection.connection], [], [], 5.0) != ([], [], []):
                    connection.connection.poll()
                    while connection.connection.notifies:
                        notify = connection.connection.notifies.pop(0)
                        if notify.channel == channel:
                            # Got the signal!
                            await self.send_event('final', {
                                'message': 'Continue signal received',
                                'continue': True
                            })
                            break
                    break
                else:
                    # Timeout
                    await self.send_event('timeout', {
                        'message': 'Timeout waiting for continue signal'
                    })
                    return "Timeout"

        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
        return "Completed"


# Trigger function (PostgreSQL)
"""
CREATE OR REPLACE FUNCTION notify_task_continue()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.continue_field = TRUE AND OLD.continue_field = FALSE THEN
        PERFORM pg_notify('task_' || NEW.id || '_continue', 'go');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER task_continue_trigger
AFTER UPDATE ON tests_continuetask
FOR EACH ROW
EXECUTE FUNCTION notify_task_continue();
"""
```

### Option 4: Polling with Adaptive Intervals (Compromise)

If event-driven is not possible, at least optimize the polling:

```python
class ContinueTask(StreamTask):
    async def process(self):
        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })

        # Adaptive polling intervals
        intervals = [0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]  # Increasing delays
        interval_idx = 0
        max_iterations = 100

        for iteration in range(max_iterations):
            await self.arefresh_from_db()

            if self.continue_field:
                await self.send_event('final', {
                    'message': 'Continue signal received',
                    'continue': True
                })
                break

            # Use adaptive interval - poll less frequently over time
            current_interval = intervals[min(interval_idx, len(intervals) - 1)]
            await asyncio.sleep(current_interval)

            # Increase interval (exponential backoff)
            interval_idx += 1

        else:
            await self.send_event('timeout', {
                'message': 'Timeout waiting for continue signal'
            })
            return "Timeout"

        await self.send_event('complete', {
            'message': 'Task completed successfully'
        })
        return "Completed"
```

This reduces queries from 20/sec to:
- First 1 sec: 20 queries
- Next 1 sec: 10 queries
- Next 2 sec: 5 queries
- Next 5 sec: 2 queries
- After that: 0.5 queries/sec

Much better, but still not ideal.

## Recommended Solution

**Option 2 (Redis Pub/Sub)** is best for production:
- Works across multiple processes/servers
- Extremely efficient (event-driven, no polling)
- Low latency (immediate notification)
- Scales to thousands of tasks

**Option 1 (Django Signals)** is good for single-process dev/testing.

**Option 4 (Adaptive Polling)** is a fallback if Redis is not available.

## Migration Guide

```python
# Step 1: Add Redis support
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# Step 2: Update task implementation
class ContinueTask(StreamTask):
    async def process(self):
        # Try event-driven first, fall back to polling
        try:
            return await self._process_event_driven()
        except redis.ConnectionError:
            logger.warning("Redis unavailable, falling back to polling")
            return await self._process_polling()

    async def _process_event_driven(self):
        # Redis pub/sub implementation
        ...

    async def _process_polling(self):
        # Adaptive polling implementation
        ...
```

## Testing

```python
async def test_no_polling_queries(self):
    """Test that event-driven approach doesn't poll database"""
    from django.test.utils import override_settings
    from django.db import connection

    with override_settings(DEBUG=True):
        connection.queries.clear()

        task = await ContinueTask.objects.acreate(continue_field=False)
        await coordinator.start_task(task, 'tests', 'ContinueTask')

        await asyncio.sleep(0.5)

        # Trigger the event
        await task.set_continue()  # Uses Redis, not polling

        await asyncio.sleep(0.1)

        # Count SELECT queries
        select_queries = [q for q in connection.queries if 'SELECT' in q['sql']]

        # Should be minimal (only initial load, not 10+ polling queries)
        self.assertLess(len(select_queries), 5,
            f"Too many SELECT queries: {len(select_queries)}")
```

## Related Issues

- #3 Single-Process Architecture (Django signals don't work across processes)
- #8 Missing Observability (should track polling query counts)
