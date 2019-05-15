# -*- coding: utf-8 -*-

# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, you can obtain one at http://mozilla.org/MPL/2.0/.

# Generated by Django 1.11.7 on 2017-12-01 20:07
from __future__ import unicode_literals

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('upload', '0015_auto_20171130_2048'),
    ]

    operations = [
        migrations.AddField(
            model_name='upload',
            name='try_symbols',
            field=models.BooleanField(default=False),
        ),
    ]
