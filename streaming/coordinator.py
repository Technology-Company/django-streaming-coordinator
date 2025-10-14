import asyncio
from typing import Dict, Optional
from django.apps import apps


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

    def get_task_key(self, model_name: str, task_id: int) -> str:
        
        return f"{model_name}:{task_id}"

    async def start_task(self, task_instance: 'StreamTask', model_name: str) -> None:
        """
        Start a task if not already running.

        Args:
            task_instance: The StreamTask instance to run
            model_name: The name of the model (for lookups)

        Note: No lock needed - all operations are atomic (no await statements).
        """
        task_key = self.get_task_key(model_name, task_instance.pk)

        
        
        if task_key in self._tasks and not self._tasks[task_key].done():
            return

        
        self._task_instances[task_key] = task_instance


        async_task = asyncio.create_task(self._run_task(task_instance, task_key))
        self._tasks[task_key] = async_task

    async def _run_task(self, task_instance: 'StreamTask', task_key: str):

        try:
            final_value = await task_instance.process()

            await task_instance.mark_completed(final_value=final_value)
        except Exception as e:
            
            await task_instance.send_event('error', {
                'message': str(e),
                'error_type': type(e).__name__
            })
        finally:
            
            if task_key in self._tasks:
                del self._tasks[task_key]
            if task_key in self._task_instances:
                del self._task_instances[task_key]
            if task_key in self._locks:
                del self._locks[task_key]

    async def get_task_instance(self, model_name: str, task_id: int) -> Optional['StreamTask']:
        """
        Get a running task instance or load it from the database.

        Uses double-checked locking pattern for optimal performance:
        1. Fast path: Check cache without lock (common case after first client)
        2. Slow path: Acquire lock, check cache again, then load from DB if needed

        This prevents race condition where multiple clients could create
        different Python objects for the same database record.

        Args:
            model_name: The name of the model
            task_id: The task ID

        Returns:
            StreamTask instance or None if not found
        """
        task_key = self.get_task_key(model_name, task_id)

        
        if task_key in self._task_instances:
            return self._task_instances[task_key]

        
        
        if task_key not in self._locks:
            self._locks[task_key] = asyncio.Lock()

        lock = self._locks[task_key]

        
        async with lock:
            
            if task_key in self._task_instances:
                return self._task_instances[task_key]

            
            try:
                model_class = apps.get_model('streaming', model_name)
                task_instance = await asyncio.to_thread(
                    model_class.objects.get, pk=task_id
                )

                
                self._task_instances[task_key] = task_instance

                
                if not task_instance.completed_at:
                    await self.start_task(task_instance, model_name)

                return task_instance
            except Exception:
                return None

    def is_task_running(self, model_name: str, task_id: int) -> bool:
        
        task_key = self.get_task_key(model_name, task_id)
        return task_key in self._tasks and not self._tasks[task_key].done()



coordinator = TaskCoordinator()
