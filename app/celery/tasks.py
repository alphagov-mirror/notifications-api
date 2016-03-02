from app import create_uuid
from app import notify_celery, encryption, firetext_client, aws_ses_client
from app.clients.email.aws_ses import AwsSesClientException
from app.clients.sms.firetext import FiretextClientException
from app.dao.services_dao import dao_fetch_service_by_id
from app.dao.templates_dao import dao_get_template_by_id
from app.dao.notifications_dao import dao_create_notification, dao_update_notification
from app.dao.jobs_dao import dao_update_job, dao_get_job_by_id
from app.models import Notification
from flask import current_app
from sqlalchemy.exc import SQLAlchemyError
from app.aws import s3
from datetime import datetime
from utils.template import Template
from utils.process_csv import get_rows_from_csv, get_recipient_from_row, first_column_heading


@notify_celery.task(name="process-job")
def process_job(job_id):
    start = datetime.utcnow()
    job = dao_get_job_by_id(job_id)
    job.status = 'in progress'
    dao_update_job(job)

    file = s3.get_job_from_s3(job.bucket_name, job_id)

    for row in get_rows_from_csv(file):

        encrypted = encryption.encrypt({
            'template': job.template_id,
            'job': str(job.id),
            'to': get_recipient_from_row(row, job.template.template_type),
            'personalisation': row
        })

        if job.template.template_type == 'sms':
            send_sms.apply_async((
                str(job.service_id),
                str(create_uuid()),
                encrypted,
                str(datetime.utcnow())),
                queue='bulk-sms'
            )

        if job.template.template_type == 'email':
            send_email.apply_async((
                str(job.service_id),
                str(create_uuid()),
                job.template.subject,
                "{}@{}".format(job.service.email_from, current_app.config['NOTIFY_EMAIL_DOMAIN']),
                encrypted,
                str(datetime.utcnow())),
                queue='bulk-email')

    finished = datetime.utcnow()
    job.status = 'finished'
    job.processing_started = start
    job.processing_finished = finished
    dao_update_job(job)
    current_app.logger.info(
        "Job {} created at {} started at {} finished at {}".format(job_id, job.created_at, start, finished)
    )


@notify_celery.task(name="send-sms")
def send_sms(service_id, notification_id, encrypted_notification, created_at):
    notification = encryption.decrypt(encrypted_notification)
    service = dao_fetch_service_by_id(service_id)
    template = Template(
        dao_get_template_by_id(notification['template']).__dict__,
        values=notification.get('personalisation', {}),
        prefix=service.name
    )

    client = firetext_client

    try:
        sent_at = datetime.utcnow()
        notification_db_object = Notification(
            id=notification_id,
            template_id=notification['template'],
            to=notification['to'],
            service_id=service_id,
            job_id=notification.get('job', None),
            status='sent',
            created_at=created_at,
            sent_at=sent_at,
            sent_by=client.get_name()
        )
        dao_create_notification(notification_db_object)

        try:
            client.send_sms(
                notification['to'],
                template.replaced
            )
        except FiretextClientException as e:
            current_app.logger.exception(e)
            notification_db_object.status = 'failed'
            dao_update_notification(notification_db_object)

        current_app.logger.info(
            "SMS {} created at {} sent at {}".format(notification_id, created_at, sent_at)
        )
    except SQLAlchemyError as e:
        current_app.logger.debug(e)


@notify_celery.task(name="send-email")
def send_email(service_id, notification_id, subject, from_address, encrypted_notification, created_at):
    notification = encryption.decrypt(encrypted_notification)
    template = Template(
        dao_get_template_by_id(notification['template']).__dict__,
        values=notification.get('personalisation', {})
    )

    client = aws_ses_client

    try:
        sent_at = datetime.utcnow()
        notification_db_object = Notification(
            id=notification_id,
            template_id=notification['template'],
            to=notification['to'],
            service_id=service_id,
            job_id=notification.get('job', None),
            status='sent',
            created_at=created_at,
            sent_at=sent_at,
            sent_by=client.get_name()
        )
        dao_create_notification(notification_db_object)

        try:
            client.send_email(
                from_address,
                notification['to'],
                subject,
                template.replaced
            )
        except AwsSesClientException as e:
            current_app.logger.debug(e)
            notification_db_object.status = 'failed'
            dao_update_notification(notification_db_object)

        current_app.logger.info(
            "Email {} created at {} sent at {}".format(notification_id, created_at, sent_at)
        )
    except SQLAlchemyError as e:
        current_app.logger.debug(e)


@notify_celery.task(name='send-sms-code')
def send_sms_code(encrypted_verification):
    verification_message = encryption.decrypt(encrypted_verification)
    try:
        firetext_client.send_sms(verification_message['to'], verification_message['secret_code'])
    except FiretextClientException as e:
        current_app.logger.error(e)


@notify_celery.task(name='send-email-code')
def send_email_code(encrypted_verification_message):
    verification_message = encryption.decrypt(encrypted_verification_message)
    try:
        aws_ses_client.send_email(current_app.config['VERIFY_CODE_FROM_EMAIL_ADDRESS'],
                                  verification_message['to'],
                                  "Verification code",
                                  verification_message['secret_code'])
    except AwsSesClientException as e:
        current_app.logger.error(e)


# TODO: when placeholders in templates work, this will be a real template
def invitation_template(user_name, service_name, url, expiry_date):
    from string import Template
    t = Template(
        '$user_name has invited you to collaborate on $service_name on GOV.UK Notify.\n\n'
        'GOV.UK Notify makes it easy to keep people updated by helping you send text messages, emails and letters.\n\n'
        'Click this link to create an account on GOV.UK Notify:\n$url\n\n'
        'This invitation will stop working at midnight tomorrow. This is to keep $service_name secure.')
    return t.substitute(user_name=user_name, service_name=service_name, url=url, expiry_date=expiry_date)


def invitation_subject_line(user_name, service_name):
    from string import Template
    t = Template('$user_name has invited you to collaborate on $service_name on GOV.UK Notify')
    return t.substitute(user_name=user_name, service_name=service_name)


def invited_user_url(base_url, token):
    return '{0}/invitation/{1}'.format(base_url, token)


@notify_celery.task(name='email-invited-user')
def email_invited_user(encrypted_invitation):
    invitation = encryption.decrypt(encrypted_invitation)
    url = invited_user_url(current_app.config['ADMIN_BASE_URL'],
                           invitation['token'])
    invitation_content = invitation_template(invitation['user_name'],
                                             invitation['service_name'],
                                             url,
                                             invitation['expiry_date'])
    try:
        email_from = "{}@{}".format(current_app.config['INVITATION_EMAIL_FROM'],
                                    current_app.config['NOTIFY_EMAIL_DOMAIN'])
        subject_line = invitation_subject_line(invitation['user_name'], invitation['service_name'])
        aws_ses_client.send_email(email_from,
                                  invitation['to'],
                                  subject_line,
                                  invitation_content)
    except AwsSesClientException as e:
        current_app.logger.error(e)
