# Issue #10: Testing Gaps

## Severity
**LOW** 🧪

## Affected Components
- `tests/tests.py` (missing scenarios)

## Description

While the test suite covers basic functionality, it's missing tests for critical failure scenarios and edge cases:

**Missing test coverage:**
- Task crashes mid-execution
- Network failures during SSE streaming
- Client reconnection scenarios
- Database connection failures
- Concurrent task updates
- Task timeouts
- Queue overflow scenarios
- Process crashes and recovery
- Memory leak detection
- Race conditions under load

## Examples of Missing Tests

### 1. Task Crash Mid-Execution

```python
# NOT TESTED
async def test_task_crash_recovery(self):
    """Test what happens when a task crashes midway"""

    class CrashingTask(StreamTask):
        class Meta:
            app_label = 'tests'

        async def process(self):
            await self.send_event('start', {'message': 'Starting'})
            await self.send_event('progress', {'step': 1})

            # Crash!
            raise MemoryError("Simulated OOM")

    task = await CrashingTask.objects.acreate()

    queue = asyncio.Queue()
    await task.add_client(queue)

    await coordinator.start_task(task, 'tests', 'CrashingTask')
    await asyncio.sleep(0.1)

    # What state is the task in?
    # - Is error sent to client? ✓ (tested)
    # - Is task marked as failed in DB? ❌ (not tested - Issue #4)
    # - Are all clients cleaned up? ❌ (not tested)
    # - Is coordinator cleaned up? ❌ (not tested)
    # - Can task be restarted? ❌ (not tested)
```

### 2. Network Failures

```python
# NOT TESTED
async def test_sse_connection_drops(self):
    """Test SSE connection dropping unexpectedly"""

    # Simulate network partition
    # - Client connects
    # - Network drops mid-stream
    # - Does server detect it?
    # - Is client removed from task._clients?
    # - Does task continue for other clients?
```

### 3. Database Unavailable

```python
# NOT TESTED
async def test_database_connection_lost(self):
    """Test task behavior when database connection is lost"""

    class DBTask(StreamTask):
        async def process(self):
            # Try to query database
            await self.arefresh_from_db()
            # What if database is down?
            # - Does task crash?
            # - Is error handled gracefully?
            # - Can task continue without DB?
```

### 4. Concurrent Task Updates

```python
# NOT TESTED
async def test_concurrent_task_field_updates(self):
    """Test race conditions when updating task fields"""

    task = await ExampleTask.objects.acreate()

    # Two coroutines try to update task simultaneously
    async def update_1():
        task.message = "Update 1"
        await task.asave()

    async def update_2():
        task.message = "Update 2"
        await task.asave()

    await asyncio.gather(update_1(), update_2())

    # Which update won? Is data lost?
```

### 5. Task Timeout

```python
# NOT TESTED
async def test_task_timeout(self):
    """Test that long-running tasks can be timed out"""

    class SlowTask(StreamTask):
        async def process(self):
            await asyncio.sleep(3600)  # 1 hour!

    # There's no timeout mechanism!
    # Task runs forever
```

### 6. Memory Leaks

```python
# NOT TESTED
async def test_no_memory_leaks_after_many_tasks(self):
    """Test that running many tasks doesn't leak memory"""
    import psutil
    import gc

    process = psutil.Process()
    initial_memory = process.memory_info().rss

    # Run 1000 tasks
    for i in range(1000):
        task = await ExampleTask.objects.acreate()
        await coordinator.start_task(task, 'tests', 'ExampleTask')
        await asyncio.sleep(0.01)

    # Force garbage collection
    gc.collect()
    await asyncio.sleep(1)

    final_memory = process.memory_info().rss
    memory_increase = (final_memory - initial_memory) / 1024 / 1024  # MB

    # Should not have increased significantly
    self.assertLess(memory_increase, 50, f"Memory increased by {memory_increase}MB")
```

### 7. SSE Event Ordering

```python
# NOT TESTED
async def test_sse_event_ordering_under_load(self):
    """Test that events maintain order under high load"""

    class FastTask(StreamTask):
        async def process(self):
            for i in range(100):
                await self.send_event('progress', {'step': i})

    task = await FastTask.objects.acreate()

    queue = asyncio.Queue()
    await task.add_client(queue)

    await coordinator.start_task(task, 'tests', 'FastTask')

    # Collect events
    events = []
    for _ in range(100):
        event = await queue.get()
        events.append(event['data']['step'])

    # Verify ordering
    self.assertEqual(events, list(range(100)), "Events out of order!")
```

### 8. Multi-Worker Scenarios

```python
# NOT TESTED (requires Issue #3 fix)
async def test_multi_worker_coordination(self):
    """Test that multiple workers don't run the same task"""

    # Requires Redis-based coordinator
    # Start task from worker 1
    # Try to start same task from worker 2
    # Verify only one runs
```

### 9. Cleanup After Process Crash

```python
# NOT TESTED
async def test_orphaned_task_cleanup(self):
    """Test cleanup of tasks when process crashes"""

    # Scenario:
    # 1. Task is running
    # 2. Process crashes (SIGKILL)
    # 3. New process starts
    # 4. How do we detect the orphaned task?
    # 5. How do we clean it up?

    # Currently: No mechanism for this!
```

### 10. Rate Limiting

```python
# NOT TESTED (requires Issue #6 fix)
async def test_rate_limiting_works(self):
    """Test that rate limiting prevents abuse"""

    # Make 100 requests in 1 second
    # Should be blocked after N requests
```

## Proposed Test Additions

```python
# tests/test_edge_cases.py
class EdgeCaseTests(TransactionTestCase):
    """Tests for edge cases and failure scenarios"""

    async def test_task_exception_handling(self):
        """Test that task exceptions are properly handled"""

        class FailTask(StreamTask):
            error_to_raise = None

            async def process(self):
                raise self.error_to_raise

        # Test different exception types
        for exc_class in [ValueError, KeyError, MemoryError, asyncio.TimeoutError]:
            task = await FailTask.objects.acreate()
            task.error_to_raise = exc_class("Test error")

            queue = asyncio.Queue()
            await task.add_client(queue)

            await coordinator.start_task(task, 'tests', 'FailTask')
            await asyncio.sleep(0.1)

            # Should receive error event
            events = []
            while not queue.empty():
                events.append(await queue.get())

            error_events = [e for e in events if e['type'] == 'error']
            self.assertEqual(len(error_events), 1)
            self.assertEqual(error_events[0]['data']['error_type'], exc_class.__name__)

    async def test_client_removal_on_error(self):
        """Test that clients are removed when their queue fails"""

        task = await ExampleTask.objects.acreate()

        # Create a broken queue that raises on put
        class BrokenQueue:
            async def put(self, item):
                raise Exception("Queue is broken")

        broken_queue = BrokenQueue()
        task._clients.add(broken_queue)

        # Send event
        await task.send_event('test', {'data': 'value'})

        # Broken queue should be removed
        self.assertNotIn(broken_queue, task._clients)

    async def test_task_instance_cleanup(self):
        """Test that task instances are cleaned up after completion"""

        task = await ExampleTask.objects.acreate()
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Should be in coordinator while running
        await asyncio.sleep(0.01)
        self.assertIn(task_key, coordinator._task_instances)

        # Should be removed after completion
        await asyncio.sleep(0.1)
        self.assertNotIn(task_key, coordinator._task_instances)

    async def test_database_error_during_task(self):
        """Test task behavior when database errors occur"""

        class DBTask(StreamTask):
            class Meta:
                app_label = 'tests'

            async def process(self):
                await self.send_event('start', {})

                # Try to refresh from DB
                try:
                    await self.arefresh_from_db()
                except Exception as e:
                    # Should be able to continue
                    await self.send_event('db_error', {'error': str(e)})

                await self.send_event('complete', {})
                return "Completed despite DB error"

        # TODO: Inject database error somehow
        # This is tricky to test

    async def test_concurrent_client_add_remove(self):
        """Test concurrent client connections/disconnections"""

        task = await ExampleTask.objects.acreate()

        # Add and remove clients concurrently
        async def add_remove_client(client_id):
            queue = asyncio.Queue()
            await task.add_client(queue)
            await asyncio.sleep(0.01)
            await task.remove_client(queue)

        # 100 clients connecting/disconnecting simultaneously
        await asyncio.gather(*[add_remove_client(i) for i in range(100)])

        # No clients should remain
        self.assertEqual(len(task._clients), 0)

    async def test_send_event_with_no_clients(self):
        """Test that sending events with no clients doesn't error"""

        task = await ExampleTask.objects.acreate()

        # Should not raise
        await task.send_event('test', {'data': 'value'})

        # Latest data should still be set
        self.assertIsNotNone(task._latest_data)
        self.assertEqual(task._latest_data['type'], 'test')

    async def test_very_large_event_data(self):
        """Test sending very large event payloads"""

        task = await ExampleTask.objects.acreate()

        queue = asyncio.Queue()
        await task.add_client(queue)

        # Send 10MB event
        large_data = 'x' * (10 * 1024 * 1024)
        await task.send_event('large', {'data': large_data})

        event = await queue.get()
        self.assertEqual(len(event['data']['data']), 10 * 1024 * 1024)


# tests/test_performance.py
class PerformanceTests(TransactionTestCase):
    """Performance and load tests"""

    async def test_many_concurrent_tasks(self):
        """Test running many tasks concurrently"""

        tasks = []
        for i in range(100):
            task = await ExampleTask.objects.acreate(message=f"Task {i}")
            tasks.append(task)
            await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait for all to complete
        await asyncio.sleep(0.5)

        # All should be completed
        for task in tasks:
            await task.arefresh_from_db()
            self.assertIsNotNone(task.completed_at)

    async def test_many_clients_per_task(self):
        """Test many clients connecting to one task"""

        task = await ExampleTask.objects.acreate()

        # 1000 clients
        queues = [asyncio.Queue() for _ in range(1000)]

        for queue in queues:
            await task.add_client(queue)

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # All clients should receive events
        for queue in queues:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            self.assertEqual(event['type'], 'start')
```

## Test Coverage Goals

- **Current coverage**: ~60% (basic happy path)
- **Target coverage**: >90% (including failure scenarios)

Areas needing coverage:
- Error handling: +20%
- Edge cases: +15%
- Concurrency: +10%
- Performance: +5%

## Related Issues

- #1, #2, #3, #4, #5, #6, #7 (all need test coverage)
- #8 Missing Observability (testing would benefit from metrics)
