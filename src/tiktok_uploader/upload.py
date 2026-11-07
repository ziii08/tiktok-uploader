"""
`tiktok_uploader` module for uploading videos to TikTok

Key Classes
-----------
TikTokUploader : Client for uploading videos to TikTok
"""

import datetime
import logging
import time
from collections.abc import Callable
from os.path import abspath, exists
from typing import Any, Literal

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
                    timezone = pytz.UTC
                    if schedule.tzinfo is None:
                        schedule = schedule.astimezone(timezone)
                    elif (utc_offset := schedule.utcoffset()) is not None and int(
                        utc_offset.total_seconds()
                    ) == 0:  # Equivalent to UTC
                        schedule = timezone.localize(schedule)
                    else:
                        print(
                            f"{schedule} is invalid, the schedule datetime must be naive or aware with UTC timezone, skipping"
                        )
                        failed.append(video)
                        continue

                    valid_tiktok_minute_multiple = 5
                    schedule = _get_valid_schedule_minute(
                        schedule, valid_tiktok_minute_multiple
                    )
                    if not _check_valid_schedule(schedule):
                        print(
                            f"{schedule} is invalid, the schedule datetime must be as least 20 minutes in the future, and a maximum of 10 days, skipping"
                        )
                        failed.append(video)
                        continue

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
        """Closes the browser instance."""
        if self._page:
            try:
                self._page.context.browser.close()
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
    _post_video(page)


def _go_to_upload(page: Page) -> None:
    """
    Navigates to the upload page
    """
    logger.debug(green("Navigating to upload page"))

    try:
        if page.url != config.paths.upload:
            page.goto(str(config.paths.upload), timeout=60000, wait_until="domcontentloaded")
        else:
            # refresh
            page.reload(timeout=60000, wait_until="domcontentloaded")
    except Exception as e:
        logger.debug(red(f"Page load timeout or error (continuing anyway): {e}"))
        
    # TODO: handle alert if any (Playwright auto-dismisses dialogs usually, or we can handle)
        page.on("dialog", lambda dialog: dialog.accept())

    # waits for the root to load
    page.wait_for_selector("#root", timeout=config.explicit_wait * 1000)


def _dismiss_walkthrough_overlays(page: Page) -> None:
    """
    Proactively dismisses joyride tour overlays, floating modals, and banners.
    """
    try:
        page.evaluate(
            """
            () => {
                const overlays = document.querySelectorAll(
                    '#react-joyride-portal, .react-joyride__overlay, .react-joyride__tooltip, [data-test-id="overlay"], div[class*="modal-mask"]'
                );
                overlays.forEach(el => el.remove());
                document.querySelectorAll('button').forEach(btn => {
                    const txt = (btn.innerText || '').trim().toLowerCase();
                    if (['got it', 'okay', 'dismiss', 'close', 'not now', 'mengerti'].includes(txt)) {
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
    Sets the description of the video
    """
    if description is None:
        return

    logger.debug(green("Setting description"))

    # Remove any characters outside the BMP range (emojis, etc) & Fix accents
    description = description.encode("utf-8", "ignore").decode("utf-8")
    saved_description = description

    try:
        _dismiss_walkthrough_overlays(page)

        desc_locator = page.locator(f"xpath={config.selectors.upload.description}")
        try:
            desc_locator.first.wait_for(
                state="attached",
                timeout=config.implicit_wait * 1000,
            )
            desc_locator.first.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass

        try:
            desc_locator.first.focus()
        except Exception:
            pass
        try:
            desc_locator.first.click(force=True, timeout=5000)
        except Exception:
            pass
        try:
            desc_locator.first.press("Control+A")
            desc_locator.first.press("Backspace")
        except Exception:
            pass
        try:
            desc_locator.first.click(force=True, timeout=3000)
        except Exception:
            pass
        time.sleep(0.5)

        # Fast text insertion + auto-link up to 3 tags
        all_words = description.split()
        tags = [w for w in all_words if w.startswith("#") or w.startswith("@")]

        main_text = description
        for t in tags:
            main_text = main_text.replace(t, "")

        page.keyboard.insert_text(main_text.strip())
        if tags:
            page.keyboard.insert_text("\n\n")
            slow_tags = tags[:3]
            fast_tags = tags[3:]
            for tag in slow_tags:
                page.keyboard.type(tag, delay=80)
                time.sleep(1.2)
                page.keyboard.press("Enter")
                page.keyboard.press("Space")
                time.sleep(0.3)
            if fast_tags:
                page.keyboard.insert_text(" ".join(fast_tags))
        time.sleep(0.5)

    except Exception as exception:
        logger.warning(f"Failed to set description: {exception}")
        # fallback
        try:
            _dismiss_walkthrough_overlays(page)
            desc_locator = page.locator(f"xpath={config.selectors.upload.description}").first
            desc_locator.scroll_into_view_if_needed(timeout=3000)
            desc_locator.click(force=True, timeout=3000)
            desc_locator.press("Control+A")
            desc_locator.press("Backspace")
            page.keyboard.insert_text(saved_description)
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
    Sets the video to upload
    """
    logger.debug(green("Uploading video file"))

    for _ in range(num_retries):
        try:
            upload_box = page.locator(f"xpath={config.selectors.upload.upload_video}")
            upload_box.set_input_files(path)

            # wait until a non-draggable image is found (process confirmation)
            process_confirmation = page.locator(
                f"xpath={config.selectors.upload.process_confirmation}"
            )
            process_confirmation.wait_for(
                state="attached", timeout=config.explicit_wait * 1000
            )
            return
        except PlaywrightTimeoutError as exception:
            print("TimeoutException occurred:\n", exception)
        except Exception as exception:
            print(exception)
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


def _fill_tiktok_schedule_inputs(page: Page, local_schedule: datetime.datetime) -> bool:
    try:
        date_str = local_schedule.strftime("%Y-%m-%d")
        time_str = local_schedule.strftime("%H:%M")
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
    try:
        month_names = [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december"
        ]
        target_year = local_schedule.year
        target_month = local_schedule.month
        target_day = local_schedule.day

        # 1. Date selection
        date_trigger = page.locator(
            '.DatePicker__trigger, '
            'div[role="combobox"][aria-label*="date" i], '
            '.schedule-picker [aria-label*="date" i]'
        ).first
        if date_trigger.is_visible(timeout=2500):
            date_trigger.click(force=True, timeout=3000)
            time.sleep(0.3)

            curr_year = None
            year_el = page.locator(".DatePicker__titleYear").first
            if year_el.is_visible(timeout=500):
                try:
                    curr_year = int(year_el.inner_text().strip())
                except Exception:
                    pass

            title_month_el = page.locator(".DatePicker__titleMonth").first
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
                            '.DatePicker__nav:has([data-icon="ArrowRight"])'
                        ).first
                        for _ in range(min(diff, 12)):
                            next_btn.click(force=True, timeout=2000)
                            time.sleep(0.15)
                    elif diff < 0:
                        prev_btn = page.locator(
                            'button[aria-label="Previous month"], '
                            '.DatePicker__nav:has([data-icon="ArrowLeft"])'
                        ).first
                        for _ in range(min(abs(diff), 12)):
                            prev_btn.click(force=True, timeout=2000)
                            time.sleep(0.15)

            day_btn = page.locator(
                'button.DatePicker__day:not([disabled]):not([data-outside="true"])'
            ).filter(has_text=f"{target_day}").first
            day_btn.click(force=True, timeout=3000)
            time.sleep(0.3)

        # 2. Time selection
        time_trigger = page.locator(
            'div[role="combobox"][aria-label*="time" i], '
            '.schedule-picker [aria-label*="time" i]'
        ).first
        if time_trigger.is_visible(timeout=2500):
            time_trigger.click(force=True, timeout=3000)
            time.sleep(0.3)

            target_hour = local_schedule.hour
            target_min = int(round(local_schedule.minute / 5.0) * 5) % 60

            hour_btn = page.locator(
                f'button[role="option"][aria-label="Hours {target_hour:02d}"]'
            ).first
            if hour_btn.is_visible(timeout=2000):
                hour_btn.scroll_into_view_if_needed()
                hour_btn.click(force=True, timeout=2000)

            min_btn = page.locator(
                f'button[role="option"][aria-label="Minutes {target_min:02d}"]'
            ).first
            if min_btn.is_visible(timeout=2000):
                min_btn.scroll_into_view_if_needed()
                min_btn.click(force=True, timeout=2000)

            time_trigger.click(force=True, timeout=2000)
            time.sleep(0.3)

        return True
    except Exception:
        return False


def _set_schedule_video(page: Page, schedule: datetime.datetime) -> None:
    """
    Sets the schedule of the video
    """
    logger.debug(green("Setting schedule"))
    _dismiss_walkthrough_overlays(page)

    timezone_str = page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    driver_timezone = pytz.timezone(timezone_str)

    schedule = schedule.astimezone(driver_timezone)

    # 1. Enable schedule toggle / radio in TikTok Studio
    try:
        schedule_inputs = page.locator(
            '[data-e2e="schedule_container"] input[name="postSchedule"][value="schedule"]'
        )
        if schedule_inputs.count() > 0:
            schedule_input = schedule_inputs.first
            try:
                schedule_enabled = schedule_input.is_checked()
            except Exception:
                schedule_enabled = schedule_input.get_attribute("aria-checked") == "true"
            if not schedule_enabled:
                schedule_input.locator("xpath=ancestor::label[1]").click(force=True, timeout=5000)

            allow_button = page.get_by_role("button", name="Allow", exact=True).first
            if allow_button.is_visible(timeout=1500):
                allow_button.click(force=True, timeout=3000)
                time.sleep(0.5)
        else:
            switch = page.locator(f"xpath={config.selectors.schedule.switch}").first
            if switch.is_visible(timeout=2000):
                switch.click(force=True)
                time.sleep(0.5)
    except Exception as exc:
        logger.debug(f"Could not prepare schedule switch: {exc}")

    # 2. Try native inputs
    if _fill_tiktok_schedule_inputs(page, schedule):
        logger.debug(green("Schedule set via native inputs"))
        return

    # 3. Try modern Studio custom pickers
    if _set_tiktok_studio_custom_picker(page, schedule):
        logger.debug(green("Schedule set via modern Studio custom pickers"))
        return

    # 4. Fallback to legacy calendar
    month = schedule.month
    day = schedule.day
    hour = schedule.hour
    minute = schedule.minute

    try:
        __date_picker(page, month, day)
        __time_picker(page, hour, minute)
    except Exception as e:
        msg = f"Failed to set schedule: {e}"
        logger.error(red(msg))
        raise FailedToUpload(msg)


def __date_picker(page: Page, month: int, day: int) -> None:
    logger.debug(green("Picking date"))

    date_picker = page.locator(f"xpath={config.selectors.schedule.date_picker}")
    date_picker.click()

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


def _post_video(page: Page) -> None:
    """
    Posts the video
    """
    logger.debug(green("Clicking the post button"))

    # Early check: If already redirected to content manager, post already succeeded
    page_url = str(getattr(page, "url", "") or "")
    if "/tiktokstudio/content" in page_url or "/creator-center/content" in page_url:
        logger.debug(green(f"Upload already confirmed via URL redirect: {page_url}"))
        return

    _dismiss_walkthrough_overlays(page)

    post_btn = page.locator(f"xpath={config.selectors.upload.post}").first
    try:
        def is_enabled():
            try:
                if post_btn.get_attribute("data-disabled") == "false":
                    return True
                return post_btn.is_enabled()
            except Exception:
                return False

        for _ in range(int(config.uploading_wait / 2)):
            if is_enabled():
                break
            time.sleep(2)

        _dismiss_walkthrough_overlays(page)
        post_btn.scroll_into_view_if_needed()
        try:
            post_btn.click(timeout=5000)
        except Exception:
            post_btn.click(force=True, timeout=5000)

    except Exception:
        logger.debug(green("Trying to click on the button again (fallback)"))
        try:
            page.evaluate('document.querySelector(".TUXButton--primary").click()')
        except Exception:
            pass

    time.sleep(2)

    # Handle "Content may be restricted" warning modal
    try:
        if page.locator("text=Content may be restricted").is_visible(timeout=2000) or page.locator("text=restricted").is_visible(timeout=1000):
            logger.debug(green("Warning modal detected, closing it to bypass..."))
            page.keyboard.press("Escape")
            time.sleep(1)
            
            page.evaluate('''
                document.querySelectorAll('div[role="dialog"] button').forEach(b => {
                    if(!b.innerText || b.innerText.trim() === '') b.click(); 
                });
            ''')
            time.sleep(1.5)
            
            logger.debug(green("Clicking Post button again after closing warning..."))
            try:
                post_btn.click(force=True, timeout=5000)
            except Exception:
                pass
            time.sleep(2)
    except Exception:
        pass

    try:
        post_now = page.locator(f"xpath={config.selectors.upload.post_now}").first
        if post_now.is_visible(timeout=3000):
            post_now.click(force=True)
    except Exception:
        pass

    # Wait for the successful post redirection or confirmation
    try:
        post_confirmation = page.locator(f"xpath={config.selectors.upload.post_confirmation}").first
        post_confirmation.wait_for(state="attached", timeout=config.explicit_wait * 1000)
    except Exception:
        # Fallback check: if we are redirected to /tiktokstudio/content, upload was successful
        cur_url = str(getattr(page, "url", "") or "")
        if "/tiktokstudio/content" in cur_url or "/creator-center/content" in cur_url:
            logger.debug(green("Redirected to content page, assuming success."))
        else:
            raise

    logger.debug(green("Video posted successfully"))


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

        # ------------------- NEW TIKTOK UI FLOW -------------------
        try:
            popup_handled = False
            # Check for possible "New editing features" onboarding popup
            got_it_btn = page.locator("button:has-text('Got it')").last
            if got_it_btn.is_visible(timeout=3000):
                got_it_btn.click()
                time.sleep(1)
                popup_handled = True

            # 1. Click "Edit cover" on the thumbnail
            new_edit_cover_btn = page.locator("div:has-text('Edit cover')").last
            if not popup_handled and not new_edit_cover_btn.is_visible(timeout=2000):
                raise Exception("New UI 'Edit cover' button not visible")
            
            new_edit_cover_btn.wait_for(state="visible", timeout=5000)
            new_edit_cover_btn.click()
            time.sleep(1.5)

            # 2. Wait for modal (Upload cover image label)
            upload_label = page.locator("label[aria-label='Upload cover image']").last
            upload_label.wait_for(state="visible", timeout=6000)

            # 3. Find file input & upload
            # Looks for standard image accept pattern
            file_input = page.locator("input[type='file'][accept*='image']").last
            file_input.set_input_files(cover_path)
            time.sleep(2.5)  # Wait for upload & preview to reflect

            # 4. Click Save
            # Use exact match to prevent matching "Save draft" button which never hides
            save_btn = page.locator("xpath=//button[.//div[text()='Save']] | //button[text()='Save']").first
            # Give it 2 seconds to appear just in case
            save_btn.wait_for(state="visible", timeout=2000)
            save_btn.click()

            # 5. Wait for modal to close
            save_btn.wait_for(state="hidden", timeout=12000)
            time.sleep(1.5)
            logger.debug(green(f"Successfully set cover (New UI)"))
            return  # Success, exit function

        except Exception as e_new:
            logger.debug(f"New UI cover flow failed, fallback to Old UI. Reason: {str(e_new)}")
            try:
                # Attempt to close the modal if stuck
                cancel_btn = page.locator("xpath=//button[.//div[text()='Cancel']] | //button[text()='Cancel']").first
                if cancel_btn.is_visible(timeout=1000):
                    cancel_btn.click()
                    time.sleep(1)
            except Exception:
                pass

        # ------------------- OLD TIKTOK UI FLOW -------------------
        preview_loc = page.locator(
            f"xpath={config.selectors.upload.cover.cover_preview}"
        ).first
        current_cover_src = preview_loc.get_attribute("src") if preview_loc.is_visible() else ""

        upload_tab = page.locator("text=Upload cover").last
        
        # Only click Edit Cover if the modal is not already open
        if not upload_tab.is_visible(timeout=2000):
            edit_cover_btn = page.locator(
                f"xpath={config.selectors.upload.cover.edit_cover_button}"
            ).first
            if edit_cover_btn.is_visible(timeout=2000):
                try:
                    edit_cover_btn.click(force=True)
                except Exception:
                    pass
                time.sleep(1)

        # 1. Click 'Upload cover' tab
        upload_tab = page.locator("text=Upload cover").last
        if upload_tab.is_visible(timeout=3000):
            upload_tab.click()
            time.sleep(1)

        # 2. Upload file
        upload_box = page.locator("input[type='file'][accept*='image']").last
        upload_box.set_input_files(cover_path)
        time.sleep(2)

        # 3. Click Confirm
        confirm_btn = page.locator("button:has-text('Confirm')").last
        if confirm_btn.is_visible(timeout=3000):
            confirm_btn.click()
            time.sleep(2)

        def check_src_change():
            if not preview_loc.is_visible():
                return True
            return preview_loc.get_attribute("src") != current_cover_src

        for _ in range(20):
            if check_src_change():
                break
            time.sleep(0.5)
        
        logger.debug(green(f"Successfully set cover (Old UI)"))

    except Exception as e:
        logger.error(red(f"Error setting cover: {e}"))
        try:
            exit_icon = page.locator(
                f"xpath={config.selectors.upload.cover.exit_cover_container}"
            )
            if exit_icon.is_visible():
                exit_icon.click()
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
    datetime_utc_now = pytz.UTC.localize(datetime.datetime.utcnow())
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
