# Issue #8: Missing Observability

## Severity
**MEDIUM** 📊

## Affected Components
- Entire codebase (no logging, metrics, or tracing)

## Description

The system has **zero observability**:
- No logging (except Django defaults)
- No metrics (Prometheus, StatsD, etc.)
- No distributed tracing
- No performance monitoring
- Health check returns nothing useful

Production operators cannot answer basic questions like:
- How many tasks are currently running?
- What's the average task duration?
- How many SSE clients are connected?
- What's the error rate?
- Is the system healthy?

## How to Reproduce

### Scenario 1: Production Incident

```bash
# Site is slow, users complaining
# Operations team needs to diagnose

# Try to get metrics:
curl http://127.0.0.1:8888/health
# Returns: "OK"
# ❌ That's it. No useful information!

# Try to find logs:
grep "task" /var/log/app.log
# ❌ No structured logging

# Try to check metrics:
# ❌ No metrics endpoint

# Try to see active tasks:
# ❌ No admin endpoint

# Try to see performance data:
# ❌ No tracing, no profiling
```

### Scenario 2: Debugging a Slow Task

```python
# A task is taking 10 minutes instead of 1 minute
# How do you debug it?

# ❌ No timing logs
# ❌ No span/trace data
# ❌ No profile data
# ❌ No event timing information
```

### Scenario 3: Capacity Planning

```python
# Need to answer: "How many servers do we need?"

# Required data:
# - Tasks per second ❌ Not tracked
# - Average task duration ❌ Not tracked
# - Peak concurrent tasks ❌ Not tracked
# - Memory per task ❌ Not tracked
# - CPU per task ❌ Not tracked

# ❌ Can't do capacity planning without metrics
```

## Impact

1. **Blind Operations** - Can't see what system is doing
2. **Slow Incident Response** - No data to diagnose issues
3. **No SLA Tracking** - Can't measure uptime, latency, errors
4. **No Capacity Planning** - Don't know when to scale
5. **No Alerting** - Can't set up alerts without metrics

## Proposed Fix

### Fix 8.1: Add Structured Logging

```python
# streaming/logging_config.py
import logging
import json
from datetime import datetime

class JSONFormatter(logging.Formatter):
    """Format logs as JSON for structured logging"""
    def format(self, record):
        log_data = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function': record.funcName,
            'line': record.lineno,
        }

        # Add extra fields
        if hasattr(record, 'extra'):
            log_data.update(record.extra)

        return json.dumps(log_data)


# settings.py
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'json': {
            '()': 'streaming.logging_config.JSONFormatter',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'json',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': 'logs/streaming.log',
            'maxBytes': 10485760,  # 10MB
            'backupCount': 5,
            'formatter': 'json',
        },
    },
    'loggers': {
        'streaming': {
            'handlers': ['console', 'file'],
            'level': 'INFO',
        },
        'streaming.coordinator': {
            'handlers': ['console', 'file'],
            'level': 'DEBUG',
        },
    },
}


# coordinator.py
import logging

logger = logging.getLogger('streaming.coordinator')

async def start_task(self, task_instance, app_name, model_name):
    task_key = self.get_task_key(app_name, model_name, task_instance.pk)

    logger.info(
        'Starting task',
        extra={
            'task_id': task_instance.pk,
            'app_name': app_name,
            'model_name': model_name,
            'task_key': task_key,
        }
    )

    # ... implementation ...

async def _run_task(self, task_instance, task_key):
    start_time = time.time()

    logger.debug(f'Task {task_key} started', extra={'task_key': task_key})

    try:
        final_value = await task_instance.process()
        duration = time.time() - start_time

        logger.info(
            'Task completed successfully',
            extra={
                'task_key': task_key,
                'duration_seconds': duration,
                'final_value': final_value,
            }
        )
    except Exception as e:
        duration = time.time() - start_time

        logger.error(
            'Task failed',
            extra={
                'task_key': task_key,
                'duration_seconds': duration,
                'error_type': type(e).__name__,
                'error_message': str(e),
            },
            exc_info=True
        )
        raise
```

### Fix 8.2: Add Prometheus Metrics

```python
# streaming/metrics.py
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry

# Create metrics registry
registry = CollectorRegistry()

# Counters
tasks_started = Counter(
    'streaming_tasks_started_total',
    'Total number of tasks started',
    ['app', 'model'],
    registry=registry
)

tasks_completed = Counter(
    'streaming_tasks_completed_total',
    'Total number of tasks completed',
    ['app', 'model'],
    registry=registry
)

tasks_failed = Counter(
    'streaming_tasks_failed_total',
    'Total number of tasks failed',
    ['app', 'model', 'error_type'],
    registry=registry
)

# Histograms
task_duration = Histogram(
    'streaming_task_duration_seconds',
    'Task execution duration',
    ['app', 'model'],
    registry=registry
)

# Gauges
active_tasks = Gauge(
    'streaming_active_tasks',
    'Number of currently running tasks',
    ['app', 'model'],
    registry=registry
)

active_clients = Gauge(
    'streaming_active_clients',
    'Number of connected SSE clients',
    registry=registry
)


# coordinator.py
from streaming.metrics import (
    tasks_started, tasks_completed, tasks_failed,
    task_duration, active_tasks
)

async def start_task(self, task_instance, app_name, model_name):
    # ... existing code ...

    # Increment metrics
    tasks_started.labels(app=app_name, model=model_name).inc()
    active_tasks.labels(app=app_name, model=model_name).inc()

async def _run_task(self, task_instance, task_key):
    app_name, model_name, task_id = task_key.split(':')
    start_time = time.time()

    try:
        # ... run task ...

        # Record success metrics
        duration = time.time() - start_time
        task_duration.labels(app=app_name, model=model_name).observe(duration)
        tasks_completed.labels(app=app_name, model=model_name).inc()

    except Exception as e:
        # Record failure metrics
        tasks_failed.labels(
            app=app_name,
            model=model_name,
            error_type=type(e).__name__
        ).inc()
        raise

    finally:
        # Decrement active tasks
        active_tasks.labels(app=app_name, model=model_name).dec()


# Add metrics endpoint to server
# server.py
from prometheus_client import generate_latest

async def metrics_handler(request):
    """Prometheus metrics endpoint"""
    from streaming.metrics import registry

    if request.path == '/metrics':
        metrics_output = generate_latest(registry)
        return 200, {'Content-Type': 'text/plain'}, metrics_output

    return None

async def main_handler(request):
    # Check metrics first
    metrics_response = await metrics_handler(request)
    if metrics_response:
        return metrics_response

    # ... rest of handlers ...
```

### Fix 8.3: Enhanced Health Check

```python
# server.py
async def health_check(request):
    """Enhanced health check with system status"""
    if request.path != '/health':
        return None

    from streaming.coordinator import coordinator
    from django.db import connection

    health_data = {
        'status': 'healthy',
        'timestamp': timezone.now().isoformat(),
        'checks': {}
    }

    # Check database
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        health_data['checks']['database'] = 'ok'
    except Exception as e:
        health_data['checks']['database'] = f'error: {str(e)}'
        health_data['status'] = 'unhealthy'

    # Check coordinator
    try:
        task_count = len(coordinator._tasks)
        health_data['checks']['coordinator'] = 'ok'
        health_data['active_tasks'] = task_count
    except Exception as e:
        health_data['checks']['coordinator'] = f'error: {str(e)}'
        health_data['status'] = 'unhealthy'

    # Check Redis (if used)
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL, socket_connect_timeout=1)
        r.ping()
        health_data['checks']['redis'] = 'ok'
    except Exception as e:
        health_data['checks']['redis'] = f'error: {str(e)}'
        # Redis is optional, don't mark unhealthy

    status_code = 200 if health_data['status'] == 'healthy' else 503

    return status_code, {'Content-Type': 'application/json'}, json.dumps(health_data)
```

### Fix 8.4: Add Admin/Stats Endpoint

```python
# server.py
async def stats_handler(request):
    """Admin endpoint for system statistics"""
    if request.path != '/stats':
        return None

    # Require authentication (see Issue #6)
    # ... auth check ...

    from streaming.coordinator import coordinator

    stats = {
        'timestamp': timezone.now().isoformat(),
        'tasks': {
            'active': len(coordinator._tasks),
            'cached_instances': len(coordinator._task_instances),
            'locks': len(coordinator._locks),
        },
        'tasks_by_status': {},
    }

    # Count tasks by type
    for task_key, task in coordinator._tasks.items():
        app, model, task_id = task_key.split(':')
        model_key = f"{app}.{model}"

        if model_key not in stats['tasks_by_status']:
            stats['tasks_by_status'][model_key] = {
                'running': 0,
                'done': 0,
            }

        if task.done():
            stats['tasks_by_status'][model_key]['done'] += 1
        else:
            stats['tasks_by_status'][model_key]['running'] += 1

    # Get task instances with client counts
    stats['client_connections'] = {}
    for task_key, instance in coordinator._task_instances.items():
        app, model, task_id = task_key.split(':')
        model_key = f"{app}.{model}"

        if model_key not in stats['client_connections']:
            stats['client_connections'][model_key] = 0

        stats['client_connections'][model_key] += len(instance._clients)

    return 200, {'Content-Type': 'application/json'}, json.dumps(stats)
```

### Fix 8.5: Add Distributed Tracing

```python
# streaming/tracing.py
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.jaeger.thrift import JaegerExporter

# Setup tracing
trace.set_tracer_provider(TracerProvider())
tracer = trace.get_tracer(__name__)

jaeger_exporter = JaegerExporter(
    agent_host_name='localhost',
    agent_port=6831,
)

trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(jaeger_exporter)
)


# coordinator.py
from streaming.tracing import tracer

async def _run_task(self, task_instance, task_key):
    app_name, model_name, task_id = task_key.split(':')

    # Create trace span
    with tracer.start_as_current_span(
        'task.process',
        attributes={
            'task.id': task_id,
            'task.app': app_name,
            'task.model': model_name,
        }
    ) as span:
        try:
            final_value = await task_instance.process()
            span.set_attribute('task.status', 'completed')
            span.set_attribute('task.final_value', str(final_value))
        except Exception as e:
            span.set_attribute('task.status', 'failed')
            span.set_attribute('task.error', str(e))
            span.record_exception(e)
            raise
```

## Testing

```python
async def test_metrics_recorded(self):
    """Test that metrics are properly recorded"""
    from streaming.metrics import tasks_started, tasks_completed

    initial_started = tasks_started.labels(app='tests', model='ExampleTask')._value.get()

    task = await ExampleTask.objects.acreate()
    await coordinator.start_task(task, 'tests', 'ExampleTask')
    await asyncio.sleep(0.1)

    final_started = tasks_started.labels(app='tests', model='ExampleTask')._value.get()
    self.assertEqual(final_started, initial_started + 1)

async def test_health_check_endpoint(self):
    """Test enhanced health check"""
    async with httpx.AsyncClient() as client:
        response = await client.get('http://127.0.0.1:8888/health')
        self.assertEqual(response.status_code, 200)

        data = response.json()
        self.assertEqual(data['status'], 'healthy')
        self.assertIn('database', data['checks'])
        self.assertIn('coordinator', data['checks'])
```

## Configuration

```python
# settings.py
STREAMING_METRICS_ENABLED = True
STREAMING_TRACING_ENABLED = True
STREAMING_JAEGER_HOST = 'localhost'
STREAMING_JAEGER_PORT = 6831

STREAMING_LOG_LEVEL = 'INFO'
STREAMING_LOG_FILE = 'logs/streaming.log'
```

## Related Issues

- #2 Memory Leaks (metrics help detect memory issues)
- #4 Error Handling (need to track error rates)
- #6 Authentication (audit logs track access)
