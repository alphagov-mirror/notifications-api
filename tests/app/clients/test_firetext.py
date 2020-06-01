from requests import HTTPError
from urllib.parse import parse_qs
from requests.exceptions import ConnectTimeout, ReadTimeout

import pytest
import requests_mock

from app.clients.sms.firetext import get_firetext_responses, SmsClientResponseException, FiretextClientResponseException


@pytest.mark.parametrize('code, result', [
    (None, ('delivered', None)), ('000', ('delivered', None))
])
def test_get_firetext_responses_should_return_correct_details_for_delivery(code, result):
    assert get_firetext_responses('0', code) == result


@pytest.mark.parametrize('code, result', [
    (None, ('permanent-failure', None)), ('401', ('permanent-failure', 'Message Rejected'))
])
def test_get_firetext_responses_should_return_correct_details_for_bounced(code, result):
    assert get_firetext_responses('1', code) == result


def test_get_firetext_responses_should_return_correct_details_for_complaint():
    assert get_firetext_responses('2') == ('pending', None)


def test_get_firetext_responses_raises_KeyError_if_unrecognised_status_code():
    with pytest.raises(KeyError) as e:
        get_firetext_responses('99')
    assert '99' in str(e.value)


def test_send_sms_successful_returns_firetext_response(mocker, mock_firetext_client):
    to = content = reference = 'foo'
    response_dict = {
        'data': [],
        'description': 'SMS successfully queued',
        'code': 0,
        'responseData': 1
    }

    with requests_mock.Mocker() as request_mock:
        request_mock.post('https://example.com/firetext', json=response_dict, status_code=200)
        response = mock_firetext_client.send_sms(to, content, reference)

    response_json = response.json()
    assert response.status_code == 200
    assert response_json['code'] == 0
    assert response_json['description'] == 'SMS successfully queued'


def test_send_sms_calls_firetext_correctly(mocker, mock_firetext_client):
    to = '+447234567890'
    content = 'my message'
    reference = 'my reference'
    response_dict = {
        'code': 0,
    }

    with requests_mock.Mocker() as request_mock:
        request_mock.post('https://example.com/firetext', json=response_dict, status_code=200)
        mock_firetext_client.send_sms(to, content, reference)

    assert request_mock.call_count == 1
    assert request_mock.request_history[0].url == 'https://example.com/firetext'
    assert request_mock.request_history[0].method == 'POST'

    request_args = parse_qs(request_mock.request_history[0].text)
    assert request_args['apiKey'][0] == 'foo'
    assert request_args['from'][0] == 'bar'
    assert request_args['to'][0] == '447234567890'
    assert request_args['message'][0] == content
    assert request_args['reference'][0] == reference


def test_send_sms_raises_if_firetext_rejects(mocker, mock_firetext_client):
    to = content = reference = 'foo'
    response_dict = {
        'data': [],
        'description': 'Some kind of error',
        'code': 1,
        'responseData': ''
    }

    with pytest.raises(SmsClientResponseException) as exc, requests_mock.Mocker() as request_mock:
        request_mock.post('https://example.com/firetext', json=response_dict, status_code=200)
        mock_firetext_client.send_sms(to, content, reference)

    assert exc.value.status_code == 200
    assert '"description": "Some kind of error"' in exc.value.text
    assert '"code": 1' in exc.value.text


def test_send_sms_raises_if_firetext_rejects_with_unexpected_data(mocker, mock_firetext_client):
    to = content = reference = 'foo'
    response_dict = {"something": "gone bad"}

    with pytest.raises(SmsClientResponseException) as exc, requests_mock.Mocker() as request_mock:
        request_mock.post('https://example.com/firetext', json=response_dict, status_code=400)
        mock_firetext_client.send_sms(to, content, reference)

    assert exc.value.status_code == 400
    assert exc.value.text == '{"something": "gone bad"}'
    assert type(exc.value.exception) == HTTPError


def test_send_sms_override_configured_shortcode_with_sender(mocker, mock_firetext_client):
    to = '+447234567890'
    content = 'my message'
    reference = 'my reference'
    response_dict = {
        'code': 0,
    }
    sender = 'fromservice'

    with requests_mock.Mocker() as request_mock:
        request_mock.post('https://example.com/firetext', json=response_dict, status_code=200)
        mock_firetext_client.send_sms(to, content, reference, sender=sender)

    request_args = parse_qs(request_mock.request_history[0].text)
    assert request_args['from'][0] == 'fromservice'


def test_send_sms_raises_if_firetext_rejects_with_connect_timeout(rmock, mock_firetext_client):
    to = content = reference = 'foo'

    with pytest.raises(FiretextClientResponseException) as exc:
        rmock.register_uri('POST', 'https://example.com/firetext', exc=ConnectTimeout)
        mock_firetext_client.send_sms(to, content, reference)

    assert exc.value.status_code == 504
    assert exc.value.text == 'Gateway Time-out'


def test_send_sms_raises_if_firetext_rejects_with_read_timeout(rmock, mock_firetext_client):
    to = content = reference = 'foo'

    with pytest.raises(FiretextClientResponseException) as exc:
        rmock.register_uri('POST', 'https://example.com/firetext', exc=ReadTimeout)
        mock_firetext_client.send_sms(to, content, reference)

    assert exc.value.status_code == 504
    assert exc.value.text == 'Gateway Time-out'
