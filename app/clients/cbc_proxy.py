import json
from abc import ABC, abstractmethod

import boto3
from flask import current_app
from notifications_utils.template import non_gsm_characters

from app.config import BroadcastProvider
from app.utils import DATETIME_FORMAT, format_sequential_number

# The variable names in this file have specific meaning in a CAP message
#
# identifier is a unique field for each CAP message
#
# headline is a field which we are not sure if we will use
#
# description is the body of the message

# areas is a list of dicts, with the following items
# * description is a string which populates the areaDesc field
# * polygon is a list of lat/long pairs
#
# previous_provider_messages is a list of previous events (models.py::BroadcastProviderMessage)
# ie a Cancel message would have a unique event but have the event of
#    the preceeding Alert message in the previous_provider_messages field


class CBCProxyException(Exception):
    pass


class CBCProxyClient:
    _lambda_client = None

    def init_app(self, app):
        if app.config.get('CBC_PROXY_ENABLED'):
            self._lambda_client = boto3.client(
                'lambda',
                region_name='eu-west-2',
                aws_access_key_id=app.config['CBC_PROXY_AWS_ACCESS_KEY_ID'],
                aws_secret_access_key=app.config['CBC_PROXY_AWS_SECRET_ACCESS_KEY'],
            )

    def get_proxy(self, provider):
        proxy_classes = {
            'canary': CBCProxyCanary,
            BroadcastProvider.EE: CBCProxyEE,
            BroadcastProvider.VODAFONE: CBCProxyVodafone,
        }
        return proxy_classes[provider](self._lambda_client)


class CBCProxyClientBase(ABC):
    @property
    @abstractmethod
    def lambda_name(self):
        pass

    @property
    @abstractmethod
    def failover_lambda_name(self):
        pass

    @property
    @abstractmethod
    def LANGUAGE_ENGLISH(self):
        pass

    @property
    @abstractmethod
    def LANGUAGE_WELSH(self):
        pass

    def __init__(self, lambda_client):
        self._lambda_client = lambda_client

    def send_canary(
        self,
        identifier,
    ):
        pass

    def send_link_test(
        self,
        identifier,
        sequential_number
    ):
        pass

    def create_and_send_broadcast(
        self, identifier, headline, description, areas, sent, expires, message_number=None
    ):
        pass

    # We have not implementated updating a broadcast
    def update_and_send_broadcast(
        self,
        identifier, previous_provider_messages, headline, description, areas,
        sent, expires, message_number=None
    ):
        pass

    # We have not implemented cancelling a broadcast
    def cancel_broadcast(
        self,
        identifier, previous_provider_messages, headline, description, areas,
        sent, expires, message_number=None
    ):
        pass

    def _invoke_lambda_with_failover(self, payload):
        payload_bytes = bytes(json.dumps(payload), encoding='utf8')
        result = self._invoke_lambda(self.lambda_name, payload_bytes)

        if not result:
            failover_result = self._invoke_lambda(self.failover_lambda_name, payload_bytes)
            if not failover_result:
                raise CBCProxyException(f'Lambda failed for both {self.lambda_name} and {self.failover_lambda_name}')

        return result

    def _invoke_lambda(self, lambda_name, payload_bytes):
        current_app.logger.info(f"Calling lambda {lambda_name}")
        result = self._lambda_client.invoke(
            FunctionName=lambda_name,
            InvocationType='RequestResponse',
            Payload=payload_bytes,
        )
        current_app.logger.info(f"Finished calling lambda {lambda_name}")

        if result['StatusCode'] > 299:
            current_app.logger.info(
                f"Error calling lambda {self.lambda_name} with status code { result['StatusCode']}, {result.get('Payload')}"
            )
            success = False

        elif 'FunctionError' in result:
            current_app.logger.info(
                f"Error calling lambda {self.lambda_name} with function error { result['Payload']['errorType'] }"
            )
            success = False

        else:
            success = True

        return success

    def infer_language_from(self, content):
        if non_gsm_characters(content):
            return self.LANGUAGE_WELSH
        return self.LANGUAGE_ENGLISH


class CBCProxyCanary(CBCProxyClientBase):
    """
    The canary is a lambda which tests notify's connectivity to the Cell Broadcast AWS infrastructure. It calls the
    canary, a specific lambda that does not open a vpn or connect to a provider but just responds from within AWS.
    """
    lambda_name = 'canary'
    # we don't need a failover lambda for the canary as it doesn't actually make calls out to a CBC
    # so we just reuse the normal one in case of a failover scenario
    failover_lambda_name = 'canary'

    LANGUAGE_ENGLISH = None
    LANGUAGE_WELSH = None

    def send_canary(
        self,
        identifier,
    ):
        self._invoke_lambda_with_failover(payload={'identifier': identifier})


class CBCProxyEE(CBCProxyClientBase):
    lambda_name = 'bt-ee-1-proxy'
    failover_lambda_name = 'bt-ee-2-proxy'

    LANGUAGE_ENGLISH = 'en-GB'
    LANGUAGE_WELSH = 'cy-GB'

    def send_link_test(
        self,
        identifier,
        sequential_number=None,
    ):
        """
        link test - open up a connection to a specific provider, and send them an xml payload with a <msgType> of
        test.
        """
        payload = {
            'message_type': 'test',
            'identifier': identifier,
            'message_format': 'cap'
        }

        self._invoke_lambda_with_failover(payload=payload)

    def create_and_send_broadcast(
        self, identifier, headline, description, areas, sent, expires, message_number=None
    ):
        payload = {
            'message_type': 'alert',
            'identifier': identifier,
            'message_format': 'cap',
            'headline': headline,
            'description': description,
            'areas': areas,
            'sent': sent,
            'expires': expires,
            'language': self.infer_language_from(description),
        }
        self._invoke_lambda_with_failover(payload=payload)

    def cancel_broadcast(
        self,
        identifier, previous_provider_messages,
        sent, message_number=None
    ):
        payload = {
            'message_type': 'cancel',
            'identifier': identifier,
            'message_format': 'cap',
            "references": [
                {
                    "message_id": str(message.id),
                    "sent": message.created_at.strftime(DATETIME_FORMAT)
                } for message in previous_provider_messages
            ],
            'sent': sent,
        }
        self._invoke_lambda_with_failover(payload=payload)


class CBCProxyVodafone(CBCProxyClientBase):
    lambda_name = 'vodafone-1-proxy'
    failover_lambda_name = 'vodafone-2-proxy'

    LANGUAGE_ENGLISH = 'English'
    LANGUAGE_WELSH = 'Welsh'

    def send_link_test(
        self,
        identifier,
        sequential_number,
    ):
        """
        link test - open up a connection to a specific provider, and send them an xml payload with a <msgType> of
        test.
        """
        payload = {
            'message_type': 'test',
            'identifier': identifier,
            'message_number': sequential_number,
            'message_format': 'ibag'
        }

        self._invoke_lambda_with_failover(payload=payload)

    def create_and_send_broadcast(
        self, identifier, message_number, headline, description, areas, sent, expires,
    ):
        payload = {
            'message_type': 'alert',
            'identifier': identifier,
            'message_number': message_number,
            'message_format': 'ibag',
            'headline': headline,
            'description': description,
            'areas': areas,
            'sent': sent,
            'expires': expires,
            'language': self.infer_language_from(description),
        }
        self._invoke_lambda_with_failover(payload=payload)

    def cancel_broadcast(
        self, identifier, previous_provider_messages, sent, message_number
    ):

        payload = {
            'message_type': 'cancel',
            'identifier': identifier,
            'message_number': message_number,
            'message_format': 'ibag',
            "references": [
                {
                    "message_id": str(message.id),
                    "message_number": format_sequential_number(message.message_number),
                    "sent": message.created_at.strftime(DATETIME_FORMAT)
                } for message in previous_provider_messages
            ],
            'sent': sent,
        }
        self._invoke_lambda_with_failover(payload=payload)
