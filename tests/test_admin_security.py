from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from skyledger.main import require_admin_access


def request_from(host: str) -> Request:
    return Request({"type": "http", "client": (host, 12345), "headers": []})


def test_local_admin_access_requires_no_token() -> None:
    asyncio.run(require_admin_access(request_from("127.0.0.1"), None))
    asyncio.run(require_admin_access(request_from("::1"), None))


def test_remote_admin_access_requires_matching_bearer_token() -> None:
    with patch.dict("os.environ", {"SKYLEDGER_ADMIN_TOKEN": "correct-horse"}, clear=False):
        with pytest.raises(HTTPException) as missing:
            asyncio.run(require_admin_access(request_from("192.0.2.5"), None))
        assert missing.value.status_code == 403

        with pytest.raises(HTTPException):
            asyncio.run(require_admin_access(request_from("192.0.2.5"), "Bearer wrong"))

        asyncio.run(
            require_admin_access(request_from("192.0.2.5"), "Bearer correct-horse")
        )
