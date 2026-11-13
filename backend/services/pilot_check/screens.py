"""Screenshots of what a rater sees, taken in a real browser.

Opens the rater page in preview mode, the same URL "Preview as participant"
uses, in headless Chromium: the consent screen, the intro (when the
experiment has one), then each sampled question pinned by id, waiting for
the assistance to arrive on an assisted arm. Nothing is answered. Each
preview session is one preview rater row, which rating counts already skip.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

VIEWPORT = {"width": 1280, "height": 900}
# Taller pages are cut here: a vision model downsizes anything longer until
# the text is unreadable, and the top of a question is what a rater meets.
MAX_HEIGHT = 4000
PAGE_TIMEOUT_MS = 30_000
# The assistance call behind a question can take as long as a provider call
# with retries; the pilot run's own probe has the same ceiling.
ASSISTANCE_TIMEOUT_MS = 120_000
# Fonts, math and images settle after the content appears.
SETTLE_MS = 500

CONSENT = "consent"
INTRO = "intro"


def question_screen(question_id: int) -> str:
    return f"question-{question_id}"


@dataclass
class Screen:
    key: str
    png: bytes
    question_id: int | None = None
    # What went wrong reaching or reading this screen, in a sentence.
    problems: list[str] = field(default_factory=list)


class BrowserUnavailable(RuntimeError):
    """Chromium could not be started on this server."""


def preview_url(app_url: str, experiment_id: int, run_id: int, question_id: int) -> str:
    query = urlencode(
        {
            "experiment_id": experiment_id,
            "PROLIFIC_PID": f"pilot-check-{run_id}",
            "STUDY_ID": "pilot-check",
            "SESSION_ID": f"pilot-check-{run_id}",
            "preview": "true",
            "question_id": question_id,
        }
    )
    return f"{app_url.rstrip('/')}/rate?{query}"


async def capture(
    *,
    app_url: str,
    experiment_id: int,
    run_id: int,
    question_ids: list[int],
    on_progress: Callable[[Screen], Awaitable[None]] | None = None,
) -> list[Screen]:
    """The consent, intro and question screens, in the order a rater meets
    them. A screen that does not finish loading is still captured, with a
    problem saying so: the stuck page is what a rater would see."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - the dependency is pinned
        raise BrowserUnavailable(str(exc)) from exc

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch()
        except Exception as exc:
            raise BrowserUnavailable(str(exc).splitlines()[0]) from exc
        try:
            context = await browser.new_context(viewport=VIEWPORT)
            page = await context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            screens: list[Screen] = []

            async def shoot(key: str, question_id: int | None, problems: list[str]) -> None:
                height = await page.evaluate("document.documentElement.scrollHeight")
                png = await page.screenshot(
                    full_page=True,
                    clip={
                        "x": 0,
                        "y": 0,
                        "width": VIEWPORT["width"],
                        "height": min(height, MAX_HEIGHT),
                    },
                )
                found = problems + [f"The page raised an error: {e}" for e in page_errors]
                page_errors.clear()
                screens.append(Screen(key, png, question_id, found))
                if on_progress is not None:
                    await on_progress(screens[-1])

            for index, question_id in enumerate(question_ids):
                await page.goto(preview_url(app_url, experiment_id, run_id, question_id))
                if index == 0:
                    await _consent_and_intro(page, shoot)
                problems = await _wait_for_question(page)
                await shoot(question_screen(question_id), question_id, problems)
            return screens
        finally:
            await browser.close()


async def _consent_and_intro(page, shoot) -> None:
    consent = page.get_by_test_id("consent-screen")
    try:
        await consent.wait_for(timeout=PAGE_TIMEOUT_MS)
    except Exception:
        await shoot(CONSENT, None, ["The consent screen did not appear within 30 seconds."])
        return
    await page.wait_for_timeout(SETTLE_MS)
    await shoot(CONSENT, None, [])
    await page.get_by_role("checkbox").first.check()
    await page.get_by_role("button", name="I agree").click()

    intro = page.get_by_test_id("session-expectations")
    question = page.get_by_test_id("question-card")
    try:
        await intro.or_(question).first.wait_for(timeout=PAGE_TIMEOUT_MS)
    except Exception:
        return  # the question wait that follows reports it
    if await intro.count():
        await page.wait_for_timeout(SETTLE_MS)
        await shoot(INTRO, None, [])
        await page.get_by_role("button", name="Continue").click()


async def _wait_for_question(page) -> list[str]:
    try:
        await page.get_by_test_id("question-card").first.wait_for(timeout=PAGE_TIMEOUT_MS)
    except Exception:
        return ["The question did not appear within 30 seconds."]
    loading = page.get_by_test_id("assistance-loading")
    try:
        await loading.wait_for(state="detached", timeout=ASSISTANCE_TIMEOUT_MS)
    except Exception:
        return ["The assistance was still loading after two minutes."]
    await page.wait_for_timeout(SETTLE_MS)
    return []
