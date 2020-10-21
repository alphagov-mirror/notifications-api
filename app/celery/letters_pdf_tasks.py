from datetime import datetime, timedelta
from hashlib import sha512
from base64 import urlsafe_b64encode

from botocore.exceptions import ClientError as BotoClientError
from flask import current_app
from notifications_utils.postal_address import PostalAddress

from notifications_utils.statsd_decorators import statsd
from notifications_utils.letter_timings import LETTER_PROCESSING_DEADLINE
from notifications_utils.timezones import convert_utc_to_bst

from app import encryption, notify_celery
from app.aws import s3
from app.config import QueueNames, TaskNames
from app.dao.notifications_dao import (
    get_notification_by_id,
    update_notification_status_by_id,
    dao_update_notification,
    dao_get_notification_by_reference,
    dao_update_notifications_by_reference,
    dao_get_letters_to_be_printed,
)
from app.letters.utils import get_letter_pdf_filename
from app.errors import VirusScanError
from app.exceptions import NotificationTechnicalFailureException
from app.letters.utils import (
    get_billable_units_for_letter_page_count,
    get_reference_from_filename,
    ScanErrorType,
    move_failed_pdf,
    move_sanitised_letter_to_test_or_live_pdf_bucket,
    move_scan_to_invalid_pdf_bucket,
    move_error_pdf_to_scan_bucket,
    get_file_names_from_error_bucket,
)
from app.models import (
    INTERNATIONAL_LETTERS,
    KEY_TYPE_TEST,
    NOTIFICATION_CREATED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_PENDING_VIRUS_CHECK,
    NOTIFICATION_TECHNICAL_FAILURE,
    NOTIFICATION_VALIDATION_FAILED,
    NOTIFICATION_VIRUS_SCAN_FAILED,
    POSTAGE_TYPES,
    RESOLVE_POSTAGE_FOR_FILE_NAME,
    INTERNATIONAL_POSTAGE_TYPES)
from app.cronitor import cronitor


@notify_celery.task(bind=True, name="create-letters-pdf", max_retries=15, default_retry_delay=300)
@statsd(namespace="tasks")
def create_letters_pdf(self, notification_id):
    get_pdf_for_templated_letter(notification_id)


@notify_celery.task(bind=True, name="get-pdf-for-templated-letter", max_retries=15, default_retry_delay=300)
@statsd(namespace="tasks")
def get_pdf_for_templated_letter(self, notification_id):
    try:
        notification = get_notification_by_id(notification_id, _raise=True)
        letter_filename = get_letter_pdf_filename(
            reference=notification.reference,
            crown=notification.service.crown,
            created_at=notification.created_at,
            ignore_folder=notification.key_type == KEY_TYPE_TEST,
            postage=notification.postage
        )
        letter_data = {
            'letter_contact_block': notification.reply_to_text,
            'template': {
                "subject": notification.template.subject,
                "content": notification.template.content,
                "template_type": notification.template.template_type
            },
            'values': notification.personalisation,
            'logo_filename': notification.service.letter_branding and notification.service.letter_branding.filename,
            'letter_filename': letter_filename,
            "notification_id": str(notification_id),
            'key_type': notification.key_type
        }

        encrypted_data = encryption.encrypt(letter_data)

        notify_celery.send_task(
            name=TaskNames.CREATE_PDF_FOR_TEMPLATED_LETTER,
            args=(encrypted_data,),
            queue=QueueNames.SANITISE_LETTERS
        )
    except Exception:
        try:
            current_app.logger.exception(
                f"RETRY: calling create-letter-pdf task for notification {notification_id} failed"
            )
            self.retry(queue=QueueNames.RETRY)
        except self.MaxRetriesExceededError:
            message = f"RETRY FAILED: Max retries reached. " \
                      f"The task create-letter-pdf failed for notification id {notification_id}. " \
                      f"Notification has been updated to technical-failure"
            update_notification_status_by_id(notification_id, NOTIFICATION_TECHNICAL_FAILURE)
            raise NotificationTechnicalFailureException(message)


@notify_celery.task(bind=True, name="update-billable-units-for-letter", max_retries=15, default_retry_delay=300)
@statsd(namespace="tasks")
def update_billable_units_for_letter(self, notification_id, page_count):
    notification = get_notification_by_id(notification_id, _raise=True)

    billable_units = get_billable_units_for_letter_page_count(page_count)

    if notification.key_type != KEY_TYPE_TEST:
        notification.billable_units = billable_units
        dao_update_notification(notification)

        current_app.logger.info(
            f"Letter notification id: {notification_id} reference {notification.reference}: "
            f"billable units set to {billable_units}"
        )


@notify_celery.task(name='collate-letter-pdfs-to-be-sent')
@cronitor("collate-letter-pdfs-to-be-sent")
def collate_letter_pdfs_to_be_sent():
    """
    Finds all letters which are still waiting to be sent to DVLA for printing

    This would usually be run at 5.50pm and collect up letters created between before 5:30pm today
    that have not yet been sent.
    If run after midnight, it will collect up letters created before 5:30pm the day before.
    """
    current_app.logger.info("starting collate-letter-pdfs-to-be-sent")
    print_run_date = convert_utc_to_bst(datetime.utcnow())
    if print_run_date.time() < LETTER_PROCESSING_DEADLINE:
        print_run_date = print_run_date - timedelta(days=1)

    print_run_deadline = print_run_date.replace(
        hour=17, minute=30, second=0, microsecond=0
    )
    for postage in POSTAGE_TYPES:
        current_app.logger.info(f"starting collate-letter-pdfs-to-be-sent processing for postage class {postage}")
        letters_to_print = get_key_and_size_of_letters_to_be_sent_to_print(print_run_deadline, postage)

        for i, letters in enumerate(group_letters(letters_to_print)):
            filenames = [letter['Key'] for letter in letters]
            service_id = letters[0]['ServiceId']

            hash = urlsafe_b64encode(sha512(''.join(filenames).encode()).digest())[:20].decode()
            # eg NOTIFY.2018-12-31.001.Wjrui5nAvObjPd-3GEL-.ZIP
            dvla_filename = 'NOTIFY.{date}.{postage}.{num:03}.{hash}.{service_id}.ZIP'.format(
                date=print_run_deadline.strftime("%Y-%m-%d"),
                postage=RESOLVE_POSTAGE_FOR_FILE_NAME[postage],
                num=i + 1,
                hash=hash,
                service_id=service_id
            )

            current_app.logger.info(
                'Calling task zip-and-send-letter-pdfs for {} pdfs to upload {} with total size {:,} bytes'.format(
                    len(filenames),
                    dvla_filename,
                    sum(letter['Size'] for letter in letters)
                )
            )
            notify_celery.send_task(
                name=TaskNames.ZIP_AND_SEND_LETTER_PDFS,
                kwargs={
                    'filenames_to_zip': filenames,
                    'upload_filename': dvla_filename
                },
                queue=QueueNames.PROCESS_FTP,
                compression='zlib'
            )
        current_app.logger.info(f"finished collate-letter-pdfs-to-be-sent processing for postage class {postage}")

    current_app.logger.info("finished collate-letter-pdfs-to-be-sent")


def get_key_and_size_of_letters_to_be_sent_to_print(print_run_deadline, postage):
    letters_awaiting_sending = dao_get_letters_to_be_printed(print_run_deadline, postage)
    letter_pdfs = []
    for letter in letters_awaiting_sending:
        try:
            letter_file_name = get_letter_pdf_filename(
                reference=letter.reference,
                crown=letter.service.crown,
                created_at=letter.created_at,
                postage=letter.postage
            )
            letter_head = s3.head_s3_object(current_app.config['LETTERS_PDF_BUCKET_NAME'], letter_file_name)
            letter_pdfs.append({
                "Key": letter_file_name,
                "Size": letter_head['ContentLength'],
                "ServiceId": str(letter.service.id)
            })
        except BotoClientError as e:
            current_app.logger.exception(
                f"Error getting letter from bucket for notification: {letter.id} with reference: {letter.reference}", e)

    return letter_pdfs


def group_letters(letter_pdfs):
    """
    Group letters in chunks of MAX_LETTER_PDF_ZIP_FILESIZE. Will add files to lists, never going over that size.
    If a single file is (somehow) larger than MAX_LETTER_PDF_ZIP_FILESIZE that'll be in a list on it's own.
    If there are no files, will just exit (rather than yielding an empty list).
    """
    running_filesize = 0
    list_of_files = []
    service_id = None
    for letter in letter_pdfs:
        if letter['Key'].lower().endswith('.pdf'):
            if not service_id:
                service_id = letter['ServiceId']
            if (
                running_filesize + letter['Size'] > current_app.config['MAX_LETTER_PDF_ZIP_FILESIZE']
                or len(list_of_files) >= current_app.config['MAX_LETTER_PDF_COUNT_PER_ZIP']
                or letter['ServiceId'] != service_id
            ):
                yield list_of_files
                running_filesize = 0
                list_of_files = []
                service_id = None

            if not service_id:
                service_id = letter['ServiceId']
            running_filesize += letter['Size']
            list_of_files.append(letter)

    if list_of_files:
        yield list_of_files


@notify_celery.task(bind=True, name='sanitise-letter', max_retries=15, default_retry_delay=300)
def sanitise_letter(self, filename):
    try:
        reference = get_reference_from_filename(filename)
        notification = dao_get_notification_by_reference(reference)

        current_app.logger.info('Notification ID {} Virus scan passed: {}'.format(notification.id, filename))

        if notification.status != NOTIFICATION_PENDING_VIRUS_CHECK:
            current_app.logger.info('Sanitise letter called for notification {} which is in {} state'.format(
                notification.id, notification.status))
            return

        notify_celery.send_task(
            name=TaskNames.SANITISE_LETTER,
            kwargs={
                'notification_id': str(notification.id),
                'filename': filename,
                'allow_international_letters': notification.service.has_permission(
                    INTERNATIONAL_LETTERS
                ),
            },
            queue=QueueNames.SANITISE_LETTERS,
        )
    except Exception:
        try:
            current_app.logger.exception(
                "RETRY: calling sanitise_letter task for notification {} failed".format(notification.id)
            )
            self.retry(queue=QueueNames.RETRY)
        except self.MaxRetriesExceededError:
            message = "RETRY FAILED: Max retries reached. " \
                      "The task sanitise_letter failed for notification {}. " \
                      "Notification has been updated to technical-failure".format(notification.id)
            update_notification_status_by_id(notification.id, NOTIFICATION_TECHNICAL_FAILURE)
            raise NotificationTechnicalFailureException(message)


@notify_celery.task(bind=True, name='process-sanitised-letter', max_retries=15, default_retry_delay=300)
def process_sanitised_letter(self, sanitise_data):
    letter_details = encryption.decrypt(sanitise_data)

    filename = letter_details['filename']
    notification_id = letter_details['notification_id']

    current_app.logger.info('Processing sanitised letter with id {}'.format(notification_id))
    notification = get_notification_by_id(notification_id, _raise=True)

    if notification.status != NOTIFICATION_PENDING_VIRUS_CHECK:
        current_app.logger.info(
            'process-sanitised-letter task called for notification {} which is in {} state'.format(
                notification.id, notification.status)
        )
        return

    try:
        original_pdf_object = s3.get_s3_object(current_app.config['LETTERS_SCAN_BUCKET_NAME'], filename)

        if letter_details['validation_status'] == 'failed':
            current_app.logger.info('Processing invalid precompiled pdf with id {} (file {})'.format(
                notification_id, filename))

            _move_invalid_letter_and_update_status(
                notification=notification,
                filename=filename,
                scan_pdf_object=original_pdf_object,
                message=letter_details['message'],
                invalid_pages=letter_details['invalid_pages'],
                page_count=letter_details['page_count'],
            )
            return

        current_app.logger.info('Processing valid precompiled pdf with id {} (file {})'.format(
            notification_id, filename))

        billable_units = get_billable_units_for_letter_page_count(letter_details['page_count'])
        is_test_key = notification.key_type == KEY_TYPE_TEST

        # Updating the notification needs to happen before the file is moved. This is so that if updating the
        # notification fails, the task can retry because the file is in the same place.
        update_letter_pdf_status(
            reference=notification.reference,
            status=NOTIFICATION_DELIVERED if is_test_key else NOTIFICATION_CREATED,
            billable_units=billable_units,
            recipient_address=letter_details['address']
        )

        # The original filename could be wrong because we didn't know the postage.
        # Now we know if the letter is international, we can check what the filename should be.
        upload_file_name = get_letter_pdf_filename(
            reference=notification.reference,
            crown=notification.service.crown,
            created_at=notification.created_at,
            ignore_folder=True,
            postage=notification.postage
        )

        move_sanitised_letter_to_test_or_live_pdf_bucket(
            filename,
            is_test_key,
            notification.created_at,
            upload_file_name,
        )
        # We've moved the sanitised PDF from the sanitise bucket, but still need to delete the original file:
        original_pdf_object.delete()

    except BotoClientError:
        # Boto exceptions are likely to be caused by the file(s) being in the wrong place, so retrying won't help -
        # we'll need to manually investigate
        current_app.logger.exception(
            f"Boto error when processing sanitised letter for notification {notification.id} (file {filename})"
        )
        update_notification_status_by_id(notification.id, NOTIFICATION_TECHNICAL_FAILURE)
        raise NotificationTechnicalFailureException
    except Exception:
        try:
            current_app.logger.exception(
                "RETRY: calling process_sanitised_letter task for notification {} failed".format(notification.id)
            )
            self.retry(queue=QueueNames.RETRY)
        except self.MaxRetriesExceededError:
            message = "RETRY FAILED: Max retries reached. " \
                      "The task process_sanitised_letter failed for notification {}. " \
                      "Notification has been updated to technical-failure".format(notification.id)
            update_notification_status_by_id(notification.id, NOTIFICATION_TECHNICAL_FAILURE)
            raise NotificationTechnicalFailureException(message)


def _move_invalid_letter_and_update_status(
    *, notification, filename, scan_pdf_object, message=None, invalid_pages=None, page_count=None
):
    try:
        move_scan_to_invalid_pdf_bucket(
            source_filename=filename,
            message=message,
            invalid_pages=invalid_pages,
            page_count=page_count
        )
        scan_pdf_object.delete()

        update_letter_pdf_status(
            reference=notification.reference,
            status=NOTIFICATION_VALIDATION_FAILED,
            billable_units=0)
    except BotoClientError:
        current_app.logger.exception(
            "Error when moving letter with id {} to invalid PDF bucket".format(notification.id)
        )
        update_notification_status_by_id(notification.id, NOTIFICATION_TECHNICAL_FAILURE)
        raise NotificationTechnicalFailureException


@notify_celery.task(name='process-virus-scan-failed')
def process_virus_scan_failed(filename):
    move_failed_pdf(filename, ScanErrorType.FAILURE)
    reference = get_reference_from_filename(filename)
    notification = dao_get_notification_by_reference(reference)
    updated_count = update_letter_pdf_status(reference, NOTIFICATION_VIRUS_SCAN_FAILED, billable_units=0)

    if updated_count != 1:
        raise Exception(
            "There should only be one letter notification for each reference. Found {} notifications".format(
                updated_count
            )
        )

    error = VirusScanError('notification id {} Virus scan failed: {}'.format(notification.id, filename))
    current_app.logger.exception(error)
    raise error


@notify_celery.task(name='process-virus-scan-error')
def process_virus_scan_error(filename):
    move_failed_pdf(filename, ScanErrorType.ERROR)
    reference = get_reference_from_filename(filename)
    notification = dao_get_notification_by_reference(reference)
    updated_count = update_letter_pdf_status(reference, NOTIFICATION_TECHNICAL_FAILURE, billable_units=0)

    if updated_count != 1:
        raise Exception(
            "There should only be one letter notification for each reference. Found {} notifications".format(
                updated_count
            )
        )
    error = VirusScanError('notification id {} Virus scan error: {}'.format(notification.id, filename))
    current_app.logger.exception(error)
    raise error


def update_letter_pdf_status(reference, status, billable_units, recipient_address=None):
    postage = None
    if recipient_address:
        # fix allow_international_letters
        postage = PostalAddress(raw_address=recipient_address.replace(',', '\n'),
                                allow_international_letters=True
                                ).postage
        postage = postage if postage in INTERNATIONAL_POSTAGE_TYPES else None
    update_dict = {'status': status, 'billable_units': billable_units, 'updated_at': datetime.utcnow()}
    if postage:
        update_dict.update({'postage': postage, 'international': True})
    if recipient_address:
        update_dict['to'] = recipient_address
    return dao_update_notifications_by_reference(
        references=[reference],
        update_dict=update_dict)[0]


def replay_letters_in_error(filename=None):
    # This method can be used to replay letters that end up in the ERROR directory.
    # We had an incident where clamAV was not processing the virus scan.
    if filename:
        move_error_pdf_to_scan_bucket(filename)
        # call task to add the filename to anti virus queue
        current_app.logger.info("Calling scan_file for: {}".format(filename))

        if current_app.config['ANTIVIRUS_ENABLED']:
            notify_celery.send_task(
                name=TaskNames.SCAN_FILE,
                kwargs={'filename': filename},
                queue=QueueNames.ANTIVIRUS,
            )
        else:
            # stub out antivirus in dev
            sanitise_letter.apply_async(
                [filename],
                queue=QueueNames.LETTERS
            )
    else:
        error_files = get_file_names_from_error_bucket()
        for item in error_files:
            moved_file_name = item.key.split('/')[1]
            current_app.logger.info("Calling scan_file for: {}".format(moved_file_name))
            move_error_pdf_to_scan_bucket(moved_file_name)
            # call task to add the filename to anti virus queue
            if current_app.config['ANTIVIRUS_ENABLED']:
                notify_celery.send_task(
                    name=TaskNames.SCAN_FILE,
                    kwargs={'filename': moved_file_name},
                    queue=QueueNames.ANTIVIRUS,
                )
            else:
                # stub out antivirus in dev
                sanitise_letter.apply_async(
                    [filename],
                    queue=QueueNames.LETTERS
                )
