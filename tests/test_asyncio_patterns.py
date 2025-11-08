"""
Test cases for asyncio patterns: gather, wait_for, timeout, TaskGroup.

These tests verify proper usage of asyncio primitives according to best practices:
- asyncio.gather with return_exceptions
- asyncio.wait_for timeout handling
- Modern asyncio.timeout context manager
- Python 3.11+ TaskGroup for structured concurrency

Many of these tests will FAIL or show warnings with current implementation.
"""
import asyncio
import sys
import pytest
from django.test import TransactionTestCase
from tests.models import ExampleTask, HttpxFetchTask
from streaming.coordinator import coordinator
from streaming.models import StreamTask


class GatherPatternTests(TransactionTestCase):
    """Test asyncio.gather usage patterns."""

    async def test_gather_in_send_event_uses_return_exceptions(self):
        """
        MAJOR: gather() should use return_exceptions=True for robustness.

        Current: streaming/models.py:61 uses gather without return_exceptions
        Expected: Explicit return_exceptions=True prevents crashes from client errors.
        """
        task = await ExampleTask.objects.acreate(message="Gather test")

        # Add normal clients
        queue1 = asyncio.Queue()
        queue2 = asyncio.Queue()
        await task.add_client(queue1)
        await task.add_client(queue2)

        # Add a "broken" queue that will fail on put
        broken_queue = asyncio.Queue()

        # Mock put to raise exception
        original_put = broken_queue.put

        async def failing_put(item):
            raise RuntimeError("Simulated client failure")

        broken_queue.put = failing_put
        task._clients.add(broken_queue)

        # Send event - should not crash even with broken client
        # Current implementation might crash if not using return_exceptions
        try:
            await task.send_event('test', {'message': 'test'})
        except RuntimeError:
            pytest.fail("send_event should not propagate client exceptions")

        # Good clients should still receive event
        event1 = await asyncio.wait_for(queue1.get(), timeout=0.5)
        event2 = await asyncio.wait_for(queue2.get(), timeout=0.5)

        assert event1['type'] == 'test'
        assert event2['type'] == 'test'

        # Broken client should be removed
        assert broken_queue not in task._clients

    async def test_gather_handles_cancellation_correctly(self):
        """
        IMPORTANT: gather() during cancellation should handle CancelledError.

        When task is cancelled while sending events to clients,
        the gather should not suppress CancelledError.
        """
        task = await ExampleTask.objects.acreate(message="Cancel during gather")

        queues = [asyncio.Queue() for _ in range(3)]
        for queue in queues:
            await task.add_client(queue)

        # Start task
        await coordinator.start_task(task, 'tests', 'ExampleTask')

        # Cancel immediately while it's sending events
        task_key = coordinator.get_task_key('tests', 'ExampleTask', task.pk)
        running_task = coordinator._tasks[task_key]
        running_task.cancel()

        # CancelledError should propagate
        with pytest.raises(asyncio.CancelledError):
            await running_task


class TimeoutPatternTests(TransactionTestCase):
    """Test timeout handling patterns."""

    async def test_wait_for_timeout_raises_timeouterror(self):
        """
        DOCUMENTED: asyncio.wait_for should raise TimeoutError on timeout.

        This documents current correct usage in tests.
        """
        async def slow_operation():
            await asyncio.sleep(10)

        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(slow_operation(), timeout=0.1)

    async def test_timeout_context_manager_available(self):
        """
        MODERN FEATURE: asyncio.timeout() context manager (Python 3.11+).

        Expected: Use async with asyncio.timeout() instead of wait_for.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def slow_operation():
            await asyncio.sleep(10)
            return "done"

        # Modern pattern
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(0.1):
                await slow_operation()

    async def test_timeout_context_manager_for_multiple_operations(self):
        """
        MODERN FEATURE: timeout() can wrap multiple operations.

        Advantage over wait_for: applies to multiple awaits, cleaner syntax.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def operation1():
            await asyncio.sleep(0.05)

        async def operation2():
            await asyncio.sleep(0.05)

        async def operation3():
            await asyncio.sleep(0.05)

        # All three operations under one timeout
        with pytest.raises(asyncio.TimeoutError):
            async with asyncio.timeout(0.1):
                await operation1()
                await operation2()
                await operation3()

    async def test_timeout_reschedule_feature(self):
        """
        MODERN FEATURE: timeout.reschedule() to extend deadline (Python 3.11+).
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def adaptive_operation():
            await asyncio.sleep(0.05)
            # Operation needs more time
            return "needs_extension"

        # Timeout with dynamic rescheduling
        deadline = asyncio.timeout(0.1)
        async with deadline:
            result = await adaptive_operation()
            if result == "needs_extension":
                # Extend deadline by 0.2 seconds from now
                deadline.reschedule(asyncio.get_event_loop().time() + 0.2)
                await asyncio.sleep(0.15)  # Would timeout without reschedule

    async def test_coordinator_tasks_have_timeout_protection(self):
        """
        MAJOR MISSING FEATURE: Tasks should have maximum execution time.

        Current: No timeout protection, tasks can run forever.
        Expected: Configurable timeout per task or global default.
        """
        # This documents what SHOULD exist but doesn't
        task = await ExampleTask.objects.acreate(message="Timeout test")

        # Would like to do:
        # task.max_execution_time = 0.5
        # or
        # coordinator.start_task(task, 'tests', 'ExampleTask', timeout=0.5)

        # For now, this test documents the gap
        pytest.skip("Task timeout protection not implemented")


class TaskGroupTests(TransactionTestCase):
    """Test Python 3.11+ TaskGroup for structured concurrency."""

    async def test_taskgroup_available(self):
        """
        MODERN FEATURE: asyncio.TaskGroup (Python 3.11+).
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def worker(n):
            await asyncio.sleep(0.01)
            return n * 2

        async with asyncio.TaskGroup() as tg:
            task1 = tg.create_task(worker(1))
            task2 = tg.create_task(worker(2))
            task3 = tg.create_task(worker(3))

        # After context exits, all tasks are done
        assert task1.result() == 2
        assert task2.result() == 4
        assert task3.result() == 6

    async def test_taskgroup_cancels_all_on_exception(self):
        """
        MODERN FEATURE: TaskGroup auto-cancels all tasks if one fails.

        This is superior to gather() for structured concurrency.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        completed = []

        async def worker(n):
            try:
                await asyncio.sleep(0.1)
                completed.append(n)
            except asyncio.CancelledError:
                completed.append(f"{n}_cancelled")
                raise

        async def failing_worker():
            await asyncio.sleep(0.05)
            raise ValueError("Task failed")

        # TaskGroup should cancel all tasks when one fails
        with pytest.raises(ExceptionGroup) as exc_info:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(worker(1))
                tg.create_task(failing_worker())
                tg.create_task(worker(3))

        # Workers should have been cancelled
        assert "1_cancelled" in completed or 1 not in completed
        assert "3_cancelled" in completed or 3 not in completed

    async def test_taskgroup_exception_group_handling(self):
        """
        MODERN FEATURE: TaskGroup with ExceptionGroup and except*.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def value_error_task():
            raise ValueError("Value error")

        async def type_error_task():
            raise TypeError("Type error")

        # Multiple exceptions combined into ExceptionGroup
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(value_error_task())
                tg.create_task(type_error_task())
        except* ValueError as eg:
            assert len(eg.exceptions) == 1
            assert isinstance(eg.exceptions[0], ValueError)
        except* TypeError as eg:
            assert len(eg.exceptions) == 1
            assert isinstance(eg.exceptions[0], TypeError)

    async def test_send_event_could_use_taskgroup(self):
        """
        IMPROVEMENT OPPORTUNITY: send_event could use TaskGroup.

        Current: Uses gather()
        Better: Use TaskGroup for automatic cancellation of all sends if one fails.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        task = await ExampleTask.objects.acreate(message="TaskGroup send test")

        queues = [asyncio.Queue() for _ in range(3)]
        for queue in queues:
            await task.add_client(queue)

        # Current implementation uses gather
        # Improved version would use TaskGroup:
        #
        # async with asyncio.TaskGroup() as tg:
        #     for queue in self._clients:
        #         tg.create_task(queue.put(event_data))
        #
        # This ensures if one put fails, all are cancelled

        await task.send_event('test', {'message': 'test'})

        # All should receive
        for queue in queues:
            event = await asyncio.wait_for(queue.get(), timeout=0.5)
            assert event['type'] == 'test'


class ShieldPatternTests(TransactionTestCase):
    """Test asyncio.shield() for protecting critical operations."""

    async def test_shield_protects_critical_operation(self):
        """
        ADVANCED: shield() protects operation from outer cancellation.

        Use case: Critical cleanup that must complete even if parent is cancelled.
        """
        cleanup_completed = False

        async def critical_cleanup():
            nonlocal cleanup_completed
            await asyncio.sleep(0.1)
            cleanup_completed = True
            return "cleanup done"

        async def operation():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Shield critical cleanup from cancellation
                result = await asyncio.shield(critical_cleanup())
                raise

        task = asyncio.create_task(operation())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Cleanup should complete despite cancellation
        await asyncio.sleep(0.15)
        assert cleanup_completed, "Critical cleanup should complete"

    async def test_shield_use_case_in_task_cleanup(self):
        """
        USE CASE: Task cleanup should use shield for critical operations.

        Example: Saving final state to database must complete even if cancelled.
        """
        save_completed = False

        async def save_to_database():
            nonlocal save_completed
            await asyncio.sleep(0.1)
            save_completed = True

        # Simulate task with shielded cleanup
        async def task_with_shielded_cleanup():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # Shield database save from cancellation
                await asyncio.shield(save_to_database())
                raise

        task = asyncio.create_task(task_with_shielded_cleanup())
        await asyncio.sleep(0.01)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # Give shield time to complete
        await asyncio.sleep(0.15)
        assert save_completed, "Shielded database save should complete"


class WaitPatternTests(TransactionTestCase):
    """Test asyncio.wait() for fine-grained control."""

    async def test_wait_return_when_first_exception(self):
        """
        ADVANCED: asyncio.wait() with FIRST_EXCEPTION for early failure detection.

        Advantage over gather: Can detect and respond to failures immediately.
        """
        async def fast_task():
            await asyncio.sleep(0.05)
            return "fast"

        async def failing_task():
            await asyncio.sleep(0.02)
            raise ValueError("Failed")

        async def slow_task():
            await asyncio.sleep(10)
            return "slow"

        tasks = {
            asyncio.create_task(fast_task()),
            asyncio.create_task(failing_task()),
            asyncio.create_task(slow_task()),
        }

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION
        )

        # Should return when failing_task raises
        assert len(done) >= 1

        # Cancel remaining tasks
        for task in pending:
            task.cancel()

        await asyncio.gather(*pending, return_exceptions=True)

    async def test_wait_return_when_first_completed(self):
        """
        ADVANCED: asyncio.wait() with FIRST_COMPLETED for racing tasks.
        """
        async def task1():
            await asyncio.sleep(0.05)
            return "task1"

        async def task2():
            await asyncio.sleep(0.1)
            return "task2"

        tasks = {
            asyncio.create_task(task1()),
            asyncio.create_task(task2()),
        }

        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED
        )

        # First to complete
        assert len(done) == 1
        result = done.pop().result()
        assert result == "task1"

        # Cancel pending
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)


class CancellationPropagationTests(TransactionTestCase):
    """Test that CancelledError propagates correctly through async call stack."""

    async def test_cancelled_error_propagates_through_await_chain(self):
        """
        FUNDAMENTAL: CancelledError must propagate through nested awaits.
        """
        propagation_path = []

        async def level3():
            try:
                propagation_path.append("level3_enter")
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                propagation_path.append("level3_cancelled")
                raise

        async def level2():
            try:
                propagation_path.append("level2_enter")
                await level3()
            except asyncio.CancelledError:
                propagation_path.append("level2_cancelled")
                raise

        async def level1():
            try:
                propagation_path.append("level1_enter")
                await level2()
            except asyncio.CancelledError:
                propagation_path.append("level1_cancelled")
                raise

        task = asyncio.create_task(level1())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Should propagate through all levels
        assert "level1_enter" in propagation_path
        assert "level2_enter" in propagation_path
        assert "level3_enter" in propagation_path
        assert "level3_cancelled" in propagation_path
        assert "level2_cancelled" in propagation_path
        assert "level1_cancelled" in propagation_path

    async def test_cancelled_error_not_suppressed_by_bare_except(self):
        """
        CRITICAL: CancelledError as BaseException prevents bare except suppression.

        Python 3.8+ moved CancelledError from Exception to BaseException.
        """
        suppressed = False

        async def task_with_bare_except():
            try:
                await asyncio.sleep(10)
            except Exception:  # Should NOT catch CancelledError
                nonlocal suppressed
                suppressed = True

        task = asyncio.create_task(task_with_bare_except())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert not suppressed, \
            "CancelledError should not be caught by 'except Exception'"

    async def test_cancelled_error_is_suppressed_by_base_exception(self):
        """
        WARNING: bare except: or except BaseException: will catch CancelledError.

        This is why explicit handling is important.
        """
        caught = False

        async def task_with_base_exception_handler():
            try:
                await asyncio.sleep(10)
            except BaseException:
                nonlocal caught
                caught = True
                # Should re-raise!
                raise

        task = asyncio.create_task(task_with_base_exception_handler())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert caught, "BaseException catches CancelledError"


class CancellationCounterTests(TransactionTestCase):
    """Test Python 3.11+ cancellation counter features."""

    async def test_cancelling_counter_tracks_cancel_calls(self):
        """
        PYTHON 3.11+: cancelling() returns count of cancel() calls.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def worker():
            # Check cancellation counter
            current = asyncio.current_task()
            count_before = current.cancelling()
            assert count_before == 0, "Should start with 0"

            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                count_during = current.cancelling()
                assert count_during > 0, "Should have positive count during cancellation"
                raise

        task = asyncio.create_task(worker())
        await asyncio.sleep(0.01)

        # Cancel multiple times
        task.cancel()
        task.cancel()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    async def test_uncancel_removes_cancellation_request(self):
        """
        PYTHON 3.11+: uncancel() decrements the cancellation counter.

        Use case: Catching CancelledError from child, not propagating to parent.
        """
        if sys.version_info < (3, 11):
            pytest.skip("Requires Python 3.11+")

        async def child_task():
            await asyncio.sleep(10)

        async def parent_task():
            child = asyncio.create_task(child_task())
            await asyncio.sleep(0.02)

            # Cancel child
            child.cancel()

            try:
                await child
            except asyncio.CancelledError:
                # We're handling child cancellation, not being cancelled ourselves
                current = asyncio.current_task()
                if current.cancelling() > 0:
                    # Remove the cancellation we just handled
                    current.uncancel()

            # Continue working
            await asyncio.sleep(0.01)
            return "completed"

        result = await parent_task()
        assert result == "completed"
