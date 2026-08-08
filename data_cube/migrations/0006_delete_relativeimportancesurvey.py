from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('data_cube', '0005_environmentsurvey_7point_3phase'),
    ]

    operations = [
        migrations.DeleteModel(
            name='RelativeImportanceSurvey',
        ),
    ]
