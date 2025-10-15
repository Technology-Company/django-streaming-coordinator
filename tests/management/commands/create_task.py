import asyncio
from django.core.management.base import BaseCommand
from tests.models import ExampleTask, ContinueTask
from streaming.coordinator import coordinator


class Command(BaseCommand):
    help = 'Create and start a streaming task'

    def add_arguments(self, parser):
        parser.add_argument(
            '--type',
            type=str,
            default='example',
            choices=['example', 'continue'],
            help='Type of task to create (example or continue)'
        )
        parser.add_argument(
            '--message',
            type=str,
            default=None,
            help='Message for the task'
        )
        parser.add_argument(
            '--no-start',
            action='store_true',
            help='Create task but do not start it'
        )

    def handle(self, *args, **options):
        task_type = options['type']
        message = options['message']
        no_start = options['no_start']


        asyncio.run(self._create_task(task_type, message, no_start))

    async def _create_task(self, task_type, message, no_start):


        if task_type == 'example':
            if message is None:
                message = "Task created via management command"

            task = await ExampleTask.objects.acreate(message=message)
            app_name = 'tests'
            model_name = 'ExampleTask'

            self.stdout.write(self.style.SUCCESS(f'Created ExampleTask with ID: {task.pk}'))
            self.stdout.write(f'  Message: {task.message}')

        elif task_type == 'continue':
            if message is None:
                message = "Waiting for continue signal..."

            task = await ContinueTask.objects.acreate(
                message=message,
                continue_field=False
            )
            app_name = 'tests'
            model_name = 'ContinueTask'

            self.stdout.write(self.style.SUCCESS(f'Created ContinueTask with ID: {task.pk}'))
            self.stdout.write(f'  Message: {task.message}')
            self.stdout.write(f'  Continue: {task.continue_field}')
            self.stdout.write(self.style.WARNING(
                f'\n  To continue the task, run:'
            ))
            self.stdout.write(self.style.WARNING(
                f'    poetry run python manage.py shell -c "from tests.models import ContinueTask; import asyncio; asyncio.run(ContinueTask.objects.filter(pk={task.pk}).aupdate(continue_field=True))"'
            ))

        if not no_start:
            self.stdout.write('\nStarting task...')
            await coordinator.start_task(task, app_name, model_name)
            self.stdout.write(self.style.SUCCESS('✓ Task started'))

        self.stdout.write(f'\nConnect to task stream:')
        self.stdout.write(f'  curl http://127.0.0.1:8888/stream/{app_name}/{model_name}/{task.pk}')
