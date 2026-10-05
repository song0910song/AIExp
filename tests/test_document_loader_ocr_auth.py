from __future__ import annotations

import pytest
import requests

from lighting_agent.config import Settings
from lighting_agent.document_loader import DocumentLoadError, PaddleOCRClient


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.HTTPError(response=response)

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, fail_method=None, status_code=401):
        self.calls = []
        self.fail_method = fail_method
        self.status_code = status_code

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        status = self.status_code if self.fail_method == "post" else 200
        return FakeResponse({"job_id": "job-1"}, status_code=status)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        status = self.status_code if self.fail_method == "get" else 200
        return FakeResponse({"text": "recognized text"}, status_code=status)


def test_paddleocr_token_is_sent_for_submission_and_polling(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    token = "test-access-token"
    session = FakeSession()
    client = PaddleOCRClient(
        Settings(
            paddleocr_api_url="https://ocr.example/api/v2/ocr/jobs",
            paddleocr_access_token=token,
            paddleocr_timeout_seconds=5,
            paddleocr_poll_interval_seconds=0,
        ),
        session=session,
    )

    result = client.extract_pdf_result(pdf)

    assert result == {"text": "recognized text"}
    assert [method for method, _, _ in session.calls] == ["post", "get"]
    assert all(
        kwargs["headers"]["Authorization"] == f"Bearer {token}"
        for _, _, kwargs in session.calls
    )


def test_paddleocr_missing_token_fails_before_request(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = FakeSession()
    client = PaddleOCRClient(Settings(paddleocr_access_token=None), session=session)

    with pytest.raises(DocumentLoadError, match="PADDLEOCR_ACCESS_TOKEN"):
        client.extract_pdf_result(pdf)

    assert session.calls == []


@pytest.mark.parametrize("fail_method", ["post", "get"])
def test_paddleocr_auth_error_explains_how_to_fix_token(tmp_path, fail_method):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = FakeSession(fail_method=fail_method)
    client = PaddleOCRClient(
        Settings(
            paddleocr_access_token="rejected-token",
            paddleocr_timeout_seconds=5,
            paddleocr_poll_interval_seconds=0,
        ),
        session=session,
    )

    with pytest.raises(DocumentLoadError, match="valid, current AI Studio access token"):
        client.extract_pdf_result(pdf)
