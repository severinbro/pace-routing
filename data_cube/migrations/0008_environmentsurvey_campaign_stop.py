from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('data_cube', '0007_gnss_phone_sensor_split'),
    ]

    operations = [
        migrations.AddField(
            model_name='environmentsurvey',
            name='campaign_stop',
            field=models.IntegerField(blank=True, null=True),
        ),
    ]
