# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, you can obtain one at http://mozilla.org/MPL/2.0/.

import datetime
import re
import logging
import io
import fnmatch
import hashlib
import zipfile
import time
import os

import requests
from botocore.exceptions import ClientError
import markus

from django import http
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.core.exceptions import ImproperlyConfigured
from django.views.decorators.csrf import csrf_exempt
from django.core.cache import cache

from tecken.base.utils import filesizeformat
from tecken.base.decorators import (
    api_login_required,
    api_permission_required,
    api_require_POST
)
from tecken.upload.utils import (
    get_archive_members,
    UnrecognizedArchiveFileExtension,
)
from tecken.upload.models import Upload
from tecken.upload.tasks import upload_inbox_upload
from tecken.upload.forms import UploadByDownloadForm
from tecken.s3 import S3Bucket


logger = logging.getLogger('tecken')
metrics = markus.get_metrics('tecken')


_not_hex_characters = re.compile(r'[^a-f0-9]', re.I)


def check_symbols_archive_file_listing(file_listings):
    """return a string (the error) if there was something not as expected"""
    for file_listing in file_listings:
        for snippet in settings.DISALLOWED_SYMBOLS_SNIPPETS:
            if snippet in file_listing.name:
                return (
                    f"Content of archive file contains the snippet "
                    f"'{snippet}' which is not allowed"
                )
        # Now check that the filename is matching according to these rules:
        # 1. Either /<name1>/hex/<name2>,
        # 2. Or, /<name>-symbols.txt
        # Anything else should be considered and unrecognized file pattern
        # and thus rejected.
        split = file_listing.name.split('/')
        if len(split) == 3:
            # check that the middle part is only hex characters
            if not _not_hex_characters.findall(split[1]):
                continue
        elif len(split) == 1:
            if file_listing.name.lower().endswith('-symbols.txt'):
                continue
        # If it didn't get "continued" above, it's an unrecognized file
        # pattern.
        return (
            'Unrecognized file pattern. Should only be <module>/<hex>/<file> '
            'or <name>-symbols.txt and nothing else.'
        )


def get_bucket_info(user):
    """return an object that has 'bucket', 'endpoint_url',
    'region'.
    Only 'bucket' is mandatory in the response object.
    """
    url = settings.UPLOAD_DEFAULT_URL
    exceptions = settings.UPLOAD_URL_EXCEPTIONS
    if user.email.lower() in exceptions:
        # easy
        exception = exceptions[user.email.lower()]
    else:
        # match against every possible wildcard
        exception = None  # assume no match
        for email_or_wildcard in settings.UPLOAD_URL_EXCEPTIONS:
            if fnmatch.fnmatch(user.email.lower(), email_or_wildcard.lower()):
                # a match!
                exception = settings.UPLOAD_URL_EXCEPTIONS[
                    email_or_wildcard
                ]
                break

    if exception:
        url = exception

    return S3Bucket(url)


@metrics.timer_decorator('upload_archive')
@api_require_POST
@csrf_exempt
@api_login_required
@api_permission_required('upload.upload_symbols')
@transaction.atomic
def upload_archive(request):
    for name in request.FILES:
        upload = request.FILES[name]
        size = upload.size
        url = None
        break
    else:
        if request.POST.get('url'):
            form = UploadByDownloadForm(request.POST)
            if form.is_valid():
                url = form.cleaned_data['url']
                name = form.cleaned_data['upload']['name']
                size = form.cleaned_data['upload']['size']
                size_fmt = filesizeformat(size)
                logger.info(
                    f'Download to upload {url} ({size_fmt})'
                )
                upload = io.BytesIO(requests.get(url).content)
            else:
                for key, errors in form.errors.as_data().items():
                    return http.JsonResponse(
                        {'error': errors[0].message},
                        status=400,
                    )
        else:
            return http.JsonResponse(
                {
                    'error': (
                        'Must be multipart form data with at least one file'
                    )
                },
                status=400,
            )
    if not size:
        return http.JsonResponse(
            {'error': 'File size 0'},
            status=400
        )

    try:
        file_listing = list(get_archive_members(upload, name))
    except zipfile.BadZipfile as exception:
        return http.JsonResponse(
            {'error': str(exception)},
            status=400,
        )
    except UnrecognizedArchiveFileExtension as exception:
        return http.JsonResponse(
            {'error': f'Unrecognized archive file extension "{exception}"'},
            status=400,
        )
    error = check_symbols_archive_file_listing(file_listing)
    if error:
        return http.JsonResponse({'error': error.strip()}, status=400)

    # Even if we don't upload the "inbox file" into this bucket we need
    # to make sure the bucket exists here even before the actual
    # Celery job starts.
    bucket_info = get_bucket_info(request.user)
    try:
        bucket_info.s3_client.head_bucket(Bucket=bucket_info.name)
    except ClientError as exception:
        if exception.response['Error']['Code'] == '404':
            # This warning message hopefully makes it easier to see what
            # you need to do to your configuration.
            # XXX Is this the best exception for runtime'y type of
            # bad configurations.
            raise ImproperlyConfigured(
                "S3 bucket '{}' can not be found. "
                'Connected with region={!r} endpoint_url={!r}'.format(
                    bucket_info.name,
                    bucket_info.region,
                    bucket_info.endpoint_url,
                )
            )
        else:  # pragma: no cover
            raise
    # Turn the file listing into a string to turn it into a hash
    content = '\n'.join(
        '{}:{}'.format(x.name, x.size) for x in file_listing
    )
    # The MD5 is just used to make the temporary S3 file unique in name
    # if the client uploads with the same filename in quick succession.
    content_hash = hashlib.md5(
        content.encode('utf-8')
    ).hexdigest()[:12]  # nosec
    key = 'inbox/{date}/{content_hash}/{name}'.format(
        date=timezone.now().strftime('%Y-%m-%d'),
        content_hash=content_hash,
        name=name,
    )
    # Bundle the creation of the upload object with the task of uploading
    # the inbox file. If the latter fails, the Upload object creation
    # should be cancelled too.
    with transaction.atomic():
        # There's a potential fork of functionality here.
        # Either we use filesystem to store the inbox file,
        # or we use S3.
        # At some point we can probably simplify the code by picking
        # one strategy that we know works.
        if settings.UPLOAD_INBOX_DIRECTORY:
            inbox_filepath = os.path.join(
                settings.UPLOAD_INBOX_DIRECTORY, key
            )
            inbox_filedir = os.path.dirname(inbox_filepath)
            if not os.path.isdir(inbox_filedir):
                os.makedirs(inbox_filedir)
            upload_obj = Upload.objects.create(
                user=request.user,
                filename=name,
                inbox_filepath=inbox_filepath,
                bucket_name=bucket_info.name,
                bucket_region=bucket_info.region,
                bucket_endpoint_url=bucket_info.endpoint_url,
                size=size,
                download_url=url,
            )
            with metrics.timer('store_in_inbox_directory'):
                upload.seek(0)
                size_human = filesizeformat(size)
                logger.info(
                    f'About to store {inbox_filepath} ({size_human}) '
                    f'into {settings.UPLOAD_INBOX_DIRECTORY}'
                )
                t0 = time.time()
                with open(inbox_filepath, 'wb') as f:
                    f.write(upload.read())
                t1 = time.time()
                store_time = t1 - t0
                store_rate = size / store_time
                store_rate_human = filesizeformat(store_rate)
                logger.info(
                    f'Took {store_time:.3f} seconds to store {size_human} '
                    f'({store_rate_human}/second)'
                )
                # Now let's make sure the file really is there before
                # we carry on and trigger the Celery task.
                # This is only really ever applicable on network mounted
                # filesystems where consistency is important and writes
                # to disk might be async.
                attempts = 0
                while not os.path.isfile(inbox_filepath):
                    attempts += 1
                    time.sleep(attempts)
                    logger.info(
                        f"{inbox_filepath} apparently doesn't exist yet. "
                        f'Sleeping for {attempts} seconds to retry.'
                    )
                    if attempts > 4:
                        break
        else:
            upload_obj = Upload.objects.create(
                user=request.user,
                filename=name,
                inbox_key=key,
                bucket_name=bucket_info.name,
                bucket_region=bucket_info.region,
                bucket_endpoint_url=bucket_info.endpoint_url,
                size=size,
                download_url=url,
            )
            with metrics.timer('upload_to_inbox'):
                upload.seek(0)
                size_human = filesizeformat(size)
                logger.info(
                    f'About to upload {key} ({size_human}) '
                    f'into {bucket_info.name}'
                )
                t0 = time.time()
                bucket_info.s3_client.put_object(
                    Bucket=bucket_info.name,
                    Key=key,
                    Body=upload,
                )
                t1 = time.time()
                upload_time = t1 - t0
                upload_rate = size / upload_time
                upload_rate_human = filesizeformat(upload_rate)
                logger.info(
                    f'Took {upload_time:.3f} seconds to upload {size_human} '
                    f'({upload_rate_human}/second)'
                )
        logger.info(f'Upload object created with ID {upload_obj.id}')

    upload_inbox_upload.delay(upload_obj.id)

    # Take the opportunity to also try to clear out old uploads that
    # have gotten stuck and still hasn't moved for some reason.
    # Note! This might be better to do with a cron job some day. But that
    # requires a management command that can be run in production.
    incomplete_uploads = Upload.objects.filter(
        completed_at__isnull=True,
        cancelled_at__isnull=True,
        created_at__lt=(
            timezone.now() - datetime.timedelta(
                seconds=settings.UPLOAD_REATTEMPT_LIMIT_SECONDS
            )
        ),
        attempts__lt=settings.UPLOAD_REATTEMPT_LIMIT_TIMES
    )
    for old_upload_obj in incomplete_uploads.order_by('created_at'):
        cache_key = f'reattempt:{old_upload_obj.id}'
        if not cache.get(cache_key):
            logger.info(
                f'Reattempting incomplete upload from {old_upload_obj!r}'
            )
            upload_inbox_upload.delay(old_upload_obj.id)
            cache.set(
                cache_key,
                True,
                settings.UPLOAD_REATTEMPT_LIMIT_SECONDS
            )

    return http.JsonResponse(
        {'upload': _serialize_upload(upload_obj)},
        status=201,
    )


def _serialize_upload(upload):
    return {
        'id': upload.id,
        'size': upload.size,
        'filename': upload.filename,
        'bucket': upload.bucket_name,
        'region': upload.bucket_region,
        'download_url': upload.download_url,
        'completed_at': upload.completed_at,
        'created_at': upload.created_at,
        'user': upload.user.email,
        'skipped_keys': upload.skipped_keys or [],
    }
