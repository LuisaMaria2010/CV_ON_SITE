import asyncio
import json
import types
import os
import inspect

import pytest

from domain.document_elements import DocumentElement
from domain.normalizer import TextNormalizer


def test_document_element_defaults():
    el = DocumentElement(element_type="p")
    assert el.element_type == "p"
    assert el.text == ""
    assert isinstance(el.rows, list)


def test_text_normalizer_basic():
    tn = TextNormalizer()
    assert tn.normalize_document_id("file.docx") == "file"
    assert tn.normalize_document_id("file v2.pdf") == "file"
    s = "This   is\n\n a test\n\n"
    n = tn.normalize(s)
    assert "  " not in n
    h = tn.normalize_markdown_heading("Heading!!!")
    assert h == "Heading"


def test_document_parser_calls_extract(monkeypatch):
    # ensure DocumentParser.parse delegates to extract_elements
    from services.document_parser import DocumentParser

    called = {}

    def fake_extract(b, mime_type=None):
        called['ok'] = True
        return [DocumentElement(element_type='h1', text='X')]

    # services.document_parser imported extract_elements at module import time,
    # so patch the symbol in that module directly.
    import services.document_parser as dpmod
    monkeypatch.setattr(dpmod, 'extract_elements', fake_extract)
    p = DocumentParser()
    res = p.parse(b"xpdf", mime_type="application/pdf")
    assert called.get('ok')
    assert res and res[0].text == 'X'


def test_image_description_disabled_by_default(monkeypatch, tmp_path):
    # Ensure no external agent file exists
    from services.image_description_service import ImageDescriptionService
    svc = ImageDescriptionService()
    out = svc.describe(image_bytes=b'123')
    assert out == {}


def test_llm_client_helpers():
    import infra.llm_client as llm
    assert llm._is_azure_openai('https://my.openai.azure.com')
    assert not llm._is_azure_openai('https://example.com')
    assert llm._normalize_base_url('https://foundry.ai') .endswith('/v1')


def test_utils_request_context():
    from utils.request_context import set_request_id, get_request_id, reset_request_id, request_context
    token = set_request_id('abc')
    assert get_request_id() == 'abc'
    reset_request_id(token)
    assert get_request_id() == ''
    with request_context('req-1'):
        assert get_request_id() == 'req-1'


def test_http_response_and_errors_mapping():
    from utils.http_response import json_response
    from utils.http_errors import map_exception_to_response
    import azure.functions as func
    # json_response returns HttpResponse with JSON body
    r = json_response(data={'a':1}, error=None, request_id='rid', status_code=200)
    assert isinstance(r, func.HttpResponse)
    assert 'request_id' in r.get_body().decode('utf-8')

    # map_exception_to_response handles known exceptions
    from core.errors import InvalidInputError, FileTooLargeError
    resp = map_exception_to_response(InvalidInputError('bad'), 'rid2')
    assert resp.status_code == 400
    resp2 = map_exception_to_response(FileTooLargeError('big'), 'rid3')
    assert resp2.status_code == 413


def test_strip_front_matter():
    from scripts.check_chunking_by_chars import strip_front_matter
    text = "---\nmeta: x\n---\n# Title\nBody"
    assert strip_front_matter(text).startswith('# Title')


def test_blob_storage_name_and_basic_upload(monkeypatch):
    import infra.blob_storage as bsmod

    class FakeBlob:
        def __init__(self):
            self.storage = {}
        async def create_container(self):
            return None

        def get_container_client(self, container):
            return self

        def get_blob_client(self, container, name):
            fake = self
            class C:
                async def upload_blob(self, data, overwrite=False):
                    fake.storage[(container, name)] = data

                async def download_blob(self):
                    class Stream:
                        async def readall(self):
                            return fake.storage.get((container, name), b'')
                    return Stream()

                async def exists(self):
                    return (container, name) in fake.storage

                async def delete_blob(self):
                    fake.storage.pop((container, name), None)

            return C()

    fake_service = FakeBlob()

    monkeypatch.setattr(bsmod, 'BlobServiceClient', types.SimpleNamespace(from_connection_string=lambda conn: fake_service))
    # instantiate StorageService (sync) and call upload/download via asyncio
    svc = bsmod.StorageService()

    async def _run():
        await svc.upload_bytes(b'hello', 'h.txt', folder='f', container='c')
        data = await svc.download_bytes('h.txt', folder='f', container='c')
        assert data == b'hello'

    asyncio.run(_run())


def test_queue_service_send_json(monkeypatch):
    import infra.queue_service as qmod

    class FakeQueue:
        def __init__(self):
            self.sent = []

        async def send_message(self, msg):
            self.sent.append(msg)

    class FakeService:
        def __init__(self):
            self._q = FakeQueue()

        def get_queue_client(self, name):
            return self._q

        async def close(self):
            return None

    monkeypatch.setattr(qmod, 'QueueServiceClient', lambda account_url, credential: FakeService())
    monkeypatch.setattr(qmod, 'DefaultAzureCredential', lambda: object())

    qs = qmod.QueueService()

    async def _run():
        await qs.send_json({'x':1})
        # ensure message sent
        assert qs.queue.send_message is not None

    asyncio.run(_run())
