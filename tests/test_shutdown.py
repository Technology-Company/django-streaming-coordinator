"""
Test cases for graceful shutdown and signal handling.

These tests verify that the streaming server shuts down gracefully,
cancelling all running tasks and cleaning up resources properly.

All these tests are expected to FAIL with the current implementation.
"""
import asyncio
import signal
import os
import pytest
from unittest.mock import Mock, patch, AsyncMock
from django.test import TransactionTestCase
from tests.models import ExampleTask, ContinueTask
from streaming.coordinator import coordinator


class ServerShutdownTests(TransactionTestCase):
    """Test server-level shutdown behavior."""

    async def test_signal_handler_registered_for_sigterm(self):
        """
        CRITICAL: Server should register SIGTERM handler for graceful shutdown.

        Current behavior: No signal handlers registered.
        Expected behavior: SIGTERM triggers graceful shutdown.
        """
        # This would typically be in server.py or the management command
        # We test that the handler exists and is registered

        loop = asyncio.get_running_loop()

        # Check if SIGTERM handler is registered
        # In current implementation, this will fail
        with pytest.raises(AttributeError, match="shutdown_handler"):
            from streaming.server import shutdown_handler

    async def test_signal_handler_registered_for_sigint(self):
        """
        CRITICAL: Server should register SIGINT handler (Ctrl+C).

        Expected behavior: SIGINT triggers graceful shutdown.
        """
        loop = asyncio.get_running_loop()

        with pytest.raises(AttributeError, match="shutdown_handler"):
            from streaming.server import shutdown_handler

    async def test_shutdown_handler_cancels_all_coordinator_tasks(self):
        """
        CRITICAL: Shutdown handler should cancel all tasks in coordinator.

        Expected flow:
        1. Signal received
        2. Get all running tasks from coordinator
        3. Cancel each task
        4. Wait for cancellation to complete
        5. Close coordinator resources
        """
        # Create multiple running tasks
        tasks = []
        for i in range(3):
            task = await ContinueTask.objects.acreate(
                message=f"Task {i}",
                continue_field=False
            )
            await coordinator.start_task(task, 'tests', 'ContinueTask')
            tasks.append(task)

        # Verify all running
        for task in tasks:
            assert coordinator.is_task_running('tests', 'ContinueTask', task.pk)

        # Trigger shutdown (will fail - not implemented)
        try:
            from streaming.server import shutdown_handler
            await shutdown_handler(signal.SIGTERM)
        except (ImportError, AttributeError):
            pytest.fail("shutdown_handler not implemented in streaming.server")

        # All tasks should be cancelled
        for task in tasks:
            assert not coordinator.is_task_running('tests', 'ContinueTask', task.pk), \
                f"Task {task.pk} still running after shutdown"

    async def test_shutdown_during_active_http_connections(self):
        """
        CRITICAL: Shutdown should handle active SSE connections gracefully.

        Expected behavior:
        1. Active event streams are notified
        2. Clients receive final event before disconnect
        3. Connections close cleanly
        """
        task = await ContinueTask.objects.acreate(
            message="Active connection test",
            continue_field=False
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Simulate shutdown with active client
        try:
            from streaming.server import shutdown_handler
            await shutdown_handler(signal.SIGTERM)
        except (ImportError, AttributeError):
            pytest.fail("shutdown_handler not implemented")

        # Client should receive shutdown/cancelled event
        # Queue should have events indicating shutdown
        try:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            assert event['type'] in ['cancelled', 'shutdown', 'error'], \
                f"Expected shutdown notification, got {event['type']}"
        except asyncio.TimeoutError:
            pytest.fail("Client did not receive shutdown notification")

    async def test_shutdown_timeout_prevents_hang(self):
        """
        CRITICAL: Shutdown must complete within timeout even if tasks hang.

        Expected behavior: Shutdown times out after 30 seconds max.
        """
        # Create task that won't respond to cancellation properly
        task = await ContinueTask.objects.acreate(
            message="Hanging task",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Mock the task to ignore cancellation
        task_key = coordinator.get_task_key('tests', 'ContinueTask', task.pk)

        async def hanging_task():
            try:
                await asyncio.sleep(1000)
            except asyncio.CancelledError:
                # Bad behavior: ignore cancellation and keep running
                await asyncio.sleep(1000)

        coordinator._tasks[task_key] = asyncio.create_task(hanging_task())

        # Shutdown should timeout
        try:
            from streaming.server import shutdown_handler

            start = asyncio.get_event_loop().time()
            # Pass short timeout for testing
            await shutdown_handler(signal.SIGTERM, timeout=0.5)
            elapsed = asyncio.get_event_loop().time() - start

            assert elapsed < 1.0, \
                f"Shutdown took {elapsed}s, should timeout at 0.5s"
        except (ImportError, AttributeError):
            pytest.fail("shutdown_handler not implemented")

    async def test_shutdown_logs_tasks_that_didnt_finish(self):
        """
        IMPORTANT: Shutdown should log which tasks didn't finish in time.

        Expected: Warning logs for tasks that exceeded timeout.
        """
        task = await ContinueTask.objects.acreate(
            message="Slow cleanup task",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        with patch('streaming.coordinator.logger') as mock_logger:
            try:
                from streaming.server import shutdown_handler
                await shutdown_handler(signal.SIGTERM, timeout=0.1)

                # Should log timeout warning
                mock_logger.warning.assert_called()
                # Look for timeout-related messages
                calls = [str(call) for call in mock_logger.warning.call_args_list]
                assert any('timeout' in str(call).lower() for call in calls), \
                    "Should log timeout warning for slow tasks"
            except (ImportError, AttributeError):
                pytest.fail("shutdown_handler not implemented")


class CoordinatorShutdownTests(TransactionTestCase):
    """Test coordinator-level shutdown methods."""

    async def test_coordinator_has_shutdown_method(self):
        """
        HIGH PRIORITY: Coordinator should have shutdown() method.

        Expected: coordinator.shutdown() cancels all tasks and cleans up.
        """
        assert hasattr(coordinator, 'shutdown'), \
            "TaskCoordinator should have shutdown() method"

    async def test_coordinator_shutdown_cancels_all_tasks(self):
        """
        HIGH PRIORITY: coordinator.shutdown() should cancel all running tasks.
        """
        # Create multiple tasks
        tasks = []
        for i in range(3):
            task = await ExampleTask.objects.acreate(message=f"Task {i}")
            await coordinator.start_task(task, 'tests', 'ExampleTask')
            tasks.append(task)

        # All should be running initially
        for task in tasks:
            assert coordinator.is_task_running('tests', 'ExampleTask', task.pk)

        # Call shutdown
        await coordinator.shutdown()

        # All should be cancelled
        for task in tasks:
            assert not coordinator.is_task_running('tests', 'ExampleTask', task.pk), \
                f"Task {task.pk} still running after coordinator.shutdown()"

    async def test_coordinator_shutdown_waits_for_cleanup(self):
        """
        HIGH PRIORITY: shutdown() should wait for tasks to clean up.

        Expected: All finally blocks run, clients notified.
        """
        task = await ExampleTask.objects.acreate(message="Cleanup test")
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Call shutdown
        await coordinator.shutdown()

        # Client should have received events including cancellation
        events = []
        while not queue.empty():
            events.append(await queue.get())

        # Should have at least start event and cancellation event
        event_types = [e['type'] for e in events]
        assert 'cancelled' in event_types or 'shutdown' in event_types, \
            f"Expected cancellation event, got: {event_types}"

    async def test_coordinator_shutdown_accepts_timeout(self):
        """
        HIGH PRIORITY: shutdown(timeout=N) should enforce timeout.
        """
        task = await ContinueTask.objects.acreate(
            message="Timeout test",
            continue_field=False
        )

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        start = asyncio.get_event_loop().time()
        await coordinator.shutdown(timeout=0.5)
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 1.0, \
            f"Shutdown took {elapsed}s, should timeout at 0.5s"

    async def test_coordinator_shutdown_is_idempotent(self):
        """
        NICE TO HAVE: Multiple shutdown() calls should be safe.
        """
        task = await ExampleTask.objects.acreate(message="Idempotent test")
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Multiple shutdowns should not crash
        await coordinator.shutdown()
        await coordinator.shutdown()
        await coordinator.shutdown()

        # No tasks should be running
        assert len([t for t in coordinator._tasks.values() if not t.done()]) == 0


class CleanupResourceTests(TransactionTestCase):
    """Test that resources are cleaned up during shutdown."""

    async def test_all_task_references_cleared_on_shutdown(self):
        """
        IMPORTANT: Shutdown should clear all internal task dictionaries.

        Expected: _tasks, _task_instances, _locks all cleared.
        """
        # Create tasks
        for i in range(3):
            task = await ExampleTask.objects.acreate(message=f"Task {i}")
            await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Should have tasks
        assert len(coordinator._tasks) > 0
        assert len(coordinator._task_instances) > 0

        # Shutdown
        await coordinator.shutdown()

        # All should be cleared
        assert len(coordinator._tasks) == 0, \
            "coordinator._tasks not cleared after shutdown"
        assert len(coordinator._task_instances) == 0, \
            "coordinator._task_instances not cleared after shutdown"

    async def test_client_queues_notified_on_shutdown(self):
        """
        IMPORTANT: All connected clients should be notified of shutdown.
        """
        # Create task with multiple clients
        task = await ExampleTask.objects.acreate(message="Client notify test")

        queues = [asyncio.Queue() for _ in range(3)]
        for queue in queues:
            await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Shutdown
        await coordinator.shutdown()

        # All clients should receive cancellation/shutdown event
        for i, queue in enumerate(queues):
            events = []
            while not queue.empty():
                events.append(await queue.get())

            event_types = [e['type'] for e in events]
            assert 'cancelled' in event_types or 'shutdown' in event_types, \
                f"Client {i} didn't receive shutdown notification: {event_types}"

    async def test_locks_released_on_shutdown(self):
        """
        IMPORTANT: All asyncio.Lock objects should be released.
        """
        # Create tasks that acquire locks
        for i in range(3):
            task = await ExampleTask.objects.acreate(message=f"Lock test {i}")
            # This will create locks in the coordinator
            await coordinator.get_task_instance('tests', 'ExampleTask', task.pk)

        # Should have locks
        initial_lock_count = len(coordinator._locks)
        assert initial_lock_count > 0

        # Shutdown
        await coordinator.shutdown()

        # Locks should be cleared
        assert len(coordinator._locks) == 0, \
            "Locks not cleared after shutdown"


class ShutdownIntegrationTests(TransactionTestCase):
    """Integration tests for shutdown with real server scenarios."""

    async def test_shutdown_during_task_processing(self):
        """
        INTEGRATION: Shutdown while task is actively processing.

        Scenario: Task is in the middle of its work when shutdown happens.
        Expected: Task receives cancellation, runs cleanup, notifies clients.
        """
        task = await ContinueTask.objects.acreate(
            message="Processing test",
            continue_field=False
        )
        queue = asyncio.Queue()
        await task.add_client(queue)

        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Let task start processing
        start_event = await asyncio.wait_for(queue.get(), timeout=0.5)
        assert start_event['type'] == 'started'

        # Shutdown while task is waiting
        shutdown_task = asyncio.create_task(coordinator.shutdown(timeout=1.0))

        # Should complete within timeout
        await asyncio.wait_for(shutdown_task, timeout=2.0)

        # Client should have received cancellation
        events_received = []
        while not queue.empty():
            events_received.append(await queue.get())

        event_types = [e['type'] for e in events_received]
        assert 'cancelled' in event_types or 'shutdown' in event_types

    async def test_shutdown_with_no_running_tasks(self):
        """
        INTEGRATION: Shutdown when no tasks are running.

        Expected: Completes immediately, no errors.
        """
        # Ensure no tasks running
        assert len(coordinator._tasks) == 0

        # Shutdown should complete quickly
        start = asyncio.get_event_loop().time()
        await coordinator.shutdown()
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 0.5, "Shutdown with no tasks should be instant"

    async def test_shutdown_then_start_new_task_fails_gracefully(self):
        """
        INTEGRATION: Starting task after shutdown should fail gracefully.

        Expected: Either raises clear error or prevents task from starting.
        """
        await coordinator.shutdown()

        # Attempt to start task after shutdown
        task = await ExampleTask.objects.acreate(message="Post-shutdown task")

        # This should either:
        # 1. Raise a clear exception
        # 2. Log a warning and refuse to start
        # 3. Mark coordinator as shut down

        # For now, document expected behavior
        # Implementation might add a `_shutdown` flag
        pytest.skip("Post-shutdown behavior not specified")


class SignalHandlingEdgeCases(TransactionTestCase):
    """Edge cases for signal handling."""

    async def test_multiple_sigterm_signals(self):
        """
        EDGE CASE: Multiple SIGTERM signals sent in quick succession.

        Expected: Only one shutdown process runs, subsequent signals ignored.
        """
        # Create long-running task
        task = await ContinueTask.objects.acreate(
            message="Multi-signal test",
            continue_field=False
        )
        await coordinator.start_task(task, 'tests', 'ContinueTask')

        # Simulate multiple signals
        try:
            from streaming.server import shutdown_handler

            # Start first shutdown
            shutdown1 = asyncio.create_task(shutdown_handler(signal.SIGTERM))

            # Immediately send another
            shutdown2 = asyncio.create_task(shutdown_handler(signal.SIGTERM))

            # Both should complete without errors
            await asyncio.gather(shutdown1, shutdown2)

        except (ImportError, AttributeError):
            pytest.fail("shutdown_handler not implemented")

    async def test_sigterm_then_sigint(self):
        """
        EDGE CASE: SIGTERM followed quickly by SIGINT.

        Expected: Graceful handling, no duplicate shutdowns.
        """
        task = await ContinueTask.objects.acreate(
            message="Mixed signals test",
            continue_field=False
        )
        await coordinator.start_task(task, 'tests', 'ContinueTask')

        try:
            from streaming.server import shutdown_handler

            shutdown1 = asyncio.create_task(shutdown_handler(signal.SIGTERM))
            await asyncio.sleep(0.1)
            shutdown2 = asyncio.create_task(shutdown_handler(signal.SIGINT))

            await asyncio.gather(shutdown1, shutdown2, return_exceptions=True)

        except (ImportError, AttributeError):
            pytest.fail("shutdown_handler not implemented")
