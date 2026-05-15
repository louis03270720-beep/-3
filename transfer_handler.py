"""
Shared transfer-code handler with automatic save-version detection.

Instead of hardcoding a game version, this module:
  1. Sends the transfer reception request using the latest known client version.
  2. Validates the raw HTTP response (status, content-type, body length) and
     emits structured debug logs before touching the payload bytes.
  3. Reads the actual game version from the first 4 bytes of valid save data.
  4. Parses the save (bcsfe reads version from the header automatically).
  5. Falls back to IGNORE_PARSE_ERROR mode if parsing fails, allowing the bot to
     continue operating even when a save contains content bcsfe does not yet parse.
"""

import json as _json
import logging
import struct as _struct

from bcsfe import core
from bcsfe.core.io.config import ConfigKey
from bcsfe.core.io.save import FailedToLoadError
from bcsfe.core.server.server_handler import ServerHandler

_log = logging.getLogger(__name__)

_SAVE_URL = "https://nyanko-save.ponosgames.com"
_LATEST_GV = "15.2.0"

_REQUEST_HEADERS = {
    "content-type": "application/json",
    "accept-encoding": "gzip",
    "connection": "keep-alive",
    "user-agent": "Dalvik/2.1.0 (Linux; U; Android 9; SM-G955F Build/N2G48B)",
}

# Minimum plausible save size — 4-byte version header + at least a few bytes.
_MIN_SAVE_BYTES = 16

# Valid range for a Battle Cats game-version integer stored in the save header.
# e.g. 80000 (v8.0.0) – 999999 (v99.9.99)
_GV_MIN = 80_000
_GV_MAX = 999_999


class TransferError(RuntimeError):
    """Raised for known, user-displayable transfer/API errors."""


# ---------------------------------------------------------------------------
# Response validation helpers
# ---------------------------------------------------------------------------

def _log_response_debug(response) -> None:
    """Emit a structured debug block for every raw API response."""
    status = getattr(response, "status_code", "?")
    headers = dict(getattr(response, "headers", {}))
    body = getattr(response, "content", b"")

    _log.debug(
        "[Transfer API] status=%s  content-type=%s  body_len=%d",
        status,
        headers.get("content-type", "<missing>"),
        len(body),
    )
    _log.debug("[Transfer API] response headers: %s", headers)

    # Log first 256 bytes of body as hex + best-effort UTF-8 snippet
    preview_bytes = body[:256]
    _log.debug("[Transfer API] body (hex)  : %s", preview_bytes.hex())
    try:
        _log.debug("[Transfer API] body (text) : %s", preview_bytes.decode("utf-8", errors="replace"))
    except Exception:
        pass


# Nyanko transfer reception API: known statusCode values returned in JSON bodies.
# The reception endpoint ONLY returns application/octet-stream on true success.
# Any application/json response is an error; the statusCode tells us which one.
_NYANKO_STATUS_MESSAGES: dict[int, str] = {
    1:   "サーバーが予期しない成功コードを JSON で返しました (statusCode: 1)。",
    4:   "引き継ぎコードが存在しないか期限切れです。(statusCode: 4)",
    5:   "確認コードの試行回数が上限に達しました。しばらく待ってから再試行してください。(statusCode: 5)",
    6:   "確認コードが間違っています。(statusCode: 6)",
    7:   "このアカウントは既に別の端末で引き継ぎ済みです。(statusCode: 7)",
    8:   "リクエスト回数が上限に達しました。しばらく待ってから再試行してください。(statusCode: 8)",
    111: "引き継ぎコードは既に使用済みか、本日の引き継ぎ上限に達しています。(statusCode: 111)",
}


def _classify_error_response(response) -> str:
    """
    Inspect a non-octet-stream response and return a human-readable Japanese
    error string that describes what the server actually said.

    The Nyanko save API always returns application/octet-stream for a successful
    transfer reception. Any other content-type is an error. When the body is JSON,
    the 'statusCode' field (integer) identifies the specific failure reason.
    """
    status = getattr(response, "status_code", None)
    content_type = response.headers.get("content-type", "")
    body = getattr(response, "content", b"")

    # --- HTTP-level errors ---
    if status == 429:
        return "レートリミットに達しました。しばらく待ってから再試行してください。(HTTP 429)"
    if status in (401, 403):
        return f"認証エラー: 引き継ぎコードまたは確認コードが正しくありません。(HTTP {status})"
    if status is not None and status >= 500:
        return f"サーバー内部エラーが発生しました。しばらく待ってから再試行してください。(HTTP {status})"

    # --- Empty body ---
    if len(body) == 0:
        return f"サーバーが空のレスポンスを返しました。(HTTP {status})"

    # --- JSON body: Nyanko API error envelope ---
    if "json" in content_type or "text" in content_type:
        try:
            obj = _json.loads(body)
            if isinstance(obj, dict):
                # Primary field used by the Nyanko API for all JSON responses
                nyanko_code = obj.get("statusCode")
                if isinstance(nyanko_code, int):
                    known_msg = _NYANKO_STATUS_MESSAGES.get(nyanko_code)
                    if known_msg:
                        _log.warning(
                            "[Transfer API] Nyanko statusCode=%d (%s)",
                            nyanko_code, known_msg,
                        )
                        return known_msg
                    # Unknown statusCode — log full body for investigation
                    _log.warning(
                        "[Transfer API] Unknown Nyanko statusCode=%d  full body: %s",
                        nyanko_code, body.decode("utf-8", errors="replace"),
                    )
                    return (
                        f"サーバーエラー応答 (statusCode: {nyanko_code}, HTTP {status})。"
                        "このエラーは未知のコードです。管理者に報告してください。"
                    )
                # JSON but no statusCode field
                _log.warning(
                    "[Transfer API] JSON body without statusCode: %s",
                    body[:256].decode("utf-8", errors="replace"),
                )
        except _json.JSONDecodeError:
            pass
        # Non-JSON text or unparseable JSON
        snippet = body[:120].decode("utf-8", errors="replace").strip()
        _log.warning("[Transfer API] Non-JSON text body (HTTP %s): %r", status, snippet)
        return f"サーバーが不明なテキスト応答を返しました。(HTTP {status})"

    # --- Binary body with wrong content-type ---
    _log.warning(
        "[Transfer API] Unexpected content-type=%r  HTTP %s  body_len=%d",
        content_type, status, len(body),
    )
    return (
        f"サーバーが不正な形式のレスポンスを返しました。"
        f"(HTTP {status}, content-type: {content_type!r})"
    )


# ---------------------------------------------------------------------------
# Core download
# ---------------------------------------------------------------------------

def _download_raw_save(
    tc: str, confirmation_code: str, cc_obj, gv
) -> tuple[bytes, object]:
    """
    POST to the transfer reception endpoint and return (raw_bytes, response_headers).

    All response validation happens here; callers receive clean bytes or an
    exception with a descriptive message.

    Raises:
        ConnectionError : No response (network failure / timeout).
        TransferError   : Server returned a classifiable error (rate limit, bad
                          code, server 5xx, JSON error body, empty body).
        ValueError      : Unexpected content-type that isn't a known error shape.
    """
    url = f"{_SAVE_URL}/v2/transfers/{tc}/reception"

    data = core.ClientInfo(cc_obj, gv).get_client_info()
    data["pin"] = confirmation_code
    data_str = _json.dumps(data, separators=(",", ":")).replace(" ", "")

    _log.debug("[Transfer API] POST %s  tc=%s", url, tc)

    response = core.RequestHandler(url, _REQUEST_HEADERS, core.Data(data_str)).post()

    if response is None:
        _log.error("[Transfer API] No response object returned (network failure)")
        raise ConnectionError("サーバーへの接続に失敗しました")

    # Always emit the debug block before any further checks
    _log_response_debug(response)

    content_type = response.headers.get("content-type", "")
    body = getattr(response, "content", b"")

    # ── Happy path: canonical response ───────────────────────────────────────
    if "application/octet-stream" in content_type:
        if len(body) < _MIN_SAVE_BYTES:
            raise TransferError(
                f"サーバーが短すぎるセーブデータを返しました ({len(body)} bytes)。"
                "コードを確認してください。"
            )
        _log.info("[Transfer API] valid save payload received (%d bytes)", len(body))
        return body, response.headers

    # ── Fallback: wrong content-type but body is valid save bytes ─────────────
    # The Nyanko API sometimes returns content-type: application/json with
    # statusCode 111 (HTTP 200) while the response body is still raw binary
    # save data. Detect this by checking the first 4 bytes for a plausible
    # game-version integer before treating the response as an error.
    http_status = getattr(response, "status_code", None)
    if http_status == 200 and len(body) >= _MIN_SAVE_BYTES:
        try:
            raw_int = _struct.unpack("<i", body[:4])[0]
            if _GV_MIN <= raw_int <= _GV_MAX:
                _log.warning(
                    "[Transfer API] content-type=%r (expected octet-stream) but "
                    "body header bytes decode to valid game version %d — "
                    "treating as save data (%d bytes)",
                    content_type, raw_int, len(body),
                )
                return body, response.headers
        except Exception:
            pass

        # Body is JSON — check for statusCode 111 specifically and log it
        # clearly so the exact server response is visible in the console.
        if "json" in content_type or "text" in content_type:
            try:
                obj = _json.loads(body)
                sc = obj.get("statusCode") if isinstance(obj, dict) else None
                _log.warning(
                    "[Transfer API] HTTP 200 JSON body  statusCode=%s  full=%s",
                    sc, body.decode("utf-8", errors="replace"),
                )
            except Exception:
                pass

    # ── Error path ────────────────────────────────────────────────────────────
    _log.warning(
        "[Transfer API] unexpected response  content-type=%r  status=%s  body_len=%d",
        content_type, http_status, len(body),
    )
    raise TransferError(_classify_error_response(response))


# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------

def detect_version_from_bytes(raw_bytes: bytes) -> core.GameVersion | None:
    """
    Read game version from the first 4 bytes of save data (little-endian int32).

    Returns None if the bytes are too short or the decoded integer is outside
    the plausible Battle Cats version range (guards against interpreting random
    data as a version).
    """
    if len(raw_bytes) < 4:
        _log.warning("[Version] payload too short to read version header (%d bytes)", len(raw_bytes))
        return None

    raw_int = _struct.unpack("<i", raw_bytes[:4])[0]

    if not (_GV_MIN <= raw_int <= _GV_MAX):
        _log.warning(
            "[Version] first 4 bytes decode to %d — outside plausible range [%d, %d]; "
            "version undetected (payload may be invalid)",
            raw_int, _GV_MIN, _GV_MAX,
        )
        return None

    gv = core.GameVersion(raw_int)
    _log.info("[Version] detected game version %s (raw int=%d)", gv, raw_int)
    return gv


# ---------------------------------------------------------------------------
# Save parsing
# ---------------------------------------------------------------------------

def _parse_save(raw_bytes: bytes, cc_obj, detected_gv) -> core.SaveFile:
    """
    Parse raw save bytes into a SaveFile.
    Retries with IGNORE_PARSE_ERROR on version-related parse failures so fields
    parsed before the failure point are still usable.
    """
    cfg = core.core_data.config
    gv_str = str(detected_gv) if detected_gv else "unknown"

    try:
        sf = core.SaveFile(core.Data(raw_bytes), cc=cc_obj)
        _log.info("[Parse] save parsed successfully (version=%s)", gv_str)
        return sf
    except FailedToLoadError as first_err:
        _log.warning(
            "[Parse] initial parse failed for version=%s — retrying with "
            "IGNORE_PARSE_ERROR=True. Error: %s",
            gv_str, first_err,
        )
        original = cfg.get_bool(ConfigKey.IGNORE_PARSE_ERROR)
        cfg.set(ConfigKey.IGNORE_PARSE_ERROR, True)
        try:
            sf = core.SaveFile(core.Data(raw_bytes), cc=cc_obj)
            _log.info("[Parse] fallback parse succeeded (version=%s)", gv_str)
            return sf
        except Exception as e2:
            _log.error("[Parse] fallback parse also failed (version=%s): %s", gv_str, e2)
            raise FailedToLoadError(
                f"セーブデータのパースに完全に失敗しました (version={gv_str}): {e2}"
            ) from first_err
        finally:
            cfg.set(ConfigKey.IGNORE_PARSE_ERROR, original)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_handler(
    tc: str,
    confirmation_code: str,
    country_code: str = "jp",
) -> ServerHandler:
    """
    Download and parse a Battle Cats save, returning a ready-to-use ServerHandler.

    Args:
        tc               : Transfer code (e.g. "1a2b3c4d5").
        confirmation_code: Confirmation/PIN code (e.g. "1234").
        country_code     : Region — "jp", "en", "kr", or "tw". Default "jp".

    Returns:
        ServerHandler with a fully (or partially, on IGNORE_PARSE_ERROR fallback)
        populated save_file.

    Raises:
        ConnectionError  : Network failure before any data was received.
        TransferError    : Server returned a classifiable API error.
        FailedToLoadError: Valid save bytes were received but cannot be parsed.
    """
    cc_obj = core.CountryCode.from_code(country_code)
    probe_gv = core.GameVersion.from_string(_LATEST_GV)

    _log.info("[Transfer] starting download  tc=%s  gv_probe=%s", tc, _LATEST_GV)

    raw_bytes, resp_headers = _download_raw_save(tc, confirmation_code, cc_obj, probe_gv)

    detected_gv = detect_version_from_bytes(raw_bytes)
    if not detected_gv:
        _log.warning(
            "[Transfer] could not detect version from header bytes — "
            "proceeding with parse anyway (bcsfe will try auto-detect)"
        )

    save_file = _parse_save(raw_bytes, cc_obj, detected_gv)

    token = resp_headers.get("Nyanko-Password-Refresh-Token")
    if token:
        save_file.password_refresh_token = token

    handler = ServerHandler(save_file)
    password = resp_headers.get("Nyanko-Password")
    if password:
        handler.save_password(password)

    _log.info("[Transfer] handler ready  tc=%s", tc)
    return handler
