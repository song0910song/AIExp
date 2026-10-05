from __future__ import annotations

import base64
import json

import pytest
import requests

from lighting_agent.config import Settings
from lighting_agent.document_loader import DocumentLoadError, PaddleOCRClient, _extract_text


class FakeResponse:
    def __init__(self, payload, status_code=200, text=None):
        self.payload = payload
        self.status_code = status_code
        self.text = text if text is not None else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            response._content = self.text.encode("utf-8")
            response.encoding = "utf-8"
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


class WrappedJobSession(FakeSession):
    def __init__(self, submission):
        super().__init__()
        self.submission = submission

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResponse(self.submission)

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        return FakeResponse({
            "data": {
                "taskStatus": "completed",
                "result": {"markdown": {"text": "recognized text"}},
            }
        })


class OfficialPaddleOCRSession:
    def __init__(self):
        self.calls = []
        self.status_responses = [
            {"data": {"state": "running", "extractProgress": {"totalPages": 1}}},
            {"data": {
                "state": "done",
                "resultUrl": {"jsonUrl": "https://ocr.example/results/job-1.jsonl"},
            }},
        ]

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResponse({"data": {"jobId": "job-1"}})

    def get(self, url, **kwargs):
        self.calls.append(("get", url, kwargs))
        if url.endswith(".jsonl"):
            return FakeResponse(None, text=(
                '{"result":{"layoutParsingResults":[{"markdown":{"text":"page one"}}]}}\n'
                '{"result":{"layoutParsingResults":[{"markdown":{"text":"page two"}}]}}\n'
            ))
        return FakeResponse(self.status_responses.pop(0))


class SyncPaddleOCRSession:
    def __init__(self):
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append(("post", url, kwargs))
        return FakeResponse({
            "result": {
                "layoutParsingResults": [
                    {"markdown": {"text": "synchronous OCR result"}},
                ]
            }
        })


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
    assert json.loads(session.calls[0][2]["data"]["optionalPayload"]) == {
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    assert session.calls[0][2]["data"]["model"] == "PaddleOCR-VL-1.6"
    assert session.calls[0][2]["files"]["file"][0] == "scan.pdf"


def test_paddleocr_missing_token_fails_before_request(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = FakeSession()
    client = PaddleOCRClient(Settings(paddleocr_access_token=None), session=session)

    with pytest.raises(DocumentLoadError, match="PADDLEOCR_ACCESS_TOKEN"):
        client.extract_pdf_result(pdf)

    assert session.calls == []


@pytest.mark.parametrize(
    "submission",
    [
        {"job_id": "job-1"},
        {"task_id": "job-1"},
        {"jobId": "job-1"},
        {"data": {"taskId": "job-1"}},
    ],
)
def test_paddleocr_accepts_wrapped_and_camel_case_job_identifiers(tmp_path, submission):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = WrappedJobSession(submission)
    client = PaddleOCRClient(
        Settings(paddleocr_access_token="test-token", paddleocr_timeout_seconds=5, paddleocr_poll_interval_seconds=0),
        session=session,
    )

    result = client.extract_pdf_result(pdf)

    assert result["data"]["result"]["markdown"]["text"] == "recognized text"
    assert session.calls[1][1].endswith("/job-1")


def test_paddleocr_follows_official_job_state_and_jsonl_result_format(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = OfficialPaddleOCRSession()
    client = PaddleOCRClient(
        Settings(
            paddleocr_access_token="test-token",
            paddleocr_timeout_seconds=5,
            paddleocr_poll_interval_seconds=0,
        ),
        session=session,
    )

    result = client.extract_pdf_result(pdf)

    assert result["data"]["result"][0]["layoutParsingResults"][0]["markdown"]["text"] == "page one"
    assert result["data"]["result"][1]["layoutParsingResults"][0]["markdown"]["text"] == "page two"
    assert _extract_text(result) == "page one\n\npage two"
    assert [method for method, _, _ in session.calls] == ["post", "get", "get", "get"]
    assert session.calls[-1][1] == "https://ocr.example/results/job-1.jsonl"
    assert "headers" not in session.calls[-1][2]


def test_paddleocr_sync_endpoint_uses_documented_base64_json_protocol(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf bytes")
    session = SyncPaddleOCRSession()
    client = PaddleOCRClient(
        Settings(
            paddleocr_api_url="https://ocr.example/api/v2/ocr/parse",
            paddleocr_access_token="test-token",
        ),
        session=session,
    )

    result = client.extract_pdf_result(pdf)

    _, url, kwargs = session.calls[0]
    assert url == "https://ocr.example/api/v2/ocr/parse"
    assert kwargs["headers"] == {
        "Authorization": "token test-token",
        "Content-Type": "application/json",
    }
    assert kwargs["json"] == {
        "file": base64.b64encode(b"pdf bytes").decode("ascii"),
        "fileType": 0,
        "useDocOrientationClassify": False,
        "useDocUnwarping": False,
        "useChartRecognition": False,
    }
    assert _extract_text(result) == "synchronous OCR result"


def test_paddleocr_http_500_error_includes_provider_response_body(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")

    class ServerErrorSession(FakeSession):
        def post(self, url, **kwargs):
            self.calls.append(("post", url, kwargs))
            return FakeResponse({"message": "internal error"}, status_code=500, text="model gateway unavailable")

    client = PaddleOCRClient(
        Settings(paddleocr_access_token="test-token"),
        session=ServerErrorSession(),
    )

    with pytest.raises(DocumentLoadError, match="HTTP 500.*model gateway unavailable"):
        client.extract_pdf_result(pdf)


def test_paddleocr_reports_provider_error_payload(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = WrappedJobSession({"error_code": 1002, "error_msg": "unsupported model"})
    client = PaddleOCRClient(Settings(paddleocr_access_token="test-token"), session=session)

    with pytest.raises(DocumentLoadError, match="unsupported model"):
        client.extract_pdf_result(pdf)


def test_paddleocr_reports_camel_case_provider_error_payload(tmp_path):
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"pdf")
    session = WrappedJobSession({"code": 401, "message": "invalid access token"})
    client = PaddleOCRClient(Settings(paddleocr_access_token="test-token"), session=session)

    with pytest.raises(DocumentLoadError, match="invalid access token"):
        client.extract_pdf_result(pdf)


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
