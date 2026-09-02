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


def _build_minimal_pdf_bytes(label: str) -> bytes:
    """Build a minimal but genuinely valid single-page PDF from scratch,
    with correctly-computed object byte offsets in its xref table.

    (An earlier version of this used a fixed-offset template string with a
    naive text substitution for the label -- that's fragile by
    construction, since a label of different length shifts every byte
    offset after it, and it also silently carried a dead line that tried
    to '%'-format the whole template, which choked on the literal
    "%PDF-1.4" header. This version computes each object's real offset as
    it's written, and has been verified to parse cleanly with a strict PDF
    reader (pypdf) rather than merely eyeballed.)
    """
    text = f"{label} - UAT dummy document".encode("ascii", "replace")
    stream_content = b"BT /F1 12 Tf 20 100 Td (" + text + b") Tj ET"

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream_content)).encode() + b" >>\nstream\n"
        + stream_content + b"\nendstream",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]  # object 0 is the free-list head, unused
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_offset = len(out)
    object_count = len(objects) + 1
    out += f"xref\n0 {object_count}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {object_count} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_dummy_pdf(directory: Path, label: str) -> Path:
    """Write a minimal, valid, throwaway PDF for UAT upload slots.

    The portal only validates that *a* file was provided for each required
    slot, not its content, so a tiny hand-built PDF is sufficient.
    """
    directory.mkdir(parents=True, exist_ok=True)
    safe_label = "".join(c if c.isalnum() else "_" for c in label)[:40]
    path = directory / f"dummy_{safe_label}.pdf"
    path.write_bytes(_build_minimal_pdf_bytes(label))
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


def click_radio_by_id(page: Page, radio_id: str, attempts: int = 3) -> None:
    """Click a radio option by its exact id and VERIFY it actually ends up
    checked, retrying a few times if not.

    Observed live: a plain `.click()` on a radio can be issued successfully
    by Playwright (no exception) and still not end up checked — most likely
    a React re-render triggered by an adjacent field (e.g. the Insured ID
    Type autocomplete selected just beforehand) landing right as the click
    is processed, resetting that bit of form state. Left unchecked, this
    fails *silently* here and only surfaces much later as a confusing
    "<field> is required" downstream, or as the wizard refusing to advance
    with no obvious cause. Verify-and-retry converts that into either a
    self-healed click or a clear, immediate error at the actual point of
    failure.
    """
    radio = by_id(page, radio_id)
    for attempt in range(attempts):
        radio.click()
        page.wait_for_timeout(200)
        if radio.is_checked():
            return
    raise RuntimeError(
        f"Radio {radio_id!r} did not register as checked after {attempts} click attempts "
        "(the page likely re-rendered mid-click -- see click_radio_by_id docstring)"
    )


def click_radio(page: Page, field_key: str, yes: bool) -> None:
    """Click a Yes/No (or opt_1/opt_0) radio pair.

    Confirmed convention on this portal: opt_1 == Yes, opt_0 == No.
    """
    opt = "opt_1" if yes else "opt_0"
    click_radio_by_id(page, f"{field_key}_{opt}")


def click_radio_index(page: Page, field_key: str, index: int) -> None:
    """Click a radio option by its opt_<index> suffix (for non Yes/No radios
    such as insured_type opt_0=Individual / opt_1=Company)."""
    click_radio_by_id(page, f"{field_key}_opt_{index}")


def fill_date_field(page: Page, real_field_id: str, day: str, month: str, year: str,
                     index: int = 0, hour: Optional[str] = None, minute: Optional[str] = None,
                     meridiem: Optional[str] = None) -> None:
    """Fill an MUI X DatePicker / DateTimePicker field.

    The element bearing `real_field_id` is a hidden (aria-hidden) backing
    input — the actual interactive controls are role="spinbutton" children
    (aria-label Day/Month/Year[/Hours/Minutes/Meridiem]) inside the nearest
    ancestor container. Click the Day spinbutton directly (role-based, not
    coordinate-based) and type digits — MUI auto-advances between segments.

    IMPORTANT: pass the id of the specific real hidden input for the exact
    field you mean (e.g. Travel Period's "From" and "To" are two distinct
    ids: "...travel_period" and "...travel_period_to"). The ancestor lookup
    walks up to the NEAREST container that has any spinbutton descendant,
    which resolves to a group scoped to just that one field's own Day/
    Month/Year(/Hours/Minutes/Meridiem) triplet — not a wider container
    shared with a sibling field, even when two fields sit side by side
    visually. Confirmed by trial: reusing one field's id with `index=1` to
    reach a "second" triplet does NOT work — that ancestor only contains
    the one triplet, so nth(1) finds nothing and hangs until timeout. Leave
    `index` at its default (0) unless you've directly confirmed live that a
    single container genuinely holds more than one same-labelled triplet.
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


def click_visible_button(page: Page, name: str, exact: bool = True) -> None:
    """Click the currently-VISIBLE button matching `name`.

    Confirmed live: this wizard keeps more than one step's form mounted in
    the DOM at once (e.g. a "Next" button belonging to a later step was
    already present while Basic Details was the visible step), so plain
    `page.get_by_role("button", name=...).click()` can hit a Playwright
    strict-mode violation (multiple matches) — and picking `.first`/`.last`
    is a guess about DOM order, not a guarantee of which one is on-screen.
    This walks all matches and clicks the one that's actually visible.
    """
    locator = page.get_by_role("button", name=name, exact=exact)
    count = locator.count()
    for i in range(count):
        candidate = locator.nth(i)
        if candidate.is_visible():
            candidate.click()
            return
    # Fall back to Playwright's own wait/click if none looked visible yet
    # (e.g. it's still animating in) -- last one is the most likely match
    # based on every case observed while building this script.
    locator.last.click()


def select_autocomplete(page: Page, field_id: str, type_text: str, option_text: Optional[str] = None) -> None:
    """Fill an MUI Autocomplete combobox: click, type to filter, click the
    matching option from the popup listbox."""
    option_text = option_text or type_text
    loc = by_id(page, field_id)
    loc.click()
    loc.fill("")
    page.keyboard.type(type_text)
    page.get_by_role("option", name=option_text, exact=False).first.click()
    # Selecting a value here can mount/re-render fields elsewhere on the
    # page (e.g. picking an Insured ID Type just before the "Are you a
    # Singlife staff?" radio). Give that a moment to settle before the
    # caller's next action, to avoid racing a click against a re-render.
    page.wait_for_timeout(200)


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


def check_category(page: Page, category_key: str, attempts: int = 3) -> None:
    """Check a Claim Category card checkbox, verifying it registers.

    Confirmed id convention: ClpDashboardSchema_<category_key>.is_<category_key>

    Same verify-and-retry rationale as click_radio_by_id -- checking this
    box mounts a whole new block of category-specific fields below it, so
    it's exactly the kind of click that can race a re-render.
    """
    checkbox = by_id(page, f"ClpDashboardSchema_{category_key}.is_{category_key}")
    for attempt in range(attempts):
        checkbox.check()
        page.wait_for_timeout(200)
        if checkbox.is_checked():
            return
    raise RuntimeError(
        f"Category checkbox {category_key!r} did not register as checked after {attempts} attempts"
    )


# --------------------------------------------------------------------------
# Wizard step functions
# --------------------------------------------------------------------------


def goto_and_start_claim(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.wait_for_selector('[id="ClpDashboardSchema_clm_service_opt_NC"]', timeout=DEFAULT_TIMEOUT_MS)

    # "Select Service" -> Make a new claim
    by_id(page, "ClpDashboardSchema_clm_service_opt_NC").click()

    # "Select Claim Type" -> Travel (MUI Autocomplete). Confirmed live: the
    # dropdown option's actual text is exactly "Travel", not "Travel Claim"
    # (the wizard/page title says "Travel Claim", but that's not the option
    # label) -- searching for "Travel Claim" here would never match and
    # hang until timeout, which is exactly what broke on the first page.
    select_autocomplete(page, "ClpDashboardSchema_clm_type", "Travel", "Travel")

    click_visible_button(page, "Next")

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
    # NOTE: From and To are two SEPARATE real hidden inputs
    # ("...travel_period" and "...travel_period_to") — each has its own
    # narrowly-scoped ancestor container holding just its own 3
    # spinbuttons, so each is filled with the default index=0. (An earlier
    # version of this script incorrectly reused the "From" id for both
    # calls with index=0/1, assuming they shared one container scoped
    # exactly to both triplets — that ancestor search actually resolves to
    # the narrower per-field group, so index=1 found nothing and hung.)
    fill_date_field(page, "ClpDashboardSchema_basic_details.travel_period", *travel_from)
    fill_date_field(page, "ClpDashboardSchema_basic_details.travel_period_to", *travel_to)

    fill_text(page, "ClpDashboardSchema_basic_details.name", contact_name)

    phone = by_id(page, "ClpDashboardSchema_basic_details.mobile_number")
    phone.click()
    page.keyboard.type(mobile_number)

    click_visible_button(page, "Next")


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

    click_visible_button(page, "Next")


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
    click_visible_button(page, "Next")


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

    click_visible_button(page, "Next")


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

    click_visible_button(page, "Next")

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
