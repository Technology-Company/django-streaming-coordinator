import asyncio
from datetime import timedelta
from django.core.management.base import BaseCommand
from django.utils import timezone
from django.apps import apps


class Command(BaseCommand):
    help = 'Delete completed tasks older than 1 hour'

    def add_arguments(self, parser):
        parser.add_argument(
            '--hours',
            type=int,
            default=1,
            help='Delete tasks older than this many hours (default: 1)'
        )
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='Show what would be deleted without actually deleting'
        )

    def handle(self, *args, **options):
        hours = options['hours']
        dry_run = options['dry_run']

        asyncio.run(self._cleanup_tasks(hours, dry_run))

    async def _cleanup_tasks(self, hours, dry_run):
        cutoff_time = timezone.now() - timedelta(hours=hours)

        self.stdout.write(f'Looking for completed tasks older than {cutoff_time.isoformat()}')

        # Get all StreamTask subclasses
        streaming_app = apps.get_app_config('streaming')
        task_models = [
            model for model in streaming_app.get_models()
            if hasattr(model, 'completed_at')
        ]

        total_deleted = 0

        for model in task_models:
            model_name = model.__name__

            # Find old completed tasks
            old_tasks = model.objects.filter(
                completed_at__isnull=False,
                completed_at__lt=cutoff_time
            )

            count = await old_tasks.acount()

            if count > 0:
                self.stdout.write(f'\n{model_name}:')
                self.stdout.write(f'  Found {count} old completed task(s)')

                if dry_run:
                    self.stdout.write(self.style.WARNING(f'  [DRY RUN] Would delete {count} task(s)'))
                else:
                    deleted_count, _ = await old_tasks.adelete()
                    total_deleted += deleted_count
                    self.stdout.write(self.style.SUCCESS(f'  Deleted {deleted_count} task(s)'))

        if dry_run:
            self.stdout.write(self.style.WARNING(f'\n[DRY RUN] Would delete {total_deleted} total task(s)'))
        else:
            self.stdout.write(self.style.SUCCESS(f'\nTotal deleted: {total_deleted} task(s)'))
