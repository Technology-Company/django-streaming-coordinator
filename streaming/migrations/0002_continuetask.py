

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('streaming', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='ContinueTask',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('completed_at', models.DateTimeField(blank=True, null=True)),
                ('continue_field', models.BooleanField(db_column='continue', default=False)),
                ('message', models.CharField(default='Waiting for continue signal', max_length=255)),
            ],
            options={
                'abstract': False,
            },
        ),
    ]
