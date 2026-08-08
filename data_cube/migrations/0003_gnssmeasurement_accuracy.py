from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('data_cube', '0002_noisemeasurement_particulatemeasurement'),
    ]

    operations = [
        migrations.AddField(
            model_name='gnssmeasurement',
            name='accuracy',
            field=models.FloatField(default=0.0, help_text='Position accuracy in meters (from phone GPS)'),
        ),
    ]
