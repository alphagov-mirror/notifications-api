"""

Revision ID: 0323_int_letter_rates
Revises: 0322_broadcast_service_perm
Create Date: 2020-07-01 15:52:26.339363

"""
import itertools
import uuid
from datetime import datetime

from alembic import op
from sqlalchemy.sql import text

from app.models import LetterRate


revision = '0323_int_letter_rates'
down_revision = '0322_broadcast_service_perm'

base_rate = 76
start_date = datetime(2020, 7, 1, 0, 0)


def upgrade():
    """
    Inserts these international letter rates:

    1 sheet - £0.84
    2 sheets - £0.92
    3 sheets - £1.00
    4 sheets - £1.08
    5 sheets - £1.16
    """
    op.bulk_insert(LetterRate.__table__, [
        {
            'id': uuid.uuid4(),
            'start_date': start_date,
            'end_date': None,
            'sheet_count': sheet_count,
            'rate': (base_rate + (8 * sheet_count)) / 100.0,
            'crown': crown,
            'post_class': 'international',
        }
        for sheet_count, crown in itertools.product(
            range(1, 6),
            [True, False]
        )
    ])


def downgrade():
    conn = op.get_bind()
    conn.execute(text("DELETE FROM letter_rates WHERE start_date = :start"), start=start_date)
