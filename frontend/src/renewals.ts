/**
 * The §5.1.9 panel's translations and the **shared refusal vocabulary**, kept
 * out of the views so the SSR harness can call them directly (the
 * `coverage.ts` / `filters.ts` precedent for a rule that outlives one render).
 *
 * `describeRefusal` is the one place a `detail` is read into the sentence a
 * panel shows — every screen's refusals go through it, so a code is given its
 * remedy once: the renewal routes' (§5.1.9), the change-event lifecycle's
 * (§5.1.3), the draft lifecycle's (§5.1, §5.1.3), the staff-MFA screens'
 * `totp_invalid` (scope §8) and the shapes whose
 * `detail` carries a joined violation list (`tenant_config_invalid`,
 * `change_event_invalid`, `filing_metadata_invalid`).
 *
 * The rest **translate, they do not decide**. `correctionTargets` reads the
 * ADR-0008 transition table's own targets off a period's status, so the form
 * offers only what the route admits; `blockedReasonNote` turns the blocked
 * milestone's own `blocked_reason` — the leg the backend named, never a
 * client-side re-derivation — into the remedy a staff member acts on; an
 * unknown code is left as the API's own line rather than given an invented
 * remedy. Nothing here re-derives the release gate: the reason comes from the
 * read, a document's form leg is judged at the download, and where the panel
 * and the API could ever disagree the API's own answer is what is shown.
 */

import { ApiError, errorText } from './api/client'
import type { GatingReason, RenewalDecision } from './api/types'
import { gatingReasonLabel } from './format'

/** The refusal `GET/PUT /tenant-config` answers for an unreadable stored block. */
const TENANT_CONFIG_INVALID = 'tenant_config_invalid'

/**
 * Whether a period's status is one the record defines (`pending | confirmed |
 * declined | payment_received`, or `null` for a period nobody has recorded).
 * The panel offers no act for a status outside this set: it would be describing
 * a row in words it cannot check.
 */
export function knownRenewalStatus(status: string | null): boolean {
  return (
    status === null ||
    status === 'pending' ||
    status === 'confirmed' ||
    status === 'declined' ||
    status === 'payment_received'
  )
}

/**
 * The decisions a correction of this period may land on.
 *
 * The transition table's targets, spelled out for the two statuses that admit
 * one: a `declined` period is corrected to `confirmed`, a `confirmed` one to
 * `declined`, and a paid one to either (correcting away from
 * `payment_received` takes the payment back — the bounced-cheque case). A
 * period nobody has answered admits none: there is no recorded decision to put
 * right, and the remedy is recording one.
 */
export function correctionTargets(status: string | null): RenewalDecision[] {
  if (status === 'confirmed') return ['declined']
  if (status === 'declined') return ['confirmed']
  if (status === 'payment_received') return ['confirmed', 'declined']
  return []
}

/**
 * Why a `blocked` milestone is blocked, as the sentence staff act on — or
 * `null` for a reason this build does not know.
 *
 * The reason itself is the backend's (`blocked_reason` on the read): the
 * release gate's legs a period can be judged by, `no_confirmation_request`
 * for the reply milestone, whose block is a missing letter rather than a
 * gate, or `reminders_exhausted` (§5.1.12) when the request's reminder chain
 * has run out with the reply still missing — staff take over by hand.
 * The gate's legs reuse the refusal notes below, so the panel's sentence and
 * the download's `409` cannot describe one refusal in two words; the
 * precondition has its own, because nothing refuses a request for it. A reason
 * this vocabulary does not know reads `null`, which leaves the caller's own
 * fallback to speak rather than inventing a remedy.
 */
export function blockedReasonNote(reason: string): string | null {
  if (reason === 'no_confirmation_request') {
    return 'no confirmation request exists for this period yet, so there is nothing to reply to — a precondition, not a gate'
  }
  return refusalNote(reason)
}

/**
 * What a refusal code means for staff, in one sentence.
 *
 * The release gate's six legs (§5.1.9) and the renewal record's own five
 * refusals, each naming the remedy: assign or widen a package, record the
 * client's answer, record the fee, correct a decision that was recorded
 * wrongly. The codes are the wire's — the panel shows them beside the sentence
 * so a reader can match what they see against the contract — and a code this
 * vocabulary does not know reads `null`, which leaves the caller's own fallback
 * to speak.
 */
export function refusalNote(code: string): string | null {
  return REFUSAL_NOTES[code] ?? null
}

const REFUSAL_NOTES: Record<string, string> = {
  // The release gate's scope legs (the §5.1 gating vocabulary itself).
  no_assignment: 'the company has no service package assigned — assign one and the letter can be released',
  assignment_ended: 'the assignment was ended by hand — assign the company again, which is how a renewal is recorded',
  out_of_coverage: 'the release day is outside the assignment’s coverage period — widen it, or assign the company again',
  form_not_covered: 'the assigned package does not cover this work — add the form to the package’s covered forms',
  // The release gate's period leg — the two errands it keeps apart.
  continuation_not_recorded:
    'the client’s answer for the period is not on file — ask the client, then record the answer (not the fee: nobody has asked for it yet)',
  payment_not_received:
    'the client confirmed the period and the fee is not recorded — record the fee, correcting the decision first if it was recorded wrongly',
  // The renewal record’s own routes.
  service_not_confirmed: 'the client’s answer comes first — a fee can be recorded only for a confirmed period',
  already_closed: 'the period is already answered — a decision stands, and a wrong one is put right with Correct the decision',
  not_decided: 'nothing is recorded for this period yet — record the client’s answer first',
  decision_unchanged: 'that is the answer already on the record — a correction has to change something',
  note_required: 'a decline and every correction need a reason in the note',
  // The change-event lifecycle’s refusals (contract §5.1.3). The code is the
  // API’s; the sentence says what a staff member does next.
  transition_not_allowed:
    'the record’s current status does not admit that action — the move did not happen, so reload it and act on what it now says',
  rationale_required:
    'that action needs a reason — a rejection and a staff-recorded client withdrawal are refused without one, and the reason is what a reader finds in the record',
  change_event_conflict:
    'another revision of this event was saved first — reload the event and retry',
  protected_class_unavailable:
    'the payload carries a §1 protected-class field, and this build refuses the class rather than storing it unencrypted — drop the field the refusal names; the partial identifiers (hkid_partial, passport_partial, passport_issuing_country_region) are ordinary fields',
  reserved_event_type:
    'company_name_change is reserved for Wave 2/NNC2 — it is not enterable in this version',
  reconfirmation_not_wired:
    'sending for reconfirmation is not wired yet (it waits on the counsel-gated reply parser) — the action was refused, never half-performed',
  supersede_not_wired:
    'a supersession waits on the submitted status and the new-event linkage — the action was refused, never half-performed',
  // The draft lifecycle’s refusals (contract §5.1.3, §5.1) — every §4.2 row
  // is wired, so the draft vocabulary names live gates, not pending work.
  statement_confirmation_required:
    'the draft makes NAR1 item 16’s declaration and the approval arrived without confirming it — tick the confirmation and approve again; the draft did not move and nothing was written',
  statement_not_made:
    'the draft makes no item-16 statement (ND2A has none) — a confirmation cannot be recorded for it; the draft did not move',
  statement_not_confirmed:
    'the NAR1 declaration stands unconfirmed — approve it with the confirmation recorded first; the export was refused and nothing was written',
  export_mode_required:
    'the export needs its mode stated — choose e_registry_prep, paper or api_ready; refused before anything was written',
  export_mode_invalid:
    'the mode is outside the three the product knows — choose e_registry_prep, paper or api_ready; refused before anything was written',
  draft_conflict:
    'another generation won the version race — reload the company’s drafts and retry',
  nar1_rule_not_established:
    'the company type has no established anniversary rule for an NAR1 (public and guarantee companies are dated from the return date, which this product does not hold) — nothing was written',
  // §5.1.12's exhaustion (slice R): the reply milestone's own block — the
  // chain ran out, which is a state of the request, not a gate leg.
  reminders_exhausted:
    'every reminder on this request has been sent and the reply is still missing — no further reminder will go out automatically; contact the client by hand',
  // The staff-MFA refusals (phase 7, scope §8) the challenge and enrollment
  // screens show: one named refusal covers a wrong, malformed or replayed
  // code, so the sentence names no cause and the session stays where it was.
  totp_invalid:
    'the code did not match — the attempt wrote nothing; check the authenticator app’s clock and code and try again',
}

/** Why a stored configuration nobody can read refuses, and what repairs it. */
const TENANT_CONFIG_INVALID_NOTE =
  'the stored configuration cannot be read, so the panel can say nothing about this tenant’s dates — ' +
  'saving a valid configuration repairs it, because the write validates its own request and replaces ' +
  'the blocks it carries whatever the stored row holds — with one exception, the optional send_modes ' +
  'block: a save that omits it keeps what is stored, and an unreadable stored block is refused rather ' +
  'than reset, so repairing that one takes a request that carries it'

/**
 * The refusals whose `detail` is one joined string of violations (contract
 * §5.1.3). The API composes `<code>: <violations>` with `; ` between them,
 * and the panel shows the list it was rather than one line.
 */
const JOINED_VIOLATION_NOTES: Record<string, string> = {
  [TENANT_CONFIG_INVALID]: TENANT_CONFIG_INVALID_NOTE,
  change_event_invalid:
    'the entry or edit leaves the change-event rules — every violation the API named is listed below, field path first',
  filing_metadata_invalid:
    'the edit would leave the filing-metadata document outside its shape — each field the API named is listed below, and the merged document is what it judged, so fixing one key must leave the whole legal',
}

export interface RefusalView {
  /** The line to show: the code and its remedy, or the plain failure. */
  message: string
  /** Every violation the refusal listed, one per line; empty when it listed none. */
  violations: string[]
}

/**
 * A refusal as the panel shows it: the backend's own code, what it means, and
 * every violation it listed — never one generic line.
 *
 * `error.detail` is read as `<code>` or `<code>: <rest>`; a code the vocabulary
 * knows becomes its remedy sentence, and the shapes whose `rest` is more than
 * a coordinate get it back separately: `service_not_in_scope: <reason>` and
 * `automation_denied: <reason>` name the reason whose remedy applies, and the
 * joined-violation codes (`tenant_config_invalid`, `change_event_invalid`,
 * `filing_metadata_invalid`) carry their violation list — which the API joins
 * — as the list it was. Anything else — an unknown code, a plain sentence such
 * as `company not found`, a network failure — falls back to `errorText`, with
 * any listed violations still shown beneath it.
 */
export function describeRefusal(error: unknown): RefusalView {
  if (!(error instanceof ApiError)) return { message: 'Something went wrong.', violations: [] }
  const separator = error.detail.indexOf(': ')
  const code = separator === -1 ? error.detail : error.detail.slice(0, separator)
  const rest = separator === -1 ? '' : error.detail.slice(separator + 2)

  const joinedNote = JOINED_VIOLATION_NOTES[code]
  if (joinedNote !== undefined) {
    return {
      message: `${code} — ${joinedNote}`,
      violations:
        rest === ''
          ? []
          : rest.split('; ').map((item) => withoutCode(item, code)),
    }
  }
  if (code === 'automation_denied') {
    // The §5.1 gate's 403: the code names the denial, the rest is the gate's
    // own `GatingReason` — whose remedy is a phrase of its own.
    const note = gatingReasonLabel(rest as GatingReason)
    return { message: `${code}: ${rest} — ${note}`, violations: error.violations }
  }
  if (code === 'service_not_in_scope') {
    const note = refusalNote(rest)
    if (note !== null) {
      return { message: `${code}: ${rest} — ${note}`, violations: error.violations }
    }
  }
  const note = refusalNote(code)
  if (note !== null) {
    return {
      message: rest === '' ? `${code} — ${note}` : `${code}: ${rest} — ${note}`,
      violations: error.violations,
    }
  }
  return { message: errorText(error), violations: error.violations }
}

/**
 * One joined violation, without a repeated code prefix: `filing_metadata_invalid`
 * names each field with the code in front, and the panel's list should show the
 * field, not the code four times.
 */
function withoutCode(item: string, code: string): string {
  const prefix = `${code}: `
  return item.startsWith(prefix) ? item.slice(prefix.length) : item
}
