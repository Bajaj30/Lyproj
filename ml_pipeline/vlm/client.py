"""
The one place the pipeline talks to the Claude API.  (docs/API_IMPLEMENTATION.md)

Both readers (the page reader and the pack-label reader) call through here, so
every call gets the same treatment:

  * RETRIES AND TIMEOUTS are the SDK's own (exponential backoff on connection
    errors, 408, 409, 429 and 5xx). We only set the numbers; we do not wrap the
    SDK in a second retry loop, which would multiply the wait.
  * A CONCURRENCY CAP. A multi-page upload reads its pages in parallel; without
    a cap, three doctors uploading at once is a burst the rate limit refuses.
  * THE SPEND CAP HOLDS UNDER CONCURRENCY. Each call reserves its estimated
    cost before it starts, so N parallel calls cannot all pass the check and
    overshoot together.
  * A REFUSAL IS NOT A READING. The API can answer HTTP 200 with
    `stop_reason: "refusal"` (a safety classifier declined) or `"max_tokens"`
    (the output was cut off). Both would otherwise reach the doctor as an empty
    or half prescription. They raise instead.
  * SERVER-SIDE FALLBACK for refusals (`fallbacks: "default"`): a declined
    request is re-run by the API on the model Anthropic recommends for that
    refusal category, inside the same call. It is a beta parameter, so if the
    API rejects it the reader drops it for the rest of the process and carries
    on with the plain, live-verified request. READER_FALLBACKS=off disables it.

Nothing here logs the API key, the image, or the model's reply.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from env import load_env  # noqa: E402
from vlm.spend import PRICES, CallCost, get_guard, price_call  # noqa: E402

FALLBACK_BETA = "server-side-fallback-2026-07-01"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


class ReaderRefused(RuntimeError):
    """The API declined the request (stop_reason == "refusal"). Not a reading."""

    def __init__(self, category: Optional[str] = None):
        self.category = category
        super().__init__(f"The model declined to read this image (category: {category or 'unspecified'}).")


class ReaderTruncated(RuntimeError):
    """The reply hit max_tokens. A truncated PageRead is a failed read, never a short one."""


# Provider-neutral failures, for readers that do not use the Anthropic SDK (the
# Koncile reader). They map onto the same user-facing messages as the SDK's own
# errors in describe_error(), so a doctor sees one wording whichever service reads.

class ReaderAuthError(RuntimeError):
    """The reading service rejected the API key."""


class ReaderQuotaExceeded(RuntimeError):
    """The reading service's credits or quota are used up."""


class ReaderBusy(RuntimeError):
    """Rate limited or overloaded: worth trying again shortly."""


class ReaderTimedOut(RuntimeError):
    """The reading service did not finish in time."""


class ReaderUnavailable(RuntimeError):
    """The reading service could not be reached, or failed on its side."""


class ReaderFailed(RuntimeError):
    """The reading service processed the page and reported that it could not read it."""


@dataclass
class Completed:
    """One finished API call: the parsed output plus what the correction log needs."""

    parsed: Any
    raw_response: dict
    usage: dict
    cost: CallCost
    served_by: str          # the model that actually answered (differs after a fallback)


class ClaudeCaller:
    """A configured client plus the shared rules above. Thread-safe: nothing
    about a single call is kept on the instance."""

    def __init__(self, max_retries: Optional[int] = None, timeout: Optional[float] = None,
                 max_concurrency: Optional[int] = None, fallbacks: Optional[bool] = None,
                 client: Any = None):
        load_env()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                max_retries=max_retries if max_retries is not None else _int_env("READER_MAX_RETRIES", 2),
                # Per attempt. Worst case is timeout x (retries + 1), so this is
                # kept well under the browser's own patience.
                timeout=timeout if timeout is not None else float(_int_env("READER_TIMEOUT_SECONDS", 90)),
            )
        self.client = client
        self._slots = threading.BoundedSemaphore(
            max_concurrency if max_concurrency is not None else _int_env("READER_MAX_CONCURRENCY", 4))
        if fallbacks is None:
            fallbacks = os.environ.get("READER_FALLBACKS", "default").lower() not in ("off", "0", "false", "no")
        self._fallbacks = fallbacks
        self._fallback_lock = threading.Lock()
        # Monotonic start time of the last successful call per (model, prefix):
        # the prompt cache stays warm for 5 minutes from a request's START.
        self._warm: dict = {}

    # ── prompt-cache warmth ──────────────────────────────────────────────

    def prefix_is_warm(self, key: str, ttl_seconds: float = 270.0) -> bool:
        """True when a call with this cached prefix started recently enough that
        the next one should READ the prompt cache. Used to decide whether a
        parallel fan-out needs a lead request first (see read_many)."""
        import time

        started = self._warm.get(key)
        return started is not None and (time.monotonic() - started) < ttl_seconds

    # ── the call ─────────────────────────────────────────────────────────

    def parse(self, *, warm_key: str, estimated_usd: float = 0.05, spend_tag: str = "",
              enforce_spend_cap: bool = True, **request) -> Completed:
        """`client.messages.parse(**request)` under the shared rules.

        `request` is exactly what the live-verified call sends (model,
        max_tokens, thinking, output_config, system, messages, output_format).
        """
        import time

        guard = get_guard()
        with self._slots:
            reservation = guard.reserve(estimated_usd) if enforce_spend_cap else _noop()
            with reservation:
                started = time.monotonic()
                response = self._send(request)
                served_by = getattr(response, "model", None) or request["model"]
                # Price at the rates of the model that answered. An unknown
                # fallback model is priced at the requested model's rates and
                # said so, rather than silently recorded at zero.
                priced_as = served_by if served_by in PRICES else request["model"]
                cost = guard.record(price_call(priced_as, response.usage), tag=spend_tag)
                if priced_as != served_by:
                    print(f"[READER] served by {served_by!r}, which has no price entry; "
                          f"costed at {priced_as!r} rates. Add it to vlm/spend.py PRICES.", flush=True)

        stop = getattr(response, "stop_reason", None)
        if stop == "refusal":
            details = getattr(response, "stop_details", None)
            raise ReaderRefused(getattr(details, "category", None))
        if stop == "max_tokens":
            raise ReaderTruncated("The reply was cut off at max_tokens; the page was not fully read.")
        parsed = getattr(response, "parsed_output", None)
        if parsed is None:
            raise ReaderTruncated("The reply did not contain a complete structured reading.")

        self._warm[warm_key] = started
        usage = response.usage.model_dump() if hasattr(response.usage, "model_dump") else dict(response.usage)
        # `warnings=False`: the SDK's ParsedMessage carries a ParsedTextBlock that
        # does not match the declared ContentBlock union, so a plain dump emits
        # ~15 serialisation warnings per call. The dump itself is correct.
        raw = response.model_dump(mode="json", warnings=False)
        return Completed(parsed=parsed, raw_response=raw, usage=usage, cost=cost, served_by=served_by)

    def _send(self, request: dict):
        if not self._fallbacks:
            return self.client.messages.parse(**request)
        import anthropic

        try:
            return self.client.beta.messages.parse(betas=[FALLBACK_BETA], fallbacks="default", **request)
        except anthropic.BadRequestError as exc:
            # A 400 is not billed. Either this request is itself invalid (then
            # the plain call fails the same way and that error is raised), or
            # the API does not accept the fallback parameter for this account
            # (then the plain call succeeds, and we stop sending it).
            response = self.client.messages.parse(**request)
            with self._fallback_lock:
                if self._fallbacks:
                    self._fallbacks = False
                    print(f"[READER] server-side refusal fallback was rejected by the API "
                          f"({getattr(exc, 'message', exc)}); continuing without it.", flush=True)
            return response

    # ── key check ────────────────────────────────────────────────────────

    def check_access(self, model: str) -> str:
        """One free call (GET /v1/models/{id}) that says whether the key works
        and the model is available: ok | auth_error | model_unavailable |
        unreachable. Never raises."""
        import anthropic

        try:
            self.client.with_options(timeout=10.0, max_retries=0).models.retrieve(model)
            return "ok"
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
            return "auth_error"
        except anthropic.NotFoundError:
            return "model_unavailable"
        except Exception:
            return "unreachable"


class _noop:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def describe_error(exc: BaseException) -> tuple:
    """(http_status, message safe to show a signed-in user) for a failed read.

    The raw exception text is for the server log only: it can carry file paths,
    account identifiers and provider internals that a browser has no business
    seeing. Typed SDK exceptions are matched most-specific first.
    """
    from vlm.spend import SpendCapExceeded

    if isinstance(exc, SpendCapExceeded):
        return 503, "The reading budget for this deployment is used up. Ask the administrator to raise it."
    if isinstance(exc, ReaderRefused):
        return 422, ("The reading service declined to read this image. "
                     "Check it is a photo of a prescription and try again.")
    if isinstance(exc, ReaderTruncated):
        return 502, "The reading was cut off before it finished. Try again, or photograph the page in two halves."
    if isinstance(exc, FileNotFoundError):
        return 503, "The medicine database is not installed on this server. Tell the administrator."
    if isinstance(exc, ReaderAuthError):
        return 502, "The reading service is not set up correctly (API key). Tell the administrator."
    if isinstance(exc, ReaderQuotaExceeded):
        return 503, "The reading service's credits are used up. Tell the administrator."
    if isinstance(exc, ReaderBusy):
        return 503, "The reading service is busy. Try again in a minute."
    if isinstance(exc, ReaderTimedOut):
        return 504, "The reading service did not answer in time. Try again."
    if isinstance(exc, ReaderUnavailable):
        return 503, "The reading service is having trouble. Try again in a minute."
    if isinstance(exc, ReaderFailed):
        return 422, "The reading service could not process this image. Try a clearer photo."
    try:
        import anthropic
    except ImportError:
        anthropic = None
    if anthropic is not None:
        if isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return 502, "The reading service is not set up correctly (API key). Tell the administrator."
        if isinstance(exc, anthropic.NotFoundError):
            return 502, "The reading model is not available to this deployment. Tell the administrator."
        if isinstance(exc, anthropic.RateLimitError):
            return 503, "The reading service is busy. Try again in a minute."
        if isinstance(exc, anthropic.RequestTooLargeError):
            return 413, "That image is too large to read. Use a smaller photo."
        if isinstance(exc, anthropic.BadRequestError):
            return 422, "The reading service could not process this image. Try a clearer photo."
        if isinstance(exc, anthropic.APITimeoutError):
            return 504, "The reading service did not answer in time. Try again."
        if isinstance(exc, anthropic.APIConnectionError):
            return 503, "The reading service could not be reached. Check the server's internet connection."
        if isinstance(exc, anthropic.APIStatusError):
            return 503, "The reading service is having trouble. Try again in a minute."
    return 502, "The page could not be read. Try again."
