#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#
import asyncio
import csv
import io
import logging
import re
from datetime import datetime
from typing import List
from unittest.mock import Mock
from yarl import URL

import freezegun
import pendulum
import pytest
import requests_mock
from aioresponses import CallbackResult, aioresponses
from airbyte_cdk.models import AirbyteStream, ConfiguredAirbyteCatalog, ConfiguredAirbyteStream, DestinationSyncMode, SyncMode, Type
from airbyte_cdk.sources.async_cdk import source_dispatcher
from airbyte_cdk.sources.streams import Stream
from airbyte_cdk.sources.streams.concurrent.adapters import StreamFacade
from airbyte_cdk.sources.streams.http.utils import HttpError
from airbyte_cdk.utils import AirbyteTracedException
from conftest import encoding_symbols_parameters, generate_stream_async
from source_salesforce.api import Salesforce
from source_salesforce.exceptions import AUTHENTICATION_ERROR_MESSAGE_MAPPING
from source_salesforce.async_salesforce.source import SalesforceSourceDispatcher, AsyncSourceSalesforce
from source_salesforce.async_salesforce.streams import (
    CSV_FIELD_SIZE_LIMIT,
    BulkIncrementalSalesforceStream,
    BulkSalesforceStream,
    BulkSalesforceSubStream,
    IncrementalRestSalesforceStream,
    RestSalesforceStream,
)

_ANY_CATALOG = ConfiguredAirbyteCatalog.parse_obj({"streams": []})
_ANY_CONFIG = {}


@pytest.mark.parametrize(
    "login_status_code, login_json_resp, expected_error_msg, is_config_error",
    [
        (
            400,
            {"error": "invalid_grant", "error_description": "expired access/refresh token"},
            AUTHENTICATION_ERROR_MESSAGE_MAPPING.get("expired access/refresh token"),
            True,
        ),
        (
            400,
            {"error": "invalid_grant", "error_description": "Authentication failure."},
            'An error occurred: {"error": "invalid_grant", "error_description": "Authentication failure."}',
            False,
        ),
        (
            401,
            {"error": "Unauthorized", "error_description": "Unautorized"},
            'An error occurred: {"error": "Unauthorized", "error_description": "Unautorized"}',
            False,
        ),
    ],
)
def test_login_authentication_error_handler(
    stream_config, requests_mock, login_status_code, login_json_resp, expected_error_msg, is_config_error
):
    source = SalesforceSourceDispatcher(AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG))
    logger = logging.getLogger("airbyte")
    requests_mock.register_uri(
        "POST", "https://login.salesforce.com/services/oauth2/token", json=login_json_resp, status_code=login_status_code
    )

    if is_config_error:
        with pytest.raises(AirbyteTracedException) as err:
            source.check_connection(logger, stream_config)
        assert err.value.message == expected_error_msg
    else:
        result, msg = source.check_connection(logger, stream_config)
        assert result is False
        assert msg == expected_error_msg


@pytest.mark.asyncio
async def test_bulk_sync_creation_failed(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    def callback(*args, **kwargs):
        return CallbackResult(status=400, payload={"message": "test_error"})

    with aioresponses() as m:
        m.post("https://fase-account.salesforce.com/services/data/v57.0/jobs/query", status=400, callback=callback)
        with pytest.raises(HttpError) as err:
            stream_slices = await anext(stream.stream_slices(sync_mode=SyncMode.incremental))
            [r async for r in stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slices)]

    assert err.value.json()["message"] == "test_error"
    await stream._session.close()


@pytest.mark.asyncio
async def test_bulk_stream_fallback_to_rest(stream_config, stream_api):
    """
    Here we mock BULK API with response returning error, saying BULK is not supported for this kind of entity.
    On the other hand, we mock REST API for this same entity with a successful response.
    After having instantiated a BulkStream, sync should succeed in case it falls back to REST API. Otherwise it would throw an error.
    """
    stream = await generate_stream_async("CustomEntity", stream_config, stream_api)
    await stream.ensure_session()

    def callback(*args, **kwargs):
        return CallbackResult(status=400, payload={"errorCode": "INVALIDENTITY", "message": "CustomEntity is not supported by the Bulk API"}, content_type="application/json")

    rest_stream_records = [
        {"id": 1, "name": "custom entity", "created": "2010-11-11"},
        {"id": 11, "name": "custom entity", "created": "2020-01-02"},
    ]
    async def get_records(*args, **kwargs):
        nonlocal rest_stream_records
        for record in rest_stream_records:
            yield record

    with aioresponses() as m:
        # mock a BULK API
        m.post("https://fase-account.salesforce.com/services/data/v57.0/jobs/query", status=400, callback=callback)
        # mock REST API
        stream.read_records = get_records
        assert type(stream) is BulkIncrementalSalesforceStream
        stream_slices = await anext(stream.stream_slices(sync_mode=SyncMode.incremental))
        assert [r async for r in stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slices)] == rest_stream_records

    await stream._session.close()


@pytest.mark.asyncio
async def test_stream_unsupported_by_bulk(stream_config, stream_api):
    """
    Stream `AcceptedEventRelation` is not supported by BULK API, so that REST API stream will be used for it.
    """
    stream_name = "AcceptedEventRelation"
    stream = await generate_stream_async(stream_name, stream_config, stream_api)
    assert not isinstance(stream, BulkSalesforceStream)


@pytest.mark.asyncio
async def test_stream_contains_unsupported_properties_by_bulk(stream_config, stream_api_v2):
    """
    Stream `Account` contains compound field such as BillingAddress, which is not supported by BULK API (csv),
    in that case REST API stream will be used for it.
    """
    stream_name = "Account"
    stream = await generate_stream_async(stream_name, stream_config, stream_api_v2)
    assert not isinstance(stream, BulkSalesforceStream)


@pytest.mark.asyncio
async def test_bulk_sync_pagination(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()
    job_id = "fake_job"
    call_counter = 0

    def cb1(*args, **kwargs):
        nonlocal call_counter
        call_counter += 1
        return CallbackResult(headers={"Sforce-Locator": "somelocator_1"}, body="\n".join(resp_text))

    def cb2(*args, **kwargs):
        nonlocal call_counter
        call_counter += 1
        return CallbackResult(headers={"Sforce-Locator": "somelocator_2"}, body="\n".join(resp_text))

    def cb3(*args, **kwargs):
        nonlocal call_counter
        call_counter += 1
        return CallbackResult(headers={"Sforce-Locator": "null"}, body="\n".join(resp_text))

    with aioresponses() as m:
        base_url = f"{stream.sf_api.instance_url}{stream.path()}"
        m.post(f"{base_url}", callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id}))
        m.get(f"{base_url}/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        resp_text = ["Field1,LastModifiedDate,ID"] + [f"test,2021-11-16,{i}" for i in range(5)]
        m.get(f"{base_url}/{job_id}/results", callback=cb1)
        m.get(f"{base_url}/{job_id}/results?locator=somelocator_1", callback=cb2)
        m.get(f"{base_url}/{job_id}/results?locator=somelocator_2", callback=cb3)
        m.delete(base_url + f"/{job_id}")

        stream_slices = await anext(stream.stream_slices(sync_mode=SyncMode.incremental))
        loaded_ids = [int(record["ID"]) async for record in stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slices)]
        assert loaded_ids == [0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4]
        assert call_counter == 3
        await stream._session.close()




def _prepare_mock(m, stream):
    job_id = "fake_job_1"
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"
    m.post(base_url, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id}))
    m.delete(base_url + f"/{job_id}")
    m.get(base_url + f"/{job_id}/results", callback=lambda *args, **kwargs: CallbackResult(body="Field1,LastModifiedDate,ID\ntest,2021-11-16,1"))
    m.patch(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(body=""))
    return job_id


async def _get_result_id(stream):
    stream_slices = await anext(stream.stream_slices(sync_mode=SyncMode.incremental))
    records = [r async for r in stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slices)]
    return int(list(records)[0]["ID"])


@pytest.mark.asyncio
async def test_bulk_sync_successful(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    with aioresponses() as m:
        m.post(base_url, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id}))

    with aioresponses() as m:
        job_id = _prepare_mock(m, stream)
        m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        assert await _get_result_id(stream) == 1


@pytest.mark.asyncio
async def test_bulk_sync_successful_long_response(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    with aioresponses() as m:
        job_id = _prepare_mock(m, stream)
        m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "UploadComplete", "id": job_id}))
        m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "InProgress", "id": job_id}))
        m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete", "id": job_id}))
        assert await _get_result_id(stream) == 1


# maximum timeout is wait_timeout * max_retry_attempt
# this test tries to check a job state 17 times with +-1second for very one
@pytest.mark.asyncio
@pytest.mark.timeout(17)
async def test_bulk_sync_successful_retry(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    stream.DEFAULT_WAIT_TIMEOUT_SECONDS = 6  # maximum wait timeout will be 6 seconds
    await stream.ensure_session()
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    with aioresponses() as m:
        job_id = _prepare_mock(m, stream)
        # 2 failed attempts, 3rd one should be successful
        states = [{"json": {"state": "InProgress", "id": job_id}}] * 17
        states.append({"json": {"state": "JobComplete", "id": job_id}})
        # raise Exception(states)
        for _ in range(17):
            m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "InProgress", "id": job_id}))
        m.get(base_url + f"/{job_id}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete", "id": job_id}))

        assert await _get_result_id(stream) == 1


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_bulk_sync_failed_retry(stream_config, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    stream.DEFAULT_WAIT_TIMEOUT_SECONDS = 6  # maximum wait timeout will be 6 seconds
    await stream.ensure_session()
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    with aioresponses() as m:
        job_id = _prepare_mock(m, stream)
        m.get(base_url + f"/{job_id}", repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload={"state": "InProgress", "id": job_id}))
        m.post(base_url, repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id}))
        with pytest.raises(Exception) as err:
            stream_slices = await anext(stream.stream_slices(sync_mode=SyncMode.incremental))
            [record async for record in stream.read_records(sync_mode=SyncMode.full_refresh, stream_slice=stream_slices)]
        assert "stream using BULK API was failed" in str(err.value)

    await stream._session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start_date_provided,stream_name,expected_start_date",
    [
        (True, "Account", "2010-01-18T21:18:20Z"),
        (True, "ActiveFeatureLicenseMetric", "2010-01-18T21:18:20Z"),
    ],
)
async def test_stream_start_date(
    start_date_provided,
    stream_name,
    expected_start_date,
    stream_config,
    stream_api,
    stream_config_without_start_date,
):
    if start_date_provided:
        stream = await generate_stream_async(stream_name, stream_config, stream_api)
        assert stream.start_date == expected_start_date
    else:
        stream = await generate_stream_async(stream_name, stream_config_without_start_date, stream_api)
        assert datetime.strptime(stream.start_date, "%Y-%m-%dT%H:%M:%SZ").year == datetime.now().year - 2


@pytest.mark.asyncio
async def test_stream_start_date_should_be_converted_to_datetime_format(stream_config_date_format, stream_api):
    stream: IncrementalRestSalesforceStream = await generate_stream_async("ActiveFeatureLicenseMetric", stream_config_date_format, stream_api)
    assert stream.start_date == "2010-01-18T00:00:00Z"


@pytest.mark.asyncio
async def test_stream_start_datetime_format_should_not_changed(stream_config, stream_api):
    stream: IncrementalRestSalesforceStream = await generate_stream_async("ActiveFeatureLicenseMetric", stream_config, stream_api)
    assert stream.start_date == "2010-01-18T21:18:20Z"


@pytest.mark.asyncio
async def test_download_data_filter_null_bytes(stream_config, stream_api):
    job_full_url_results: str = "https://fase-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(job_full_url_results, callback=lambda *args, **kwargs: CallbackResult(body=b"\x00"))
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == []

        m.get(job_full_url_results, callback=lambda *args, **kwargs: CallbackResult(body=b'"Id","IsDeleted"\n\x00"0014W000027f6UwQAI","false"\n\x00\x00'))
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == [{"Id": "0014W000027f6UwQAI", "IsDeleted": "false"}]


@pytest.mark.asyncio
async def test_read_with_chunks_should_return_only_object_data_type(stream_config, stream_api):
    job_full_url_results: str = "https://fase-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(job_full_url_results, callback=lambda *args, **kwargs: CallbackResult(body=b'"IsDeleted","Age"\n"false",24\n'))
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == [{"IsDeleted": "false", "Age": "24"}]


@pytest.mark.asyncio
async def test_read_with_chunks_should_return_a_string_when_a_string_with_only_digits_is_provided(stream_config, stream_api):
    job_full_url_results: str = "https://fase-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(job_full_url_results, body=b'"ZipCode"\n"01234"\n')
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == [{"ZipCode": "01234"}]


@pytest.mark.asyncio
async def test_read_with_chunks_should_return_null_value_when_no_data_is_provided(stream_config, stream_api):
    job_full_url_results: str = "https://fase-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(job_full_url_results, body=b'"IsDeleted","Age","Name"\n"false",,"Airbyte"\n')
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == [{"IsDeleted": "false", "Age": None, "Name": "Airbyte"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunk_size, content_type_header, content, expected_result",
    encoding_symbols_parameters(),
    ids=[f"charset: {x[1]}, chunk_size: {x[0]}" for x in encoding_symbols_parameters()],
)
async def test_encoding_symbols(stream_config, stream_api, chunk_size, content_type_header, content, expected_result):
    job_full_url_results: str = "https://fase-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(job_full_url_results, headers=content_type_header, body=content)
        tmp_file, response_encoding, _ = await stream.download_data(url=job_full_url_results)
        res = list(stream.read_with_chunks(tmp_file, response_encoding))
        assert res == expected_result


@pytest.mark.parametrize(
    "login_status_code, login_json_resp, discovery_status_code, discovery_resp_json, expected_error_msg",
    (
        (403, [{"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "TotalRequests Limit exceeded."}], 200, {}, "API Call limit is exceeded"),
        (
            200,
            {"access_token": "access_token", "instance_url": "https://instance_url"},
            403,
            [{"errorCode": "FORBIDDEN", "message": "You do not have enough permissions"}],
            'An error occurred: [{"errorCode": "FORBIDDEN", "message": "You do not have enough permissions"}]',
        ),
    ),
)
async def test_check_connection_rate_limit(
    stream_config, login_status_code, login_json_resp, discovery_status_code, discovery_resp_json, expected_error_msg
):
    source = AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG)
    logger = logging.getLogger("airbyte")

    with requests_mock.Mocker() as m:
        m.register_uri("POST", "https://login.salesforce.com/services/oauth2/token", json=login_json_resp, status_code=login_status_code)
        m.register_uri(
            "GET", "https://instance_url/services/data/v57.0/sobjects", json=discovery_resp_json, status_code=discovery_status_code
        )
        result, msg = source.check_connection(logger, stream_config)
        assert result is False
        assert msg == expected_error_msg


def configure_request_params_mock(stream_1, stream_2):
    stream_1.request_params = Mock()
    stream_1.request_params.return_value = {"q": "query"}

    stream_2.request_params = Mock()
    stream_2.request_params.return_value = {"q": "query"}


def test_rate_limit_bulk(stream_config, stream_api, bulk_catalog, state):
    """
    Connector should stop the sync if one stream reached rate limit
    stream_1, stream_2, stream_3, ...
    While reading `stream_1` if 403 (Rate Limit) is received, it should finish that stream with success and stop the sync process.
    Next streams should not be executed.
    """
    source_dispatcher.DEFAULT_SESSION_LIMIT = 1  # ensure that only one stream runs at a time
    stream_config.update({"start_date": "2021-10-01"})
    loop = asyncio.get_event_loop()
    stream_1: BulkIncrementalSalesforceStream = loop.run_until_complete(generate_stream_async("Account", stream_config, stream_api))
    stream_2: BulkIncrementalSalesforceStream = loop.run_until_complete(generate_stream_async("Asset", stream_config, stream_api))
    streams = [stream_1, stream_2]
    configure_request_params_mock(stream_1, stream_2)

    stream_1.page_size = 6
    stream_1.state_checkpoint_interval = 5

    source = SalesforceSourceDispatcher(AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG))
    source.streams = Mock()
    source.streams.return_value = streams
    logger = logging.getLogger("airbyte")

    json_response = {"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "TotalRequests Limit exceeded."}

    orig_read_stream = source.async_source.read_stream

    async def patched_read_stream(*args, **kwargs):
        base_url = f"{stream_1.sf_api.instance_url}{stream_1.path()}"
        with aioresponses() as m:
            creation_responses = []
            for page in [1, 2]:
                job_id = f"fake_job_{page}_{stream_1.name}"
                creation_responses.append({"id": job_id})

                m.get(base_url + f"/{job_id}", callback=lambda *_, **__: CallbackResult(payload={"state": "JobComplete"}))

                resp = ["Field1,LastModifiedDate,Id"] + [f"test,2021-10-0{i},{i}" for i in range(1, 7)]  # 6 records per page

                if page == 1:
                    # Read the first page successfully
                    m.get(base_url + f"/{job_id}/results", callback=lambda *_, **__: CallbackResult(body="\n".join(resp)))
                else:
                    # Requesting for results when reading second page should fail with 403 (Rate Limit error)
                    m.get(base_url + f"/{job_id}/results", status=403, callback=lambda *_, **__: CallbackResult(status=403, payload=json_response))

                m.delete(base_url + f"/{job_id}")

            def cb(response):
                return lambda *_, **__: CallbackResult(payload=response)

            for response in creation_responses:
                m.post(base_url, callback=cb(response))

            async for r in orig_read_stream(**kwargs):
                yield r

    source.async_source.read_stream = patched_read_stream

    result = [i for i in source.read(logger=logger, config=stream_config, catalog=bulk_catalog, state=state)]
    assert stream_1.request_params.called
    assert (
        not stream_2.request_params.called
    ), "The second stream should not be executed, because the first stream finished with Rate Limit."

    records = [item for item in result if item.type == Type.RECORD]
    assert len(records) == 6  # stream page size: 6

    state_record = [item for item in result if item.type == Type.STATE][0]
    assert state_record.state.data["Account"]["LastModifiedDate"] == "2021-10-05T00:00:00+00:00"  # state checkpoint interval is 5.


@pytest.mark.asyncio
async def test_rate_limit_rest(stream_config, stream_api, rest_catalog, state):
    source_dispatcher.DEFAULT_SESSION_LIMIT = 1  # ensure that only one stream runs at a time
    stream_config.update({"start_date": "2021-11-01"})
    stream_1: IncrementalRestSalesforceStream = await generate_stream_async("KnowledgeArticle", stream_config, stream_api)
    stream_2: IncrementalRestSalesforceStream = await generate_stream_async("AcceptedEventRelation", stream_config, stream_api)
    stream_1.state_checkpoint_interval = 3
    configure_request_params_mock(stream_1, stream_2)

    source = SalesforceSourceDispatcher(AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG))
    source.streams = Mock()
    source.streams.return_value = [stream_1, stream_2]

    logger = logging.getLogger("airbyte")

    next_page_url = "/services/data/v57.0/query/012345"
    response_1 = {
        "done": False,
        "totalSize": 10,
        "nextRecordsUrl": next_page_url,
        "records": [
            {
                "ID": 1,
                "LastModifiedDate": "2021-11-15",
            },
            {
                "ID": 2,
                "LastModifiedDate": "2021-11-16",
            },
            {
                "ID": 3,
                "LastModifiedDate": "2021-11-17",  # check point interval
            },
            {
                "ID": 4,
                "LastModifiedDate": "2021-11-18",
            },
            {
                "ID": 5,
                "LastModifiedDate": "2021-11-19",
            },
        ],
    }
    response_2 = {"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "TotalRequests Limit exceeded."}

    def cb1(*args, **kwargs):
        return CallbackResult(payload=response_1, status=200)

    def cb2(*args, **kwargs):
        return CallbackResult(payload=response_2, status=403, reason="")

    orig_read_records_s1 = stream_1.read_records
    orig_read_records_s2 = stream_2.read_records

    async def patched_read_records_s1(*args, **kwargs):
        with aioresponses() as m:
            m.get(re.compile(re.escape(rf"{stream_1.sf_api.instance_url}{stream_1.path()}") + rf"\??.*"), repeat=True, callback=cb1)
            m.get(re.compile(re.escape(rf"{stream_1.sf_api.instance_url}{next_page_url}") + rf"\??.*"), repeat=True, callback=cb2)

            async for r in orig_read_records_s1(**kwargs):
                yield r

    async def patched_read_records_s2(*args, **kwargs):
        with aioresponses() as m:
            m.get(re.compile(re.escape(rf"{stream_2.sf_api.instance_url}{stream_2.path()}") + rf"\??.*"), repeat=True, callback=cb1)
            m.get(re.compile(re.escape(rf"{stream_2.sf_api.instance_url}{next_page_url}") + rf"\??.*"), repeat=True, callback=cb1)
            async for r in orig_read_records_s2(**kwargs):
                yield r

    async def check_availability(*args, **kwargs):
        return (True, None)

    stream_1.read_records = lambda *args, **kwargs: patched_read_records_s1(stream_1, *args, **kwargs)
    stream_1.check_availability = check_availability
    stream_2.read_records = lambda *args, **kwargs: patched_read_records_s2(stream_2, *args, **kwargs)
    stream_2.check_availability = check_availability

    result = [i for i in source.read(logger=logger, config=stream_config, catalog=rest_catalog, state=state)]

    assert stream_1.request_params.called
    assert (
        not stream_2.request_params.called
    ), "The second stream should not be executed, because the first stream finished with Rate Limit."

    records = [item for item in result if item.type == Type.RECORD]
    assert len(records) == 5

    state_record = [item for item in result if item.type == Type.STATE][0]
    assert state_record.state.data["KnowledgeArticle"]["LastModifiedDate"] == "2021-11-17T00:00:00+00:00"


@pytest.mark.asyncio
async def test_pagination_rest(stream_config, stream_api):
    stream_name = "AcceptedEventRelation"
    stream: RestSalesforceStream = await generate_stream_async(stream_name, stream_config, stream_api)
    stream.DEFAULT_WAIT_TIMEOUT_SECONDS = 6  # maximum wait timeout will be 6 seconds
    next_page_url = "/services/data/v57.0/query/012345"
    await stream.ensure_session()

    resp_1 = {
        "done": False,
        "totalSize": 4,
        "nextRecordsUrl": next_page_url,
        "records": [
            {
                "ID": 1,
                "LastModifiedDate": "2021-11-15",
            },
            {
                "ID": 2,
                "LastModifiedDate": "2021-11-16",
            },
        ],
    }
    resp_2 = {
        "done": True,
        "totalSize": 4,
        "records": [
            {
                "ID": 3,
                "LastModifiedDate": "2021-11-17",
            },
            {
                "ID": 4,
                "LastModifiedDate": "2021-11-18",
            },
        ],
    }

    with aioresponses() as m:
        m.get(re.compile(r"https://fase-account\.salesforce\.com/services/data/v57\.0\??.*"), callback=lambda *args, **kwargs: CallbackResult(payload=resp_1))
        m.get("https://fase-account.salesforce.com" + next_page_url, repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload=resp_2))

        records = [record async for record in stream.read_records(sync_mode=SyncMode.full_refresh)]
        assert len(records) == 4


@pytest.mark.asyncio
async def test_csv_reader_dialect_unix():
    stream: BulkSalesforceStream = BulkSalesforceStream(stream_name=None, sf_api=None, pk=None)
    url_results = "https://fake-account.salesforce.com/services/data/v57.0/jobs/query/7504W00000bkgnpQAA/results"
    await stream.ensure_session()

    data = [
        {"Id": "1", "Name": '"first_name" "last_name"'},
        {"Id": "2", "Name": "'" + 'first_name"\n' + "'" + 'last_name\n"'},
        {"Id": "3", "Name": "first_name last_name"},
    ]

    with io.StringIO("", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=["Id", "Name"], dialect="unix")
        writer.writeheader()
        for line in data:
            writer.writerow(line)
        text = csvfile.getvalue()

    with aioresponses() as m:
        m.get(url_results, callback=lambda *args, **kwargs: CallbackResult(body=text))
        tmp_file, response_encoding, _ = await stream.download_data(url=url_results)
        result = [i for i in stream.read_with_chunks(tmp_file, response_encoding)]
        assert result == data


@pytest.mark.parametrize(
    "stream_names,catalog_stream_names,",
    (
        (
            ["stream_1", "stream_2", "Describe"],
            None,
        ),
        (
            ["stream_1", "stream_2"],
            ["stream_1", "stream_2", "Describe"],
        ),
        (
            ["stream_1", "stream_2", "stream_3", "Describe"],
            ["stream_1", "Describe"],
        ),
    ),
)
async def test_forwarding_sobject_options(stream_config, stream_names, catalog_stream_names) -> None:
    sobjects_matcher = re.compile("/sobjects$")
    token_matcher = re.compile("/token$")
    describe_matcher = re.compile("/describe$")
    catalog = None
    if catalog_stream_names:
        catalog = ConfiguredAirbyteCatalog(
            streams=[
                ConfiguredAirbyteStream(
                    stream=AirbyteStream(
                        name=catalog_stream_name, supported_sync_modes=[SyncMode.full_refresh], json_schema={"type": "object"}
                    ),
                    sync_mode=SyncMode.full_refresh,
                    destination_sync_mode=DestinationSyncMode.overwrite,
                )
                for catalog_stream_name in catalog_stream_names
            ]
        )
    with requests_mock.Mocker() as m:
        m.register_uri("POST", token_matcher, json={"instance_url": "https://fake-url.com", "access_token": "fake-token"})
        m.register_uri(
            "GET",
            describe_matcher,
            json={
                "fields": [
                    {
                        "name": "field",
                        "type": "string",
                    }
                ]
            },
        )
        m.register_uri(
            "GET",
            sobjects_matcher,
            json={
                "sobjects": [
                    {
                        "name": stream_name,
                        "flag1": True,
                        "queryable": True,
                    }
                    for stream_name in stream_names
                    if stream_name != "Describe"
                ],
            },
        )
        source = AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG)
        source.catalog = catalog
        streams = source.streams(config=stream_config)
    expected_names = catalog_stream_names if catalog else stream_names
    assert not set(expected_names).symmetric_difference(set(stream.name for stream in streams)), "doesn't match excepted streams"

    for stream in streams:
        if stream.name != "Describe":
            if isinstance(stream, StreamFacade):
                assert stream._legacy_stream.sobject_options == {"flag1": True, "queryable": True}
            else:
                assert stream.sobject_options == {"flag1": True, "queryable": True}
    return


def _get_streams(stream_config, stream_names, catalog_stream_names, sync_type) -> List[Stream]:
    sobjects_matcher = re.compile("/sobjects$")
    token_matcher = re.compile("/token$")
    describe_matcher = re.compile("/describe$")
    catalog = None
    if catalog_stream_names:
        catalog = ConfiguredAirbyteCatalog(
            streams=[
                ConfiguredAirbyteStream(
                    stream=AirbyteStream(name=catalog_stream_name, supported_sync_modes=[sync_type], json_schema={"type": "object"}),
                    sync_mode=sync_type,
                    destination_sync_mode=DestinationSyncMode.overwrite,
                )
                for catalog_stream_name in catalog_stream_names
            ]
        )
    with requests_mock.Mocker() as m:
        m.register_uri("POST", token_matcher, json={"instance_url": "https://fake-url.com", "access_token": "fake-token"})
        m.register_uri(
            "GET",
            describe_matcher,
            json={
                "fields": [
                    {
                        "name": "field",
                        "type": "string",
                    }
                ]
            },
        )
        m.register_uri(
            "GET",
            sobjects_matcher,
            json={
                "sobjects": [
                    {
                        "name": stream_name,
                        "flag1": True,
                        "queryable": True,
                    }
                    for stream_name in stream_names
                    if stream_name != "Describe"
                ],
            },
        )
        source = AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG)
        source.catalog = catalog
        return source.streams(config=stream_config)


def test_csv_field_size_limit():
    DEFAULT_CSV_FIELD_SIZE_LIMIT = 1024 * 128

    field_size = 1024 * 1024
    text = '"Id","Name"\n"1","' + field_size * "a" + '"\n'

    csv.field_size_limit(DEFAULT_CSV_FIELD_SIZE_LIMIT)
    reader = csv.reader(io.StringIO(text))
    with pytest.raises(csv.Error):
        for _ in reader:
            pass

    csv.field_size_limit(CSV_FIELD_SIZE_LIMIT)
    reader = csv.reader(io.StringIO(text))
    for _ in reader:
        pass


@pytest.mark.asyncio
async def test_convert_to_standard_instance(stream_config, stream_api):
    bulk_stream = await generate_stream_async("Account", stream_config, stream_api)
    rest_stream = bulk_stream.get_standard_instance()
    assert isinstance(rest_stream, IncrementalRestSalesforceStream)


@pytest.mark.asyncio
async def test_rest_stream_init_with_too_many_properties(stream_config, stream_api_v2_too_many_properties):
    with pytest.raises(AssertionError):
        # v2 means the stream is going to be a REST stream.
        # A missing primary key is not allowed
        await generate_stream_async("Account", stream_config, stream_api_v2_too_many_properties)


@pytest.mark.asyncio
async def test_too_many_properties(stream_config, stream_api_v2_pk_too_many_properties, requests_mock):
    stream = await generate_stream_async("Account", stream_config, stream_api_v2_pk_too_many_properties)
    await stream.ensure_session()
    chunks = list(stream.chunk_properties())
    for chunk in chunks:
        assert stream.primary_key in chunk
    chunks_len = len(chunks)
    assert stream.too_many_properties
    assert stream.primary_key
    assert type(stream) == RestSalesforceStream
    next_page_url = "https://fase-account.salesforce.com/services/data/v57.0/queryAll"
    url_pattern = re.compile(r"https://fase-account\.salesforce\.com/services/data/v57\.0/queryAll\??.*")
    with aioresponses() as m:
        m.get(url_pattern, callback=lambda *args, **kwargs: CallbackResult(payload={
                    "records": [
                        {"Id": 1, "propertyA": "A"},
                        {"Id": 2, "propertyA": "A"},
                        {"Id": 3, "propertyA": "A"},
                        {"Id": 4, "propertyA": "A"},
                    ]
                }))
        m.get(url_pattern, callback=lambda *args, **kwargs: CallbackResult(payload={"nextRecordsUrl": next_page_url, "records": [{"Id": 1, "propertyB": "B"}, {"Id": 2, "propertyB": "B"}]}))
        # 2 for 2 chunks above
        for _ in range(chunks_len - 2):
            m.get(url_pattern, callback=lambda *args, **kwargs: CallbackResult(payload={"records": [{"Id": 1}, {"Id": 2}], "nextRecordsUrl": next_page_url}))
        m.get(url_pattern, callback=lambda *args, **kwargs: CallbackResult(payload={"records": [{"Id": 3, "propertyB": "B"}, {"Id": 4, "propertyB": "B"}]}))
        # 2 for 1 chunk above and 1 chunk had no next page
        for _ in range(chunks_len - 2):
            m.get(url_pattern, callback=lambda *args, **kwargs: CallbackResult(payload={"records": [{"Id": 3}, {"Id": 4}]}))

        records = [r async for r in stream.read_records(sync_mode=SyncMode.full_refresh)]
    assert records == [
        {"Id": 1, "propertyA": "A", "propertyB": "B"},
        {"Id": 2, "propertyA": "A", "propertyB": "B"},
        {"Id": 3, "propertyA": "A", "propertyB": "B"},
        {"Id": 4, "propertyA": "A", "propertyB": "B"},
    ]
    for call in requests_mock.request_history:
        assert len(call.url) < Salesforce.REQUEST_SIZE_LIMITS


@pytest.mark.asyncio
async def test_stream_with_no_records_in_response(stream_config, stream_api_v2_pk_too_many_properties):
    stream = await generate_stream_async("Account", stream_config, stream_api_v2_pk_too_many_properties)
    chunks = list(stream.chunk_properties())
    for chunk in chunks:
        assert stream.primary_key in chunk
    assert stream.too_many_properties
    assert stream.primary_key
    assert type(stream) == RestSalesforceStream
    url = re.compile(r"https://fase-account\.salesforce\.com/services/data/v57\.0/queryAll\??.*")
    await stream.ensure_session()

    with aioresponses() as m:
        m.get(url, repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload={"records": []}))
        records = [record async for record in stream.read_records(sync_mode=SyncMode.full_refresh)]
        assert records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,response_json,log_message",
    [
        (
                400,
                {"errorCode": "INVALIDENTITY", "message": "Account is not supported by the Bulk API"},
                "Account is not supported by the Bulk API",
        ),
        (403, {"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "API limit reached"}, "API limit reached"),
        (400, {"errorCode": "API_ERROR", "message": "API does not support query"}, "The stream 'Account' is not queryable,"),
        (
                400,
                {"errorCode": "LIMIT_EXCEEDED", "message": "Max bulk v2 query jobs (10000) per 24 hrs has been reached (10021)"},
                "Your API key for Salesforce has reached its limit for the 24-hour period. We will resume replication once the limit has elapsed.",
        ),
    ],
)
async def test_bulk_stream_error_in_logs_on_create_job(stream_config, stream_api, status_code, response_json, log_message, caplog):
    stream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()
    url = f"{stream.sf_api.instance_url}/services/data/{stream.sf_api.version}/jobs/query"

    with aioresponses() as m:
        m.post(url, status=status_code, callback=lambda *args, **kwargs: CallbackResult(status=status_code, payload=response_json, reason=""))
        query = "Select Id, Subject from Account"
        with caplog.at_level(logging.ERROR):
            assert await stream.create_stream_job(query, url) is None, "this stream should be skipped"

    # check logs
    assert log_message in caplog.records[-1].message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code,response_json,error_message",
    [
        (
            400,
            {
                "errorCode": "TXN_SECURITY_METERING_ERROR",
                "message": "We can't complete the action because enabled transaction security policies took too long to complete.",
            },
            'A transient authentication error occurred. To prevent future syncs from failing, assign the "Exempt from Transaction Security" user permission to the authenticated user.',
        ),
    ],
)
async def test_bulk_stream_error_on_wait_for_job(stream_config, stream_api, status_code, response_json, error_message):
    stream = await generate_stream_async("Account", stream_config, stream_api)
    await stream.ensure_session()
    url = f"{stream.sf_api.instance_url}/services/data/{stream.sf_api.version}/jobs/query/queryJobId"

    with aioresponses() as m:
        m.get(url, status=status_code, callback=lambda *args, **kwargs: CallbackResult(status=status_code, payload=response_json, reason=""))
        with pytest.raises(AirbyteTracedException) as e:
            await stream.wait_for_job(url=url)
        assert e.value.message == error_message



@pytest.mark.asyncio()
@freezegun.freeze_time("2023-01-01")
async def test_bulk_stream_slices(stream_config_date_format, stream_api):
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("FakeBulkStream", stream_config_date_format, stream_api)
    stream_slices = [s async for s in stream.stream_slices(sync_mode=SyncMode.full_refresh)]
    expected_slices = []
    today = pendulum.today(tz="UTC")
    start_date = pendulum.parse(stream.start_date, tz="UTC")
    while start_date < today:
        expected_slices.append(
            {
                "start_date": start_date.isoformat(timespec="milliseconds"),
                "end_date": min(today, start_date.add(days=stream.STREAM_SLICE_STEP)).isoformat(timespec="milliseconds"),
            }
        )
        start_date = start_date.add(days=stream.STREAM_SLICE_STEP)
    assert expected_slices == stream_slices


@pytest.mark.asyncio
@freezegun.freeze_time("2023-04-01")
async def test_bulk_stream_request_params_states(stream_config_date_format, stream_api, bulk_catalog):
    stream_config_date_format.update({"start_date": "2023-01-01"})
    stream: BulkIncrementalSalesforceStream = await generate_stream_async("Account", stream_config_date_format, stream_api)
    await stream.ensure_session()

    source = SalesforceSourceDispatcher(AsyncSourceSalesforce(_ANY_CATALOG, _ANY_CONFIG))
    source.streams = Mock()
    source.streams.return_value = [stream]
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    job_id_1 = "fake_job_1"
    job_id_2 = "fake_job_2"
    job_id_3 = "fake_job_3"

    with aioresponses() as m:
        m.post(base_url, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id_1}))
        m.get(base_url + f"/{job_id_1}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        m.delete(base_url + f"/{job_id_1}")
        m.get(base_url + f"/{job_id_1}/results",
              callback=lambda *args, **kwargs: CallbackResult(body="Field1,LastModifiedDate,ID\ntest,2023-01-15,1"))
        m.patch(base_url + f"/{job_id_1}")

        m.post(base_url, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id_2}))
        m.get(base_url + f"/{job_id_2}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        m.delete(base_url + f"/{job_id_2}")
        m.get(base_url + f"/{job_id_2}/results",
              callback=lambda *args, **kwargs: CallbackResult(body="Field1,LastModifiedDate,ID\ntest,2023-04-01,2\ntest,2023-02-20,22"))
        m.patch(base_url + f"/{job_id_2}")

        m.post(base_url, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id_3}))
        m.get(base_url + f"/{job_id_3}", callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        m.delete(base_url + f"/{job_id_3}")
        m.get(base_url + f"/{job_id_3}/results",
              callback=lambda *args, **kwargs: CallbackResult(body="Field1,LastModifiedDate,ID\ntest,2023-04-01,3"))
        m.patch(base_url + f"/{job_id_3}")

        logger = logging.getLogger("airbyte")
        state = {"Account": {"LastModifiedDate": "2023-01-01T10:10:10.000Z"}}
        bulk_catalog.streams.pop(1)
        result = [i for i in source.read(logger=logger, config=stream_config_date_format, catalog=bulk_catalog, state=state)]

    actual_state_values = [item.state.data.get("Account").get(stream.cursor_field) for item in result if item.type == Type.STATE]
    queries_history = m.requests

    # assert request params
    assert (
            "LastModifiedDate >= 2023-01-01T10:10:10.000+00:00 AND LastModifiedDate < 2023-01-31T10:10:10.000+00:00"
            in queries_history[("POST", URL(base_url))][0].kwargs["json"]["query"]
    )
    assert (
            "LastModifiedDate >= 2023-01-31T10:10:10.000+00:00 AND LastModifiedDate < 2023-03-02T10:10:10.000+00:00"
            in queries_history[("POST", URL(base_url))][1].kwargs["json"]["query"]
    )
    assert (
            "LastModifiedDate >= 2023-03-02T10:10:10.000+00:00 AND LastModifiedDate < 2023-04-01T00:00:00.000+00:00"
            in queries_history[("POST", URL(base_url))][2].kwargs["json"]["query"]
    )

    # assert states
    expected_state_values = ["2023-01-15T00:00:00+00:00", "2023-03-02T10:10:10+00:00", "2023-04-01T00:00:00+00:00"]
    assert actual_state_values == expected_state_values


@pytest.mark.asyncio
async def test_request_params_incremental(stream_config_date_format, stream_api):
    stream = await generate_stream_async("ContentDocument", stream_config_date_format, stream_api)
    params = stream.request_params(stream_state={}, stream_slice={'start_date': '2020', 'end_date': '2021'})

    assert params == {'q': 'SELECT LastModifiedDate, Id FROM ContentDocument WHERE LastModifiedDate >= 2020 AND LastModifiedDate < 2021'}


@pytest.mark.asyncio
async def test_request_params_substream(stream_config_date_format, stream_api):
    stream = await generate_stream_async("ContentDocumentLink", stream_config_date_format, stream_api)
    params = stream.request_params(stream_state={}, stream_slice={'parents': [{'Id': 1}, {'Id': 2}]})

    assert params == {"q": "SELECT LastModifiedDate, Id FROM ContentDocumentLink WHERE ContentDocumentId IN ('1','2')"}


@pytest.mark.asyncio
@freezegun.freeze_time("2023-03-20")
async def test_stream_slices_for_substream(stream_config, stream_api):
    stream_config['start_date'] = '2023-01-01'
    stream: BulkSalesforceSubStream = await generate_stream_async("ContentDocumentLink", stream_config, stream_api)
    stream.SLICE_BATCH_SIZE = 2  # each ContentDocumentLink should contain 2 records from parent ContentDocument stream
    await stream.ensure_session()

    job_id = "fake_job"
    base_url = f"{stream.sf_api.instance_url}{stream.path()}"

    with aioresponses() as m:
        m.post(base_url, repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload={"id": job_id}))
        m.get(base_url + f"/{job_id}", repeat=True, callback=lambda *args, **kwargs: CallbackResult(payload={"state": "JobComplete"}))
        m.get(base_url + f"/{job_id}/results", repeat=True, callback=lambda *args, **kwargs: CallbackResult(body="Field1,LastModifiedDate,ID\ntest,2021-11-16,123", headers={"Sforce-Locator": "null"}))
        m.delete(base_url + f"/{job_id}", repeat=True, callback=lambda *args, **kwargs: CallbackResult())

        stream_slices = [slice async for slice in stream.stream_slices(sync_mode=SyncMode.full_refresh)]
        assert stream_slices == [
             {'parents': [{'Field1': 'test', 'ID': '123', 'LastModifiedDate': '2021-11-16'},
                          {'Field1': 'test', 'ID': '123', 'LastModifiedDate': '2021-11-16'}]},
             {'parents': [{'Field1': 'test', 'ID': '123', 'LastModifiedDate': '2021-11-16'}]}
        ]
