from app import db
from app.models import BroadcastMessage
from app.dao.dao_utils import transactional


@transactional
def dao_create_broadcast_message(broadcast_message):
    db.session.add(broadcast_message)


@transactional
def dao_update_broadcast_message(broadcast_message):
    db.session.add(broadcast_message)


def dao_get_broadcast_message_by_id_and_service_id(broadcast_message_id, service_id):
    return BroadcastMessage.query.filter(
        BroadcastMessage.id == broadcast_message_id,
        BroadcastMessage.service_id == service_id
    ).one()
