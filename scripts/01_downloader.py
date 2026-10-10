"""
DerbyEdge Engine — Equibase Chart Downloader v3 (Selenium)
Drives real Chrome to bypass Imperva bot protection.
Run: python 01_downloader.py
"""

import os
import time
import shutil
import logging
from datetime import date, timedelta
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
from tqdm import tqdm

# ── CONFIG ───────────────────────────────────────────────────────────────────

BASE_DIR   = Path(r"C:\Projects\derbyedge-engine\data\raw\historical_results")
DOWNLOAD_TMP = Path(r"C:\Projects\derbyedge-engine\data\raw\_tmp_downloads")
START_DATE = date(2024, 1, 1)
END_DATE   = date(2026, 4, 28)

TRACKS = {
    "CD":  [1, 2, 3, 4, 5, 10, 11, 12],
    "GP":  [1, 2, 3, 4, 11, 12],
    "FG":  [1, 2, 3, 4, 11, 12],
    "SA":  [1, 2, 3, 4, 5, 10, 11, 12],
    "OP":  [1, 2, 3, 4, 5],
    "KEE": [4, 5, 10],
}

SLEEP_AFTER_CLICK  = 4    # seconds to wait for PDF to download
SLEEP_BETWEEN_DAYS = 2    # pause between days
PAGE_LOAD_TIMEOUT  = 20

# ── LOGGING ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("downloader.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── HELPERS ───────────────────────────────────────────────────────────────────

def chart_index_url(track: str, race_date: date) -> str:
    dt = race_date.strftime("%m/%d/%Y")
    return (
        f"https://www.equibase.com/premium/eqbPDFChartPlusIndex.cfm"
        f"?tid={track}&dt={dt}&ctry=USA"
    )

def full_pdf_url(track: str, race_date: date) -> str:
    """Direct URL to the 'All races' full card PDF viewer."""
    dt = race_date.strftime("%m/%d/%Y")
    return (
        f"https://www.equibase.com/premium/chartEmb.cfm"
        f"?track={track}&raceDate={dt}&cy=USA&rn=All"
    )

def dest_path(track: str, race_date: date) -> Path:
    d = BASE_DIR / str(race_date.year) / f"{race_date.month:02d}" / f"{race_date.day:02d}"
    return d / f"eqb_{track}_{race_date.isoformat()}_fullcard.pdf"

def wait_for_download(tmp_dir: Path, timeout: int = 30) -> Path | None:
    """Wait for a .pdf file to appear in tmp_dir (not .crdownload)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pdfs = [f for f in tmp_dir.iterdir()
                if f.suffix == ".pdf" and not f.name.endswith(".crdownload")]
        if pdfs:
            return pdfs[0]
        time.sleep(0.5)
    return None

def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)

# ── DRIVER SETUP ─────────────────────────────────────────────────────────────

def make_driver() -> webdriver.Chrome:
    DOWNLOAD_TMP.mkdir(parents=True, exist_ok=True)

    opts = Options()
    # Do NOT use headless — Imperva detects headless Chrome
    opts.add_argument("--start-minimized")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)

    # Auto-download PDFs to our tmp folder instead of opening them
    opts.add_experimental_option("prefs", {
        "download.default_directory": str(DOWNLOAD_TMP),
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,   # key: don't open in viewer, download
        "safebrowsing.enabled": True,
    })

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)

    # Mask webdriver flag (anti-bot)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver

# ── CORE DOWNLOAD ─────────────────────────────────────────────────────────────

def has_racing(driver: webdriver.Chrome, track: str, race_date: date) -> bool:
    """Check the index page to see if racing occurred."""
    try:
        driver.get(chart_index_url(track, race_date))
        time.sleep(1.5)
        return "Race 1" in driver.page_source
    except Exception:
        return False

def download_full_card(driver: webdriver.Chrome, track: str, race_date: date) -> bool:
    """
    Navigate to the full card PDF page, wait for download, move to dest.
    Returns True on success.
    """
    dest = dest_path(track, race_date)
    if dest.exists():
        log.info(f"  Already downloaded: {dest.name}")
        return True

    # Clear tmp dir before download
    for f in DOWNLOAD_TMP.iterdir():
        f.unlink(missing_ok=True)

    try:
        # Go to the chart index and click "View the Full Card Here"
        driver.get(chart_index_url(track, race_date))
        time.sleep(2)

        # Find and click the full card link
        try:
            full_card_link = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable(
                    (By.PARTIAL_LINK_TEXT, "View the Full Card")
                )
            )
            full_card_link.click()
            time.sleep(2)
        except Exception:
            pass  # link may not exist, fall through to direct URL

        # Navigate to the "VIEW FULL PDF" button page
        driver.get(full_pdf_url(track, race_date))
        time.sleep(2)

        try:
            view_pdf_btn = WebDriverWait(driver, 8).until(
                EC.element_to_be_clickable(
                    (By.XPATH, "//a[contains(text(),'VIEW FULL PDF') or contains(text(),'View Full PDF')]")
                )
            )
            view_pdf_btn.click()
        except Exception:
            # If button not found, try the direct static PDF URL
            mmddyy = race_date.strftime("%m%d%y")
            static_url = f"https://www.equibase.com/static/chart/pdf/{track}{mmddyy}USA.pdf"
            driver.get(static_url)

        # Wait for PDF download to complete
        pdf_file = wait_for_download(DOWNLOAD_TMP, timeout=30)
        if not pdf_file:
            log.warning(f"  No PDF downloaded for {track} {race_date}")
            return False

        # Move to correct destination
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(pdf_file), str(dest))
        log.info(f"  Saved: {dest.name} ({dest.stat().st_size:,} bytes)")
        return True

    except Exception as e:
        log.error(f"  Error {track} {race_date}: {e}")
        return False

# ── MAIN ─────────────────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info(f"DerbyEdge Selenium Downloader  |  {START_DATE} → {END_DATE}")
    log.info(f"Tracks: {', '.join(TRACKS.keys())}")
    log.info("=" * 60)

    driver = make_driver()
    log.info("Chrome launched. Navigating to Equibase to warm up session...")
    driver.get("https://www.equibase.com")
    time.sleep(3)

    total_saved = 0
    total_skipped = 0

    all_dates = list(daterange(START_DATE, END_DATE))

    try:
        for track, active_months in TRACKS.items():
            log.info(f"\n── {track} ──────────────────────────────")
            eligible = [d for d in all_dates if d.month in active_months]
            log.info(f"  Eligible days to check: {len(eligible)}")

            for race_date in tqdm(eligible, desc=track, unit="day"):
                if not has_racing(driver, track, race_date):
                    total_skipped += 1
                    continue

                success = download_full_card(driver, track, race_date)
                if success:
                    total_saved += 1

                time.sleep(SLEEP_BETWEEN_DAYS)

    finally:
        driver.quit()
        log.info("\n" + "=" * 60)
        log.info(f"DONE. Saved: {total_saved}  |  No racing: {total_skipped}")
        log.info("=" * 60)

if __name__ == "__main__":
    run()