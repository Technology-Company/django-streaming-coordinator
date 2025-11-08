# Issue #4: Error Handling Gaps

## Severity
**MEDIUM** ⚠️

## Affected Components
- `streaming/coordinator.py:59-64` (task error handling)
- `streaming/models.py` (no error state)
- `streaming/management/commands/cleanup_old_tasks.py:36-40` (wrong app)

## Description

The system has several error handling problems:

1. **Task errors are not persisted** - When a task fails, error is sent to clients but not saved to database
2. **Failed tasks remain incomplete forever** - No way to distinguish between running and failed tasks
3. **Cleanup command targets wrong app** - Only looks in 'streaming' app, but tasks are in 'tests' app
4. **No retry mechanism** - Transient failures cause permanent task failure
5. **Silent failures** - Exception details are lost

## How to Reproduce

### Sub-Issue 4.1: Task Errors Not Persisted

```python
# test_error_persistence.py
import asyncio
from tests.models import StreamTask
from django.db import models
from streaming.coordinator import coordinator

# Create a failing task
class FailingTask(StreamTask):
    class Meta:
        app_label = 'tests'

    async def process(self):
        await self.send_event('start', {'message': 'Starting...'})
        # Simulate an error
        raise ValueError("Something went wrong!")

async def test_error_not_persisted():
    # Create and start task
    task = await FailingTask.objects.acreate()
    print(f"Task created: {task.pk}")

    # Connect a client
    queue = asyncio.Queue()
    await task.add_client(queue)

    # Start task (will fail)
    await coordinator.start_task(task, 'tests', 'FailingTask')

    # Wait for events
    await asyncio.sleep(0.1)

    # Check what was received
    events = []
    while not queue.empty():
        events.append(await queue.get())

    print(f"\nEvents received: {len(events)}")
    for event in events:
        print(f"  {event['type']}: {event['data']}")

    # Check database
    await task.arefresh_from_db()
    print(f"\nDatabase state:")
    print(f"  completed_at: {task.completed_at}")  # ❌ Still None!
    print(f"  final_value: {task.final_value}")    # ❌ Still None!
    print(f"  Has error field? {hasattr(task, 'error_message')}")  # ❌ No!

    print("\n❌ Error was sent to client but NOT saved to database!")
    print("❌ Task remains in limbo - looks like it's still running")

if __name__ == "__main__":
    import django, os
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
    django.setup()
    asyncio.run(test_error_not_persisted())
```

### Expected Output

```
Task created: 1

Events received: 2
  start: {'message': 'Starting...'}
  error: {'message': 'Something went wrong!', 'error_type': 'ValueError'}

Database state:
  completed_at: None      # ❌ Should have timestamp!
  final_value: None       # ❌ Should have error info!
  Has error field? False  # ❌ Should exist!

❌ Error was sent to client but NOT saved to database!
❌ Task remains in limbo - looks like it's still running
```

### Sub-Issue 4.2: Cleanup Command Bug

```bash
# The cleanup command only checks the 'streaming' app
python manage.py cleanup_old_tasks

# Expected: "Found 10 old completed tasks"
# Actual: "Found 0 old completed tasks"  ❌

# Why? Because it only looks at models in 'streaming' app:
```

```python
# cleanup_old_tasks.py:36
streaming_app = apps.get_app_config('streaming')  # ❌ Wrong app!
task_models = [
    model for model in streaming_app.get_models()
    if hasattr(model, 'completed_at')
]
# This returns [] because all task models are in 'tests' app!
```

### Sub-Issue 4.3: Query for Failed Tasks Impossible

```python
# Try to find failed tasks
from tests.models import ExampleTask

# How do you find failed tasks?
failed_tasks = ExampleTask.objects.filter(???)  # ❌ No way to filter!

# completed_at is None for both running AND failed tasks
# No error_message field
# No status field
# No failed_at field
```

## Root Cause Analysis

### Cause 1: Error Handling Doesn't Update Database

```python
# coordinator.py:59-64
try:
    final_value = await task_instance.process()
    await task_instance.mark_completed(final_value=final_value)
except Exception as e:
    # Send error to clients ✓
    await task_instance.send_event('error', {
        'message': str(e),
        'error_type': type(e).__name__
    })
    # ❌ But don't save to database!
    # ❌ Don't call mark_completed or mark_failed
```

### Cause 2: No Error State in Model

```python
# models.py
class StreamTask(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    final_value = models.JSONField(null=True, blank=True)

    # ❌ Missing:
    # status = models.CharField(...)  # pending/running/completed/failed
    # error_message = models.TextField(...)
    # failed_at = models.DateTimeField(...)
    # retry_count = models.IntegerField(...)
```

### Cause 3: Wrong App in Cleanup Command

```python
# cleanup_old_tasks.py:36
streaming_app = apps.get_app_config('streaming')  # ❌ Hardcoded!
```

Should iterate all apps and find StreamTask subclasses.

## Impact

1. **Failed tasks invisible** - Can't query, monitor, or alert on failures
2. **Cleanup doesn't work** - Failed tasks accumulate in database forever
3. **No debugging** - Error details are lost after clients disconnect
4. **No retry logic** - Transient failures are permanent
5. **Monitoring impossible** - Can't track success/failure rates

## Proposed Fix

### Fix 4.1: Add Error State to Model

```python
# models.py
class StreamTask(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Status tracking
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('running', 'Running'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
    ]
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending',
        db_index=True
    )

    # Timestamps
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)

    # Results
    final_value = models.JSONField(null=True, blank=True)

    # Error tracking
    error_message = models.TextField(null=True, blank=True)
    error_type = models.CharField(max_length=255, null=True, blank=True)
    error_traceback = models.TextField(null=True, blank=True)

    # Retry tracking
    retry_count = models.IntegerField(default=0)
    max_retries = models.IntegerField(default=0)

    class Meta:
        abstract = True
        indexes = [
            models.Index(fields=['status', 'created_at']),
        ]

    async def mark_started(self):
        """Mark task as started"""
        self.status = 'running'
        self.started_at = timezone.now()
        await self.asave(update_fields=['status', 'started_at', 'updated_at'])

    async def mark_completed(self, final_value=None):
        """Mark task as successfully completed"""
        self.status = 'completed'
        self.completed_at = timezone.now()
        self.final_value = final_value
        await self.asave(update_fields=[
            'status', 'completed_at', 'final_value', 'updated_at'
        ])

    async def mark_failed(self, error: Exception):
        """Mark task as failed"""
        import traceback

        self.status = 'failed'
        self.failed_at = timezone.now()
        self.error_message = str(error)
        self.error_type = type(error).__name__
        self.error_traceback = ''.join(traceback.format_exception(
            type(error), error, error.__traceback__
        ))
        await self.asave(update_fields=[
            'status', 'failed_at', 'error_message', 'error_type',
            'error_traceback', 'updated_at'
        ])
```

### Fix 4.2: Update Coordinator Error Handling

```python
# coordinator.py
async def _run_task(self, task_instance: 'StreamTask', task_key: str):
    try:
        # Mark as started
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

        # Save error to database
        await task_instance.mark_failed(e)

        # Optionally: retry logic
        if task_instance.retry_count < task_instance.max_retries:
            await self._retry_task(task_instance, task_key)

    finally:
        # Cleanup
        if task_key in self._tasks:
            del self._tasks[task_key]
        if task_key in self._task_instances:
            del self._task_instances[task_key]
        if task_key in self._locks:
            del self._locks[task_key]

async def _retry_task(self, task_instance: 'StreamTask', task_key: str):
    """Retry a failed task with exponential backoff"""
    task_instance.retry_count += 1
    await task_instance.asave(update_fields=['retry_count'])

    # Exponential backoff: 2^retry_count seconds
    delay = 2 ** task_instance.retry_count

    await task_instance.send_event('retry', {
        'attempt': task_instance.retry_count,
        'max_retries': task_instance.max_retries,
        'delay_seconds': delay
    })

    await asyncio.sleep(delay)

    # Retry the task
    await self._run_task(task_instance, task_key)
```

### Fix 4.3: Fix Cleanup Command

```python
# cleanup_old_tasks.py
async def _cleanup_tasks(self, hours, dry_run):
    cutoff_time = timezone.now() - timedelta(hours=hours)

    self.stdout.write(f'Looking for tasks older than {cutoff_time.isoformat()}')

    # Get ALL StreamTask subclasses from ALL apps ✓
    from streaming.models import StreamTask
    task_models = []

    for app_config in apps.get_app_configs():
        for model in app_config.get_models():
            # Check if it's a StreamTask subclass
            if (issubclass(model, StreamTask) and
                model != StreamTask and
                not model._meta.abstract):
                task_models.append(model)

    total_deleted = 0

    for model in task_models:
        model_name = f"{model._meta.app_label}.{model.__name__}"

        # Find old completed OR failed tasks
        old_tasks = model.objects.filter(
            models.Q(
                status='completed',
                completed_at__isnull=False,
                completed_at__lt=cutoff_time
            ) | models.Q(
                status='failed',
                failed_at__isnull=False,
                failed_at__lt=cutoff_time
            )
        )

        count = await old_tasks.acount()

        if count > 0:
            self.stdout.write(f'\n{model_name}:')
            self.stdout.write(f'  Found {count} old task(s)')

            if dry_run:
                self.stdout.write(self.style.WARNING(
                    f'  [DRY RUN] Would delete {count} task(s)'
                ))
            else:
                deleted_count, _ = await old_tasks.adelete()
                total_deleted += deleted_count
                self.stdout.write(self.style.SUCCESS(
                    f'  Deleted {deleted_count} task(s)'
                ))

    self.stdout.write(self.style.SUCCESS(
        f'\nTotal: {total_deleted} task(s)'
    ))
```

### Fix 4.4: Add Management Command for Failed Tasks

```python
# streaming/management/commands/retry_failed_tasks.py
from django.core.management.base import BaseCommand
from django.apps import apps
from streaming.coordinator import coordinator
import asyncio

class Command(BaseCommand):
    help = 'Retry failed tasks'

    def add_arguments(self, parser):
        parser.add_argument(
            '--max-age-hours',
            type=int,
            default=24,
            help='Only retry tasks failed in last N hours'
        )

    def handle(self, *args, **options):
        asyncio.run(self._retry_failed(options['max_age_hours']))

    async def _retry_failed(self, max_age_hours):
        from streaming.models import StreamTask
        from django.utils import timezone
        from datetime import timedelta

        cutoff = timezone.now() - timedelta(hours=max_age_hours)

        # Find all failed tasks
        for app_config in apps.get_app_configs():
            for model in app_config.get_models():
                if issubclass(model, StreamTask) and model != StreamTask:
                    failed = model.objects.filter(
                        status='failed',
                        failed_at__gte=cutoff,
                        retry_count__lt=models.F('max_retries')
                    )

                    async for task in failed:
                        self.stdout.write(f"Retrying {model.__name__} {task.pk}")
                        await coordinator.start_task(
                            task,
                            model._meta.app_label,
                            model.__name__
                        )
```

## Testing

```python
async def test_error_persisted(self):
    """Test that errors are saved to database"""
    class FailTask(StreamTask):
        class Meta:
            app_label = 'tests'

        async def process(self):
            raise ValueError("Test error")

    task = await FailTask.objects.acreate()
    await coordinator.start_task(task, 'tests', 'FailTask')
    await asyncio.sleep(0.1)

    await task.arefresh_from_db()
    self.assertEqual(task.status, 'failed')
    self.assertIsNotNone(task.failed_at)
    self.assertEqual(task.error_type, 'ValueError')
    self.assertEqual(task.error_message, 'Test error')
    self.assertIn('Traceback', task.error_traceback)

async def test_cleanup_finds_all_tasks(self):
    """Test that cleanup command finds tasks in all apps"""
    # Create tasks
    task1 = await ExampleTask.objects.acreate(message="test")
    await task1.mark_completed()

    # Run cleanup (should find it)
    call_command('cleanup_old_tasks', hours=0, dry_run=True)
    # Verify output shows the task
```

## Related Issues

- #8 Missing Observability (need to track error rates)
- #10 Testing Gaps (missing error scenario tests)
- #11 No Task Cancellation (related to task state management)
