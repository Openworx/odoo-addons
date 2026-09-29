# -*- coding: utf-8 -*-
"""Exception hierarchy for the provider client and status -> error mapping.

Never include the API key or the full request body in an exception message.
"""
from __future__ import annotations


class AIProviderError(Exception):
    """Base class for all errors raised by the provider client."""

    code = 'provider_error'
    retryable = False
    user_message = "The AI provider returned an error."

    def __init__(self, message: str = '', *, status: int | None = None, body: dict | None = None):
        super().__init__(message)
        self.status = status
        self.body = body


# old name; remove on the 20.0 forward-port
OpenRouterError = AIProviderError


class AuthError(AIProviderError):
    code = 'auth'
    retryable = False
    user_message = "The AI provider refused the API key."


class PaymentRequired(AIProviderError):
    code = 'payment_required'
    retryable = False
    user_message = "The AI provider account has insufficient credits."


class RateLimited(AIProviderError):
    code = 'rate_limited'
    retryable = True
    user_message = "The AI provider is rate-limiting requests. Please try again shortly."

    def __init__(self, message: str = '', *, status: int | None = None, body: dict | None = None,
                 retry_after: float | None = None):
        super().__init__(message, status=status, body=body)
        self.retry_after = retry_after


class BadRequest(AIProviderError):
    code = 'bad_request'
    retryable = False
    user_message = "The AI provider rejected the request."


class ContentModerated(AIProviderError):
    code = 'moderated'
    retryable = False
    user_message = "The AI provider refused this content."


class ProviderError(AIProviderError):
    code = 'provider_error'
    retryable = True
    user_message = "The AI provider is temporarily unavailable."


class TransportTimeout(AIProviderError):
    code = 'timeout'
    retryable = True
    user_message = "The AI provider did not answer in time."


def _extract_message(body: dict | None) -> str:
    """The error text of a response body: ``error.message`` (OpenAI), a string
    ``error`` (e.g. LM Studio), a string ``detail`` (FastAPI servers, e.g.
    vLLM) or a top-level ``message``; '' when there is none."""
    if not isinstance(body, dict):
        return ''
    error = body.get('error')
    if isinstance(error, dict):
        error = error.get('message')
    for message in (error, body.get('detail'), body.get('message')):
        if isinstance(message, str) and message.strip():
            return message.strip()
    return ''


def _error_code(body: dict | None):
    error = body.get('error') if isinstance(body, dict) else None
    return error.get('code') if isinstance(error, dict) else None


def _is_moderation(body: dict | None) -> bool:
    if not isinstance(body, dict):
        return False
    error = body.get('error')
    if not isinstance(error, dict):
        return False
    if error.get('code') == 'moderation':
        return True
    metadata = error.get('metadata')
    return isinstance(metadata, dict) and 'reasons' in metadata


def classify(status: int | None, body: dict | None, headers: dict | None = None) -> AIProviderError:
    """Map an HTTP status / response body / headers to an AIProviderError instance.

    The message is the body's error text (see ``_extract_message``), else
    "HTTP <status>": a plain-text 404 (a Base URL without ``/v1``) still says
    what happened.
    """
    message = _extract_message(body)
    if not message and status == 200:
        has_error = isinstance(body, dict) and bool(body.get('error'))
        message = "HTTP 200 with an error body" if has_error else "HTTP 200 without choices"
    elif not message and status is not None:
        message = f"HTTP {status}"
    headers = headers or {}

    if status in (401, 403):
        if status == 403 and _is_moderation(body):
            return ContentModerated(message, status=status, body=body)
        return AuthError(message, status=status, body=body)

    if status == 402:
        return PaymentRequired(message, status=status, body=body)

    if status == 429:
        if _error_code(body) == 'insufficient_quota':
            # OpenAI's exhausted quota: waiting does not help
            return PaymentRequired(message, status=status, body=body)
        retry_after = None
        raw_retry_after = headers.get('Retry-After') or headers.get('retry-after')
        if raw_retry_after is not None:
            try:
                retry_after = float(raw_retry_after)
            except (TypeError, ValueError):
                retry_after = None
        return RateLimited(message, status=status, body=body, retry_after=retry_after)

    # 405: a POST to a web page (a wrong Base URL)
    if status in (400, 404, 405, 413, 422):
        return BadRequest(message, status=status, body=body)

    if status == 200:
        # 200 with an error object, or empty choices, is treated as a
        # (usually transient) provider error unless it is a moderation refusal;
        # a string error (e.g. LM Studio's unknown model or route) is not
        # worth a retry.
        if _is_moderation(body):
            return ContentModerated(message, status=status, body=body)
        if isinstance(body, dict) and isinstance(body.get('error'), str):
            return BadRequest(message, status=status, body=body)
        return ProviderError(message, status=status, body=body)

    # 5xx, 408 (request timeout), 524 (Cloudflare timeout), and anything else
    # unrecognised are treated as transient provider errors.
    return ProviderError(message, status=status, body=body)
