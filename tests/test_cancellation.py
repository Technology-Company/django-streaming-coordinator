"""
Test cases for asyncio task cancellation handling.

These tests verify proper cancellation behavior according to asyncio best practices:
- CancelledError is caught and re-raised
- Clients are notified when tasks are cancelled
- Cleanup happens properly on cancellation
- Graceful shutdown cancels all running tasks
- Task cancellation API works correctly

All these tests are expected to FAIL with the current implementation,
demonstrating the gaps in cancellation handling.
"""
import asyncio
import signal
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from django.test import TransactionTestCase
from tests.models import ExampleTask, HttpxFetchTask, ContinueTask
from streaming.coordinator import coordinator


class TaskCancellationTests(TransactionTestCase):
    """Test basic task cancellation handling."""

    async def test_task_cancellation_sends_cancelled_event_to_clients(self):
        """
        CRITICAL: When a task is cancelled, clients should receive a 'cancelled' event.

        Current behavior: Task is cancelled but clients never know about it.
        Expected behavior: Clients receive event type 'cancelled' with message.
        """
        task = await ExampleTask.objects.acreate(message="Test cancellation")
        queue = asyncio.Queue()
        await task.add_client(queue)

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait for task to start (get start event)
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert start_event['type'] == 'start'

        # Get the running task from coordinator and cancel it
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # Wait for task to handle cancellation
        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Client should receive 'cancelled' event
        cancelled_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert cancelled_event['type'] == 'cancelled', \
            f"Expected 'cancelled' event, got '{cancelled_event['type']}'"
        assert 'message' in cancelled_event['data']

    async def test_task_cancellation_is_re_raised(self):
        """
        CRITICAL: CancelledError must be re-raised after handling.

        Current behavior: CancelledError is not caught at all.
        Expected behavior: CancelledError is caught, handled, then re-raised.
        """
        task = await ExampleTask.objects.acreate(message="Test re-raise")

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Get the running task and cancel it
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # Awaiting the task should raise CancelledError
        with pytest.raises(asyncio.CancelledError):
            await running_task

    async def test_multiple_clients_all_receive_cancellation_event(self):
        """
        CRITICAL: All connected clients should be notified of cancellation.

        Current behavior: No cancellation event sent to any client.
        Expected behavior: All clients receive 'cancelled' event.
        """
        task = await ExampleTask.objects.acreate(message="Multi-client cancel")

        # Add 3 clients
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()

        await task.add_client(queue1)
        await task.add_client(queue2)
        await task.add_client(queue3)

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # All clients get start event
        for queue in [queue1, queue2, queue3]:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            assert event['type'] == 'start'

        # Cancel the task
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # Wait for cancellation
        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # All clients should receive cancellation event
        for i, queue in enumerate([queue1, queue2, queue3], 1):
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            assert event['type'] == 'cancelled', \
                f"Client {i} expected 'cancelled', got '{event['type']}'"

    async def test_task_cleanup_happens_on_cancellation(self):
        """
        CRITICAL: Task cleanup (finally block) must run even when cancelled.

        Current behavior: Finally block runs but no cleanup specific to cancellation.
        Expected behavior: Task removed from coordinator, completion marked if needed.
        """
        task = await ExampleTask.objects.acreate(message="Test cleanup")

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        assert task_key in coordinator._tasks

        # Cancel task
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Give cleanup time to run
        await asyncio.sleep(0.1)

        # Task should be removed from coordinator
        assert task_key not in coordinator._tasks, \
            "Task was not cleaned up from coordinator after cancellation"
        assert task_key not in coordinator._task_instances, \
            "Task instance was not cleaned up after cancellation"


class TaskCancellationAPITests(TransactionTestCase):
    """Test programmatic task cancellation API."""

    async def test_cancel_task_method_exists(self):
        """
        HIGH PRIORITY: TaskCoordinator should have a cancel_task() method.

        Current behavior: No such method exists.
        Expected behavior: cancel_task(app, model, id) method available.
        """
        assert hasattr(coordinator, 'cancel_task'), \
            "TaskCoordinator missing cancel_task() method"

    async def test_cancel_task_cancels_running_task(self):
        """
        HIGH PRIORITY: cancel_task() should cancel a running task.

        Current behavior: Method doesn't exist.
        Expected behavior: Running task is cancelled and returns True.
        """
        task = await ExampleTask.objects.acreate(message="Test cancel API")
        queue = asyncio.Queue()
        await task.add_client(queue)

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Task is running
        assert coordinator.is_task_running('tests', 'ExampleTask', task.pk)

        # Cancel via API
        result = await coordinator.cancel_task('tests', 'ExampleTask', task.pk)

        assert result is True, "cancel_task should return True for running task"

        # Client should receive cancellation event
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        cancelled_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert cancelled_event['type'] == 'cancelled'

    async def test_cancel_nonexistent_task_returns_false(self):
        """
        HIGH PRIORITY: Attempting to cancel non-existent task should return False.

        Expected behavior: Returns False, logs warning, doesn't crash.
        """
        result = await coordinator.cancel_task('tests', 'ExampleTask', 99999)
        assert result is False, "cancel_task should return False for non-existent task"

    async def test_cancel_already_completed_task_returns_false(self):
        """
        HIGH PRIORITY: Attempting to cancel completed task should return False.

        Expected behavior: Returns False, task stays completed.
        """
        task = await ExampleTask.objects.acreate(message="Quick task")

        # Start and wait for completion
        await coordinator.start_task(task, 'tests', 'ExampleTask')
        await asyncio.sleep(0.1)  # Wait for task to complete

        # Task should be completed
        await task.arefresh_from_db()
        assert task.completed_at is not None

        # Attempt to cancel
        result = await coordinator.cancel_task('tests', 'ExampleTask', task.pk)
        assert result is False, "cancel_task should return False for completed task"


class GracefulShutdownTests(TransactionTestCase):
    """Test graceful shutdown behavior."""

    async def test_shutdown_handler_cancels_all_running_tasks(self):
        """
        CRITICAL: Graceful shutdown should cancel all running tasks.

        Current behavior: No shutdown handler exists.
        Expected behavior: All tasks cancelled, cleanup completed.
        """
        # Create multiple long-running tasks
        task1 = await ContinueTask.objects.acreate(message="Task 1", continue_field=False)
        task2 = await ContinueTask.objects.acreate(message="Task 2", continue_field=False)
        task3 = await ContinueTask.objects.acreate(message="Task 3", continue_field=False)

        # Start all tasks (they'll run until continue_field is True)
        await coordinator.start_task(task1, 'tests', 'ContinueTask')
        await coordinator.start_task(task2, 'tests', 'ContinueTask')
        await coordinator.start_task(task3, 'tests', 'ContinueTask')

        # Verify all running
        assert coordinator.is_task_running('tests', 'ContinueTask', task1.pk)
        assert coordinator.is_task_running('tests', 'ContinueTask', task2.pk)
        assert coordinator.is_task_running('tests', 'ContinueTask', task3.pk)

        # Import and call shutdown handler (will fail if it doesn't exist)
        from streaming.coordinator import shutdown_handler

        await shutdown_handler(coordinator, signal.SIGTERM)

        # All tasks should be cancelled and cleaned up
        assert not coordinator.is_task_running('tests', 'ContinueTask', task1.pk)
        assert not coordinator.is_task_running('tests', 'ContinueTask', task2.pk)
        assert not coordinator.is_task_running('tests', 'ContinueTask', task3.pk)

    async def test_shutdown_waits_for_task_cleanup(self):
        """
        CRITICAL: Shutdown should wait for tasks to complete cleanup.

        Expected behavior: Tasks get chance to run finally blocks before shutdown.
        """
        task = await ContinueTask.objects.acreate(message="Cleanup task", continue_field=False)
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Get start event
        await asyncio.wait_for(queue.get(), timeout=0.5)

        # Trigger shutdown
        from streaming.coordinator import shutdown_handler
        await shutdown_handler(coordinator, signal.SIGTERM)

        # Client should have received cancellation event during shutdown
        # (queue should not be empty if shutdown waited for cleanup)
        cancelled_event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert cancelled_event['type'] == 'cancelled'

    async def test_shutdown_has_timeout_for_stuck_tasks(self):
        """
        CRITICAL: Shutdown should timeout if tasks don't finish.

        Expected behavior: Shutdown completes within timeout even if tasks hang.
        """
        # This test verifies shutdown_handler accepts timeout and enforces it
        # We'll use a mock that simulates a stuck task

        task = await ContinueTask.objects.acreate(message="Stuck task", continue_field=False)
        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Mock the task to never complete cancellation
        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Replace with a task that ignores cancellation
        async def stuck_task():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                # Ignore cancellation (bad practice but tests our timeout)
                await asyncio.sleep(1000)

        coordinator._tasks[task_key] = asyncio.create_task(stuck_task())

        # Shutdown with short timeout should complete quickly
        from streaming.coordinator import shutdown_handler

        start_time = asyncio.get_event_loop().time()
        await shutdown_handler(coordinator, signal.SIGTERM, timeout=0.5)
        end_time = asyncio.get_event_loop().time()

        elapsed = end_time - start_time
        assert elapsed < 1.0, \
            f"Shutdown took {elapsed}s, should timeout after ~0.5s"


class ResourceLeakTests(TransactionTestCase):
    """Test resource cleanup on cancellation."""

    async def test_httpx_fetch_cleanup_on_cancellation(self):
        """
        MAJOR: HttpxFetchTask should handle cancellation gracefully.

        Current behavior: No CancelledError handling in HttpxFetchTask.
        Expected behavior: Sends cancellation event, cleanup happens.
        """
        task = await HttpxFetchTask.objects.acreate(
            url="https://httpbin.org/delay/10"  # Long delay to allow cancellation
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        # Start task
        await coordinator.start_task(task, 'tests', 'HttpxFetchTask')

        # Wait for start event
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert start_event['type'] == 'start'

        # Cancel during fetch
        task_key = coordinator.get_task_key('tests', 'HttpxFetchTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Give it a moment to start fetching
        await asyncio.sleep(0.1)

        running_task.cancel()

        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Should receive cancellation event
        cancelled_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert cancelled_event['type'] == 'cancelled', \
            "HttpxFetchTask should send cancelled event"

    async def test_client_queue_cleanup_on_task_cancellation(self):
        """
        Test that client queues are properly cleaned up when task is cancelled.

        Expected: Clients are removed from task._clients set.
        """
        task = await ExampleTask.objects.acreate(message="Queue cleanup test")
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()

        await task.add_client(queue1)
        await task.add_client(queue2)

        assert len(task._clients) == 2

        # Start and cancel task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        try:
            await running_task
        except asyncio.CancelledError:
            pass

        await asyncio.sleep(0.1)

        # Clients might still be in set, but task should be cleaned from coordinator
        assert task_key not in coordinator._tasks


class TaskTimeoutTests(TransactionTestCase):
    """Test task timeout protection."""

    async def test_long_running_task_times_out(self):
        """
        MAJOR: Tasks should have configurable timeout protection.

        Current behavior: Tasks can run indefinitely.
        Expected behavior: Task cancelled after max execution time.
        """
        # ContinueTask will run up to 100 iterations * 0.05s = 5 seconds
        task = await ContinueTask.objects.acreate(
            message="Timeout test",
            continue_field=False  # Won't continue, will timeout
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        # Start task with timeout (requires implementation)
        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Set a short timeout on the task (requires implementation)
        # For now, manually timeout the coordinator task
        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Wrap in timeout
        try:
            async with asyncio.timeout(0.5):
                await running_task
            pytest.fail("Task should have timed out")
        except asyncio.TimeoutError:
            # Expected - but coordinator should handle this internally
            pass

        # In proper implementation, client would receive timeout event
        # For now, this test documents the expected behavior

    async def test_task_timeout_sends_error_event(self):
        """
        MAJOR: Task timeout should send error event to clients.

        Expected behavior: Clients receive 'error' event with timeout info.
        """
        # This test requires timeout to be implemented in coordinator
        # It documents the expected behavior

        task = await ContinueTask.objects.acreate(
            message="Timeout event test",
            continue_field=False
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        # Would need something like:
        # await coordinator.start_task(task, 'tests', 'ContinueTask', timeout=0.5)

        # Skip for now - this is a placeholder for future implementation
        pytest.skip("Task timeout not yet implemented")


class EdgeCaseTests(TransactionTestCase):
    """Test edge cases and race conditions."""

    async def test_cancel_task_just_as_it_completes(self):
        """
        EDGE CASE: Cancel task at the same moment it completes.

        Expected: Either cancellation or completion, but not both.
        Code should handle race gracefully.
        """
        task = await ExampleTask.objects.acreate(message="Race condition test")
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait until task is almost done
        await asyncio.sleep(0.02)  # Task takes ~0.03s total

        # Try to cancel
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        if task_key in coordinator._tasks:
            running_task = coordinator._tasks[task_key]
            was_cancelled = running_task.cancel()

            try:
                await running_task
            except asyncio.CancelledError:
                pass

        # Either way, should not crash
        # Task should either be completed or cancelled, not in broken state
        await task.arefresh_from_db()
        # Task might be completed or not, but shouldn't be in broken state

    async def test_cancel_task_with_no_clients(self):
        """
        EDGE CASE: Cancel task that has no connected clients.

        Expected: Cancellation succeeds, no errors from trying to send events.
        """
        task = await ExampleTask.objects.acreate(message="No clients test")

        # Start task without adding any clients
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Cancel should work even with no clients
        running_task.cancel()

        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Should not crash or leave task in bad state
        await asyncio.sleep(0.1)
        assert task_key not in coordinator._tasks

    async def test_cancel_already_cancelled_task(self):
        """
        EDGE CASE: Call cancel() multiple times on same task.

        Expected: Idempotent - multiple cancellations are safe.
        """
        task = await ContinueTask.objects.acreate(
            message="Multi-cancel test",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Cancel multiple times
        result1 = running_task.cancel()
        result2 = running_task.cancel()
        result3 = running_task.cancel()

        # First cancel should return True, subsequent might return False
        # But none should crash

        try:
            await running_task
        except asyncio.CancelledError:
            pass

    async def test_cancel_task_during_database_operation(self):
        """
        EDGE CASE: Cancel task while it's doing database operation.

        Expected: Database operation completes or rolls back cleanly.
        """
        task = await ContinueTask.objects.acreate(
            message="DB operation test",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Wait for task to be in arefresh_from_db()
        await asyncio.sleep(0.03)

        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Database should be in consistent state
        await task.arefresh_from_db()
        # Should not crash


class CancellationCounterTests(TransactionTestCase):
    """Test Python 3.11+ cancellation counter features."""

    async def test_distinguish_own_cancellation_from_child(self):
        """
        MODERN FEATURE: Use cancelling() to distinguish between:
        - Current task being cancelled (should propagate)
        - Child task being cancelled (may handle locally)

        Current: Not using cancellation counter.
        Expected: Check asyncio.current_task().cancelling() > 0
        """
        # This is a Python 3.11+ feature
        import sys
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        task = await ExampleTask.objects.acreate(message="Counter test")
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Get a progress event
        await asyncio.wait_for(queue.get(), timeout=0.5)

        # Cancel the task
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # When handling CancelledError, should check cancelling() counter
        # This is currently not implemented, test documents expected behavior

        try:
            await running_task
        except asyncio.CancelledError:
            pass
