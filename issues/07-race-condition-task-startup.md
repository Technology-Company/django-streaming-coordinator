# Issue #7: Race Condition in Task Startup

## Severity
**MEDIUM** ⚠️

## Affected Components
- `streaming/coordinator.py:43-51`

## Description

The `start_task()` method has a race condition between checking if a task is running and actually starting it. Multiple concurrent calls could both pass the check and start duplicate task instances.

```python
# coordinator.py:43-51
if task_key in self._tasks and not self._tasks[task_key].done():
    return  # ❌ Not atomic! Race window here!

# Another call could check here ↑ before we set _tasks below ↓

async_task = asyncio.create_task(self._run_task(task_instance, task_key))
self._tasks[task_key] = async_task
```

## How to Reproduce

```python
# test_race_condition.py
import asyncio
from tests.models import ExampleTask
from streaming.coordinator import coordinator

async def test_concurrent_start_race():
    """Demonstrate race condition in concurrent task starts"""

    task = await ExampleTask.objects.acreate(message="Race test")

    # Create a slow-starting task to widen the race window
    original_process = task.process

    async def slow_start():
        await asyncio.sleep(0.1)  # Delay to widen race window
        return await original_process()

    task.process = slow_start

    # Try to start the same task 10 times concurrently
    results = await asyncio.gather(*[
        coordinator.start_task(task, 'tests', 'ExampleTask')
        for _ in range(10)
    ])

    # Check how many tasks are actually running
    task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)

    # Wait a bit for tasks to start
    await asyncio.sleep(0.05)

    # Count running tasks
    running_count = sum(1 for r in results if r is not None)

    print(f"Tasks started: {running_count}")
    print(f"Expected: 1")
    print(f"Task in _tasks dict: {task_key in coordinator._tasks}")

    if running_count > 1:
        print("❌ RACE CONDITION: Multiple tasks started!")

    # Clean up
    await asyncio.sleep(0.2)

if __name__ == "__main__":
    import django, os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_concurrent_start_race())
```

### Expected Output (Buggy Behavior)

```
Tasks started: 2  # or more!
Expected: 1
Task in _tasks dict: True
❌ RACE CONDITION: Multiple tasks started!
```

## Root Cause Analysis

The check-then-set pattern is not atomic:

```python
# Thread/Coroutine 1                    # Thread/Coroutine 2
# Time 0: Check
if task_key in self._tasks:
    # Key not in dict, passes check
                                        # Time 1: Check (before T1 sets)
                                        if task_key in self._tasks:
                                            # Also passes! Race!

# Time 2: Create task
async_task = asyncio.create_task(...)

# Time 3: Set in dict
self._tasks[task_key] = async_task
                                        # Time 4: Create task (duplicate!)
                                        async_task = asyncio.create_task(...)

                                        # Time 5: Set in dict (overwrites!)
                                        self._tasks[task_key] = async_task
```

Now there are **two tasks running**, but only one is tracked in `_tasks`.

## Impact

1. **Duplicate Task Execution** - Same task runs multiple times
2. **Resource Waste** - CPU, memory, and I/O wasted on duplicates
3. **Data Corruption** - Multiple instances updating the same DB record
4. **Billing Issues** - If tasks are billable, customer charged multiple times
5. **Orphaned Tasks** - One task is in `_tasks`, others are invisible

## Proposed Fix

### Option 1: Use asyncio.Lock (Recommended)

```python
# coordinator.py
class TaskCoordinator:
    def __init__(self):
        if self._initialized:
            return

        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_instances: Dict[str, 'StreamTask'] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._start_locks: Dict[str, asyncio.Lock] = {}  # ✓ New: locks for starting
        self._initialized = True

    async def start_task(self, task_instance: 'StreamTask', app_name: str, model_name: str) -> None:
        """Start a task if not already running (thread-safe)"""
        task_key = self.get_task_key(app_name, model_name, task_instance.pk)

        # ✓ Ensure lock exists for this task
        if task_key not in self._start_locks:
            self._start_locks[task_key] = asyncio.Lock()

        # ✓ Acquire lock before checking
        async with self._start_locks[task_key]:
            # Now this check-then-set is atomic
            if task_key in self._tasks and not self._tasks[task_key].done():
                return

            # Cache the task instance
            self._task_instances[task_key] = task_instance

            # Start the task
            async_task = asyncio.create_task(self._run_task(task_instance, task_key))
            self._tasks[task_key] = async_task
```

### Option 2: Try-Except with Lock Object as Sentinel

```python
# coordinator.py
async def start_task(self, task_instance: 'StreamTask', app_name: str, model_name: str) -> bool:
    """Start a task, return True if started, False if already running"""
    task_key = self.get_task_key(app_name, model_name, task_instance.pk)

    # Check if running
    if task_key in self._tasks and not self._tasks[task_key].done():
        return False

    # Try to set a "claiming" sentinel
    sentinel = object()
    existing = self._tasks.get(task_key)

    if existing is not None and not existing.done():
        # Someone else is starting/running it
        return False

    # Claim the slot with sentinel
    self._tasks[task_key] = sentinel

    try:
        # Now we own this slot, create the actual task
        self._task_instances[task_key] = task_instance

        async_task = asyncio.create_task(self._run_task(task_instance, task_key))
        self._tasks[task_key] = async_task

        return True
    except Exception:
        # If anything fails, remove our claim
        if self._tasks.get(task_key) is sentinel:
            del self._tasks[task_key]
        raise
```

### Option 3: Use setdefault() Pattern (Not Recommended - Doesn't Work)

```python
# ❌ This doesn't work because dict.setdefault is not await-able
# and we need to create a Task, which requires await

# This would work for synchronous code:
task = self._tasks.setdefault(
    task_key,
    asyncio.create_task(...)  # But this always executes!
)
```

### Option 4: Database-Level Locking

For multi-process scenarios (see Issue #3):

```python
# coordinator.py
async def start_task(self, task_instance: 'StreamTask', app_name: str, model_name: str) -> bool:
    """Start task with database-level locking"""
    from django.db import transaction

    task_key = self.get_task_key(app_name, model_name, task_instance.pk)

    # Use database row lock
    async with transaction.atomic():
        # Lock the row
        locked_task = await (
            task_instance.__class__.objects
            .select_for_update(nowait=True)
            .aget(pk=task_instance.pk)
        )

        # Check if already running (in this process or another)
        if task_key in self._tasks and not self._tasks[task_key].done():
            return False

        # Check database status field (requires Issue #4 fix)
        if locked_task.status == 'running':
            return False

        # Mark as running in DB
        locked_task.status = 'running'
        locked_task.started_at = timezone.now()
        await locked_task.asave(update_fields=['status', 'started_at'])

        # Now start the task
        self._task_instances[task_key] = locked_task
        async_task = asyncio.create_task(self._run_task(locked_task, task_key))
        self._tasks[task_key] = async_task

        return True
```

## Recommended Solution

**Option 1 (asyncio.Lock)** for single-process deployments.

**Option 4 (Database Lock)** for multi-process deployments (addresses Issue #3 too).

## Complete Fixed Implementation

```python
# coordinator.py (fixed)
class TaskCoordinator:
    def __init__(self):
        if self._initialized:
            return

        self._tasks: Dict[str, asyncio.Task] = {}
        self._task_instances: Dict[str, 'StreamTask'] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._start_locks: Dict[str, asyncio.Lock] = {}  # ✓ New
        self._initialized = True

    async def start_task(
        self,
        task_instance: 'StreamTask',
        app_name: str,
        model_name: str
    ) -> bool:
        """
        Start a task if not already running (race-condition safe).

        Args:
            task_instance: The StreamTask instance to run
            app_name: The name of the app (for lookups)
            model_name: The name of the model (for lookups)

        Returns:
            True if task was started, False if already running
        """
        task_key = self.get_task_key(app_name, model_name, task_instance.pk)

        # Ensure lock exists for this task (this itself needs to be safe)
        # But dict access in Python is thread-safe for simple operations
        if task_key not in self._start_locks:
            self._start_locks[task_key] = asyncio.Lock()

        # Acquire lock for atomic check-and-start
        async with self._start_locks[task_key]:
            # Check if already running
            if task_key in self._tasks:
                existing_task = self._tasks[task_key]
                if not existing_task.done():
                    # Task is already running
                    return False
                else:
                    # Task is done, remove it
                    del self._tasks[task_key]

            # Cache the task instance
            self._task_instances[task_key] = task_instance

            # Start the task
            async_task = asyncio.create_task(
                self._run_task(task_instance, task_key)
            )
            self._tasks[task_key] = async_task

            return True

    async def _run_task(self, task_instance: 'StreamTask', task_key: str):
        """Run a task and handle cleanup"""
        try:
            # Mark as started (requires Issue #4 fix)
            if hasattr(task_instance, 'mark_started'):
                await task_instance.mark_started()

            # Run the task
            final_value = await task_instance.process()

            # Mark as completed
            await task_instance.mark_completed(final_value=final_value)

        except Exception as e:
            # Send error event to clients
            await task_instance.send_event('error', {
                'message': str(e),
                'error_type': type(e).__name__
            })

            # Mark as failed (requires Issue #4 fix)
            if hasattr(task_instance, 'mark_failed'):
                await task_instance.mark_failed(e)

        finally:
            # Cleanup
            async with self._start_locks.get(task_key, asyncio.Lock()):
                if task_key in self._tasks:
                    del self._tasks[task_key]
                if task_key in self._task_instances:
                    del self._task_instances[task_key]

            # Clean up the lock itself after some delay
            # (keep it around briefly in case of immediate restarts)
            if task_key in self._start_locks:
                await asyncio.sleep(60)  # Keep for 1 minute
                if task_key in self._start_locks:
                    # Only delete if task isn't running again
                    if task_key not in self._tasks:
                        del self._start_locks[task_key]

            # Also clean up from main locks dict
            if task_key in self._locks:
                del self._locks[task_key]
```

## Testing

```python
async def test_concurrent_start_no_race(self):
    """Test that concurrent starts don't create duplicate tasks"""
    task = await ExampleTask.objects.acreate(message="Concurrent test")

    # Try to start the same task 100 times concurrently
    results = await asyncio.gather(*[
        coordinator.start_task(task, 'tests', 'ExampleTask')
        for _ in range(100)
    ])

    # Exactly one should return True (started)
    started_count = sum(1 for r in results if r is True)
    self.assertEqual(started_count, 1, "Exactly one task should start")

    # Others should return False (already running)
    already_running_count = sum(1 for r in results if r is False)
    self.assertEqual(already_running_count, 99)

async def test_sequential_restarts_work(self):
    """Test that a task can be restarted after completion"""
    task = await ExampleTask.objects.acreate(message="Restart test")

    # Start task
    result1 = await coordinator.start_task(task, 'tests', 'ExampleTask')
    self.assertTrue(result1, "First start should succeed")

    # Wait for completion
    await asyncio.sleep(0.1)

    # Start again (should work now that first is done)
    result2 = await coordinator.start_task(task, 'tests', 'ExampleTask')
    self.assertTrue(result2, "Second start should succeed after completion")

async def test_lock_cleanup(self):
    """Test that locks are cleaned up after task completion"""
    task = await ExampleTask.objects.acreate(message="Lock cleanup test")

    initial_locks = len(coordinator._start_locks)

    # Start and complete task
    await coordinator.start_task(task, 'tests', 'ExampleTask')
    await asyncio.sleep(0.1)  # Wait for completion

    # Wait for lock cleanup (60 seconds in production, could be configurable)
    # For testing, we'd want a shorter timeout
    await asyncio.sleep(61)

    # Lock should be cleaned up
    final_locks = len(coordinator._start_locks)
    self.assertEqual(initial_locks, final_locks, "Locks should be cleaned up")
```

## Configuration

```python
# settings.py
STREAMING_LOCK_CLEANUP_DELAY = 60  # Seconds to keep locks after task completion
```

## Related Issues

- #1 Lost State on Task Instance Reload (both involve race conditions)
- #3 Single-Process Architecture (this race is worse in multi-process)
- #4 Error Handling Gaps (need status field to detect running tasks)
