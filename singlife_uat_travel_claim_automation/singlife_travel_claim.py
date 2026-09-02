#!/usr/bin/env python3
"""
Singlife Travel Claim — Merimen UAT Client Portal automation.

Fills out and submits the "Travel Claim" flow for Singlife General Insurance
on the Merimen UAT Client Portal, using Playwright, for fast/repeatable
QA/regression testing with dummy data.

>>> THIS SCRIPT IS SCOPED TO UAT ONLY <<<
It auto-confirms the final submission dialog without prompting, matching the
team's standing approval for this specific UAT/dummy-data testing workflow.
The BASE_URL below is hard-pinned to the UAT host. Do NOT repoint this
script at a production claims portal — if you ever change BASE_URL to
anything other than the UAT host, remove the auto-confirm behaviour and
require an explicit human confirmation before submitting.

Usage:
    python singlife_travel_claim.py --case medical
    python singlife_travel_claim.py --case flight_delay
    python singlife_travel_claim.py --case baggage
    python singlife_travel_claim.py --case medical --headed --slow-mo 150
    python singlife_travel_claim.py --case medical --no-submit   # fill but stop before Confirm

Requires: pip install playwright && playwright install chromium
(In this sandboxed environment, Chromium is already available; see
PLAYWRIGHT_BROWSERS_PATH / PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD if set.)
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright

BASE_URL = "https://clientportaluat.merimen.com/public/client/clp/clpdashboard?ins_code=SG_SINGLIFE"

DEFAULT_TIMEOUT_MS = 15_000


# --------------------------------------------------------------------------
# Dummy PDF generation (self-contained — no external file dependencies)
# --------------------------------------------------------------------------

_MINIMAL_PDF_TEMPLATE = b"""%PDF-1.4
1 0 obj
<< /Type /Catalog /Pages 2 0 R >>
endobj
2 0 obj
<< /Type /Pages /Kids [3 0 R] /Count 1 >>
endobj
3 0 obj
<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>
endobj
4 0 obj
<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>
endobj
5 0 obj
<< /Length 70 >>
stream
BT /F1 12 Tf 20 100 Td (%(label)s - UAT dummy document) Tj ET
endstream
endobj
xref
0 6
0000000000 65535 f
trailer
<< /Size 6 /Root 1 0 R >>
startxref
0
%%%%EOF
"""


def make_dummy_pdf(directory: Path, label: str) -> Path:
    """Write a minimal, valid, throwaway PDF for UAT upload slots.

    The portal only validates that *a* file was provided for each required
    slot, not its content, so a tiny hand-built PDF is sufficient.
    """
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() else "_" for c in label)[:40]
    path = directory / f"dummy_{safe_label}.pdf"
    content = _MINIMAL_PDF_TEMPLATE % {b"%(label)s": label.encode("ascii", "replace")}
    # The template above uses %-formatting awkwardly with bytes; do it simply instead.
    content = _MINIMAL_PDF_TEMPLATE.replace(b"%(label)s", label.encode("ascii", "replace"))
    path.write_bytes(content)
    return path


# --------------------------------------------------------------------------
# Low-level field helpers
# --------------------------------------------------------------------------


def by_id(page: Page, element_id: str):
    """Locate an element by exact id, safely handling ids that contain dots
    (schema-style ids like "basic_details.policy_no" are NOT valid CSS
    id-selectors when used with '#', since '.' is a class-combinator)."""
    return page.locator(f'[id="{element_id}"]')


def fill_text(page: Page, element_id: str, value: str) -> None:
    loc = by_id(page, element_id)
    loc.click()
    loc.fill(value)


def click_radio(page: Page, field_key: str, yes: bool) -> None:
    """Click a Yes/No (or opt_1/opt_0) radio pair.

    Confirmed convention on this portal: opt_1 == Yes, opt_0 == No.
    """
    opt = "opt_1" if yes else "opt_0"
    by_id(page, f"{field_key}_{opt}").click()


def click_radio_index(page: Page, field_key: str, index: int) -> None:
    """Click a radio option by its opt_<index> suffix (for non Yes/No radios
    such as insured_type opt_0=Individual / opt_1=Company)."""
    by_id(page, f"{field_key}_opt_{index}").click()


def fill_date_field(page: Page, real_field_id: str, day: str, month: str, year: str,
                     index: int = 0, hour: Optional[str] = None, minute: Optional[str] = None,
                     meridiem: Optional[str] = None) -> None:
    """Fill an MUI X DatePicker / DateTimePicker field.

    The element bearing `real_field_id` is a hidden (aria-hidden) backing
    input — the actual interactive controls are role="spinbutton" children
    (aria-label Day/Month/Year[/Hours/Minutes/Meridiem]) inside the nearest
    ancestor container. Click the Day spinbutton directly (role-based, not
    coordinate-based) and type digits — MUI auto-advances between segments.

    `index` disambiguates fields that hold TWO date triplets sharing the
    same aria-labels in one container (e.g. Travel Period From/To): pass
    index=0 for the first (From) and index=1 for the second (To).
    """
    hidden_input = by_id(page, real_field_id)
    container = hidden_input.locator(
        'xpath=ancestor::*[.//*[@role="spinbutton"]][1]'
    )
    day_spin = container.locator('[role="spinbutton"][aria-label="Day"]').nth(index)
    day_spin.click()
    page.keyboard.type(f"{day.zfill(2)}{month.zfill(2)}{year}")
    if hour is not None and minute is not None:
        # For combined date+time fields, the time inputs sit in the same
        # container as a second, independent spinbutton group.
        hour_spin = container.locator('[role="spinbutton"][aria-label="Hours"]').nth(index)
        hour_spin.click()
        page.keyboard.type(f"{hour.zfill(2)}{minute.zfill(2)}")
        if meridiem:
            page.keyboard.press(meridiem[0].lower())


def select_autocomplete(page: Page, field_id: str, type_text: str, option_text: Optional[str] = None) -> None:
    """Fill an MUI Autocomplete combobox: click, type to filter, click the
    matching option from the popup listbox."""
    option_text = option_text or type_text
    loc = by_id(page, field_id)
    loc.click()
    loc.fill("")
    page.keyboard.type(type_text)
    page.get_by_role("option", name=option_text, exact=False).first.click()


def select_dropdown_option(page: Page, field_id: str, option_text: str) -> None:
    """Fill a short-list MUI Autocomplete/select where all options are shown
    on click without needing to type (e.g. Gender, Marital Status)."""
    loc = by_id(page, field_id)
    loc.click()
    page.get_by_role("option", name=option_text, exact=True).first.click()


def select_claim_type(page: Page, claim_type_field_id: str, labels: list[str]) -> None:
    """Open the Claim Type multi-select modal for a Claim Category and check
    the given option label(s), then close it.

    Confirmed live: EVERY category's Claim Type field opens this same
    multi-select modal (title "Claim Type", a "Select All" link, a grid of
    checkboxes, and Clear All / Close buttons) — not just Loss or Damage of
    Property as earlier documentation assumed.
    """
    by_id(page, claim_type_field_id).click()
    dialog = page.get_by_role("dialog") if page.get_by_role("dialog").count() else page
    for label in labels:
        page.get_by_text(label, exact=True).click()
    page.get_by_role("button", name="Close").click()


def check_category(page: Page, category_key: str) -> None:
    """Check a Claim Category card checkbox.

    Confirmed id convention: ClpDashboardSchema_<category_key>.is_<category_key>
    """
    by_id(page, f"ClpDashboardSchema_{category_key}.is_{category_key}").check()


# --------------------------------------------------------------------------
# Wizard step functions
# --------------------------------------------------------------------------


def goto_and_start_claim(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_selector('[id="ClpDashboardSchema_clm_service_opt_NC"]', timeout=DEFAULT_TIMEOUT_MS)

    # "Select Service" -> Make a new claim
    by_id(page, "ClpDashboardSchema_clm_service_opt_NC").click()

    # "Select Claim Type" -> Travel Claim (MUI Autocomplete)
    select_autocomplete(page, "ClpDashboardSchema_clm_type", "Travel", "Travel Claim")

    page.get_by_role("button", name="Next", exact=True).first.click()

    # One-time intro screen ("Here are some quick reminders before you
    # start") only appears on a fresh session — click through if present.
    try:
        get_started = page.get_by_role("button", name="Get started")
        get_started.wait_for(state="visible", timeout=4000)
        get_started.click()
    except PWTimeoutError:
        pass

    page.wait_for_selector('text=Basic Details', timeout=DEFAULT_TIMEOUT_MS)


def fill_basic_details(page: Page, *, policy_no: str, accident_date: tuple[str, str, str],
                        travel_from: tuple[str, str, str], travel_to: tuple[str, str, str],
                        contact_name: str, mobile_number: str) -> None:
    fill_text(page, "ClpDashboardSchema_basic_details.policy_no", policy_no)

    fill_date_field(page, "ClpDashboardSchema_basic_details.accident_date", *accident_date)
    fill_date_field(page, "ClpDashboardSchema_basic_details.travel_period", *travel_from, index=0)
    fill_date_field(page, "ClpDashboardSchema_basic_details.travel_period", *travel_to, index=1)

    fill_text(page, "ClpDashboardSchema_basic_details.name", contact_name)

    phone = by_id(page, "ClpDashboardSchema_basic_details.mobile_number")
    phone.click()
    page.keyboard.type(mobile_number)

    page.get_by_role("button", name="Next", exact=True).click()


def fill_insured_and_claimant(page: Page, *, surname: str, given_name: str, id_number: str,
                               nationality: str, dob: tuple[str, str, str], gender: str,
                               marital_status: str, email: str, block_street_no: str,
                               street_name: str, postal_code: str, country: str) -> None:
    page.wait_for_selector("text=Insured Details", timeout=DEFAULT_TIMEOUT_MS)

    click_radio_index(page, "ClpDashboardSchema_insured_type", 0)  # Individual

    fill_text(page, "ClpDashboardSchema_insured_details.surname", surname)
    fill_text(page, "ClpDashboardSchema_insured_details.givenname", given_name)

    select_autocomplete(page, "ClpDashboardSchema_insured_details.id_no_type", "Passport", "Passport No")
    fill_text(page, "ClpDashboardSchema_insured_details.id_no", id_number)

    click_radio(page, "ClpDashboardSchema_insured_details.singlife_staff", yes=False)

    select_autocomplete(page, "ClpDashboardSchema_insured_details.nationality", nationality)
    fill_date_field(page, "ClpDashboardSchema_insured_details.birthdate", *dob)
    select_dropdown_option(page, "ClpDashboardSchema_insured_details.gender", gender)
    select_dropdown_option(page, "ClpDashboardSchema_insured_details.marital", marital_status)

    fill_text(page, "ClpDashboardSchema_insured_details.email_address", email)
    fill_text(page, "ClpDashboardSchema_insured_details.address1", block_street_no)
    fill_text(page, "ClpDashboardSchema_insured_details.address2", street_name)
    fill_text(page, "ClpDashboardSchema_insured_details.postcode", postal_code)
    select_autocomplete(page, "ClpDashboardSchema_insured_details.country", country)

    click_radio_index(page, "ClpDashboardSchema_claimant_type", 0)  # Same as Insured

    page.get_by_role("button", name="Next", exact=True).click()


def fill_claim_details_common(page: Page, *, place: str, country: str, description: str) -> None:
    page.wait_for_selector("text=Claim Categories", timeout=DEFAULT_TIMEOUT_MS)

    fill_text(page, "ClpDashboardSchema_loss_details.take_place", place)
    select_autocomplete(page, "ClpDashboardSchema_loss_details.country", country)

    desc = by_id(page, "ClpDashboardSchema_loss_details.detailed")
    desc.click()
    desc.fill(description)

    click_radio(page, "ClpDashboardSchema_loss_details.covered", yes=False)


def fill_medical_related(page: Page, *, consultation_date: tuple[str, str, str],
                          claim_amount: str, injury_illness: str) -> None:
    check_category(page, "medical_related")
    select_claim_type(page, "ClpDashboardSchema_medical_related.claim_type", ["Medical Expenses"])

    fill_date_field(page, "ClpDashboardSchema_medical_related.first_consultation_date", *consultation_date)
    fill_text(page, "ClpDashboardSchema_medical_related.estimated_claim_amount", claim_amount)
    fill_text(page, "ClpDashboardSchema_medical_related.injury_illness", injury_illness)

    click_radio(page, "ClpDashboardSchema_medical_related.covid", yes=False)
    click_radio(page, "ClpDashboardSchema_medical_related.tcm", yes=False)
    click_radio(page, "ClpDashboardSchema_medical_related.oversea_assistance", yes=False)
    click_radio(page, "ClpDashboardSchema_medical_related.disability", yes=False)
    click_radio(page, "ClpDashboardSchema_medical_related.mugging", yes=False)
    click_radio(page, "ClpDashboardSchema_medical_related.suffer_before", yes=False)


def fill_travel_inconvenience(page: Page, *, flight_number: str,
                               scheduled: tuple[str, str, str, str, str, str],
                               actual: tuple[str, str, str, str, str, str],
                               cause: str, claim_amount: Optional[str] = None) -> None:
    """`scheduled` / `actual` are (day, month, year, hour, minute, meridiem)."""
    check_category(page, "travel_inconvenience")
    select_claim_type(page, "ClpDashboardSchema_travel_inconvenience.claim_type", ["Delayed Departure"])

    if claim_amount:
        fill_text(page, "ClpDashboardSchema_travel_inconvenience.estimated_claim_amount", claim_amount)

    fill_text(page, "ClpDashboardSchema_travel_inconvenience.flight_number", flight_number)

    sd, sm, sy, sh, smin, sap = scheduled
    fill_date_field(
        page, "ClpDashboardSchema_travel_inconvenience.scheduled_flight_arrival_datetime",
        sd, sm, sy, hour=sh, minute=smin, meridiem=sap,
    )
    ad, am, ay, ah, amin, aap = actual
    fill_date_field(
        page, "ClpDashboardSchema_travel_inconvenience.actual_flight_arrival_datetime",
        ad, am, ay, hour=ah, minute=amin, meridiem=aap,
    )

    fill_text(page, "ClpDashboardSchema_travel_inconvenience.cause", cause)


@dataclass
class PropertyItem:
    description: str
    purchase_date: tuple[str, str, str]
    has_receipt: bool
    claim_amount: str
    reported_to_authorities: bool
    compensation_amount: Optional[str] = None


def fill_property_damage(page: Page, items: list[PropertyItem],
                          claim_type_labels: list[str] = None) -> None:
    check_category(page, "property_damage")
    select_claim_type(
        page, "ClpDashboardSchema_property_damage.claim_type",
        claim_type_labels or ["Loss or Damage of Baggage"],
    )

    add_item_button = page.get_by_role("button", name="Add Item")
    for idx, item in enumerate(items):
        if idx > 0:
            add_item_button.click()

        prefix = f"property_damage.property_damage.{idx}"
        fill_text(page, f"ClpDashboardSchema_{prefix}.item_description", item.description)
        fill_date_field(page, f"ClpDashboardSchema_{prefix}.purchase_date", *item.purchase_date)
        click_radio(page, f"ClpDashboardSchema_{prefix}.receipt", yes=item.has_receipt)
        fill_text(page, f"ClpDashboardSchema_{prefix}.estimated_claim_amount", item.claim_amount)
        click_radio(page, f"ClpDashboardSchema_{prefix}.reported", yes=item.reported_to_authorities)
        if item.compensation_amount:
            fill_text(page, f"ClpDashboardSchema_{prefix}.compensation_amount", item.compensation_amount)


def go_next_from_claim_details(page: Page) -> None:
    page.get_by_role("button", name="Next", exact=True).last.click()


def upload_supporting_documents(page: Page, dummy_pdf_paths: list[Path]) -> None:
    """Upload a dummy PDF to every required (and, if present, optional)
    dropzone on the Supporting Documents step.

    File inputs here are Uppy-generated with random per-session names/ids,
    so they're targeted by DOM order rather than a stable id. Playwright's
    set_input_files() works directly on the hidden <input type="file">
    without needing it to be visible or clicked through the styled dropzone.
    """
    page.wait_for_selector("text=Upload Required Documents", timeout=DEFAULT_TIMEOUT_MS)
    file_inputs = page.locator('input[type="file"]')
    count = file_inputs.count()
    if count == 0:
        raise RuntimeError("No file upload inputs found on Supporting Documents step")

    for i in range(count):
        pdf = dummy_pdf_paths[i % len(dummy_pdf_paths)]
        file_inputs.nth(i).set_input_files(str(pdf))
        # Give the Uppy widget a brief moment to register + show the
        # "Files uploaded" state before moving to the next slot.
        page.wait_for_timeout(400)

    page.get_by_role("button", name="Next", exact=True).last.click()


def complete_declaration_and_submit(page: Page, *, auto_submit: bool = True) -> None:
    """Complete the Declaration step: open Review Declaration, scroll its
    text to the bottom to enable "I agree", accept, then Next -> Confirm.

    auto_submit=True clicks "Confirm" on the final "Proceed to Submit?"
    dialog automatically — this mirrors the team's standing approval for
    this UAT/dummy-data testing workflow specifically. NEVER set this True
    against anything other than the pinned UAT BASE_URL above.
    """
    page.wait_for_selector("text=Review Declaration", timeout=DEFAULT_TIMEOUT_MS)
    page.get_by_text("Review Declaration", exact=True).click()

    # Scroll the modal's declaration text box to the bottom to enable
    # "I agree" (it's disabled until the user has seen the full text).
    # Find the dialog's most-scrollable inner element and scroll it to end.
    dialog = page.get_by_role("dialog")
    dialog.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    try:
        dialog.evaluate(
            """(dialogEl) => {
                const candidates = Array.from(dialogEl.querySelectorAll('div'));
                let best = null, bestScrollable = 0;
                for (const el of candidates) {
                    const scrollable = el.scrollHeight - el.clientHeight;
                    if (scrollable > bestScrollable) { bestScrollable = scrollable; best = el; }
                }
                if (best) best.scrollTop = best.scrollHeight;
            }"""
        )
    except Exception:
        pass
    page.wait_for_timeout(500)

    agree_btn = page.get_by_role("button", name="I agree")
    agree_btn.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)
    # As a safety net if the JS scroll above didn't fully enable it, try
    # scrolling again via mouse wheel over the dialog before clicking.
    for _ in range(5):
        if agree_btn.is_enabled():
            break
        dialog.hover()
        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(300)
    agree_btn.click()

    page.get_by_role("button", name="Next", exact=True).last.click()

    confirm_dialog_text = page.get_by_text("Proceed to Submit?")
    confirm_dialog_text.wait_for(state="visible", timeout=DEFAULT_TIMEOUT_MS)

    if not auto_submit:
        print("--no-submit set: stopping before Confirm. Dialog is open for manual review.")
        return

    page.get_by_role("button", name="Confirm", exact=True).click()

    # Submission can take up to ~10s ("Submitting..." spinner).
    page.wait_for_selector("text=Thank you for your claim", timeout=30_000)
    print("Claim submitted successfully.")


# --------------------------------------------------------------------------
# Test case definitions
# --------------------------------------------------------------------------


def run_medical_case(page: Page, pdf_dir: Path, *, policy_suffix: str, auto_submit: bool) -> None:
    policy = f"MEDICAL250{policy_suffix}"
    goto_and_start_claim(page)
    fill_basic_details(
        page,
        policy_no=policy,
        accident_date=("01", "01", "2026"),
        travel_from=("15", "01", "2026"),
        travel_to=("20", "01", "2026"),
        contact_name="Script Test",
        mobile_number="91234567",
    )
    fill_insured_and_claimant(
        page,
        surname="Test", given_name="Script", id_number="S1234567A",
        nationality="Singapore", dob=("01", "01", "1990"), gender="Male",
        marital_status="Single", email="test@test.com",
        block_street_no="123", street_name="Test Street",
        postal_code="123456", country="Singapore",
    )
    fill_claim_details_common(
        page, place="Singapore Changi Airport", country="Singapore",
        description="Fell ill with flu during travel and required medical consultation.",
    )
    fill_medical_related(
        page, consultation_date=("01", "01", "2026"), claim_amount="250.00", injury_illness="Flu",
    )
    go_next_from_claim_details(page)

    pdfs = [
        make_dummy_pdf(pdf_dir, "flight_itinerary"),
        make_dummy_pdf(pdf_dir, "claimant_nric"),
        make_dummy_pdf(pdf_dir, "original_receipts"),
        make_dummy_pdf(pdf_dir, "medical_bills"),
    ]
    upload_supporting_documents(page, pdfs)
    complete_declaration_and_submit(page, auto_submit=auto_submit)


def run_flight_delay_case(page: Page, pdf_dir: Path, *, policy_suffix: str, auto_submit: bool) -> None:
    policy = f"AV222DELAY{policy_suffix}"
    goto_and_start_claim(page)
    fill_basic_details(
        page,
        policy_no=policy,
        accident_date=("24", "02", "2026"),
        travel_from=("24", "02", "2026"),
        travel_to=("28", "02", "2026"),
        contact_name="Script Test",
        mobile_number="91234567",
    )
    fill_insured_and_claimant(
        page,
        surname="Test", given_name="Script", id_number="S1234567A",
        nationality="Singapore", dob=("01", "01", "1990"), gender="Male",
        marital_status="Single", email="test@test.com",
        block_street_no="123", street_name="Test Street",
        postal_code="123456", country="Singapore",
    )
    fill_claim_details_common(
        page, place="El Dorado International Airport", country="Colombia",
        description="Flight AV222 was delayed, causing significant travel inconvenience.",
    )
    fill_travel_inconvenience(
        page,
        flight_number="AV222",
        scheduled=("24", "02", "2026", "10", "30", "PM"),
        actual=("25", "02", "2026", "02", "15", "AM"),
        cause="Technical/mechanical delay reported by airline.",
    )
    go_next_from_claim_details(page)

    pdfs = [
        make_dummy_pdf(pdf_dir, "flight_itinerary"),
        make_dummy_pdf(pdf_dir, "claimant_nric"),
        make_dummy_pdf(pdf_dir, "airline_delay_confirmation"),
    ]
    upload_supporting_documents(page, pdfs)
    complete_declaration_and_submit(page, auto_submit=auto_submit)


def run_baggage_case(page: Page, pdf_dir: Path, *, policy_suffix: str, auto_submit: bool) -> None:
    policy = f"BAGGAGE150{policy_suffix}"
    goto_and_start_claim(page)
    fill_basic_details(
        page,
        policy_no=policy,
        accident_date=("01", "01", "2026"),
        travel_from=("15", "01", "2026"),
        travel_to=("20", "01", "2026"),
        contact_name="Script Test",
        mobile_number="91234567",
    )
    fill_insured_and_claimant(
        page,
        surname="Test", given_name="Script", id_number="S1234567A",
        nationality="Singapore", dob=("01", "01", "1990"), gender="Male",
        marital_status="Single", email="test@test.com",
        block_street_no="123", street_name="Test Street",
        postal_code="123456", country="Singapore",
    )
    fill_claim_details_common(
        page, place="Singapore Changi Airport", country="Singapore",
        description="Checked-in baggage was damaged and items inside were lost or damaged during travel.",
    )
    items = [
        PropertyItem(
            description="Samsonite", purchase_date=("01", "01", "2025"), has_receipt=True,
            claim_amount="75.00", reported_to_authorities=False,
        ),
        PropertyItem(
            description="Gucci", purchase_date=("11", "01", "2025"), has_receipt=True,
            claim_amount="75.00", reported_to_authorities=True,
        ),
    ]
    fill_property_damage(page, items, claim_type_labels=["Loss or Damage of Baggage"])
    go_next_from_claim_details(page)

    pdfs = [
        make_dummy_pdf(pdf_dir, "flight_itinerary"),
        make_dummy_pdf(pdf_dir, "claimant_nric"),
        make_dummy_pdf(pdf_dir, "original_receipts"),
        make_dummy_pdf(pdf_dir, "baggage_damage_report"),
        make_dummy_pdf(pdf_dir, "photos_of_damage"),
    ]
    upload_supporting_documents(page, pdfs)
    complete_declaration_and_submit(page, auto_submit=auto_submit)


CASES = {
    "medical": run_medical_case,
    "flight_delay": run_flight_delay_case,
    "baggage": run_baggage_case,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--case", choices=sorted(CASES), required=True, help="Which UAT test case to run")
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument("--slow-mo", type=int, default=0, help="Slow down each Playwright action by N ms")
    parser.add_argument("--no-submit", action="store_true", help="Fill the whole form but stop before clicking Confirm")
    parser.add_argument("--policy-suffix", default="", help="Suffix appended to the dummy policy number, e.g. to make repeat runs distinguishable")
    parser.add_argument("--pdf-dir", default=None, help="Directory to write dummy upload PDFs into (default: a temp dir)")
    args = parser.parse_args()

    if "clientportaluat.merimen.com" not in BASE_URL:
        print("Refusing to run: BASE_URL is not the UAT host.", file=sys.stderr)
        return 2

    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else Path(tempfile.mkdtemp(prefix="singlife_uat_docs_"))

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, slow_mo=args.slow_mo)
        page = browser.new_page()
        page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        try:
            run_fn = CASES[args.case]
            run_fn(page, pdf_dir, policy_suffix=args.policy_suffix, auto_submit=not args.no_submit)
        except Exception:
            screenshot_path = Path(tempfile.gettempdir()) / f"singlife_uat_failure_{int(time.time())}.png"
            try:
                page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"Failure screenshot saved to {screenshot_path}", file=sys.stderr)
            except Exception:
                pass
            raise
        finally:
            if args.no_submit and args.headed:
                print("Leaving browser open for inspection (--no-submit --headed). Press Enter to close.")
                try:
                    input()
                except EOFError:
                    pass
            browser.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
