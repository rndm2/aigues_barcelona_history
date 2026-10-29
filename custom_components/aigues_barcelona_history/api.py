"""Provider API protocol, safe failures and token handling for History."""

from __future__ import annotations

import base64
import binascii
import datetime
from email.utils import parsedate_to_datetime
import json
import logging
import math
import re
import time
from typing import TypedDict

import requests
from twocaptcha import TwoCaptcha, api

from .const import (
    API_COOKIE_TOKEN,
    API_ERROR_TOKEN_REVOKED,
    API_HOST,
    RECAPTCHA_V2_PAGEURL,
    RECAPTCHA_V2_SITEKEY,
)
from .version import VERSION

TIMEOUT = 60
_SESSION_LIMIT_CODES = frozenset({"MAX_SESSIONS", "MAX_SESSIONS_REACHED_ERROR"})
_LOGGER = logging.getLogger(__name__)


class AiguesApiError(Exception):
    """Safe provider failure with structured context, never a response payload."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.retry_after = retry_after


class AiguesApiAuthError(AiguesApiError):
    """Authentication or authorization error."""


class AiguesApiSessionLimitError(AiguesApiAuthError):
    """A provider session limit requires intervention before another paid login."""


class AiguesApiRateLimitError(AiguesApiError):
    """Rate-limit error."""


class AiguesApiServerError(AiguesApiError):
    """Server-side API error."""


def _provider_error_code(value, depth: int = 0) -> str | None:
    """Extract recognized error codes, including nested JSON error messages."""
    if depth > 6:
        return None
    if isinstance(value, str):
        for code in _SESSION_LIMIT_CODES:
            if re.search(rf"\b{code}\b", value, flags=re.IGNORECASE):
                return code
        if API_ERROR_TOKEN_REVOKED.casefold() in value.casefold():
            return "JWT_TOKEN_REVOKED"
        try:
            nested = json.loads(value)
        except ValueError, TypeError:
            return None
        return _provider_error_code(nested, depth + 1)
    if isinstance(value, dict):
        fields = (
            "errorMessage",
            "message",
            "error",
            "errors",
            "errorCode",
            "error_code",
            "code",
            "status",
            "details",
        )
        values = (value[field] for field in fields if field in value)
    elif isinstance(value, list):
        values = iter(value)
    else:
        return None
    return next(
        (code for item in values if (code := _provider_error_code(item, depth + 1))),
        None,
    )


def _parse_retry_after(value) -> float | None:
    """Return a finite provider delay without shortening a valid Retry-After."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            # Obsolete HTTP asctime dates omit a zone but still mean UTC.
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=datetime.timezone.utc)
            delay = max(0.0, deadline.timestamp() - time.time())
        except ValueError, TypeError, OverflowError:
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


class ChallengeResponse(TypedDict):
    captchaId: str
    code: str


class AiguesBarcelonaHistoryApiClient:
    _global_captcha_cooldown_until = None
    _global_login_in_progress = False

    def __init__(
        self,
        username,
        password,
        twocaptcha_api_key,
        contract=None,
        session: requests.Session | None = None,
    ):
        if session is None:
            session = requests.Session()
        self.cli = session
        self.api_host = f"https://{API_HOST}"
        # https://www.aiguesdebarcelona.cat/o/ofex-theme/js/chunk-vendors.e5935b72.js
        # https://www.aiguesdebarcelona.cat/o/ofex-theme/js/app.0499d168.js
        self.headers = {
            "Ocp-Apim-Subscription-Key": "3cca6060fee14bffa3450b19941bd954",
            "Ocp-Apim-Trace": "false",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": f"hass-aigues-barcelona-history/{VERSION} (Home Assistant)",
        }
        self._username = username
        self._password = password
        self._twocaptcha_api_key = twocaptcha_api_key
        self._contract = contract

        self.last_response = None

    def get_token(self):
        return self.cli.cookies.get_dict().get(API_COOKIE_TOKEN)

    def _token_payload(self) -> dict:
        """Decode optional token hints; malformed stored tokens are unusable."""
        token = self.get_token()
        if not isinstance(token, str):
            return {}
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        try:
            raw = base64.b64decode(
                parts[1] + "=" * (-len(parts[1]) % 4), altchars=b"-_", validate=True
            )
            payload = json.loads(raw.decode("utf-8"))
        except ValueError, TypeError, UnicodeError, binascii.Error:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _return_token_field(self, key):
        return self._token_payload().get(key)

    def _query(self, path, query=None, json=None, headers=None, method="GET"):
        headers = {**self.headers, **(headers or {})}
        try:
            response = self.cli.request(
                method=method,
                url=f"{self.api_host}/{path.lstrip('/')}",
                params=query,
                json=json,
                headers=headers,
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException as error:
            # Transport messages can contain the complete URL, including the
            # login CAPTCHA solution. Preserve retry classification, not payloads.
            if isinstance(
                error,
                (
                    requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                ),
            ):
                raise type(error)("Provider network request failed") from None
            raise AiguesApiError("Provider request failed before a response") from None
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        self.last_response = parsed if parsed is not None else response.text
        _LOGGER.debug("Query %s %s completed with status %s", method, path, response.status_code)

        status = response.status_code
        error_code = _provider_error_code(parsed if parsed is not None else response.text)
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        context = {
            "status_code": status,
            "error_code": error_code,
            "retry_after": retry_after,
        }
        # Some provider failures carry HTTP 200 or 500 despite being an account
        # session limit. Identify these before generic status classification.
        if error_code in _SESSION_LIMIT_CODES:
            raise AiguesApiSessionLimitError(
                f"Provider session limit reached ({error_code})", **context
            )
        if error_code == "JWT_TOKEN_REVOKED":
            raise AiguesApiAuthError("Provider token has been revoked", **context)
        if status in (501, 505):
            raise AiguesApiError(f"Provider protocol is unsupported (HTTP {status})", **context)
        if status == 408 or 500 <= status < 600:
            raise AiguesApiServerError(f"Provider request failed (HTTP {status})", **context)
        if status in (401, 403):
            raise AiguesApiAuthError(f"Provider access denied (HTTP {status})", **context)
        if status == 429:
            raise AiguesApiRateLimitError("Provider rate limit reached (HTTP 429)", **context)
        if 400 <= status < 500:
            raise AiguesApiError(f"Provider request rejected (HTTP {status})", **context)
        return response

    @classmethod
    def _set_captcha_cooldown(cls, minutes: int) -> None:
        cls._global_captcha_cooldown_until = datetime.datetime.now(
            datetime.timezone.utc
        ) + datetime.timedelta(minutes=minutes)

    def login(self, user=None, password=None):
        now = datetime.datetime.now(datetime.timezone.utc)

        if (
            AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until
            and now < AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until
        ):
            raise AiguesApiRateLimitError(
                "2Captcha cooldown active until "
                f"{AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until.isoformat()}"
            )

        if AiguesBarcelonaHistoryApiClient._global_login_in_progress:
            raise AiguesApiRateLimitError("Login already in progress")

        AiguesBarcelonaHistoryApiClient._global_login_in_progress = True

        try:
            client = TwoCaptcha(self._twocaptcha_api_key)

            try:
                response: ChallengeResponse = client.recaptcha(
                    sitekey=RECAPTCHA_V2_SITEKEY, url=RECAPTCHA_V2_PAGEURL
                )

                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("code"), str)
                    or not response["code"]
                ):
                    self._set_captcha_cooldown(15)
                    raise RuntimeError("2Captcha did not return a solution")

                recaptcha = response["code"]
            except api.NetworkException as e:
                self._set_captcha_cooldown(5)
                _LOGGER.error("2Captcha network request failed (%s)", type(e).__name__)
                raise type(e)("2Captcha network request failed") from None
            except api.ApiException as e:
                self._set_captcha_cooldown(30)
                _LOGGER.error("2Captcha API request failed (%s)", type(e).__name__)
                raise type(e)("2Captcha API request failed") from None
            except Exception as e:
                self._set_captcha_cooldown(15)
                _LOGGER.error("2Captcha request failed (%s)", type(e).__name__)
                raise RuntimeError("2Captcha request failed") from None

            if user is None:
                user = self._username
            if password is None:
                password = self._password
            if recaptcha is None:
                recaptcha = ""

            path = "/ofex-login-api/auth/getToken"
            query = {"lang": "ca", "recaptchaClientResponse": recaptcha}
            body = {
                "scope": "ofex",
                "companyIdentification": "",
                "userIdentification": user,
                "password": password,
            }
            headers = {
                "Content-Type": "application/json",
                "Ocp-Apim-Subscription-Key": (
                    "6a98b8b8c7b243cda682a43f09e6588b;product=portlet-login-ofex"
                ),
            }

            try:
                r = self._query(path, query, body, headers, method="POST")
            except Exception as e:
                self._set_captcha_cooldown(30)
                _LOGGER.error("Login POST failed, setting cooldown (%s)", type(e).__name__)
                raise

            _LOGGER.debug("Login POST completed with status %s", r.status_code)

            try:
                payload = r.json()
            except ValueError, TypeError:
                payload = None
            error = payload.get("errorMessage") if isinstance(payload, dict) else True

            if error:
                _LOGGER.warning("Provider login rejected the request")
                self._set_captcha_cooldown(15)

                return False

            access_token = payload.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                _LOGGER.warning("Access token missing")
                self._set_captcha_cooldown(15)

                return False

            self.set_token(access_token)

            AiguesBarcelonaHistoryApiClient._global_captcha_cooldown_until = None

            return True
        finally:
            AiguesBarcelonaHistoryApiClient._global_login_in_progress = False

        # set as cookie: ofexTokenJwt
        # https://www.aiguesdebarcelona.cat/ca/area-clientes

    def logout(self):
        path = "/ofex-login-api/auth/logout"
        headers = {
            "Content-Type": "application/json",
            "Ocp-Apim-Subscription-Key": (
                "6a98b8b8c7b243cda682a43f09e6588b;product=portlet-login-ofex"
            ),
        }

        try:
            self._query(path, query=None, json=None, headers=headers, method="POST")
        except Exception as e:
            _LOGGER.debug("Logout failed (%s)", type(e).__name__)
        finally:
            self.cli.cookies.clear(
                domain="." + API_HOST.split(".", 1)[1],
                path="/",
                name=API_COOKIE_TOKEN,
            )

    def set_token(self, token: str):
        host = ".".join(self.api_host.split(".")[1:])
        cookie_data = {
            "name": API_COOKIE_TOKEN,
            "value": token,
            "domain": f".{host}",
            "path": "/",
            "secure": True,
            "rest": {"HttpOnly": True, "SameSite": "None"},
        }
        cookie = requests.cookies.create_cookie(**cookie_data)
        _LOGGER.debug("set_token called")

        return self.cli.cookies.set_cookie(cookie)

    def is_token_expired(self) -> bool:
        """Compare NumericDate expiry with epoch time, independent of local DST."""
        expires = self._return_token_field("exp")
        if isinstance(expires, bool) or not isinstance(expires, (int, float)):
            return True
        try:
            if not math.isfinite(expires):
                return True
        except OverflowError:
            return True
        return time.time() >= expires

    def profile(self, user=None):
        if user is None:
            user = self._return_token_field("name")

        path = "/ofex-login-api/auth/getProfile"
        query = {"lang": "ca", "userId": user, "clientId": user}
        headers = {
            "Ocp-Apim-Subscription-Key": (
                "6a98b8b8c7b243cda682a43f09e6588b;product=portlet-login-ofex"
            )
        }

        r = self._query(path, query, json=None, headers=headers, method="POST")

        profile = r.json()
        if not profile.get("user_data"):
            raise AiguesApiError("User data missing")

        return profile

    def contracts(self, user=None, status=None):
        if status is None:
            status = ["ASSIGNED", "PENDING"]
        if user is None:
            user = self._return_token_field("name")
        if isinstance(status, str):
            status = [status]

        path = "/ofex-contracts-api/contracts"
        query = {"lang": "ca", "userId": user, "clientId": user}
        for idx, stat in enumerate(status):
            query[f"assignationStatus[{str(idx)}]"] = stat.upper()

        r = self._query(path, query)

        data = r.json().get("data")

        return data

    @property
    def contract_id(self):
        return [x["contractDetail"]["contractNumber"] for x in self.contracts()]

    @property
    def first_contract(self):
        contract_ids = self.contract_id
        if len(contract_ids) != 1:
            raise AiguesApiError("Provide a Contract ID to retrieve specific invoices")

        return contract_ids[0]

    def invoices(self, contract=None, user=None, last_months=36, mode="ALL"):
        if user is None:
            user = self._return_token_field("name")
        if contract is None:
            contract = self.first_contract

        path = "/ofex-invoices-api/invoices"
        query = {
            "contractNumber": contract,
            "userId": user,
            "clientId": user,
            "lang": "ca",
            "lastMonths": last_months,
            "mode": mode,
        }

        r = self._query(path, query)

        data = r.json().get("data")

        return data

    def invoices_debt(self, contract=None, user=None):
        return self.invoices(contract, user, last_months=0, mode="DEBT")

    def consumptions(self, date_from, date_to=None, contract=None, user=None, frequency="HOURLY"):
        if user is None:
            user = self._return_token_field("name")
        if contract is None:
            contract = self.first_contract
        if frequency not in ["HOURLY", "DAILY"]:
            raise ValueError(f"Invalid {frequency=}")

        if date_to is None:
            date_to = date_from + datetime.timedelta(days=1)
        if isinstance(date_from, datetime.date):
            date_from = date_from.strftime("%d-%m-%Y")
        if isinstance(date_to, datetime.date):
            date_to = date_to.strftime("%d-%m-%Y")

        path = "/ofex-water-consumptions-api/meter/consumptions"
        query = {
            "consumptionFrequency": frequency,
            "contractNumber": contract,
            "clientId": user,
            "userId": user,
            "lang": "ca",
            "fromDate": date_from,
            "toDate": date_to,
            "showNegativeValues": "false",
        }

        r = self._query(path, query)

        data = r.json().get("data")
        return data

    def consumptions_week(self, date_from: datetime.date, contract=None, user=None):
        if date_from is None:
            date_from = datetime.datetime.now()
        # get first day of week
        monday = date_from - datetime.timedelta(days=date_from.weekday())
        sunday = monday + datetime.timedelta(days=6)

        return self.consumptions(monday, sunday, contract, user, frequency="DAILY")

    def consumptions_month(self, date_from: datetime.date, contract=None, user=None):
        first = date_from.replace(day=1)
        next_month = date_from.replace(day=28) + datetime.timedelta(days=4)
        last = next_month - datetime.timedelta(days=next_month.day)

        return self.consumptions(first, last, contract, user, frequency="DAILY")

    def parse_consumptions(self, info, key="accumulatedConsumption"):
        return [x[key] for x in info]
