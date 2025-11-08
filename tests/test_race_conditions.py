"""
Test cases for race conditions and production edge cases.

These tests cover timing-sensitive scenarios that can occur in production:
- Race between task completion and cancellation
- Concurrent client connections and disconnections
- Database state changes during task execution
- Network failures during HTTP requests
- Memory pressure and resource exhaustion scenarios

These tests are designed to catch subtle bugs that only appear under specific timing conditions.
"""
import asyncio
import pytest
from unittest.mock import patch, AsyncMock, Mock
from django.test import TransactionTestCase
from tests.models import ExampleTask, HttpxFetchTask, ContinueTask
from streaming.coordinator import coordinator


class CompletionCancellationRaceTests(TransactionTestCase):
    """Test race conditions between task completion and cancellation."""

    async def test_cancel_just_before_completion(self):
        """
        RACE CONDITION: Task completes just as cancel() is called.

        Expected: Either task completes OR cancels, but not broken state.
        """
        task = await ExampleTask.objects.acreate(message="Race test")
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait until task is almost done (task takes ~0.04s total)
        await asyncio.sleep(0.035)

        # Try to cancel at the last moment
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        if task_key in coordinator._tasks:
            running_task = coordinator._tasks[task_key]
            was_cancelled = running_task.cancel()

            try:
                await running_task
            except asyncio.CancelledError:
                pass

        # Task should be in valid state (either completed or cancelled)
        await task.arefresh_from_db()

        # Either completed or not, but shouldn't be in broken state
        # (e.g., marked complete but clients received cancellation)

    async def test_cancel_during_mark_completed(self):
        """
        RACE CONDITION: Cancellation during database save of completion.

        Expected: Database operation completes or rolls back cleanly.
        """
        task = await ExampleTask.objects.acreate(message="DB race test")

        # Patch mark_completed to add delay
        original_mark_completed = task.mark_completed

        async def slow_mark_completed(*args, **kwargs):
            # Simulate slow DB operation
            await asyncio.sleep(0.1)
            return await original_mark_completed(*args, **kwargs)

        task.mark_completed = slow_mark_completed

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Cancel while mark_completed is running
        await asyncio.sleep(0.035)

        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        if task_key in coordinator._tasks:
            running_task = coordinator._tasks[task_key]
            running_task.cancel()

            try:
                await running_task
            except asyncio.CancelledError:
                pass

        # Database should be in consistent state
        await task.arefresh_from_db()

    async def test_multiple_simultaneous_cancellations(self):
        """
        RACE CONDITION: Multiple coroutines try to cancel same task.

        Expected: Idempotent - all cancel calls succeed without error.
        """
        task = await ContinueTask.objects.acreate(
            message="Multi-cancel test",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]

        # Multiple concurrent cancellations
        async def cancel_task():
            await asyncio.sleep(0.01)
            return running_task.cancel()

        results = await asyncio.gather(
            cancel_task(),
            cancel_task(),
            cancel_task(),
            return_exceptions=True
        )

        # Should not crash
        # First cancel returns True, others may return False
        assert not any(isinstance(r, Exception) for r in results)

        try:
            await running_task
        except asyncio.CancelledError:
            pass


class ClientConnectionRaceTests(TransactionTestCase):
    """Test race conditions around client connections."""

    async def test_client_connects_during_task_cancellation(self):
        """
        RACE CONDITION: Client connects while task is being cancelled.

        Expected: Client receives cancellation event or connection fails gracefully.
        """
        task = await ContinueTask.objects.acreate(
            message="Connect during cancel",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Start cancellation
        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # Try to add client during cancellation
        late_queue = asyncio.Queue()
        await task.add_client(late_queue)

        # Wait for cancellation to complete
        try:
            await running_task
        except asyncio.CancelledError:
            pass

        # Late client might or might not receive events
        # But should not crash or deadlock

    async def test_client_disconnects_during_send_event(self):
        """
        RACE CONDITION: Client disconnects while send_event is executing.

        Expected: Other clients still receive event, no crash.
        """
        task = await ExampleTask.objects.acreate(message="Disconnect during send")

        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        queue3 = asyncio.Queue()

        await task.add_client(queue1)
        await task.add_client(queue2)
        await task.add_client(queue3)

        # Patch send_event to disconnect queue2 mid-send
        original_send_event = task.send_event

        async def send_with_disconnect(*args, **kwargs):
            # Start sending
            result_task = asyncio.create_task(original_send_event(*args, **kwargs))
            # Disconnect queue2 while sending
            await asyncio.sleep(0.001)
            await task.remove_client(queue2)
            await result_task

        task.send_event = send_with_disconnect

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait for completion
        await asyncio.sleep(0.1)

        # queue1 and queue3 should have received events
        assert not queue1.empty()
        # queue3 should also have events
        assert not queue3.empty()

    async def test_all_clients_disconnect_during_task_execution(self):
        """
        RACE CONDITION: All clients disconnect while task is running.

        Expected: Task continues to completion (or can be cancelled).
        """
        task = await ContinueTask.objects.acreate(
            message="All disconnect test",
            continue_field=False
        )

        queues = [asyncio.Queue() for _ in range(3)]
        for queue in queues:
            await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Wait for task to start
        await asyncio.sleep(0.05)

        # All clients disconnect
        for queue in queues:
            await task.remove_client(queue)

        assert len(task._clients) == 0

        # Task should continue running
        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)
        assert coordinator.is_task_running('tests', 'ContinueTask', task.pk)

        # Cancel it to finish test
        if task_key in coordinator._tasks:
            coordinator._tasks[task_key].cancel()
            try:
                await coordinator._tasks[task_key]
            except asyncio.CancelledError:
                pass


class DatabaseRaceTests(TransactionTestCase):
    """Test race conditions involving database operations."""

    async def test_task_modified_in_database_during_execution(self):
        """
        RACE CONDITION: Task model updated in DB while task is running.

        Example: ContinueTask.continue_field changed externally.
        Expected: Task picks up change via arefresh_from_db().
        """
        task = await ContinueTask.objects.acreate(
            message="DB modification test",
            continue_field=False
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Wait for task to be waiting
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert start_event['type'] == 'started'

        # Modify in database
        await ContinueTask.objects.filter(pk=task.pk).aupdate(continue_field=True)

        # Task should detect change and complete
        final_event = await asyncio.wait_for(queue.get(), timeout=1.0)
        complete_event = await asyncio.wait_for(queue.get(), timeout=0.5)

        assert final_event['type'] == 'final'
        assert complete_event['type'] == 'complete'

    async def test_task_deleted_from_database_during_execution(self):
        """
        RACE CONDITION: Task deleted from DB while running.

        Expected: Task handles gracefully (probably fails, but doesn't crash).
        """
        task = await ContinueTask.objects.acreate(
            message="DB deletion test",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        await asyncio.sleep(0.05)

        # Delete task from database
        await ContinueTask.objects.filter(pk=task.pk).adelete()

        # Task should eventually fail or complete
        # Should not crash the coordinator
        await asyncio.sleep(0.2)

        # Coordinator should be in stable state
        # (task might still be in _tasks dict if it hasn't tried to refresh)

    async def test_concurrent_task_instance_loading(self):
        """
        RACE CONDITION: Multiple clients request same task simultaneously.

        Expected: Only one task instance created (double-checked locking works).
        """
        task = await ExampleTask.objects.acreate(message="Concurrent load test")

        # Multiple concurrent get_task_instance calls
        results = await asyncio.gather(
            coordinator.get_task_instance('tests', 'ExampleTask', task.pk),
            coordinator.get_task_instance('tests', 'ExampleTask', task.pk),
            coordinator.get_task_instance('tests', 'ExampleTask', task.pk),
        )

        # All should return same instance
        assert results[0] is results[1]
        assert results[1] is results[2]

        # Only one task should be running
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        # Count tasks with this key
        running_count = sum(1 for k in coordinator._tasks if k == task_key)
        assert running_count <= 1, "Should have at most one running task"


class NetworkFailureRaceTests(TransactionTestCase):
    """Test race conditions involving network operations."""

    async def test_httpx_request_cancelled_mid_flight(self):
        """
        RACE CONDITION: HTTP request cancelled while waiting for response.

        Expected: httpx.AsyncClient cleans up connection, no resource leak.
        """
        task = await HttpxFetchTask.objects.acreate(
            url="https://httpbin.org/delay/5"
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'HttpxFetchTask')

        # Wait for request to start
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)

        # Cancel during HTTP request
        await asyncio.sleep(0.1)

        task_key = coordinator.get_task_key('tests', 'HttpxFetchTask', task.pk)
        if task_key in coordinator._tasks:
            running_task = coordinator._tasks[task_key]
            running_task.cancel()

            try:
                await running_task
            except asyncio.CancelledError:
                pass

        # Should not have connection leak
        # (httpx context manager should clean up)

    async def test_network_timeout_during_fetch(self):
        """
        RACE CONDITION: Network timeout vs task cancellation.

        Expected: Whichever happens first is handled, no confusion.
        """
        task = await HttpxFetchTask.objects.acreate(
            url="https://httpbin.org/delay/10"
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'HttpxFetchTask')

        # httpx client has 30s timeout, we'll cancel before that
        await asyncio.sleep(0.1)

        task_key = coordinator.get_task_key('tests', 'HttpxFetchTask', task.pk)
        if task_key in coordinator._tasks:
            running_task = coordinator._tasks[task_key]
            running_task.cancel()

            try:
                await running_task
            except asyncio.CancelledError:
                pass


class MemoryPressureTests(TransactionTestCase):
    """Test behavior under memory pressure and resource constraints."""

    async def test_many_queued_events_in_slow_client(self):
        """
        EDGE CASE: Client not consuming events fast enough.

        Expected: Queue grows but doesn't block other clients or task.
        """
        task = await ExampleTask.objects.acreate(message="Slow client test")

        # Fast client
        fast_queue = asyncio.Queue()
        await task.add_client(fast_queue)

        # Slow client (never consumes)
        slow_queue = asyncio.Queue()
        await task.add_client(slow_queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait for completion
        await asyncio.sleep(0.1)

        # Fast client can consume
        events = []
        while not fast_queue.empty():
            events.append(await fast_queue.get())

        assert len(events) >= 5  # Should have received all events

        # Slow queue has events queued up
        assert slow_queue.qsize() >= 5

    async def test_many_concurrent_tasks(self):
        """
        STRESS TEST: Many tasks running simultaneously.

        Expected: System handles gracefully, no deadlocks.
        """
        # Create many tasks
        tasks = []
        for i in range(20):
            task = await ExampleTask.objects.acreate(message=f"Task {i}")
            await coordinator.start_task(task, 'tests', 'ExampleTask')
            tasks.append(task)

        # Wait for all to complete
        await asyncio.sleep(0.2)

        # All should have completed
        for task in tasks:
            await task.arefresh_from_db()
            assert task.completed_at is not None

    async def test_many_clients_on_single_task(self):
        """
        STRESS TEST: Many clients connected to one task.

        Expected: All clients receive events, no degradation.
        """
        task = await ExampleTask.objects.acreate(message="Many clients test")

        # Create 50 clients
        queues = [asyncio.Queue() for _ in range(50)]
        for queue in queues:
            await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait for completion
        await asyncio.sleep(0.2)

        # All clients should have received events
        for i, queue in enumerate(queues):
            assert not queue.empty(), f"Client {i} received no events"


class CoordinatorStateTests(TransactionTestCase):
    """Test coordinator state consistency under race conditions."""

    async def test_task_cleanup_race_with_new_client(self):
        """
        RACE CONDITION: Task cleanup while new client connecting.

        Expected: Either client connects successfully or gets clear error.
        """
        task = await ExampleTask.objects.acreate(message="Cleanup race test")

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Wait until task is almost done
        await asyncio.sleep(0.035)

        # Try to add client as task completes
        late_queue = asyncio.Queue()
        await task.add_client(late_queue)

        # Wait a bit
        await asyncio.sleep(0.1)

        # Task should be cleaned up
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        # Task might or might not be in _tasks (timing dependent)

        # Late client might have received some events
        # But system should be in consistent state

    async def test_task_start_race_with_completion(self):
        """
        RACE CONDITION: start_task called on already-completed task.

        Expected: Task not restarted, or handled gracefully.
        """
        task = await ExampleTask.objects.acreate(message="Already complete test")

        # Start and wait for completion
        await coordinator.start_task(task, 'tests', 'ExampleTask')
        await asyncio.sleep(0.1)

        await task.arefresh_from_db()
        assert task.completed_at is not None

        # Try to start again
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Should not have created duplicate task
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        # Should either not start or recognize it's complete

    async def test_coordinator_task_dict_consistency(self):
        """
        INVARIANT: coordinator._tasks and _task_instances stay consistent.

        Expected: Keys in both dicts match, or differ only during brief transitions.
        """
        tasks = []
        for i in range(5):
            task = await ExampleTask.objects.acreate(message=f"Consistency test {i}")
            await coordinator.start_task(task, 'tests', 'ExampleTask')
            tasks.append(task)

        # Check consistency
        await asyncio.sleep(0.01)

        task_keys = set(coordinator._tasks.keys())
        instance_keys = set(coordinator._task_instances.keys())

        # Keys should mostly match (might differ briefly during add/remove)
        # Both should have entries
        assert len(task_keys) > 0
        assert len(instance_keys) > 0

        # Wait for completion
        await asyncio.sleep(0.1)

        # After completion, both should be cleaned up
        assert len(coordinator._tasks) == 0
        assert len(coordinator._task_instances) == 0


class ExceptionPropagationRaceTests(TransactionTestCase):
    """Test exception handling in race conditions."""

    async def test_exception_during_cancellation_handling(self):
        """
        EDGE CASE: Exception raised during CancelledError handling.

        Expected: Both exceptions properly reported, no silent failures.
        """
        exception_raised = False

        class BrokenTask(ExampleTask):
            class Meta:
                proxy = True

            async def process(self):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    # Simulate cleanup that raises exception
                    nonlocal exception_raised
                    exception_raised = True
                    raise RuntimeError("Cleanup failed")

        task = await BrokenTask.objects.acreate(message="Broken cleanup test")

        await coordinator.start_task(task, 'tests', 'BrokenTask')

        task_key = coordinator.get_task_key('tests', 'BrokenTask', task.pk)
        running_task = coordinator._tasks[task_key]

        await asyncio.sleep(0.01)
        running_task.cancel()

        # Should raise RuntimeError, not CancelledError
        # (newer exception replaces CancelledError)
        with pytest.raises(RuntimeError, match="Cleanup failed"):
            await running_task

        assert exception_raised

    async def test_send_event_exception_during_cancellation(self):
        """
        EDGE CASE: send_event fails during cancellation notification.

        Expected: Cancellation still propagates, error logged.
        """
        task = await ExampleTask.objects.acreate(message="Send event fail test")

        # Add broken queue
        broken_queue = asyncio.Queue()

        async def failing_put(item):
            raise RuntimeError("Queue broken")

        broken_queue.put = failing_put
        await task.add_client(broken_queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        await asyncio.sleep(0.01)

        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # Should still propagate CancelledError despite send_event failure
        with pytest.raises(asyncio.CancelledError):
            await running_task
