"""Isolated, bounded JavaScript rendering for public career pages."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

MAX_HTML_BYTES = 2_000_000
MAX_REQUESTS = 120
PAGE_TIMEOUT_MS = 15_000

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
render_slot = asyncio.Semaphore(1)


class RenderRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    site_url: str = Field(min_length=8, max_length=2048)


def _validate_public_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs are supported.")
    if parsed.username or parsed.password:
        raise ValueError("Credentials in URLs are not supported.")
    expected = 443 if parsed.scheme == "https" else 80
    try:
        if parsed.port not in (None, expected):
            raise ValueError("Only standard public web ports are supported.")
    except ValueError as exc:
        raise ValueError("The URL has an invalid port.") from exc

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise ValueError("Local and private network addresses cannot be rendered.")
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [
                ipaddress.ip_address(result[4][0])
                for result in socket.getaddrinfo(hostname, expected, type=socket.SOCK_STREAM)
            ]
        except OSError as exc:
            raise ValueError("The website hostname could not be resolved.") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Local and private network addresses cannot be rendered.")
    return url


def _same_job_site(root: str, target: str) -> bool:
    source, destination = urlsplit(root), urlsplit(target)
    if source.scheme.casefold() != destination.scheme.casefold():
        return False
    a, b = (source.hostname or "").casefold(), (destination.hostname or "").casefold()
    return a == b or all(host == "ibm.com" or host.endswith(".ibm.com") for host in (a, b))


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "healthy"}


@app.post("/render")
async def render(payload: RenderRequest) -> dict[str, str]:
    try:
        _validate_public_url(payload.site_url)
        _validate_public_url(payload.url)
        if not _same_job_site(payload.site_url, payload.url):
            raise ValueError("The posting URL must belong to the monitored careers site.")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    async with render_slot:
        request_count = 0
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context(
                    user_agent="JobHarborJobFinder/0.1 (user-directed career-page scanner)",
                    service_workers="block",
                    accept_downloads=False,
                    java_script_enabled=True,
                )

                async def guard(route):
                    nonlocal request_count
                    request = route.request
                    request_count += 1
                    if request_count > MAX_REQUESTS or request.method != "GET":
                        await route.abort()
                        return
                    try:
                        _validate_public_url(request.url)
                        if request.is_navigation_request() and not _same_job_site(payload.site_url, request.url):
                            await route.abort()
                            return
                    except ValueError:
                        await route.abort()
                        return
                    await route.continue_()

                await context.route("**/*", guard)
                page = await context.new_page()
                try:
                    response = await page.goto(
                        payload.url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS
                    )
                    if response is None:
                        raise HTTPException(status_code=502, detail="The page returned no response.")
                    if response.status >= 400:
                        raise HTTPException(status_code=502, detail=f"The page returned HTTP {response.status}.")
                    # Give client-side job data a short chance to populate without an unbounded network-idle wait.
                    await page.wait_for_timeout(1200)
                    html = await page.content()
                    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
                        raise HTTPException(status_code=413, detail="The rendered page exceeded the size limit.")
                    return {"url": page.url, "html": html}
                except PlaywrightError as exc:
                    raise HTTPException(status_code=502, detail="The page could not be rendered safely.") from exc
                finally:
                    await context.close()
            finally:
                await browser.close()
