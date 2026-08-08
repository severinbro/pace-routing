from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('data_cube', '0006_delete_relativeimportancesurvey'),
    ]

    operations = [
        # Rename GNSSMeasurement -> GNSSPhoneMeasurement (keeps existing data)
        migrations.RenameModel(
            old_name='GNSSMeasurement',
            new_name='GNSSPhoneMeasurement',
        ),

        # Add the new GNSSSensorMeasurement table (SAM-M10Q via I2C)
        migrations.CreateModel(
            name='GNSSSensorMeasurement',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('timestamp', models.DateTimeField(auto_now_add=True)),
                ('latitude', models.DecimalField(decimal_places=9, max_digits=12)),
                ('longitude', models.DecimalField(decimal_places=9, max_digits=12)),
                ('altitude', models.FloatField(null=True)),
                ('satellites', models.IntegerField(default=0)),
            ],
        ),
    ]
