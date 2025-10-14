import os
from django.core.management.base import BaseCommand
from django.conf import settings


class Command(BaseCommand):
    help = 'Run the streaming server on a Unix socket'

    def add_arguments(self, parser):
        parser.add_argument(
            '--socket',
            type=str,
            default='/tmp/django-stream.sock',
            help='Unix socket path (default: /tmp/django-stream.sock)'
        )
        parser.add_argument(
            '--host',
            type=str,
            default='127.0.0.1',
            help='Host to bind to (used instead of socket if provided with --port)'
        )
        parser.add_argument(
            '--port',
            type=int,
            help='Port to bind to (if provided, uses TCP instead of Unix socket)'
        )

    def handle(self, *args, **options):
        socket_path = options['socket']
        host = options['host']
        port = options['port']

        
        if not port and os.path.exists(socket_path):
            os.remove(socket_path)
            self.stdout.write(f"Removed existing socket: {socket_path}")

        
        import uvicorn

        
        if port:
            bind_addr = f"{host}:{port}"
            self.stdout.write(self.style.SUCCESS(f'Starting streaming server on {bind_addr}'))
            uvicorn.run(
                "streaming.server:app",
                host=host,
                port=port,
                log_level="info"
            )
        else:
            self.stdout.write(self.style.SUCCESS(f'Starting streaming server on Unix socket: {socket_path}'))
            uvicorn.run(
                "streaming.server:app",
                uds=socket_path,
                log_level="info"
            )
