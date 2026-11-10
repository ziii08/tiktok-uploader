"""
`tiktok_uploader` module for uploading videos to TikTok

Key Classes
-----------
TikTokUploader : Client for uploading videos to TikTok
"""

import datetime
import logging
import re
import time
from collections.abc import Callable
from os.path import abspath, exists
from typing import Any, Literal
from zoneinfo import ZoneInfo

import pytz
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tiktok_uploader import config
from tiktok_uploader.auth import AuthBackend
from tiktok_uploader.browsers import get_browser
from tiktok_uploader.types import Cookie, ProxyDict, VideoDict
from tiktok_uploader.utils import bold, green, red

logger = logging.getLogger(__name__)


class TikTokUploader:
    def __init__(
        self,
        username: str = "",
        password: str = "",
        cookies: str = "",
        cookies_list: list[Cookie] = [],
        cookies_str: str | None = None,
        sessionid: str | None = None,
        proxy: ProxyDict | None = None,
        browser: Literal["chrome", "safari", "chromium", "edge", "firefox"] = "chrome",
        headless: bool = False,
        *args,
        **kwargs,
    ):
        """
        Initializes the TikTok Uploader client.

        The browser is not started until the first upload is attempted (lazy initialization).
        """
        self.auth = AuthBackend(
            username=username,
            password=password,
            cookies=cookies,
            cookies_list=cookies_list,
            cookies_str=cookies_str,
            sessionid=sessionid,
        )
        self.proxy = proxy
        self.browser_name = browser
        self.headless = headless
        self.browser_args = args
        self.browser_kwargs = kwargs

        self._page: Page | None = None
        self._browser_context: Any = (
            None  # Stored implicitly via page.context if needed
        )

    @property
    def page(self) -> Page:
        if self._page is None:
            logger.debug(
                "Create a %s browser instance %s",
                self.browser_name,
                "in headless mode" if self.headless else "",
            )
            self._page = get_browser(
                self.browser_name,
                headless=self.headless,
                proxy=self.proxy,
                *self.browser_args,
                **self.browser_kwargs,
            )  # type: ignore[misc]
            self._page = self.auth.authenticate_agent(self._page)
        return self._page

    def upload_video(
        self,
        filename: str,
        description: str = "",
        schedule: datetime.datetime | None = None,
        product_id: str | None = None,
        cover: str | None = None,
        visibility: Literal["everyone", "friends", "only_you"] = "everyone",
        num_retries: int = 1,
        skip_split_window: bool = False,
        *args,
        **kwargs,
    ) -> bool:
        """
        Uploads a single TikTok video.

        Returns True if successful, False otherwise.
        """
        video_dict: VideoDict = {"path": filename}
        if description:
            video_dict["description"] = description
        if schedule:
            video_dict["schedule"] = schedule
        if product_id:
            video_dict["product_id"] = product_id
        if visibility != "everyone":
            video_dict["visibility"] = visibility
        if cover:
            video_dict["cover"] = cover

        failed_list = self.upload_videos(
            [video_dict], num_retries, skip_split_window, *args, **kwargs
        )

        return len(failed_list) == 0

    def upload_videos(
        self,
        videos: list[VideoDict],
        num_retries: int = 1,
        skip_split_window: bool = False,
        on_complete: Callable[[VideoDict], None] | None = None,
        *args,
        **kwargs,
    ) -> list[VideoDict]:
        """
        Uploads multiple videos to TikTok.
        Returns a list of failed videos.
        """
        videos = _convert_videos_dict(videos)  # type: ignore

        if videos and len(videos) > 1:
            logger.debug("Uploading %d videos", len(videos))

        page = self.page  # Triggers lazy loading/authentication

        failed = []
        # uploads each video
        for video in videos:
            try:
                path = abspath(video.get("path", "."))
                description = video.get("description", "")
                schedule = video.get("schedule", None)
                product_id = video.get("product_id", None)
                cover_path = video.get("cover", None)
                if cover_path is not None:
                    cover_path = abspath(cover_path)

                visibility = video.get("visibility", "everyone")

                logger.debug(
                    "Posting %s%s",
                    bold(video.get("path", "")),
                    (
                        f"\n{' ' * 15}with description: {bold(description)}"
                        if description
                        else ""
                    ),
                )

                # Video must be of supported type
                if not _check_valid_path(path):
                    print(f"{path} is invalid, skipping")
                    failed.append(video)
                    continue

                # Video must have a valid datetime for tiktok's scheduler
                if schedule:
                    try:
                        timezone = pytz.UTC
                        if schedule.tzinfo is None:
                            # Assume local system timezone if naive, and convert to UTC
                            schedule = schedule.astimezone(timezone)
                        else:
                            schedule = schedule.astimezone(timezone)

                        valid_tiktok_minute_multiple = 5
                        schedule = _get_valid_schedule_minute(
                            schedule, valid_tiktok_minute_multiple
                        )
                        if not _check_valid_schedule(schedule):
                            logger.warning(
                                "Schedule datetime %s is invalid (must be at least 20 minutes and at most 10 days in the future). "
                                "Falling back to uploading immediately (Upload Now).",
                                schedule,
                            )
                            schedule = None
                    except Exception as sched_err:
                        logger.warning(
                            "Could not parse schedule datetime (%s). Falling back to uploading immediately.",
                            sched_err,
                        )
                        schedule = None

                complete_upload_form(
                    page,
                    path,
                    description,
                    schedule,
                    skip_split_window,
                    cover_path,
                    product_id,
                    visibility,
                    num_retries,
                    self.headless,
                    *args,
                    **kwargs,
                )  # type: ignore[misc]
            except Exception as exception:
                logger.error("Failed to upload %s", path)
                logger.error(exception)
                failed.append(video)
                try:
                    page.screenshot(path="tiktok_error_screenshot.png")
                except:
                    pass
                import traceback
                traceback.print_exc()

            if on_complete and callable(
                on_complete
            ):  # calls the user-specified on-complete function
                on_complete(video)

        return failed

    def close(self):
        """Closes the browser instance and stops playwright."""
        if self._page:
            try:
                p = getattr(self._page, "_playwright", None)
                context = getattr(self._page, "context", None)
                browser = context.browser if context else None
                if context:
                    try:
                        context.close()
                    except Exception:
                        pass
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass
                if p:
                    try:
                        p.stop()
                    except Exception:
                        pass
            except Exception as e:
                logger.debug(f"Error closing browser: {e}")
            self._page = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


# Wrapper functions for backward compatibility (optional but good for transition)
def upload_video(
    filename: str,
    description: str | None = None,
    cookies: str = "",
    schedule: datetime.datetime | None = None,
    username: str = "",
    password: str = "",
    sessionid: str | None = None,
    cookies_list: list[Cookie] = [],
    cookies_str: str | None = None,
    proxy: ProxyDict | None = None,
    product_id: str | None = None,
    cover: str | None = None,
    visibility: Literal["everyone", "friends", "only_you"] = "everyone",
    browser: Literal["chrome", "safari", "chromium", "edge", "firefox"] = "chrome",
    headless: bool = False,
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Uploads a single TikTok video using the TikTokUploader class.

    Returns a list of failed videos (empty if successful).
    """
    uploader = TikTokUploader(
        username=username,
        password=password,
        cookies=cookies,
        cookies_list=cookies_list,
        cookies_str=cookies_str,
        sessionid=sessionid,
        proxy=proxy,
        browser=browser,
        headless=headless,
        *args,
        **kwargs,
    )  # type: ignore[misc]

    video_dict: VideoDict = {"path": filename}
    if description:
        video_dict["description"] = description
    if schedule:
        video_dict["schedule"] = schedule
    if product_id:
        video_dict["product_id"] = product_id
    if visibility != "everyone":
        video_dict["visibility"] = visibility
    if cover:
        video_dict["cover"] = cover

    try:
        return uploader.upload_videos([video_dict], *args, **kwargs)
    finally:
        if config.quit_on_end:
            uploader.close()


def upload_videos(
    videos: list[VideoDict],
    username: str = "",
    password: str = "",
    cookies: str = "",
    cookies_list: list[Cookie] = [],
    cookies_str: str | None = None,
    sessionid: str | None = None,
    proxy: ProxyDict | None = None,
    browser: Literal["chrome", "safari", "chromium", "edge", "firefox"] = "chrome",
    browser_agent: Page
    | None = None,  # Not fully supported in new class-based approach as constructor
    headless: bool = False,
    *args,
    **kwargs,
) -> list[VideoDict]:
    """
    Uploads multiple videos to TikTok using the TikTokUploader class.
    """
    uploader = TikTokUploader(
        username=username,
        password=password,
        cookies=cookies,
        cookies_list=cookies_list,
        cookies_str=cookies_str,
        sessionid=sessionid,
        proxy=proxy,
        browser=browser,
        headless=headless,
        *args,
        **kwargs,
    )  # type: ignore[misc]

    if browser_agent:
        uploader._page = uploader.auth.authenticate_agent(browser_agent)

    try:
        return uploader.upload_videos(videos, *args, **kwargs)
    finally:
        if config.quit_on_end:
            uploader.close()


def complete_upload_form(
    page: Page,
    path: str,
    description: str,
    schedule: datetime.datetime | None,
    skip_split_window: bool,
    cover_path: str | None = None,
    product_id: str | None = None,
    visibility: Literal["everyone", "friends", "only_you"] = "everyone",
    num_retries: int = 1,
    headless: bool = False,
    *args,
    **kwargs,
) -> None:
    """
    Actually uploads each video
    """
    _go_to_upload(page)
    _remove_cookies_window(page)

    _set_video(page, path=path, num_retries=num_retries, **kwargs)

    if not skip_split_window:
        _remove_split_window(page)
    _set_interactivity(page, **kwargs)
    _set_description(page, description)
    if cover_path:
        _set_cover(page, cover_path)
    if visibility != "everyone":
        _set_visibility(page, visibility)
    if schedule:
        _set_schedule_video(page, schedule)
    if product_id:
        _add_product_link(page, product_id)
    _post_video(page, is_scheduled=(schedule is not None))


def _go_to_upload(page: Page) -> None:
    """
    Navigates to the upload page with retry resilience against network/navigation interruptions.
    """
    upload_url = str(getattr(getattr(config, "paths", None), "upload", "https://www.tiktok.com/tiktokstudio/upload?from=upload&lang=en"))
    logger.debug(green(f"Navigating to upload page: {upload_url}"))

    max_nav_attempts = 3
    for attempt in range(1, max_nav_attempts + 1):
        try:
            if page.url != upload_url:
                page.goto(upload_url, timeout=60000, wait_until="domcontentloaded")
            else:
                try:
                    page.reload(timeout=30000, wait_until="domcontentloaded")
                except Exception:
                    page.goto(upload_url, timeout=60000, wait_until="domcontentloaded")
            break
        except Exception as e:
            if attempt < max_nav_attempts:
                logger.warning(
                    f"Navigation attempt {attempt} to {upload_url} interrupted ({e}). Retrying in 3s..."
                )
                time.sleep(3)
            else:
                logger.error(f"Failed to navigate to upload page after {max_nav_attempts} attempts: {e}")
                raise

    try:
        page.on("dialog", lambda dialog: dialog.accept())
    except Exception:
        pass

    # Wait for #root to be attached in DOM (layout rendering may leave it temporarily 0px)
    try:
        page.wait_for_selector("#root", state="attached", timeout=30000)
    except Exception:
        pass

    # Wait for video file input to be attached
    try:
        page.locator('input[type="file"]').wait_for(state="attached", timeout=30000)
    except Exception:
        pass


def _dismiss_walkthrough_overlays(page: Page) -> None:
    """
    Proactively dismisses joyride tour overlays and clicks common dismiss buttons.
    Always uses .click() to avoid breaking React 18 Virtual DOM reconciliation.
    """
    try:
        page.evaluate(
            """
            () => {
                // Click dismiss buttons safely through DOM click
                document.querySelectorAll('button').forEach(btn => {
                    const txt = (btn.innerText || '').trim().toLowerCase();
                    if (['got it', 'okay', 'dismiss', 'close', 'not now', 'mengerti', 'tutup', 'lanjutkan', 'skip', 'lewati', 'nanti saja'].includes(txt)) {
                        try { btn.click(); } catch (_) {}
                    }
                });
            }
            """
        )
    except Exception:
        pass


def _set_description(page: Page, description: str) -> None:
    """
    Sets the description of the video cleanly and reliably.
    """
    if description is None:
        return

    logger.debug(green("Setting description"))

    # Remove any characters outside the BMP range (emojis, etc) & Fix accents
    description = description.encode("utf-8", "ignore").decode("utf-8")
    saved_description = description

    try:
        _dismiss_walkthrough_overlays(page)

        desc_locator = page.locator(
            f"xpath={config.selectors.upload.description} | "
            "//div[contains(@class, 'public-DraftEditor-content')] | "
            "//div[@contenteditable='true']"
        )
        try:
            desc_locator.first.wait_for(
                state="attached",
                timeout=config.implicit_wait * 1000,
            )
            desc_locator.first.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        inserted = False

        # Primary approach: Playwright native click, select all, backspace, and insert_text
        # This notifies React / DraftJS cleanly without typing letter-by-letter (which triggers autocomplete dropdowns)
        try:
            desc_el = desc_locator.first
            if desc_el.is_visible(timeout=3000):
                try:
                    desc_el.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
            desc_el.click(force=True, timeout=3000)
            time.sleep(0.2)
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            time.sleep(0.2)
            page.keyboard.insert_text(description)
            time.sleep(0.3)
            page.keyboard.press("Escape")
            time.sleep(0.2)

            # Instant JS evaluation (0ms, avoids Playwright locator 30s timeout)
            curr_text = str(page.evaluate(
                "() => (document.querySelector('div.public-DraftEditor-content') || "
                "document.querySelector('div[contenteditable=true]'))?.innerText || ''"
            )).strip()

            if len(curr_text) > 0:
                inserted = True
                logger.debug(green(f"Description set via keyboard.insert_text ({len(curr_text)} chars)"))
        except Exception as e:
            logger.debug(f"Keyboard insert_text failed: {e}")
            inserted = False

        if not inserted:
            # Fallback: execCommand insertText directly on the editor element
            try:
                inserted = bool(page.evaluate(
                    """({ text }) => {
                        let el = document.querySelector('div.public-DraftEditor-content') ||
                                 document.querySelector('div[contenteditable="true"]');
                        if (!el) return false;
                        el.focus();
                        const sel = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(el);
                        sel.removeAllRanges();
                        sel.addRange(range);

                        document.execCommand('insertText', false, text);
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }""",
                    {"text": description},
                ))
                page.keyboard.press("Escape")
                if inserted:
                    logger.debug(green("Description set via execCommand insertText"))
            except Exception as eval_err:
                logger.debug(f"execCommand insertText failed: {eval_err}")
                inserted = False

    except Exception as exception:
        logger.warning(f"Failed to set description: {exception}")
        # Final emergency fallback
        try:
            _dismiss_walkthrough_overlays(page)
            desc_locator = page.locator(f"xpath={config.selectors.upload.description}").first
            desc_locator.scroll_into_view_if_needed(timeout=3000)
            desc_locator.click(force=True, timeout=3000)
            desc_locator.press("Control+A")
            desc_locator.press("Backspace")
            page.keyboard.insert_text(saved_description)
            page.keyboard.press("Escape")
        except Exception:
            pass


def _clear(locator) -> None:
    """
    Clears the text of the element
    """
    locator.press("Control+A")
    locator.press("Backspace")


def _set_video(page: Page, path: str = "", num_retries: int = 3, **kwargs) -> None:
    """
    Sets the video to upload and waits for upload to complete
    """
    logger.debug(green(f"Uploading video file: {path}"))

    for attempt in range(1, num_retries + 1):
        try:
            upload_box = page.locator(f"xpath={config.selectors.upload.upload_video}")
            upload_box.set_input_files(path)

            logger.debug(green("Video file attached. Waiting for video upload & processing in TikTok Studio..."))

            # Wait for upload to complete (large files like 100MB+ take 30-120 seconds)
            explicit_wait = int(getattr(config, "explicit_wait", 60))
            upload_timeout = max(explicit_wait * 3, 180)
            start_t = time.time()
            time.sleep(3.0)

            while time.time() - start_t < upload_timeout:
                elapsed = time.time() - start_t

                # Check for "Something went wrong" with Retry button
                retry_btn = page.locator("button:has-text('Retry'), button:has-text('Coba lagi')").first
                try:
                    if retry_btn.is_visible(timeout=200):
                        logger.warning("TikTok Studio showed 'Something went wrong'. Clicking Retry...")
                        retry_btn.click(force=True)
                        time.sleep(2.0)
                        upload_box.set_input_files(path)
                        start_t = time.time()
                        continue
                except Exception:
                    pass

                # Inspect upload state in page
                try:
                    state = page.evaluate("""() => {
                        const textNodes = Array.from(document.querySelectorAll('span, div, p, button'))
                            .map(el => (el.innerText || '').trim())
                            .filter(t => t.length > 0 && t.length < 60);

                        const percent = textNodes.find(t => /^\\d+(\\.\\d+)?%$/.test(t) || /\\b\\d+(\\.\\d+)?%\\b/.test(t));
                        const hasUploading = textNodes.some(t => /^uploading\\.{0,3}$/i.test(t) || /^mengunggah\\.{0,3}$/i.test(t));
                        const hasChecksNotice = textNodes.some(t => /checks can only start after the file is uploaded/i.test(t));
                        const hasUploaded = textNodes.some(t => /^uploaded$/i.test(t) || /^terunggah$/i.test(t));

                        const postBtn = document.querySelector('button[data-e2e="post_video_button"]');
                        const postBtnEnabled = postBtn ? (
                            !postBtn.disabled &&
                            postBtn.getAttribute('aria-disabled') !== 'true' &&
                            postBtn.getAttribute('data-disabled') !== 'true'
                        ) : false;

                        return {
                            percent: percent || null,
                            hasUploading,
                            hasChecksNotice,
                            hasUploaded,
                            postBtnEnabled,
                        };
                    }""")

                    if state:
                        if state.get("percent"):
                            logger.debug(f"TikTok upload progress: {state['percent']}")

                        # Completed conditions:
                        # 1. Explicitly hasUploaded is True
                        # 2. OR elapsed > 5s and post_video_button is fully enabled (data-disabled='false')
                        #    with no uploading percent or checks notice
                        if state.get("hasUploaded") or (
                            elapsed > 5
                            and state.get("postBtnEnabled")
                            and not state.get("percent")
                            and not state.get("hasUploading")
                            and not state.get("hasChecksNotice")
                        ):
                            logger.debug(green(f"Video upload completed in {elapsed:.1f}s"))
                            break
                except Exception:
                    pass

                time.sleep(2.0)

            # Dismiss any walkthrough/onboarding overlays (e.g. "New editing features added [Got it]")
            _dismiss_walkthrough_overlays(page)
            try:
                for b in ["button:has-text('Got it')", "button:has-text('Mengerti')", "button:has-text('Okay')"]:
                    btn = page.locator(b).first
                    if btn.is_visible(timeout=500):
                        btn.click(force=True)
                        time.sleep(0.5)
            except Exception:
                pass

            time.sleep(1.0)
            return

        except Exception as exception:
            logger.warning(f"Error during video upload attempt {attempt}: {exception}")
            if attempt == num_retries:
                raise FailedToUpload(exception)


def _remove_cookies_window(page: Page) -> None:
    """
    Removes the cookies window if it is open
    """
    logger.debug(green("Removing cookies window"))

    try:
        selector = f"{config.selectors.upload.cookies_banner.banner} >> {config.selectors.upload.cookies_banner.button} >> button"

        button = page.locator(selector).first
        if button.is_visible(timeout=5000):
            button.click()

    except Exception:
        page.evaluate(f"""
            const banner = document.querySelector("{config.selectors.upload.cookies_banner.banner}");
            if (banner) banner.remove();
        """)


def _remove_split_window(page: Page) -> None:
    """
    Remove the split window and any walkthrough modals if they are open
    """
    logger.debug(green("Removing walkthrough modals"))
    _dismiss_walkthrough_overlays(page)
    try:
        # Loop to catch multiple walkthroughs (e.g. "Editing features" and "Mobile Preview")
        for _ in range(3):
            page.evaluate('''
                document.querySelectorAll('button').forEach(b => {
                    const txt = (b.innerText || '').trim().toLowerCase();
                    if(txt === 'got it' || txt === 'okay' || txt === 'dismiss' || txt === 'close') {
                        try { b.click(); } catch (_) {}
                    }
                });
            ''')
            time.sleep(0.5)
    except Exception as e:
        logger.debug(red(f"Error removing walkthrough modals: {e}"))


def _set_interactivity(
    page: Page,
    comment: bool = True,
    stitch: bool = True,
    duet: bool = True,
    *args,
    **kwargs,
) -> None:
    """
    Sets the interactivity settings of the video
    """
    try:
        logger.debug(green("Setting interactivity settings"))

        comment_box = page.locator(f"xpath={config.selectors.upload.comment}")
        stitch_box = page.locator(f"xpath={config.selectors.upload.stitch}")
        duet_box = page.locator(f"xpath={config.selectors.upload.duet}")

        # Use a short timeout of 2 seconds so we don't wait 30 seconds if TikTok removed these
        if comment_box.is_visible(timeout=2000):
            if comment ^ comment_box.is_checked():
                comment_box.click()

        if stitch_box.is_visible(timeout=2000):
            if stitch ^ stitch_box.is_checked():
                stitch_box.click()

        if duet_box.is_visible(timeout=2000):
            if duet ^ duet_box.is_checked():
                duet_box.click()

        # Disable "Content check lite" switch if present to prevent long stalls
        try:
            content_check_label = page.get_by_text("Content check lite", exact=True)
            if content_check_label.count() > 0:
                switch = (
                    content_check_label.first.locator(
                        "xpath=ancestor::div[contains(@class, 'headline-wrapper')][1]"
                    )
                    .locator(".Switch__content")
                    .first
                )
                if switch.get_attribute("aria-checked") == "true":
                    switch.click(timeout=3000)
        except Exception:
            pass

    except Exception as _:
        logger.debug(red("Skipping interactivity settings (TikTok UI likely changed)"))


def _set_visibility(
    page: Page, visibility: Literal["everyone", "friends", "only_you"]
) -> None:
    """
    Sets the visibility/privacy of the video
    """
    try:
        logger.debug(green(f"Setting visibility to: {visibility}"))

        dropdown_xpath = (
            "//div[@data-e2e='video_visibility_container']//button[@role='combobox']"
        )
        dropdown = page.locator(f"xpath={dropdown_xpath}")
        dropdown.click()
        time.sleep(1.5)

        visibility_text_map = {
            "everyone": "Everyone",
            "friends": "Friends",
            "only_you": "Only you",
        }

        option_text = visibility_text_map.get(visibility, "Everyone")
        option_xpath = f"//div[@role='option' and contains(., '{option_text}')]"

        option = page.locator(f"xpath={option_xpath}")
        option.scroll_into_view_if_needed()
        time.sleep(0.5)
        option.click()

        logger.debug(green(f"Successfully set visibility to: {visibility}"))

    except Exception as e:
        logger.error(red(f"Failed to set visibility: {e}"))


_TIKTOK_SCHEDULE_DATE_INPUTS = (
    '[data-e2e="schedule_container"] input[type="date"], '
    'input[type="date"], '
    '[data-e2e="schedule_container"] input[placeholder*="date" i], '
    'input[placeholder*="date" i], '
    '[data-e2e="schedule_container"] input[aria-label*="date" i], '
    'input[aria-label*="date" i], '
    '[data-e2e="schedule_container"] input[value*="-"]'
)
_TIKTOK_SCHEDULE_TIME_INPUTS = (
    '[data-e2e="schedule_container"] input[type="time"], '
    'input[type="time"], '
    '[data-e2e="schedule_container"] input[placeholder*="time" i], '
    'input[placeholder*="time" i], '
    '[data-e2e="schedule_container"] input[aria-label*="time" i], '
    'input[aria-label*="time" i], '
    '[data-e2e="schedule_container"] input[value*=":"]'
)


def _visible_tiktok_schedule_input(page: Page, selector: str):
    try:
        for candidate in page.locator(selector).all():
            if candidate.is_visible(timeout=500):
                return candidate
    except Exception:
        return None
    return None


def _fill_tiktok_schedule_inputs(page: Page, local_schedule: datetime.datetime) -> bool:
    if not isinstance(local_schedule, datetime.datetime):
        return False
    try:
        try:
            timezone_name = str(
                page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or ""
            )
            if local_schedule.tzinfo is not None and timezone_name:
                local_schedule = local_schedule.astimezone(ZoneInfo(timezone_name))
        except Exception:
            pass

        date_str = local_schedule.strftime("%Y-%m-%d")
        time_str = local_schedule.strftime("%H:%M")

        # 1. Try filling visible native inputs via Playwright locator
        date_input = _visible_tiktok_schedule_input(page, _TIKTOK_SCHEDULE_DATE_INPUTS)
        time_input = _visible_tiktok_schedule_input(page, _TIKTOK_SCHEDULE_TIME_INPUTS)
        if date_input is not None and time_input is not None:
            try:
                date_input.fill(date_str, timeout=5000)
                time_input.fill(time_str, timeout=5000)
                return True
            except Exception:
                pass

        # 2. Fallback to DOM input value setter
        filled = page.evaluate(
            """
            ([dateStr, timeStr]) => {
                const dateInput = document.querySelector('input[type="date"], input[placeholder*="date" i]');
                const timeInput = document.querySelector('input[type="time"], input[placeholder*="time" i]');
                if (!dateInput || !timeInput) return false;

                const setVal = (el, val) => {
                    const proto = Object.getPrototypeOf(el);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set ||
                                   Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                    if (setter) {
                        setter.call(el, val);
                    } else {
                        el.value = val;
                    }
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };

                setVal(dateInput, dateStr);
                setVal(timeInput, timeStr);
                return true;
            }
            """,
            [date_str, time_str],
        )
        if filled:
            time.sleep(0.3)
            return True
        return False
    except Exception:
        return False


def _set_tiktok_studio_custom_picker(page: Page, local_schedule: datetime.datetime) -> bool:
    if not isinstance(local_schedule, datetime.datetime):
        return False
    try:
        _dismiss_walkthrough_overlays(page)

        try:
            timezone_name = str(
                page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or ""
            )
            if local_schedule.tzinfo is not None and timezone_name:
                local_schedule = local_schedule.astimezone(ZoneInfo(timezone_name))
        except Exception:
            pass

        # Align minute to 5-minute increment safely (rolling over hour if needed)
        rem = local_schedule.minute % 5
        if rem != 0:
            local_schedule = local_schedule + datetime.timedelta(minutes=(5 - rem))

        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]
        target_year = local_schedule.year
        target_month = local_schedule.month
        target_day = local_schedule.day

        # 1. Date selection
        date_trigger = page.locator(
            'div[role="combobox"][aria-label*="date" i], '
            '.scheduled-picker .Picker__root:first-child, '
            '.scheduled-picker input[value*="-"], '
            '[data-e2e="schedule_container"] input[value*="-"], '
            '.schedule-picker [aria-label*="date" i], '
            '.DatePicker__trigger, '
            'div[class*="date-picker"], '
            'div[class*="schedule"] [role="combobox"], '
            'input[placeholder*="YYYY-MM-DD" i], '
            'input[placeholder*="date" i]'
        ).first
        if not date_trigger.is_visible(timeout=3000):
            return False

        try:
            date_trigger.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        date_trigger.click(force=True, timeout=3000)
        time.sleep(0.5)

        curr_year = None
        year_el = page.locator(".DatePicker__titleYear, [class*='titleYear']").first
        if year_el.is_visible(timeout=500):
            try:
                curr_year = int(year_el.inner_text().strip())
            except Exception:
                pass

        title_month_el = page.locator(".DatePicker__titleMonth, [class*='titleMonth']").first
        if title_month_el.is_visible(timeout=1500):
            curr_month_str = title_month_el.inner_text().strip().lower()
            if curr_month_str in month_names:
                curr_month = month_names.index(curr_month_str) + 1
                diff = target_month - curr_month
                if curr_year is not None:
                    diff += (target_year - curr_year) * 12

                if diff > 0:
                    next_btn = page.locator(
                        'button[aria-label="Next month"], '
                        '.DatePicker__nav:has([data-icon="ArrowRight"]), '
                        'button:has([data-icon="ArrowRight"])'
                    ).first
                    for _ in range(min(diff, 12)):
                        next_btn.click(force=True, timeout=2000)
                        time.sleep(0.15)
                elif diff < 0:
                    prev_btn = page.locator(
                        'button[aria-label="Previous month"], '
                        '.DatePicker__nav:has([data-icon="ArrowLeft"]), '
                        'button:has([data-icon="ArrowLeft"])'
                    ).first
                    for _ in range(min(abs(diff), 12)):
                        prev_btn.click(force=True, timeout=2000)
                        time.sleep(0.15)

        day_btn = page.locator(
            'button.DatePicker__day:not([disabled]):not([data-outside="true"]), '
            'div[class*="day"]:not([class*="disabled"]):not([class*="outside"])'
        ).filter(has_text=re.compile(rf"^\s*{target_day}\s*$")).first

        if not day_btn.is_visible(timeout=500):
            day_btn = page.locator(
                f'xpath=//span[contains(@class, "day") and text()="{target_day}"] | '
                f'//div[contains(@class, "day") and text()="{target_day}"] | '
                f'//span[contains(@class, "month-day") and text()="{target_day}"]'
            ).first

        if not day_btn.is_visible(timeout=500):
            day_btn = page.locator(
                'button.DatePicker__day:not([disabled]):not([data-outside="true"])'
            ).filter(has_text=f"{target_day}").first

        if day_btn.is_visible(timeout=2000):
            day_btn.click(force=True, timeout=3000)
            time.sleep(0.3)

        # Close calendar popover cleanly if still open
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.3)

        # 2. Time selection
        time_trigger = page.locator(
            'div[role="combobox"][aria-label="Select time"], '
            'div[role="combobox"][aria-label*="Select time" i], '
            '.scheduled-picker .Picker__root:nth-child(2), '
            '.TimePicker__trigger, '
            'div[class*="time-picker"], '
            'input[placeholder*="HH:mm" i]'
        ).first

        target_hour = local_schedule.hour
        target_min = local_schedule.minute
        target_h_str = f"{target_hour:02d}"
        target_m_str = f"{target_min:02d}"

        try:
            # Click time trigger via JS to ensure we target the exact time box (not timezone)
            clicked_trigger = bool(page.evaluate("""() => {
                const pickers = Array.from(document.querySelectorAll('.scheduled-picker .Picker__root, .Picker__root.schedule-picker'));
                const timeBox = pickers.find(p => /^\\d{1,2}:\\d{2}$/.test((p.innerText || '').trim()) || p.querySelector('[aria-label="Select time"]'));
                if (timeBox) {
                    timeBox.scrollIntoView({ block: 'center' });
                    timeBox.click();
                    return true;
                }
                return false;
            }"""))

            if not clicked_trigger:
                if time_trigger.is_visible(timeout=2000):
                    time_trigger.scroll_into_view_if_needed(timeout=2000)
                    time_trigger.click(force=True, timeout=3000)
            time.sleep(0.5)

            # Select hour and minute via locator with scroll_into_view_if_needed
            hour_btn = page.locator(
                f'.TimePicker__column:first-child button:has-text("{target_h_str}"), '
                f'.TimePicker__column:first-child button:has-text("{target_hour}"), '
                f'button[role="option"][aria-label="Hours {target_h_str}"], '
                f'button[role="option"][aria-label*="{target_h_str}"], '
                f'span.tiktok-timepicker-left:has-text("{target_h_str}"), '
                f'span.tiktok-timepicker-left:has-text("{target_hour}"), '
                f'li[role="option"]:has-text("{target_h_str}"), '
                f'div[role="option"]:has-text("{target_h_str}")'
            ).first
            try:
                hour_btn.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            hour_btn.click(force=True, timeout=2000)
            time.sleep(0.2)

            min_btn = page.locator(
                f'.TimePicker__column:last-child button:has-text("{target_m_str}"), '
                f'button[role="option"][aria-label="Minutes {target_m_str}"], '
                f'button[role="option"][aria-label*="{target_m_str}"], '
                f'span.tiktok-timepicker-right:has-text("{target_m_str}"), '
                f'li[role="option"]:has-text("{target_m_str}"), '
                f'div[role="option"]:has-text("{target_m_str}")'
            ).first
            try:
                min_btn.scroll_into_view_if_needed(timeout=1500)
            except Exception:
                pass
            min_btn.click(force=True, timeout=2000)
            time.sleep(0.2)

            # Close dropdown cleanly with Escape to avoid blocking Post button
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.3)

        except Exception as t_err:
            logger.debug(f"Time selection error: {t_err}")

        return True
    except Exception as exc:
        logger.debug(f"Custom picker failed: {exc}")
        return False


def _set_schedule_video(page: Page, schedule: datetime.datetime) -> None:
    """
    Sets the schedule of the video
    """
    logger.debug(green("Setting schedule"))
    _dismiss_walkthrough_overlays(page)

    timezone_str = str(page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone") or "UTC")
    try:
        driver_timezone = pytz.timezone(timezone_str)
        local_schedule = schedule.astimezone(driver_timezone)
    except Exception:
        local_schedule = schedule

    # 1. Enable schedule toggle / radio in TikTok Studio
    try:
        # Scroll down to ensure "When to post" section is in view
        try:
            when_to_post = page.locator(
                "div:has-text('When to post'), "
                "span:has-text('When to post'), "
                "div:has-text('Kapan memposting'), "
                "span:has-text('Kapan memposting')"
            ).first
            when_to_post.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            try:
                page.mouse.wheel(0, 600)
            except Exception:
                pass

        # Activate "Schedule" radio button
        activated = False
        try:
            activated = bool(page.evaluate(
                """() => {
                    const candidates = Array.from(document.querySelectorAll('label, div, span, input'));
                    const schedTarget = candidates.find(el => {
                        const txt = (el.innerText || el.textContent || '').trim();
                        return (txt.startsWith('Schedule') || txt.startsWith('Jadwalkan')) &&
                               (el.tagName === 'LABEL' || el.tagName === 'INPUT' || el.querySelector('input[type="radio"]'));
                    });
                    if (schedTarget) {
                        schedTarget.scrollIntoView({ behavior: 'instant', block: 'center' });
                        schedTarget.click();
                        const r = schedTarget.querySelector('input[type="radio"]') ||
                                  schedTarget.parentElement?.querySelector('input[type="radio"]') ||
                                  (schedTarget.tagName === 'INPUT' ? schedTarget : null);
                        if (r) {
                            r.checked = true;
                            r.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        return true;
                    }
                    return false;
                }"""
            ))
        except Exception:
            activated = False

        if not activated:
            schedule_opt = page.locator(
                '[data-e2e="schedule_container"] label:has-text("Schedule"), '
                'label:has-text("Schedule"), '
                'label:has-text("Jadwalkan"), '
                'input[type="radio"][value*="schedule" i]'
            ).first
            if not schedule_opt.is_visible(timeout=1000):
                schedule_opt = page.locator(f"xpath={config.selectors.schedule.switch}").first

            try:
                schedule_opt.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            schedule_opt.click(force=True, timeout=3000)

        time.sleep(0.5)

        # Dismiss any "Allow" or "Got it" authorization modal
        for allow_sel in [
            "button:has-text('Allow')",
            "button:has-text('Izinkan')",
            "button:has-text('Got it')",
            "button:has-text('Mengerti')",
            "button:has-text('OK')",
        ]:
            allow_btn = page.locator(allow_sel).first
            if allow_btn.is_visible(timeout=1000):
                allow_btn.click(force=True)
                time.sleep(0.5)
                break

        # Wait for schedule pickers to mount into DOM
        time.sleep(1.5)
    except Exception as exc:
        logger.debug(f"Could not prepare schedule switch: {exc}")

    # 2. Try modern Studio custom pickers
    if _set_tiktok_studio_custom_picker(page, local_schedule):
        logger.debug(green("Schedule set via modern Studio custom pickers"))
        return

    # 3. Try native inputs
    if _fill_tiktok_schedule_inputs(page, local_schedule):
        logger.debug(green("Schedule set via native inputs"))
        return

    # 4. Fallback to legacy calendar (only if element is actually visible, never hang 30s)
    month = local_schedule.month
    day = local_schedule.day
    hour = local_schedule.hour
    minute = local_schedule.minute

    try:
        date_picker = page.locator(f"xpath={config.selectors.schedule.date_picker}").first
        if date_picker.is_visible(timeout=2000):
            __date_picker(page, month, day)
            __time_picker(page, hour, minute)
            logger.debug(green("Schedule set via legacy calendar"))
            return
    except Exception as e:
        logger.debug(f"Legacy calendar fallback failed: {e}")

    # 5. If schedule could not be set automatically, fallback to Upload Now to prevent losing the video!
    logger.warning(
        "TikTok schedule pickers could not be configured automatically. "
        "Falling back to uploading immediately (Upload Now) so the video is published."
    )
    try:
        now_radio = page.locator(
            'label:has-text("Now"), '
            'label:has-text("Sekarang"), '
            'input[type="radio"][value*="now" i]'
        ).first
        if now_radio.is_visible(timeout=1500):
            now_radio.click(force=True)
            time.sleep(0.5)
    except Exception:
        pass


def __date_picker(page: Page, month: int, day: int) -> None:
    logger.debug(green("Picking date"))

    date_picker = page.locator(f"xpath={config.selectors.schedule.date_picker}").first
    date_picker.click(timeout=3000)

    calendar = page.locator(f"xpath={config.selectors.schedule.calendar}")
    calendar.wait_for(state="visible")

    calendar_month = page.locator(
        f"xpath={config.selectors.schedule.calendar_month}"
    ).inner_text()
    n_calendar_month = datetime.datetime.strptime(calendar_month, "%B").month

    if n_calendar_month != month:
        arrows = page.locator(f"xpath={config.selectors.schedule.calendar_arrows}")
        if n_calendar_month < month:
            arrows.last.click()
        else:
            arrows.first.click()

    valid_days = page.locator(
        f"xpath={config.selectors.schedule.calendar_valid_days}"
    ).all()

    day_to_click = None
    for day_option in valid_days:
        if int(day_option.inner_text()) == day:
            day_to_click = day_option
            break
    if day_to_click:
        day_to_click.click()
    else:
        raise Exception("Day not found in calendar")

    __verify_date_picked_is_correct(page, month, day)


def __verify_date_picked_is_correct(page: Page, month: int, day: int) -> None:
    date_selected = page.locator(
        f"xpath={config.selectors.schedule.date_picker}"
    ).get_attribute("value") # 🟢 UBAH BAGIAN INI SAJA
    
    date_selected_month = int(date_selected.split("-")[1])
    date_selected_day = int(date_selected.split("-")[2])

    if date_selected_month == month and date_selected_day == day:
        logger.debug(green("Date picked correctly"))
    else:
        msg = f"Something went wrong with the date picker, expected {month}-{day} but got {date_selected_month}-{date_selected_day}"
        logger.error(msg)
        raise Exception(msg)


def __time_picker(page: Page, hour: int, minute: int) -> None:
    logger.debug(green("Picking time"))

    time_picker = page.locator(f"xpath={config.selectors.schedule.time_picker}")
    time_picker.click()

    time_picker_container = page.locator(
        f"xpath={config.selectors.schedule.time_picker_container}"
    )
    time_picker_container.wait_for(state="visible")

    hour_options = page.locator(f"xpath={config.selectors.schedule.timepicker_hours}")
    minute_options = page.locator(
        f"xpath={config.selectors.schedule.timepicker_minutes}"
    )

    # hour_to_click = hour_options.nth(hour)
    # minute_option_correct_index = int(minute / 5)
    # minute_to_click = minute_options.nth(minute_option_correct_index)
    hour_to_click = page.locator(f"xpath={config.selectors.schedule.timepicker_hours}[text()='{hour:02d}']")
    minute_to_click = page.locator(f"xpath={config.selectors.schedule.timepicker_minutes}[text()='{minute:02d}']")

    hour_to_click.scroll_into_view_if_needed()
    time.sleep(0.5)
    hour_to_click.click()

    minute_to_click.scroll_into_view_if_needed()
    time.sleep(0.5)
    minute_to_click.click()

    time_picker.click()
    time.sleep(0.5)

    __verify_time_picked_is_correct(page, hour, minute)


def __verify_time_picked_is_correct(page: Page, hour: int, minute: int) -> None:
    time_selected = page.locator(
        f"xpath={config.selectors.schedule.time_picker}" # 🟢 HAPUS '_text' di belakangnya
    ).get_attribute("value") # 🟢 UBAH JADI get_attribute("value")
    
    time_selected_hour = int(time_selected.split(":")[0])
    time_selected_minute = int(time_selected.split(":")[1])

    if time_selected_hour == hour and time_selected_minute == minute:
        logger.debug(green("Time picked correctly"))
    else:
        msg = (
            f"Something went wrong with the time picker, "
            f"expected {hour:02d}:{minute:02d} "
            f"but got {time_selected_hour:02d}:{time_selected_minute:02d}"
        )
        raise Exception(msg)


def _post_video(page: Page, cfg: Any = None, is_scheduled: bool = False) -> None:
    """
    Posts the video
    """
    logger.debug(green("Clicking the post button"))

    active_config = cfg or config

    # Early check: If already redirected to content manager, post already succeeded
    page_url = str(getattr(page, "url", "") or "")
    if "/tiktokstudio/content" in page_url or "/creator-center/content" in page_url:
        logger.debug(green(f"Upload already confirmed via URL redirect: {page_url}"))
        return

    _dismiss_walkthrough_overlays(page)

    post_xpath = "//button[@data-e2e='post_video_button']"
    try:
        post_xpath = active_config.selectors.upload.post
    except Exception:
        pass

    post_btn = page.locator(
        f"xpath={post_xpath} | "
        "//button[contains(@class, 'Button__root--type-primary') and (contains(., 'Post') or contains(., 'Schedule') or contains(., 'Posting') or contains(., 'Jadwalkan'))] | "
        "//button[@data-e2e='post_video_button']"
    ).last

    try:
        def is_enabled() -> bool:
            try:
                if not post_btn.is_visible(timeout=300):
                    # Check via DOM querySelector
                    return bool(page.evaluate(
                        """() => {
                            const b = document.querySelector('button[data-e2e="post_video_button"]') ||
                                      document.querySelector('button.Button__root--type-primary');
                            if (!b) return false;
                            return !b.disabled && b.getAttribute('aria-disabled') !== 'true' && b.getAttribute('data-disabled') !== 'true';
                        }"""
                    ))
                data_dis = post_btn.get_attribute("data-disabled", timeout=300)
                if data_dis == "false":
                    return True
                if data_dis == "true":
                    return False
                aria_dis = post_btn.get_attribute("aria-disabled", timeout=300)
                if aria_dis == "true":
                    return False
                dis = post_btn.get_attribute("disabled", timeout=300)
                if dis is not None and dis != "false":
                    return False
                return post_btn.is_enabled(timeout=300)
            except Exception:
                return False

        uploading_wait = int(getattr(active_config, "uploading_wait", 180))
        max_checks = min(max(int(uploading_wait / 2), 1), 60)
        for _ in range(max_checks):
            if is_enabled():
                break
            time.sleep(1)

        _dismiss_walkthrough_overlays(page)
        try:
            post_btn.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass

        clicked = False
        try:
            post_btn.click(timeout=3000)
            clicked = True
        except Exception:
            try:
                post_btn.click(force=True, timeout=3000)
                clicked = True
            except Exception:
                pass

        if not clicked:
            # Click via JavaScript DOM fallback
            clicked = bool(page.evaluate(
                """() => {
                    const btns = Array.from(document.querySelectorAll('button'));
                    const target = btns.reverse().find(b => {
                        const txt = (b.innerText || '').trim().toLowerCase();
                        return ['post', 'schedule', 'posting', 'jadwalkan'].includes(txt) ||
                               b.getAttribute('data-e2e') === 'post_video_button' ||
                               (b.className && b.className.includes('primary') && (txt.includes('post') || txt.includes('sched')));
                    });
                    if (target) {
                        target.click();
                        return true;
                    }
                    return false;
                }"""
            ))

    except Exception as exc:
        logger.debug(f"Error clicking post button: {exc}")

    time.sleep(1)

    # Check if page actually has schedule active
    if not is_scheduled:
        try:
            is_scheduled = bool(page.evaluate("""() => {
                const s = document.querySelector('input[name="postSchedule"][value="schedule"]') ||
                          document.querySelector('.schedule-radio-container input[type="radio"]:checked');
                return s ? (s.value === 'schedule' || s.checked) : false;
            }"""))
        except Exception:
            pass

    # Handle confirmation dialogs (copyright warnings, schedule anyway, etc.)
    if is_scheduled:
        confirm_dialog_selectors = [
            "button:has-text('Schedule anyway')",
            "button:has-text('Tetap jadwalkan')",
            "button:has-text('Continue to schedule')",
            "button:has-text('Lanjutkan menjadwalkan')",
            "div[role='dialog'] button:has-text('Schedule')",
            "div[role='dialog'] button:has-text('Jadwalkan')",
            "div[role='dialog'] button.Button__root--type-primary",
            "div[role='dialog'] button.TUXButton--primary",
            "div[role='dialog'] button[class*='primary' i]",
            "button:has-text('Schedule now')",
            "button:has-text('Jadwalkan sekarang')",
        ]
    else:
        confirm_dialog_selectors = [
            "button:has-text('Post now')",
            "button:has-text('Posting sekarang')",
            "button:has-text('Post anyway')",
            "button:has-text('Tetap posting')",
            "button:has-text('Continue to post')",
            "button:has-text('Lanjutkan posting')",
            "div[role='dialog'] button.Button__root--type-primary",
            "div[role='dialog'] button.TUXButton--primary",
            "div[role='dialog'] button[class*='primary' i]",
            "div[role='dialog'] button:has-text('Post')",
            "div[role='dialog'] button:has-text('Posting')",
            "//button[.//div[contains(text(), 'Post now') or contains(text(), 'Post anyway')]]",
        ]

    def try_dismiss_confirm_dialog() -> bool:
        for sel in confirm_dialog_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=400):
                    btn.click(force=True, timeout=2000)
                    logger.debug(green(f"Clicked confirmation dialog button: {sel}"))
                    time.sleep(0.5)
                    return True
            except Exception:
                pass
        return False

    explicit_wait = int(getattr(active_config, "explicit_wait", 60))

    for _ in range(min(explicit_wait * 2, 8)):
        if try_dismiss_confirm_dialog():
            break
        page_url = str(getattr(page, "url", "") or "")
        if "/tiktokstudio/content" in page_url or "/creator-center/content" in page_url:
            break
        time.sleep(0.5)

    # Wait for the successful post redirection or confirmation
    start_wait = time.time()
    while time.time() - start_wait < explicit_wait:
        cur_url = str(getattr(page, "url", "") or "")
        if "/tiktokstudio/content" in cur_url or "/creator-center/content" in cur_url:
            logger.debug(green(f"TikTok upload confirmed via URL redirect: {cur_url}"))
            time.sleep(2.0)
            return

        # Check late confirmation dialogs
        try_dismiss_confirm_dialog()

        # Check for toasts / success dialog
        try:
            for sel in [
                "button:has-text('Manage your posts')",
                "button:has-text('Upload another video')",
                "button:has-text('Manage posts')",
                "button:has-text('Upload another')",
                "div[role='dialog'] :has-text('Your video has been scheduled')",
                "div[role='dialog'] :has-text('Your video has been uploaded')",
                "div[role='dialog'] :has-text('Video scheduled')",
                "div[role='dialog'] :has-text('Video published')",
                "text=/Your video has been scheduled/i",
                "text=/Your video has been uploaded/i",
                "text=/Your video was scheduled/i",
                "text=/Video scheduled/i",
                "text=/Video published/i",
                "text=/dipublikasikan/i",
                "text=/dijadwalkan/i",
                "text=/视频已发布/i",
            ]:
                if page.locator(sel).first.is_visible(timeout=300):
                    logger.debug(green(f"Upload confirmed via indicator: {sel}"))
                    time.sleep(2.0)
                    return
        except Exception:
            pass

        time.sleep(1)

    cur_url = str(getattr(page, "url", "") or "")
    if "/tiktokstudio/content" in cur_url or "/creator-center/content" in cur_url:
        logger.debug(green(f"TikTok upload confirmed via URL redirect: {cur_url}"))
        time.sleep(2.0)
        return

    try:
        debug_shot_path = "/home/admin/Developer/scripts/OmniClip-Workspace/tiktok_post_timeout_debug.png"
        page.screenshot(path=debug_shot_path, full_page=True)
        dialog_txt = page.evaluate("() => Array.from(document.querySelectorAll('div[role=\"dialog\"], .TUXModal, [class*=\"modal\"]')).map(el => (el.innerText || '').trim()).filter(Boolean).join('\\n---\\n')")
        btn_txt = page.evaluate("() => Array.from(document.querySelectorAll('button')).map(b => (b.innerText || '').trim()).filter(Boolean).join(', ')")
        logger.warning(f"TikTok upload confirmation timeout. Page URL: {cur_url}")
        if dialog_txt:
            logger.warning(f"Dialog texts: {dialog_txt}")
        if btn_txt:
            logger.warning(f"Buttons on page: {btn_txt}")
    except Exception as dbg_err:
        logger.debug(f"Failed to capture debug info: {dbg_err}")

    raise TimeoutError(
        f"Timed out after {explicit_wait}s waiting for TikTok upload confirmation."
    )


def _add_product_link(page: Page, product_id: str) -> None:
    """
    Adds the product link
    """
    logger.debug(green(f"Attempting to add product link for ID: {product_id}..."))
    try:
        add_link_button = page.locator(
            "//button[contains(@class, 'Button__root') and contains(., 'Add')]"
        )
        add_link_button.click()
        time.sleep(1)

        try:
            first_next = page.locator(
                "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Next']]"
            )
            if first_next.is_visible(timeout=3000):
                first_next.click()
                time.sleep(1)
        except Exception:
            pass

        search_input = page.locator("//input[@placeholder='Search products']")
        search_input.fill(product_id)
        search_input.press("Enter")
        time.sleep(3)

        product_radio = page.locator(
            f"//tr[.//span[contains(text(), '{product_id}')] or .//div[contains(text(), '{product_id}')]]//input[@type='radio' and contains(@class, 'TUXRadioStandalone-input')]"
        )
        product_radio.click()
        time.sleep(1)

        second_next = page.locator(
            "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Next']]"
        )
        second_next.click()
        time.sleep(1)

        final_add = page.locator(
            "//button[contains(@class, 'TUXButton--primary') and .//div[text()='Add']]"
        )
        final_add.click()

        final_add.wait_for(state="hidden")

    except Exception as e:
        logger.error(red(f"Error adding product link: {e}"))


def _set_cover(page: Page, cover_path: str) -> None:
    """
    Adds a custom cover using the NEW TikTok UI (Modal) with graceful fallback to OLD UI
    """
    logger.debug(green(f"Attempting to add custom cover: {cover_path}..."))
    try:
        from os.path import exists
        if not exists(cover_path):
            raise Exception("Invalid cover image file path")

        _dismiss_walkthrough_overlays(page)

        # ------------------- NEW TIKTOK UI FLOW -------------------
        try:
            popup_handled = False
            # Check for possible "New editing features" onboarding popup
            got_it_btn = page.locator("button:has-text('Got it')").last
            if got_it_btn.is_visible(timeout=2000):
                got_it_btn.click(force=True)
                time.sleep(1)
                popup_handled = True

            # 1. Click "Edit cover" on the thumbnail
            new_edit_cover_btn = page.locator("div:has-text('Edit cover')").last
            if not new_edit_cover_btn.is_visible(timeout=5000):
                # If Edit cover is not yet visible, video might still be finalizing preview
                time.sleep(2.0)

            if not new_edit_cover_btn.is_visible(timeout=5000):
                logger.warning("Edit cover button not visible; skipping custom cover.")
                return
            
            new_edit_cover_btn.click(force=True, timeout=5000)
            time.sleep(1.5)

            # 2. Wait for modal (Upload cover image label)
            upload_label = page.locator("label[aria-label='Upload cover image'], div[role='dialog'] label").last
            upload_label.wait_for(state="visible", timeout=6000)

            # 3. Find file input & upload
            # Looks for standard image accept pattern inside dialog
            file_input = page.locator("div[role='dialog'] input[type='file'][accept*='image'], input[type='file'][accept*='image']").last
            file_input.set_input_files(cover_path, timeout=5000)
            time.sleep(2.5)  # Wait for upload & preview to reflect

            # 4. Click Save
            save_btn = page.locator("div[role='dialog'] button:has-text('Save'), div[role='dialog'] button:has-text('Simpan')").first
            save_btn.wait_for(state="visible", timeout=3000)
            save_btn.click(force=True)

            # 5. Wait for modal to close
            save_btn.wait_for(state="hidden", timeout=12000)
            time.sleep(1.5)
            logger.debug(green(f"Successfully set cover (New UI)"))
            return  # Success, exit function

        except Exception as e_new:
            logger.debug(f"New UI cover flow failed. Reason: {str(e_new)}")
            # Attempt to close the modal cleanly if stuck
            try:
                page.evaluate("""() => {
                    const cancel = Array.from(document.querySelectorAll('div[role="dialog"] button, .TUXModal button')).find(b => (b.innerText || '').trim().toLowerCase() === 'cancel');
                    if (cancel) cancel.click();
                }""")
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.5)

    except Exception as e:
        logger.warning(f"Error setting cover (non-fatal, continuing): {e}")
        try:
            # ONLY close dialogs, never click root page Cancel buttons!
            page.evaluate("""() => {
                const cancel = Array.from(document.querySelectorAll('div[role="dialog"] button, .TUXModal button')).find(b => (b.innerText || '').trim().toLowerCase() === 'cancel');
                if (cancel) cancel.click();
            }""")
            page.keyboard.press("Escape")
        except Exception:
            pass


def _check_valid_path(path: str) -> bool:
    return exists(path) and path.split(".")[-1] in config.supported_file_types


def _check_valid_cover_path(path: str) -> bool:
    return exists(path) and path.split(".")[-1] in config.supported_image_file_types


def _get_valid_schedule_minute(
    schedule: datetime.datetime, valid_multiple
) -> datetime.datetime:
    if _is_valid_schedule_minute(schedule.minute, valid_multiple):
        return schedule
    else:
        return _set_valid_schedule_minute(schedule, valid_multiple)


def _is_valid_schedule_minute(minute: int, valid_multiple) -> bool:
    if minute % valid_multiple != 0:
        return False
    else:
        return True


def _set_valid_schedule_minute(
    schedule: datetime.datetime, valid_multiple: int
) -> datetime.datetime:
    minute = schedule.minute
    remainder = minute % valid_multiple
    integers_to_valid_multiple = 5 - remainder
    schedule += datetime.timedelta(minutes=integers_to_valid_multiple)
    return schedule


def _check_valid_schedule(schedule: datetime.datetime) -> bool:
    valid_tiktok_minute_multiple = 5
    margin_to_complete_upload_form = 5
    datetime_utc_now = datetime.datetime.now(pytz.UTC)
    if schedule.tzinfo is None:
        schedule = pytz.UTC.localize(schedule)
    else:
        schedule = schedule.astimezone(pytz.UTC)

    min_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(minutes=15)
    min_datetime_tiktok_valid += datetime.timedelta(
        minutes=margin_to_complete_upload_form
    )
    max_datetime_tiktok_valid = datetime_utc_now + datetime.timedelta(days=10)
    if schedule < min_datetime_tiktok_valid or schedule > max_datetime_tiktok_valid:
        return False
    elif not _is_valid_schedule_minute(schedule.minute, valid_tiktok_minute_multiple):
        return False
    else:
        return True


def _convert_videos_dict(
    videos_list_of_dictionaries: list[dict[str, Any]],
) -> list[VideoDict]:
    if not videos_list_of_dictionaries:
        raise RuntimeError("No videos to upload")

    valid_path = config.valid_path_names
    valid_description = config.valid_descriptions

    correct_path = valid_path[0]
    correct_description = valid_description[0]

    def intersection(lst1, lst2):
        return list(set(lst1) & set(lst2))

    return_list: list[VideoDict] = []
    for elem in videos_list_of_dictionaries:
        elem = {k.strip().lower(): v for k, v in elem.items()}
        keys = elem.keys()
        path_intersection = intersection(valid_path, keys)
        description_intersection = intersection(valid_description, keys)

        if path_intersection:
            path = elem[path_intersection.pop()]
            if not _check_valid_path(path):
                raise RuntimeError("Invalid path: " + path)
            elem[correct_path] = path
        else:
            for _, value in elem.items():
                if _check_valid_path(value):
                    elem[correct_path] = value
                    break
            else:
                raise RuntimeError("Path not found in dictionary: " + str(elem))

        if description_intersection:
            elem[correct_description] = elem[description_intersection.pop()]
        else:
            for _, value in elem.items():
                if not _check_valid_path(value):
                    elem[correct_description] = value
                    break
            else:
                elem[correct_description] = ""

        return_list.append(elem)  # type: ignore

    return return_list


class DescriptionTooLong(Exception):
    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__)


class FailedToUpload(Exception):
    def __init__(self, message=None):
        super().__init__(message or self.__doc__)
