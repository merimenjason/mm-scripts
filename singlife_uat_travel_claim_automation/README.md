# Singlife Travel Claim — UAT Playwright Script

Fast, repeatable Playwright automation for the Singlife General Insurance
"Travel Claim" flow on the Merimen UAT Client Portal. Companion to the
`singlife-travel-claim-uat` Claude skill — use this script for quick
regression runs of the three known test cases; use the Claude skill when you
need something flexible (a new/unusual scenario, or judgment calls).

**UAT only.** The script is hard-pinned to
`https://clientportaluat.merimen.com/public/client/clp/clpdashboard?ins_code=SG_SINGLIFE`
and auto-confirms the final submission dialog, mirroring the team's standing
approval for this UAT/dummy-data workflow. Do not repoint it at production.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
# Auto-approved medical expense claim (S$250)
python singlife_travel_claim.py --case medical

# Flight delay claim (AV222 / Colombia / 24 Feb 2026)
python singlife_travel_claim.py --case flight_delay

# Baggage loss/damage, max limit (2 items x S$75)
python singlife_travel_claim.py --case baggage
```

Useful flags:

- `--headed` — show the browser window instead of running headless.
- `--slow-mo 150` — slow down each action by N ms (handy with `--headed` for
  watching a run).
- `--no-submit` — fill out the entire form and stop right before the
  "Confirm" click on the final "Proceed to Submit?" dialog, so you can
  eyeball everything first. Combine with `--headed` to leave the browser
  open for inspection (press Enter in the terminal to close it).
- `--policy-suffix SOMETHING` — appended to the dummy policy number, so
  repeat runs are distinguishable in the UAT system.
- `--pdf-dir ./docs` — write the generated dummy upload PDFs somewhere
  persistent instead of a temp directory (they're tiny, hand-built,
  valid-but-empty PDFs — the portal only checks that *a* file was
  provided).

On failure, a full-page screenshot is saved to the system temp directory and
its path is printed, to help diagnose what the portal looked like at the
point of failure.

## How it was built

Every selector in this script (element ids, radio/checkbox value
conventions, the MUI DatePicker/Autocomplete/multi-select-modal interaction
patterns) was reverse-engineered by directly inspecting the live UAT
portal's DOM through several full manual runs, not guessed from the visual
layout. In particular:

- All Yes/No radios follow the convention `<field>_opt_1` = Yes,
  `<field>_opt_0` = No.
- Every Claim Category's "Claim Type" field opens the same multi-select
  modal (checkbox grid + Select All / Clear All / Close) — not a plain
  dropdown, even for categories where it looks like one at a glance.
- MUI X DatePicker/DateTimePicker fields are filled by clicking the
  accessible `role="spinbutton"` "Day" (or "Hours") segment directly and
  typing digits — MUI auto-advances between segments. The Travel Period
  field holds two Day/Month/Year triplets sharing identical aria-labels
  (From/To); they're disambiguated by position (`.nth(0)` / `.nth(1)`).
- Supporting Documents file inputs are Uppy-generated with random
  per-session names, so they're targeted by DOM order and filled directly
  via Playwright's `set_input_files()`, which works on the hidden
  `<input type="file">` without needing the styled dropzone to be clicked.

## Caveats / things worth knowing

- **Not validated end-to-end from this sandbox** — the environment this
  script was written in has restricted network egress and can't reach the
  UAT portal directly, so this script has not been run to completion here.
  It was built from real, live-confirmed selectors (see above), but please
  do a first run with `--no-submit --headed` and watch it before trusting
  it for unattended/CI use.
- If Merimen changes the portal's DOM structure or field set, selectors
  here will need updating — this is the tradeoff for speed vs. the
  Claude-skill approach, which reasons about the page visually each time
  and adapts automatically.
- The "Are you covered by the airline or other insurance policy for this
  incident/loss?" question is always answered **No** in all three cases,
  which skips the conditional "Amount Recovered" field. If you need a
  scenario with Yes, you'll need to extend `fill_claim_details_common()`.
