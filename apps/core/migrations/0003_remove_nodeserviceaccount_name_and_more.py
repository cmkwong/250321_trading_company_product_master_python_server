# Re-created to match the ``core 0003_remove_nodeserviceaccount_name_and_more``
# row already recorded in django_migrations (the original file was lost), so the
# migration state agrees with both the database and apps/core/models.py.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0002_nodeserviceaccount'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='nodeserviceaccount',
            name='name',
        ),
        migrations.AddField(
            model_name='nodeserviceaccount',
            name='first_name',
            field=models.CharField(default='', max_length=100),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name='nodeserviceaccount',
            name='last_name',
            field=models.CharField(default='', max_length=100),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name='nodeserviceaccount',
            name='email',
            field=models.EmailField(max_length=254, unique=True),
        ),
    ]
