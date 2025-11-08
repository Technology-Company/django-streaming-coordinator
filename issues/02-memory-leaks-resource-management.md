# Issue #2: Memory Leaks & Resource Management

## Severity
**HIGH** ⚠️

## Affected Components
- `streaming/models.py:46` (unbounded queues)
- `streaming/server.py:47` (queue creation)
- `streaming/client.py:38-54` (unclosed httpx clients)

## Description

The system has multiple resource management issues that lead to memory leaks:

1. **Unbounded Queue Growth**: Client queues have unlimited size and no backpressure mechanism
2. **HTTPx Client Never Closed**: The singleton StreamingClient creates HTTP clients but never closes them
3. **No Connection Limits**: Unlimited concurrent SSE connections possible

## Sub-Issue 2.1: Unbounded Queue Growth

### How to Reproduce

```python
# test_queue_memory_leak.py
import asyncio
import sys
from tests.models import ExampleTask
from streaming.coordinator import coordinator

async def test_unbounded_queue():
    """Demonstrate memory leak from unbounded queues"""

    # Create a custom task that sends many events very quickly
    from streaming.models import StreamTask
    from django.db import models

    class SpamTask(StreamTask):
        class Meta:
            app_label = 'tests'

        async def process(self):
            # Send 10,000 events as fast as possible
            for i in range(10000):
                await self.send_event('spam', {
                    'iteration': i,
                    'data': 'x' * 1000  # 1KB per event
                })
                if i % 1000 == 0:
                    print(f"Sent {i} events")
            return "Done"

    # Create task instance (not saved to DB, just for demo)
    task = SpamTask()

    # Create a client that DOESN'T consume events (disconnected/slow client)
    slow_queue = asyncio.Queue()  # ❌ Unlimited size!
    await task.add_client(slow_queue)

    print(f"Initial queue size: {slow_queue.qsize()}")
    print(f"Queue maxsize: {slow_queue._maxsize}")  # 0 = unlimited

    # Start task
    task_coro = asyncio.create_task(task.process())

    # Wait for task to send all events
    await task_coro

    # Check queue size
    print(f"\nFinal queue size: {slow_queue.qsize()}")
    print(f"Memory consumed: ~{slow_queue.qsize() * 1000 / 1024 / 1024:.2f} MB")
    print("❌ Queue grew unbounded! Memory leak!")

if __name__ == "__main__":
    import django
    import os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_unbounded_queue())
```

### Expected Output

```
Initial queue size: 0
Queue maxsize: 0  # ❌ Unlimited!

Sent 0 events
Sent 1000 events
Sent 2000 events
...
Sent 9000 events

Final queue size: 10000
Memory consumed: ~9.77 MB
❌ Queue grew unbounded! Memory leak!
```

### Real-World Scenario

```bash
# A client connects but network is slow/stalled
curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1 --limit-rate 1K

# Meanwhile the task is sending events at high speed
# The queue grows and grows until OOM
```

## Sub-Issue 2.2: HTTPx Client Never Closed

### How to Reproduce

```python
# test_httpx_leak.py
import asyncio
from streaming.client import get_client, StreamingClient
import psutil
import os

async def test_httpx_leak():
    """Demonstrate that httpx clients are never closed"""

    process = psutil.Process(os.getpid())

    # Get initial connection count
    initial_connections = len(process.connections())
    print(f"Initial connections: {initial_connections}")

    # Create multiple clients (should all be same singleton)
    for i in range(10):
        client = get_client(f"http://example{i}.com:8000")
        # Use the async client
        _ = client.async_client
        print(f"Created client {i}, connections: {len(process.connections())}")

    # Simulate application shutdown - no cleanup!
    print("\nSimulating shutdown... (no cleanup happens)")

    # Check connections are still open
    final_connections = len(process.connections())
    print(f"Final connections: {final_connections}")
    print(f"Leaked connections: {final_connections - initial_connections}")

    # Try to manually close
    print("\nManually closing...")
    await get_client().aclose()

    # But the singleton still exists!
    print(f"Can still access client: {get_client().async_client}")
    print("❌ Client recreated after close! No proper lifecycle management!")

if __name__ == "__main__":
    asyncio.run(test_httpx_leak())
```

### Expected Output

```
Initial connections: 5

Created client 0, connections: 6
Created client 1, connections: 6  # Same instance, but...
...

Final connections: 6
Leaked connections: 1

Manually closing...
Can still access client: <httpx.AsyncClient object at 0x...>
❌ Client recreated after close! No proper lifecycle management!
```

### Real-World Impact

```python
# In a Django application lifecycle:

# 1. Application starts
# 2. First task uses get_client()
# 3. HTTPx client created with connection pool
# 4. Application receives shutdown signal
# 5. Django shuts down... but httpx client is never closed!
# 6. Connection pool remains open
# 7. Warnings in logs: "Unclosed client session"
```

## Sub-Issue 2.3: Unlimited Concurrent Connections

### How to Reproduce

```bash
# test_connection_flood.sh

# Simulate 1000 concurrent clients connecting
for i in {1..1000}; do
    curl -N http://127.0.0.1:8888/stream/tests/ExampleTask/1 > /dev/null 2>&1 &
done

# Check memory usage
watch -n 1 'ps aux | grep "python manage.py runserver_stream"'

# ❌ Memory grows linearly with connections
# ❌ No limit on concurrent connections
# ❌ Easy DOS vector
```

## Root Cause Analysis

### Cause 1: Default Queue Behavior

```python
# models.py:46 and server.py:47
queue = asyncio.Queue()  # ❌ maxsize=0 (unlimited)
```

Python's `asyncio.Queue(maxsize=0)` means unlimited size. Events accumulate if:
- Client is slow to consume
- Client disconnected but not removed from `_clients`
- Network is congested

### Cause 2: No Cleanup Hooks

```python
# client.py
class StreamingClient:
    def __init__(self, base_url: str):
        # Creates client but no __del__ or cleanup hooks
        pass

    # No __del__ method
    # No atexit handler
    # No Django signal handlers for shutdown
```

### Cause 3: Singleton Anti-Pattern

```python
# client.py:123-147
_client_instance: Optional[StreamingClient] = None

def get_client(base_url: str = "...") -> StreamingClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = StreamingClient(base_url)
    return _client_instance
```

Once created, lives forever. No lifecycle management.

## Impact

1. **Memory Exhaustion**: Long-running tasks with slow clients can consume gigabytes of memory
2. **Resource Leaks**: HTTP connections never closed on shutdown
3. **DOS Vulnerability**: Attackers can open unlimited connections
4. **Unpredictable Behavior**: System runs fine until suddenly OOM kills the process

## Proposed Fix

### Fix 2.1: Bounded Queues with Backpressure

```python
# models.py
class StreamTask(models.Model):
    # ... existing code ...

    # Class-level config
    MAX_QUEUE_SIZE = 100  # Configurable

    async def add_client(self, queue):
        # Create bounded queue
        if queue._maxsize == 0:  # If unbounded
            # Replace with bounded queue
            bounded_queue = asyncio.Queue(maxsize=self.MAX_QUEUE_SIZE)
            queue = bounded_queue

        self._clients.add(queue)

        # Send latest event if available
        if self._latest_data:
            try:
                await asyncio.wait_for(
                    queue.put(self._latest_data),
                    timeout=1.0  # Don't block forever
                )
            except (asyncio.TimeoutError, asyncio.QueueFull):
                # Queue full - remove slow client
                self._clients.discard(queue)
                raise SlowClientError("Client queue full")

    async def send_event(self, event_type: str, data: dict):
        event_data = {
            'type': event_type,
            'data': data,
            'timestamp': timezone.now().isoformat()
        }

        self._latest_data = event_data

        if not self._clients:
            return

        # Send with timeout and handle full queues
        async def send_to_client(queue):
            try:
                await asyncio.wait_for(
                    queue.put(event_data),
                    timeout=0.1  # Fast timeout
                )
                return (queue, None)
            except (Exception, asyncio.TimeoutError) as e:
                return (queue, e)

        results = await asyncio.gather(*[send_to_client(q) for q in list(self._clients)])

        # Remove failed/slow clients
        for queue, error in results:
            if error is not None:
                self._clients.discard(queue)
                # Optionally log: f"Removed slow client: {error}"
```

### Fix 2.2: Proper HTTPx Client Lifecycle

```python
# client.py
import atexit
from contextlib import asynccontextmanager

class StreamingClient:
    _instance = None
    _client: Optional[httpx.AsyncClient] = None
    _sync_client: Optional[httpx.Client] = None
    _shutdown = False

    def __init__(self, base_url: str):
        if not hasattr(self, '_initialized'):
            self.base_url = base_url.rstrip('/')
            self._initialized = True
            # Register cleanup on exit
            atexit.register(self._cleanup_sync)

    def _cleanup_sync(self):
        """Synchronous cleanup for atexit"""
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    async def cleanup(self):
        """Async cleanup for proper shutdown"""
        self._shutdown = True
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._sync_client is not None:
            self._sync_client.close()
            self._sync_client = None

    @property
    def async_client(self) -> httpx.AsyncClient:
        if self._shutdown:
            raise RuntimeError("Client has been shut down")
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=5.0),
                follow_redirects=True,
                limits=httpx.Limits(
                    max_connections=100,
                    max_keepalive_connections=20
                )
            )
        return self._client


# Django signal handler for cleanup
from django.core.signals import request_finished
from django.dispatch import receiver

@receiver(request_finished)
async def cleanup_httpx_client(sender, **kwargs):
    """Cleanup on Django shutdown"""
    global _client_instance
    if _client_instance:
        await _client_instance.cleanup()
```

### Fix 2.3: Connection Limiting

```python
# server.py
import asyncio

# Global connection tracking
_active_connections = 0
_max_connections = 1000  # Configurable
_connection_semaphore = asyncio.Semaphore(1000)

async def stream_handler(request):
    global _active_connections

    # Check connection limit
    if _active_connections >= _max_connections:
        return 503, {'Retry-After': '60'}, "Too many connections"

    async with _connection_semaphore:
        _active_connections += 1
        try:
            # ... existing code ...

            # Use bounded queue
            client_queue = asyncio.Queue(maxsize=100)

            # ... rest of implementation ...
        finally:
            _active_connections -= 1
```

### Fix 2.4: Queue Monitoring

```python
# models.py
async def send_event(self, event_type: str, data: dict):
    # ... existing code ...

    # Monitor queue sizes
    for queue in self._clients:
        queue_size = queue.qsize()
        if queue_size > 50:  # Warning threshold
            print(f"Warning: Client queue size: {queue_size}")
        if queue_size >= queue._maxsize - 10:  # Critical threshold
            print(f"Critical: Client queue nearly full: {queue_size}/{queue._maxsize}")
```

## Recommended Solution

Implement all fixes in phases:

**Phase 1** (Immediate):
- Add bounded queues (Fix 2.1)
- Add atexit handler for httpx cleanup (Fix 2.2)

**Phase 2** (Short-term):
- Add connection limits (Fix 2.3)
- Add queue monitoring (Fix 2.4)
- Add configuration for limits

**Phase 3** (Long-term):
- Consider alternative architectures (Redis Streams, Kafka)
- Implement proper backpressure with flow control
- Add metrics and alerting

## Testing

```python
async def test_bounded_queues(self):
    """Test that queues don't grow unbounded"""
    task = await ExampleTask.objects.acreate(message="Bounded queue test")

    # Create queue with small limit
    queue = asyncio.Queue(maxsize=10)
    await task.add_client(queue)

    # Send 100 events
    for i in range(100):
        await task.send_event('test', {'i': i})

    # Queue should never exceed maxsize
    self.assertLessEqual(queue.qsize(), 10)

async def test_httpx_cleanup(self):
    """Test that httpx clients are properly closed"""
    client = get_client()
    async_client = client.async_client

    # Close
    await client.cleanup()

    # Should not be able to use after close
    with self.assertRaises(RuntimeError):
        _ = client.async_client
```

## Related Issues

- #1 Lost State on Task Instance Reload (compounds memory issues)
- #3 Single-Process Architecture (connection limits per process)
- #8 Missing Observability (need to monitor queue sizes)
