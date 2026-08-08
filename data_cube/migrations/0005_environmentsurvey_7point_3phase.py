from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('data_cube', '0004_survey_user_fields'),
    ]

    operations = [
        # Phase 1: q1-q7 already exist (were 5-point, now 7-point — alter validators)
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q1',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q2',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q3',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q4',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q5',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q6',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q7',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        # Phase 2: q8-q10 already exist (were 5-point, now 7-point — alter validators)
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q8',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q9',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        migrations.AlterField(
            model_name='environmentsurvey',
            name='q10',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
        # Phase 3: new q11 field
        migrations.AddField(
            model_name='environmentsurvey',
            name='q11',
            field=models.IntegerField(validators=[django.core.validators.MinValueValidator(1), django.core.validators.MaxValueValidator(7)]),
        ),
    ]
