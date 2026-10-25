import logging
import os
import re
import time
from urllib.parse import urljoin

import requests

from mcp_servers.common.errors import PortalIntegrationError

logger = logging.getLogger(__name__)


def _safe_path(path: str) -> str:
    path = re.sub(r"(/application-drafts/)[^/]+", r"\1<draft_ref>", path)
    return re.sub(r"(/applications/)\d+", r"\1<application_id>", path)


class PortalClient:
    def __init__(self, base_url: str | None = None):
        self.base_url = (
            base_url
            or os.getenv("PORTAL_BASE_URL", "http://localhost:8000")
        ).rstrip("/")
        self.timeout = float(os.getenv("PORTAL_TIMEOUT_SECONDS", "15"))

    def request(
        self,
        method: str,
        path: str,
        access_token: str | None = None,
        *,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> dict:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Portal API path must begin with one slash.")

        url = urljoin(self.base_url + "/", path.lstrip("/"))
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"

        started = time.perf_counter()
        status_code = None
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers=headers,
                json=json_body,
                params=params,
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = response.status_code

            if response.is_redirect:
                raise PortalIntegrationError(
                    "The portal redirected the API request. "
                    "Check the API route and authentication."
                )

            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except ValueError:
                    error_data = {}
                if not isinstance(error_data, dict):
                    error_data = {}
                message = error_data.get("error", "The portal rejected the request.")
                validation_errors = error_data.get("validation_errors")
                if validation_errors:
                    message += " " + "; ".join(validation_errors)
                raise PortalIntegrationError(
                    f"Portal request failed ({response.status_code}): {message}"
                )

            try:
                data = response.json()
            except ValueError as exc:
                raise PortalIntegrationError(
                    "The portal returned a non-JSON response."
                ) from exc

            if not isinstance(data, dict):
                raise PortalIntegrationError(
                    "Unexpected portal response format."
                )

            return data

        except requests.RequestException as exc:
            raise PortalIntegrationError(
                "The existing portal could not complete the request."
            ) from exc
        finally:
            logger.info(
                "mcp_portal_http method=%s path=%s status=%s elapsed_ms=%.2f",
                method.upper(),
                _safe_path(path),
                status_code if status_code is not None else "error",
                (time.perf_counter() - started) * 1000,
            )

    def request_bytes(
        self,
        method: str,
        path: str,
        access_token: str,
    ) -> tuple[bytes, str]:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Portal API path must begin with one slash.")

        url = urljoin(self.base_url + "/", path.lstrip("/"))
        started = time.perf_counter()
        status_code = None
        try:
            response = requests.request(
                method=method.upper(),
                url=url,
                headers={
                    "Accept": "application/pdf",
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=self.timeout,
                allow_redirects=False,
            )
            status_code = response.status_code
            if response.is_redirect:
                raise PortalIntegrationError(
                    "The portal redirected the API request. "
                    "Check the API route and authentication."
                )
            if response.status_code >= 400:
                try:
                    error_data = response.json()
                except ValueError:
                    error_data = {}
                if not isinstance(error_data, dict):
                    error_data = {}
                message = error_data.get("error", "The portal rejected the request.")
                raise PortalIntegrationError(
                    f"Portal request failed ({response.status_code}): {message}"
                )

            content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
            if content_type != "application/pdf":
                raise PortalIntegrationError(
                    "The portal returned a non-PDF application slip."
                )
            return response.content, response.headers.get(
                "Content-Disposition", 'attachment; filename="application-slip.pdf"'
            )
        except requests.RequestException as exc:
            raise PortalIntegrationError(
                "The existing portal could not complete the request."
            ) from exc
        finally:
            logger.info(
                "mcp_portal_http method=%s path=%s status=%s elapsed_ms=%.2f",
                method.upper(),
                _safe_path(path),
                status_code if status_code is not None else "error",
                (time.perf_counter() - started) * 1000,
            )
