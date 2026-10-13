# `backend/rules/` — dated policy data (ADR-0010)

The home for Hong Kong regulatory policy expressed as **dated data, not code
constants**: fee and late-fee schedules today; statutory windows,
dormant-company treatment and alert cadences land here as the rule-pack
sequence continues (ADR-0010 section 2).

The conventions mirror `backend/forms/`:

- **This directory is canonical over `research/`** — figures are verified
  against the pinned CR sources before they are written here, and each
  dated entry records its source.
- **A dated entry is never edited in place** — a CR revision appends a new
  `schedules` entry with its own `effective_from` (and closes the previous
  entry's `effective_to`). History is how "what rule applied when" stays
  answerable.
- **Curation is human-in-the-loop** (AGENTS.md) — like the forms registry,
  these values feed filing-affecting computations, so a change here gets
  line-by-line review and a source citation.
- **The JSON document is the `fee_schedule` pack payload shape** (§2.2's
  pack list) — a licensed pack carrying the same document replaces the
  in-image data through the loader's seam (`app.adapters.rules.loader`),
  exactly as `form_mapping` packs do for the forms registry (§5.1.6).

Band semantics (NAR1, counted from the company's return date — the
incorporation anniversary): `on_time_fee` applies while the delivery date is
within `over_days` of the anniversary (42 days, the Cap. 622 s.662 window);
each late band then covers *more than* its lower edge (the first band's is
`over_days`, each later band's is the previous band's `within_months`) and
*not more than* `within_months` calendar months after the anniversary
(day-clamped, the Feb-29 convention); the top band's `within_months` is
`null` (open-ended). Evaluation walks the upper edges only; the loader
verifies the bands are contiguous, ascending and positive before a schedule
is trusted.
