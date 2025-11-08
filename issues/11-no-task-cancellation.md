# Issue #11: No Task Cancellation

## Severity
**MEDIUM** 🛑

## Affected Components
- `streaming/models.py` (no cancel method)
- `streaming/coordinator.py` (no cancellation support)

## Description

Once a task starts, there is **no way to stop it**:
- No `cancel()` method
- No timeout mechanism
- No graceful shutdown
- No way to stop runaway tasks
- Tasks stuck in infinite loops run forever

Users cannot:
- Cancel a task they started by mistake
- Stop a task that's taking too long
- Interrupt a task that's waiting for external input
- Abort a task that's in an error state but not raising exceptions

## How to Reproduce

### Scenario 1: Stuck Task

```python
# test_stuck_task.py
import asyncio
from tests.models import StreamTask
from streaming.coordinator import coordinator

class StuckTask(StreamTask):
    class Meta:
        app_label = 'tests'

    async def process(self):
        await self.send_event('start', {'message': 'Starting infinite loop'})

        # Infinite loop!
        while True:
            await asyncio.sleep(1)
            await self.send_event('still_running', {'message': 'Still going...'})

async def test_cannot_cancel():
    task = await StuckTask.objects.acreate()

    # Start the task
    await coordinator.start_task(task, 'tests', 'StuckTask')

    # Wait a bit
    await asyncio.sleep(2)

    # Try to stop it
    # ❌ No way to cancel!
    # task.cancel() doesn't exist
    # coordinator.cancel_task() doesn't exist
    # asyncio.Task.cancel() not accessible

    print("Task is stuck running forever")
    print("Only way to stop: kill the process")

if __name__ == "__main__":
    import django, os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_cannot_cancel())
```

### Scenario 2: Long-Running Task

```python
class SlowTask(StreamTask):
    async def process(self):
        # This will take 1 hour
        for i in range(3600):
            await asyncio.sleep(1)
            await self.send_event('progress', {'seconds': i})

# User starts the task
task = await SlowTask.objects.acreate()
await coordinator.start_task(task, 'tests', 'SlowTask')

# User realizes they made a mistake 5 seconds in
# ❌ No way to cancel! Must wait full hour or kill server
```

### Scenario 3: Waiting for External Input

```python
# From Issue #5 - ContinueTask
class ContinueTask(StreamTask):
    async def process(self):
        # Waits up to 100 iterations * 0.05s = 5 seconds
        for iteration in range(100):
            await self.arefresh_from_db()
            if self.continue_field:
                break
            await asyncio.sleep(0.05)

# User starts task but then changes their mind
# ❌ Task keeps polling for 5 seconds
# ❌ No way to interrupt it
```

## Impact

1. **Resource Waste** - Stuck tasks consume resources forever
2. **Poor UX** - Users can't cancel mistakes
3. **No Timeouts** - Tasks can run indefinitely
4. **Server Restart Required** - Only way to kill tasks
5. **DOS Vector** - Malicious user can create infinite tasks

## Proposed Fix

### Fix 11.1: Add Cancellation Methods

```python
# models.py
class StreamTask(models.Model):
    # ... existing fields ...

    # Add cancellation tracking
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.TextField(null=True, blank=True)

    # Add status field (from Issue #4)
    status = models.CharField(
        max_length=20,
        choices=[
            ('pending', 'Pending'),
            ('running', 'Running'),
            ('completed', 'Completed'),
            ('failed', 'Failed'),
            ('cancelled', 'Cancelled'),  # ✓ New status
        ],
        default='pending'
    )

    class Meta:
        abstract = True

    async def mark_cancelled(self, reason=None):
        """Mark task as cancelled"""
        self.status = 'cancelled'
        self.cancelled_at = timezone.now()
        self.cancellation_reason = reason or "Cancelled by user"
        await self.asave(update_fields=[
            'status', 'cancelled_at', 'cancellation_reason', 'updated_at'
        ])

    async def request_cancellation(self):
        """Request task cancellation (sets DB flag)"""
        await self.mark_cancelled()

        # Send cancellation event to task
        await self.send_event('cancellation_requested', {
            'message': 'Task cancellation requested'
        })

    def is_cancellation_requested(self):
        """Check if cancellation has been requested"""
        return self.status == 'cancelled'

    async def check_cancellation(self):
        """
        Check if task should be cancelled.
        Call this periodically in long-running tasks.
        """
        await self.arefresh_from_db(fields=['status'])
        if self.is_cancellation_requested():
            raise TaskCancelledException(self.cancellation_reason)


class TaskCancelledException(Exception):
    """Raised when a task is cancelled"""
    pass
```

### Fix 11.2: Update Coordinator to Support Cancellation

```python
# coordinator.py
class TaskCoordinator:
    # ... existing code ...

    async def cancel_task(
        self,
        app_name: str,
        model_name: str,
        task_id: int,
        reason: str = None
    ) -> bool:
        """
        Cancel a running task.

        Args:
            app_name: App name
            model_name: Model name
            task_id: Task ID
            reason: Cancellation reason

        Returns:
            True if task was cancelled, False if not running
        """
        task_key = self.get_task_key(app_name, model_name, task_id)

        # Get task instance
        task_instance = await self.get_task_instance(app_name, model_name, task_id)
        if not task_instance:
            return False

        # Mark as cancelled in DB
        await task_instance.mark_cancelled(reason)

        # Cancel the asyncio task
        if task_key in self._tasks:
            async_task = self._tasks[task_key]
            if not async_task.done():
                async_task.cancel()

                # Wait for cancellation to complete
                try:
                    await async_task
                except asyncio.CancelledError:
                    pass

                return True

        return False

    async def _run_task(self, task_instance: 'StreamTask', task_key: str):
        """Run a task with cancellation support"""
        try:
            # Mark as started
            if hasattr(task_instance, 'mark_started'):
                await task_instance.mark_started()

            # Run the task (can be cancelled)
            final_value = await task_instance.process()

            # Mark as completed
            await task_instance.mark_completed(final_value=final_value)

        except asyncio.CancelledError:
            # Task was cancelled via asyncio
            await task_instance.send_event('cancelled', {
                'message': 'Task was cancelled',
                'reason': task_instance.cancellation_reason
            })

            # Ensure DB is updated
            if task_instance.status != 'cancelled':
                await task_instance.mark_cancelled('Cancelled by system')

            # Re-raise so asyncio knows task was cancelled
            raise

        except TaskCancelledException as e:
            # Task cancelled itself after checking DB
            await task_instance.send_event('cancelled', {
                'message': 'Task cancelled',
                'reason': str(e)
            })

        except Exception as e:
            # Other errors
            await task_instance.send_event('error', {
                'message': str(e),
                'error_type': type(e).__name__
            })

            if hasattr(task_instance, 'mark_failed'):
                await task_instance.mark_failed(e)

        finally:
            # Cleanup
            if task_key in self._tasks:
                del self._tasks[task_key]
            if task_key in self._task_instances:
                del self._task_instances[task_key]
```

### Fix 11.3: Update Tasks to Support Cancellation

```python
# Example: Long-running task with cancellation checks
class LongTask(StreamTask):
    async def process(self):
        await self.send_event('start', {'total_steps': 1000})

        for i in range(1000):
            # ✓ Check for cancellation every iteration
            await self.check_cancellation()

            # Do work
            await asyncio.sleep(1)
            await self.send_event('progress', {'step': i})

        await self.send_event('complete', {})
        return "Completed all 1000 steps"


# Example: Update ContinueTask to support cancellation
class ContinueTask(StreamTask):
    async def process(self):
        await self.send_event('started', {
            'message': self.message,
            'continue': self.continue_field
        })

        max_iterations = 100
        for iteration in range(max_iterations):
            # ✓ Check for cancellation
            await self.check_cancellation()

            # Check for continue signal
            await self.arefresh_from_db()
            if self.continue_field:
                await self.send_event('final', {
                    'message': 'Continue signal received',
                    'continue': True
                })
                break

            await asyncio.sleep(0.05)

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

### Fix 11.4: Add Timeout Support

```python
# coordinator.py
async def start_task(
    self,
    task_instance: 'StreamTask',
    app_name: str,
    model_name: str,
    timeout: float = None
) -> bool:
    """
    Start a task with optional timeout.

    Args:
        task_instance: Task to run
        app_name: App name
        model_name: Model name
        timeout: Optional timeout in seconds

    Returns:
        True if started, False if already running
    """
    task_key = self.get_task_key(app_name, model_name, task_instance.pk)

    async with self._start_locks.setdefault(task_key, asyncio.Lock()):
        if task_key in self._tasks and not self._tasks[task_key].done():
            return False

        self._task_instances[task_key] = task_instance

        # Create task with optional timeout
        if timeout:
            async def run_with_timeout():
                try:
                    await asyncio.wait_for(
                        self._run_task(task_instance, task_key),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    await task_instance.send_event('timeout', {
                        'message': f'Task timed out after {timeout} seconds'
                    })
                    await task_instance.mark_cancelled(f'Timeout after {timeout}s')

            async_task = asyncio.create_task(run_with_timeout())
        else:
            async_task = asyncio.create_task(self._run_task(task_instance, task_key))

        self._tasks[task_key] = async_task
        return True


# models.py - add timeout field
class StreamTask(models.Model):
    # ... existing fields ...

    timeout_seconds = models.IntegerField(
        null=True,
        blank=True,
        help_text="Timeout in seconds (None = no timeout)"
    )
```

### Fix 11.5: Add Cancel Endpoint

```python
# server.py
async def cancel_handler(request):
    """Cancel a running task"""

    if not request.path.startswith('/cancel/'):
        return None

    # Parse path: /cancel/{app_name}/{model_name}/{task_id}
    path_parts = request.path.strip('/').split('/')
    if len(path_parts) != 4:
        return 400, {}, "Invalid path"

    app_name = path_parts[1]
    model_name = path_parts[2]
    try:
        task_id = int(path_parts[3])
    except ValueError:
        return 400, {}, "Invalid task ID"

    # Require authentication (see Issue #6)
    user = await authenticate_request(request)
    if not user:
        return 401, {}, "Unauthorized"

    # Get task and check authorization
    task_instance = await coordinator.get_task_instance(app_name, model_name, task_id)
    if not task_instance:
        return 404, {}, "Task not found"

    if not task_instance.can_access(user):
        return 403, {}, "Forbidden"

    # Get cancellation reason from body
    reason = None
    if request.method == 'POST':
        try:
            body = await request.json()
            reason = body.get('reason')
        except:
            pass

    # Cancel the task
    cancelled = await coordinator.cancel_task(
        app_name,
        model_name,
        task_id,
        reason or "Cancelled by user"
    )

    if cancelled:
        return 200, {'Content-Type': 'application/json'}, json.dumps({
            'status': 'cancelled',
            'task_id': task_id,
            'reason': reason
        })
    else:
        return 404, {'Content-Type': 'application/json'}, json.dumps({
            'status': 'not_running',
            'task_id': task_id
        })


# Update main handler
async def main_handler(request):
    # ... existing handlers ...

    # Add cancel handler
    if request.path.startswith('/cancel/'):
        return await cancel_handler(request)

    # ... rest of handlers ...
```

## Usage Examples

```python
# Example 1: Cancel via API
import httpx

async def cancel_task_via_api():
    async with httpx.AsyncClient() as client:
        response = await client.post(
            'http://127.0.0.1:8888/cancel/tests/LongTask/123',
            json={'reason': 'User requested cancellation'},
            headers={'Authorization': f'Bearer {token}'}
        )
        print(response.json())


# Example 2: Cancel via coordinator
from streaming.coordinator import coordinator

await coordinator.cancel_task(
    'tests',
    'LongTask',
    123,
    reason='Cancelled by admin'
)


# Example 3: Task with timeout
task = await LongTask.objects.acreate(timeout_seconds=300)  # 5 min timeout
await coordinator.start_task(
    task,
    'tests',
    'LongTask',
    timeout=task.timeout_seconds
)


# Example 4: Self-cancelling task
class SmartTask(StreamTask):
    async def process(self):
        for i in range(1000):
            # Check for cancellation every iteration
            await self.check_cancellation()

            # Do work
            result = await do_expensive_operation()

            if result.should_stop:
                # Task can cancel itself
                await self.request_cancellation()
                await self.check_cancellation()  # Raises TaskCancelledException
```

## Testing

```python
async def test_task_cancellation(self):
    """Test that tasks can be cancelled"""

    class CancellableTask(StreamTask):
        class Meta:
            app_label = 'tests'

        async def process(self):
            for i in range(100):
                await self.check_cancellation()
                await asyncio.sleep(0.1)
            return "Should not reach here"

    task = await CancellableTask.objects.acreate()

    # Start task
    await coordinator.start_task(task, 'tests', 'CancellableTask')

    # Wait a bit
    await asyncio.sleep(0.5)

    # Cancel
    cancelled = await coordinator.cancel_task(
        'tests',
        'CancellableTask',
        task.pk,
        'Test cancellation'
    )

    self.assertTrue(cancelled)

    # Check status
    await task.arefresh_from_db()
    self.assertEqual(task.status, 'cancelled')
    self.assertIsNotNone(task.cancelled_at)
    self.assertEqual(task.cancellation_reason, 'Test cancellation')

async def test_task_timeout(self):
    """Test that tasks can timeout"""

    class SlowTask(StreamTask):
        async def process(self):
            await asyncio.sleep(10)  # Too long!

    task = await SlowTask.objects.acreate(timeout_seconds=1)

    # Start with timeout
    await coordinator.start_task(task, 'tests', 'SlowTask', timeout=1.0)

    # Wait for timeout
    await asyncio.sleep(1.5)

    # Should be cancelled
    await task.arefresh_from_db()
    self.assertEqual(task.status, 'cancelled')
    self.assertIn('Timeout', task.cancellation_reason)
```

## Related Issues

- #4 Error Handling (need status field for cancellation)
- #5 Database Polling (cancellation provides alternative to infinite polling)
- #8 Missing Observability (should track cancellation rates)
