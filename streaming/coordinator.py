import asyncio
import logging
from typing import Dict, Optional
from django.apps import apps

logger = logging.getLogger('streaming.coordinator')


class TaskCoordinator:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self._tasks: Dict[str, asyncio.Task] = {}  
        self._task_instances: Dict[str, 'StreamTask'] = {}  
        self._locks: Dict[str, asyncio.Lock] = {}  
        self._initialized = True

    def get_task_key(self, app_name: str, model_name: str, task_id: int) -> str:

        return f"{app_name}:{model_name}:{task_id}"

    async def start_task(self, task_instance: 'StreamTask', app_name: str, model_name: str) -> None:
        """
        Start a task if not already running.

        Args:
            task_instance: The StreamTask instance to run
            app_name: The name of the app (for lookups)
            model_name: The name of the model (for lookups)

        Note: No lock needed - all operations are atomic (no await statements).
        """
        task_key = self.get_task_key(app_name, model_name, task_instance.pk)



        if task_key in self._tasks and not self._tasks[task_key].done():
            logger.info(f"Task {task_key} already running, skipping start")
            return

        logger.info(f"Starting task {task_key} (ID: {task_instance.pk})")

        self._task_instances[task_key] = task_instance


        async_task = asyncio.create_task(self._run_task(task_instance, task_key))
        self._tasks[task_key] = async_task

    async def _run_task(self, task_instance: 'StreamTask', task_key: str):
        logger.info(f"Task {task_key} execution started")
        try:
            final_value = await task_instance.process()
            logger.info(f"Task {task_key} completed successfully with value: {final_value}")
            await task_instance.mark_completed(final_value=final_value)
        except asyncio.CancelledError:
            logger.info(f"Task {task_key} was cancelled")
            await task_instance.send_event('cancelled', {
                'message': 'Task was cancelled',
                'reason': 'external_cancellation'
            })
            raise  # Critical: must re-raise CancelledError
        except Exception as e:
            logger.error(
                f"Task {task_key} failed with {type(e).__name__}: {str(e)}",
                exc_info=True
            )
            await task_instance.send_event('error', {
                'message': str(e),
                'error_type': type(e).__name__
            })
        finally:
            logger.info(f"Task {task_key} cleanup: removing from coordinator")
            if task_key in self._tasks:
                del self._tasks[task_key]
            if task_key in self._task_instances:
                del self._task_instances[task_key]
            if task_key in self._locks:
                del self._locks[task_key]

    async def get_task_instance(self, app_name: str, model_name: str, task_id: int) -> Optional['StreamTask']:
        """
        Get a running task instance or load it from the database.

        Uses double-checked locking pattern for optimal performance:
        1. Fast path: Check cache without lock (common case after first client)
        2. Slow path: Acquire lock, check cache again, then load from DB if needed

        This prevents race condition where multiple clients could create
        different Python objects for the same database record.

        Args:
            app_name: The name of the app
            model_name: The name of the model
            task_id: The task ID

        Returns:
            StreamTask instance or None if not found
        """
        task_key = self.get_task_key(app_name, model_name, task_id)


        if task_key in self._task_instances:
            logger.debug(f"Task {task_key} found in cache")
            return self._task_instances[task_key]



        if task_key not in self._locks:
            self._locks[task_key] = asyncio.Lock()

        lock = self._locks[task_key]


        async with lock:

            if task_key in self._task_instances:
                logger.debug(f"Task {task_key} found in cache after acquiring lock")
                return self._task_instances[task_key]


            try:
                logger.info(f"Loading task {task_key} from database")
                model_class = apps.get_model(app_name, model_name)
                task_instance = await model_class.objects.aget(pk=task_id)


                self._task_instances[task_key] = task_instance


                if not task_instance.completed_at:
                    logger.info(f"Task {task_key} not completed, starting execution")
                    await self.start_task(task_instance, app_name, model_name)
                else:
                    logger.info(f"Task {task_key} already completed")

                return task_instance
            except Exception as e:
                logger.error(f"Failed to load task {task_key}: {type(e).__name__}: {str(e)}", exc_info=True)
                return None

    def is_task_running(self, app_name: str, model_name: str, task_id: int) -> bool:

        task_key = self.get_task_key(app_name, model_name, task_id)
        return task_key in self._tasks and not self._tasks[task_key].done()

    async def cancel_task(self, app_name: str, model_name: str, task_id: int) -> bool:
        """
        Cancel a running task.

        Args:
            app_name: The name of the app
            model_name: The name of the model
            task_id: The task ID

        Returns:
            True if task was cancelled, False if task wasn't running
        """
        task_key = self.get_task_key(app_name, model_name, task_id)

        if task_key not in self._tasks:
            logger.warning(f"Cannot cancel task {task_key} - not found")
            return False

        task = self._tasks[task_key]
        if task.done():
            logger.info(f"Task {task_key} already completed, cannot cancel")
            return False

        logger.info(f"Cancelling task {task_key}")
        task.cancel()
        return True



coordinator = TaskCoordinator()
