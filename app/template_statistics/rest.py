from flask import (
    Blueprint,
    jsonify,
    request
)

from app import redis_store
from app.dao.notifications_dao import (
    dao_get_template_usage,
    dao_get_last_template_usage
)
from app.dao.templates_dao import (
    dao_get_multiple_template_details,
    dao_get_template_by_id_and_service_id
)

from app.schemas import notification_with_template_schema
from app.utils import cache_key_for_service_template_usage_per_day, last_n_days
from app.errors import register_errors, InvalidRequest
from collections import Counter

template_statistics = Blueprint('template_statistics',
                                __name__,
                                url_prefix='/service/<service_id>/template-statistics')

register_errors(template_statistics)


@template_statistics.route('')
def get_template_statistics_for_service_by_day(service_id):
    try:
        limit_days = int(request.args.get('limit_days'))
    except ValueError:
        error = '{} is not an integer'.format(request.args.get('limit_days'))
        message = {'limit_days': [error]}
        raise InvalidRequest(message, status_code=400)

    if limit_days < 1 or limit_days > 7:
        raise InvalidRequest({'limit_days': ['limit_days must be between 1 and 7']}, status_code=400)

    return jsonify(data=get_template_statistics_for_last_n_days(service_id, limit_days))


@template_statistics.route('/<template_id>')
def get_template_statistics_for_template_id(service_id, template_id):
    template = dao_get_template_by_id_and_service_id(template_id, service_id)
    if not template:
        message = 'No template found for id {}'.format(template_id)
        errors = {'template_id': [message]}
        raise InvalidRequest(errors, status_code=404)

    data = None
    notification = dao_get_last_template_usage(template_id, template.template_type)
    if notification:
        data = notification_with_template_schema.dump(notification).data

    return jsonify(data=data)


def get_template_statistics_for_last_n_days(service_id, limit_days):
    template_stats_by_id = Counter()

    for day in last_n_days(limit_days):
        # "{SERVICE_ID}-template-usage-{YYYY-MM-DD}"
        key = cache_key_for_service_template_usage_per_day(service_id, day)
        stats = redis_store.get_all_from_hash(key)
        if not stats:
            # key didn't exist (or redis was down) - lets populate from DB.
            stats = {
                str(row.id): row.count for row in dao_get_template_usage(service_id, day=day)
            }

        template_stats_by_id += Counter(stats)

    # attach count from stats to name/type/etc from database
    template_details = dao_get_multiple_template_details(template_stats_by_id.keys())
    return [
        {
            'count': template_stats_by_id[str(template.id)],
            'template_id': str(template.id),
            'template_name': template.name,
            'template_type': template.template_type,
            'is_precompiled_letter': template.is_precompiled_letter
        }
        for template in template_details
    ]
