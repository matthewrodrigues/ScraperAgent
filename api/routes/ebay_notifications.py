"""eBay Marketplace Account Deletion notification endpoint.

eBay requires every production keyset to register a public HTTPS endpoint that
handles two things:

  GET  /ebay/account-deletion?challenge_code=...
      eBay sends this once at registration time (and occasionally to revalidate).
      We must return `{"challengeResponse": "<sha256-hex>"}` where the hash is
      sha256(challengeCode + verificationToken + endpoint).hexdigest(). All three
      inputs are utf-8 strings; the endpoint must match exactly what we registered.

  POST /ebay/account-deletion
      eBay sends this when an eBay user deletes their account. For personal-use we
      no-op — we don't store data about random eBay users, only listings. We log
      the body at INFO so it's recoverable from the server log if ever needed.

The verification token and endpoint URL come from `.env` (see config.py).
"""

import hashlib
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

import config

log = logging.getLogger(__name__)

router = APIRouter()


@router.get("/ebay/account-deletion")
def deletion_challenge(challenge_code: str) -> JSONResponse:
    token = config.EBAY_DELETION_VERIFICATION_TOKEN
    endpoint = config.EBAY_DELETION_ENDPOINT_URL
    if not token or not endpoint:
        # 500 is correct here: eBay's GET arrived but our server isn't configured.
        # Surfacing the misconfig is better than returning a bogus hash that would
        # silently fail eBay's verification with a confusing "wrong response" message.
        raise HTTPException(
            status_code=500,
            detail="EBAY_DELETION_VERIFICATION_TOKEN or EBAY_DELETION_ENDPOINT_URL not set",
        )
    payload = (challenge_code + token + endpoint).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return JSONResponse({"challengeResponse": digest})


@router.post("/ebay/account-deletion")
async def deletion_notification(request: Request) -> Response:
    body = await request.body()
    log.info("eBay account-deletion notification received: %s", body.decode("utf-8", "replace"))
    return Response(status_code=204)
