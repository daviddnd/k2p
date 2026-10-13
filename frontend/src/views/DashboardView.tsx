/**
 * The dashboard (scope §2.3): the widgets `GET /api/v1/dashboard` can source.
 *
 * Pure presentation over the response — the route component fetches, this
 * renders, which is what lets the SSR verification entry render real payloads
 * without a browser. Filtering and search are client-side: the portfolio is
 * small (the Free tier caps it at 100 companies) and the loaded response
 * already holds everything the filters read. The §2.3 search matches on the
 * company name, the BRN and the CR number — the card payload carries all
 * three.
 *
 * A company card and each pending-ingestion entry link to the company review
 * screen (`#/companies/{id}`), where extracted proposals are confirmed or
 * rejected. Widgets the backend deliberately omits (draft approval/signature
 * states, the activity feed) are not faked; one footnote says so. The
 * `pending_event_review` widget — change events awaiting staff review, the
 * same set the cross-company queue lists — and the `pending_draft_review`
 * widget — drafts whose current version awaits staff review, counted once per
 * lineage — render as counts per company, linking into the queue and each
 * company's screen (where the Drafts section is); the separate approval
 * widgets (events at `reviewed`, drafts past review) are not this phase's,
 * and the panels say so rather than implying they exist. The
 * `pending_confirmations` widget (slice R, contract §5.1.12) lists the sent
 * confirmation requests awaiting reply, each with its days elapsed and its
 * reminder chain — mid-cadence, exhausted, or gate-blocked (named) — linking
 * into the client-actions panel, whose reply milestone reads `blocked` with
 * `reminders_exhausted` once a chain runs out.
 */

import { useMemo, useState } from 'react'
import type {
  AutomationType,
  CalendarEntry,
  CompanyCard,
  DashboardResponse,
  PortfolioOverview,
  ServiceGap,
} from '../api/types'
import { DeadlineTable } from '../components/DeadlineTable'
import { NotGeneratedPanel } from '../components/NotGeneratedPanel'
import { CompanyStatusBadge, RiskBadge } from '../components/badges'
import { cardMatches } from '../filters'
import {
  automationLabel,
  formatDate,
  formatDateTime,
  formatDaysRemaining,
  formatHkd,
  gatingReasonLabel,
} from '../format'
import { hrefFor, hrefForCompany } from '../route'

const AUTOMATION_TYPES: AutomationType[] = [
  'calendar_sync',
  'automated_prefill',
  'email_ingestion',
  'document_extraction',
  'ai_reply_parsing',
  'confirmation_generation',
  'workflow_orchestration',
]

const AUTOMATION_COUNT = AUTOMATION_TYPES.length

export function DashboardView({
  data,
  viewerRole,
  onRefresh,
}: {
  data: DashboardResponse
  viewerRole?: 'administrator' | 'general'
  onRefresh?: () => void
}) {
  const { portfolio, company_cards: cards, not_generated: notGenerated } = data
  const gapsByCompany = useMemo(() => groupGaps(data.service_gaps), [data.service_gaps])
  const notGeneratedIds = useMemo(
    () => new Set(notGenerated.map((item) => item.company_id)),
    [notGenerated],
  )
  // Every company denied every automation type for the same, deployment-wide
  // reason: the licence tier itself carries nothing (an unlicensed install).
  const tierWideDenial =
    cards.length > 0 &&
    cards.every((card) => {
      const gaps = gapsByCompany.get(card.company_id) ?? []
      return (
        gaps.length === AUTOMATION_COUNT &&
        gaps.every((gap) => gap.reason === 'tier_capability_missing')
      )
    })
  const allDormant = cards.length > 0 && cards.every((card) => card.status === 'dormant')

  return (
    <div className="view">
      <section className="panel">
        <header className="panel-head">
          <div>
            <h2>Portfolio overview</h2>
            <p className="muted">
              As of {formatDate(data.as_of)}. Counts cover deadlines due by {formatDate(data.window_to)}
              ; a deadline that passed on or after {formatDate(data.window_from)} counts as overdue.
              Nothing records that a return was filed yet, so a past due date stays overdue until the
              draft/export history lands.
            </p>
          </div>
          {onRefresh !== undefined && (
            <button type="button" className="secondary" onClick={onRefresh}>
              Refresh
            </button>
          )}
        </header>
        <PortfolioTiles portfolio={portfolio} />
      </section>

      {cards.length === 0 ? (
        <EmptyPortfolio viewerRole={viewerRole} />
      ) : (
        <>
          <section className="panel">
            <h2>Risk alerts</h2>
            <p className="muted">Amber and red deadlines: due within 15 days, or overdue.</p>
            <DeadlineTable entries={data.risk_alerts} emptyText="No amber or red alerts." />
          </section>

          <section className="panel">
            <h2>Upcoming deadlines</h2>
            <p className="muted">NAR1 deadlines due within 30 days of {formatDate(data.as_of)}.</p>
            <DeadlineTable
              entries={data.upcoming_deadlines}
              emptyText={
                allDormant
                  ? `All ${cards.length} companies in this view are flagged dormant, and dormant companies are excluded from NAR1 deadline generation. A company that returns to active gets its deadlines back.`
                  : 'No NAR1 deadlines fall in the next 30 days.'
              }
            />
          </section>

          <PendingIngestionSection pending={data.pending_ingestion} cards={cards} />

          <PendingEventReviewSection pending={data.pending_event_review} cards={cards} />

          <PendingDraftReviewSection pending={data.pending_draft_review} cards={cards} />

          <PendingConfirmationsSection pending={data.pending_confirmations} cards={cards} />

          <ServiceCoverageSection gapsByCompany={gapsByCompany} cards={cards} tierWide={tierWideDenial} />

          <NotGeneratedPanel items={notGenerated} nameFor={(id) => nameOf(cards, id)} />

          <CompaniesSection
            cards={cards}
            gapsByCompany={gapsByCompany}
            notGeneratedIds={notGeneratedIds}
            tierWideDenial={tierWideDenial}
          />
        </>
      )}

      <p className="muted footnote">
        Not shown here yet, because their data sources are not built: drafts awaiting approval or
        signature (Phase 6) and the recent-activity feed. Nothing on this page estimates them.
      </p>
    </div>
  )
}

function PortfolioTiles({ portfolio }: { portfolio: PortfolioOverview }) {
  const tiles: Array<[string, number, string?]> = [
    ['Companies', portfolio.total_companies],
    ['Due within 30 days', portfolio.due_within_30_days],
    ['Due within 15 days', portfolio.due_within_15_days, 'amber'],
    ['Due within 7 days', portfolio.due_within_7_days, 'red'],
    ['Overdue', portfolio.overdue, portfolio.overdue > 0 ? 'red' : undefined],
    [
      'Late fees if filed today',
      portfolio.late_fee_exposure_hkd,
      portfolio.late_fee_exposure_hkd > 0 ? 'red' : undefined,
    ],
  ]
  return (
    <ul className="tiles">
      {tiles.map(([label, value, tone]) => (
        <li key={label} className={tone === undefined ? 'tile' : `tile tile-${tone}`}>
          <span className="tile-value">{value}</span>
          <span className="tile-label">{label}</span>
        </li>
      ))}
    </ul>
  )
}

function EmptyPortfolio({ viewerRole }: { viewerRole?: 'administrator' | 'general' }) {
  return (
    <section className="panel">
      <h2>No companies to show</h2>
      {viewerRole === 'general' ? (
        <p>
          This account has no company access grants, so its portfolio is empty. Ask an Administrator
          to grant access to the companies you work on — this is the least-privilege default, not an
          error.
        </p>
      ) : (
        <p>
          This portfolio has no companies yet. Onboarding is not part of this UI slice: it runs
          through the API — <code>POST /api/v1/companies</code> for one company,{' '}
          <code>POST /api/v1/companies/import</code> for a CSV portfolio. This view fills in as soon
          as companies exist.
        </p>
      )}
    </section>
  )
}

function PendingIngestionSection({
  pending,
  cards,
}: {
  pending: DashboardResponse['pending_ingestion']
  cards: CompanyCard[]
}) {
  return (
    <section className="panel">
      <h2>Pending data-ingestion review</h2>
      <p className="muted">
        Extracted master-data records awaiting confirmation — {pending.pending_records} in total
        across the companies you can see.
      </p>
      {pending.companies.length === 0 ? (
        <p className="muted">No extracted records are waiting for review.</p>
      ) : (
        <ul className="plain-list">
          {pending.companies.map((item) => (
            <li key={item.company_id}>
              <a href={hrefForCompany(item.company_id)}>
                {item.company_name ??
                  nameOf(cards, item.company_id) ??
                  `Company ${item.company_id.slice(0, 8)}`}
              </a>{' '}
              — {item.pending_records} record{item.pending_records === 1 ? '' : 's'} pending
            </li>
          ))}
        </ul>
      )}
      {pending.companies.length > 0 && (
        <p className="muted">
          Each company above opens its review screen: the extraction runs, and the proposed values,
          confidence and provenance behind each one, to confirm or reject.
        </p>
      )}
    </section>
  )
}

/**
 * The pending-event-review widget (scope §2.3): change events whose current
 * version awaits staff review, counted per company. It is the same set the
 * cross-company queue lists — the API builds both from one constant — so the
 * total and the queue's length cannot disagree. Events awaiting *approval*
 * (the `reviewed` status) are a separate widget this phase does not claim, and
 * the panel says so rather than leaving a reader to infer it.
 */
function PendingEventReviewSection({
  pending,
  cards,
}: {
  pending: DashboardResponse['pending_event_review']
  cards: CompanyCard[]
}) {
  return (
    <section className="panel">
      <h2>Change events awaiting review</h2>
      <p className="muted">
        Recorded changes waiting on staff — {pending.pending_events} in total across the companies
        you can see. This is the same set the cross-company queue lists; an event awaiting
        <em> approval</em> is the approval gate&rsquo;s, and its widget is not on this page.
      </p>
      {pending.companies.length === 0 ? (
        <p className="muted">No change event is awaiting review.</p>
      ) : (
        <>
          <ul className="plain-list">
            {pending.companies.map((item) => (
              <li key={item.company_id}>
                <a href={hrefForCompany(item.company_id)}>
                  {item.company_name ??
                    nameOf(cards, item.company_id) ??
                    `Company ${item.company_id.slice(0, 8)}`}
                </a>{' '}
                — {item.pending_events} event{item.pending_events === 1 ? '' : 's'} awaiting review
              </li>
            ))}
          </ul>
          <p className="muted">
            Each company above opens its screen, where its change events are listed with their
            version history — or{' '}
            <a href={hrefFor('change-events')}>open the review queue</a> to work them across
            companies, the soonest statutory deadline first.
          </p>
        </>
      )}
    </section>
  )
}

/**
 * The pending-draft-review widget (scope §2.3): drafts whose current version
 * awaits staff review (`generated` or `under_review`), counted once per
 * lineage. The API selects the current version in SQL — a regenerated lineage
 * is one draft, and a decided one is not work — so the count and the
 * company's own Drafts section cannot disagree. Drafts awaiting *approval* or
 * the client's signature are Phase 6's, and the panel says so rather than
 * leaving a reader to infer it.
 */
function PendingDraftReviewSection({
  pending,
  cards,
}: {
  pending: DashboardResponse['pending_draft_review']
  cards: CompanyCard[]
}) {
  return (
    <section className="panel">
      <h2>Drafts awaiting review</h2>
      <p className="muted">
        Generated drafts waiting on staff — {pending.pending_drafts} in total across the companies
        you can see. Each lineage counts once, on its current version; a draft awaiting
        <em> approval</em> or the client&rsquo;s signature is later work, and its widget is not on
        this page.
      </p>
      {pending.companies.length === 0 ? (
        <p className="muted">No draft is awaiting review.</p>
      ) : (
        <>
          <ul className="plain-list">
            {pending.companies.map((item) => (
              <li key={item.company_id}>
                <a href={hrefForCompany(item.company_id)}>
                  {item.company_name ??
                    nameOf(cards, item.company_id) ??
                    `Company ${item.company_id.slice(0, 8)}`}
                </a>{' '}
                — {item.pending_drafts} draft{item.pending_drafts === 1 ? '' : 's'} awaiting review
              </li>
            ))}
          </ul>
          <p className="muted">
            Each company above opens its screen, where the Drafts section lists the current versions
            and each draft&rsquo;s own screen holds the resolved values, the change highlighting and
            the actions the API allows on it now.
          </p>
        </>
      )}
    </section>
  )
}

/**
 * The pending-confirmations widget (scope §2.3; contract §5.1.12): the sent
 * confirmation requests awaiting reply, per company, each with the read-time
 * facts of its reminder chain — how many days have elapsed, how many of the
 * cadence's reminders have gone out, the next cadence instant while one is
 * planned, an exhausted state when the chain has run out (no further reminder
 * will be sent — staff contact the client by hand, which every tier allows),
 * and, where the §5.1 gate refuses the company's confirmation generation, the
 * gate's own reason named rather than implied. Ungated per ADR-0012 — this is
 * the customer's own data — and the panel links into the client-actions
 * screen, whose reply milestone turns `blocked` with `reminders_exhausted`
 * once a chain runs out.
 */
function PendingConfirmationsSection({
  pending,
  cards,
}: {
  pending: DashboardResponse['pending_confirmations']
  cards: CompanyCard[]
}) {
  const total = pending.companies.reduce((sum, item) => sum + item.requests.length, 0)
  return (
    <section className="panel">
      <h2>Confirmation requests awaiting reply</h2>
      <p className="muted">
        Sent requests the client has not answered — {total} in total across the companies you can
        see. The reminder sweep (contract §5.1.12) follows each on the configured cadence: under the
        client destination&rsquo;s <code>automatic</code> mode reminders send themselves; under{' '}
        <code>review_required</code> they are drafted and held for a staff release; where the
        automation is denied the block is named, and where the chain has run out the entry says so.
      </p>
      {pending.companies.length === 0 ? (
        <p className="muted">No confirmation request is awaiting a reply.</p>
      ) : (
        <>
          <ul className="plain-list">
            {pending.companies.map((item) => (
              <li key={item.company_id}>
                <a href={hrefForCompany(item.company_id)}>
                  {item.company_name ??
                    nameOf(cards, item.company_id) ??
                    `Company ${item.company_id.slice(0, 8)}`}
                </a>{' '}
                — {item.requests.length} request{item.requests.length === 1 ? '' : 's'} awaiting
                reply
                <ul className="plain-list">
                  {item.requests.map((request) => (
                    <li key={request.confirmation_request_id}>
                      {request.days_elapsed} day{request.days_elapsed === 1 ? '' : 's'} elapsed ·{' '}
                      {request.reminders_sent} of {request.cadence_total} reminders sent
                      {request.automation_blocked !== null ? (
                        <span className="muted">
                          {' '}
                          — held: no reminder goes out automatically (
                          {gatingReasonLabel(request.automation_blocked)})
                        </span>
                      ) : request.exhausted ? (
                        <>
                          {' '}
                          — <span className="badge state-blocked">Reminders exhausted</span>: no
                          further reminder will be sent; contact the client by hand
                        </>
                      ) : request.next_reminder_at !== null ? (
                        <span className="muted">
                          {' '}
                          — next reminder due {formatDateTime(request.next_reminder_at)}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </li>
            ))}
          </ul>
          <p className="muted">
            Each company above opens its screen; the reply-by dates these requests carry are the
            client-actions panel&rsquo;s —{' '}
            <a href={hrefFor('client-actions')}>open the client-actions panel</a> to see every
            period&rsquo;s milestones together.
          </p>
        </>
      )}
    </section>
  )
}

function ServiceCoverageSection({
  gapsByCompany,
  cards,
  tierWide,
}: {
  gapsByCompany: Map<string, ServiceGap[]>
  cards: CompanyCard[]
  tierWide: boolean
}) {
  const denied = cards.filter((card) => (gapsByCompany.get(card.company_id) ?? []).length > 0)
  return (
    <section className="panel">
      <h2>Service coverage</h2>
      <p className="muted">
        Automation types the gate denies, per company — the same check every automated action
        consults (tier capability, then the company&rsquo;s service package).
      </p>
      {tierWide ? (
        <p className="notice">
          Automation is unavailable for every company in this view: this deployment&rsquo;s licence
          tier does not carry any automation capability (
          <code>tier_capability_missing</code>), so the gate refuses every automated action. The
          reads on this page are not gated.
        </p>
      ) : denied.length === 0 ? (
        <p className="muted">No automation is denied for the companies in this view.</p>
      ) : (
        <ul className="plain-list">
          {denied.map((card) => (
            <li key={card.company_id}>
              <span className="company-name">{card.company_name ?? `BRN ${card.br_number}`}</span>
              <ul className="chip-list">
                {(gapsByCompany.get(card.company_id) ?? []).map((gap) => (
                  <li key={gap.automation} className="chip">
                    {automationLabel(gap.automation)} — {gatingReasonLabel(gap.reason)}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function CompaniesSection({
  cards,
  gapsByCompany,
  notGeneratedIds,
  tierWideDenial,
}: {
  cards: CompanyCard[]
  gapsByCompany: Map<string, ServiceGap[]>
  notGeneratedIds: Set<string>
  tierWideDenial: boolean
}) {
  const [search, setSearch] = useState('')
  const [status, setStatus] = useState('all')
  const [risk, setRisk] = useState('all')
  const [dueFrom, setDueFrom] = useState('')
  const [dueTo, setDueTo] = useState('')

  const visible = cards.filter((card) =>
    cardMatches(card, { search, status, risk, dueFrom, dueTo }),
  )

  function clearFilters() {
    setSearch('')
    setStatus('all')
    setRisk('all')
    setDueFrom('')
    setDueTo('')
  }

  return (
    <section className="panel">
      <h2>Companies</h2>
      <div className="filters">
        <label>
          Search
          <input
            type="search"
            placeholder="Name, BRN or CR number"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
        </label>
        <label>
          Status
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="all">All statuses</option>
            <option value="active">Active</option>
            <option value="dormant">Dormant</option>
            <option value="striking_off">Striking off</option>
            <option value="dissolved">Dissolved</option>
            <option value="none">No status recorded</option>
          </select>
        </label>
        <label>
          Risk
          <select value={risk} onChange={(event) => setRisk(event.target.value)}>
            <option value="all">All risks</option>
            <option value="red">Red</option>
            <option value="amber">Amber</option>
            <option value="green">Green</option>
            <option value="none">No deadline</option>
          </select>
        </label>
        <label>
          Due from
          <input type="date" value={dueFrom} onChange={(event) => setDueFrom(event.target.value)} />
        </label>
        <label>
          Due to
          <input type="date" value={dueTo} onChange={(event) => setDueTo(event.target.value)} />
        </label>
        <button type="button" className="secondary" onClick={clearFilters}>
          Clear
        </button>
      </div>
      <p className="muted">
        Showing {visible.length} of {cards.length} companies.
      </p>
      {visible.length === 0 ? (
        <p className="muted">No company matches these filters.</p>
      ) : (
        <div className="cards">
          {visible.map((card) => (
            <CompanyCardView
              key={card.company_id}
              card={card}
              gaps={gapsByCompany.get(card.company_id) ?? []}
              ruleNotEstablished={notGeneratedIds.has(card.company_id)}
              tierWideDenial={tierWideDenial}
            />
          ))}
        </div>
      )}
    </section>
  )
}

function CompanyCardView({
  card,
  gaps,
  ruleNotEstablished,
  tierWideDenial,
}: {
  card: CompanyCard
  gaps: ServiceGap[]
  ruleNotEstablished: boolean
  tierWideDenial: boolean
}) {
  return (
    <article className="card">
      <header>
        <h3>
          <a href={hrefForCompany(card.company_id)}>{card.company_name ?? 'No name on record'}</a>
        </h3>
        <CompanyStatusBadge status={card.status} />
      </header>
      <p className="brn">
        BRN {card.br_number}
        {card.cr_number === null ? '' : ` · CR ${card.cr_number}`}
      </p>
      {card.next_deadline === null ? (
        <p className="muted">
          {ruleNotEstablished
            ? 'No NAR1 deadline generated: no due-date rule is established for this company type.'
            : card.status === 'dormant'
              ? 'No NAR1 deadline generated: dormant companies are excluded.'
              : 'No NAR1 deadline generated in the read window.'}
        </p>
      ) : (
        <NextDeadline deadline={card.next_deadline} />
      )}
      <p className={card.pending_ingestion_records > 0 ? 'pending' : 'muted'}>
        {card.pending_ingestion_records} record
        {card.pending_ingestion_records === 1 ? '' : 's'} awaiting ingestion review
      </p>
      {gaps.length > 0 && (
        <p className="muted">
          {tierWideDenial
            ? 'No automation: the licence tier carries none.'
            : `Not covered: ${gaps.map((gap) => automationLabel(gap.automation)).join(', ')}.`}
        </p>
      )}
      <p>
        <a href={hrefForCompany(card.company_id)}>Open company review</a>
      </p>
    </article>
  )
}

function NextDeadline({ deadline }: { deadline: CalendarEntry }) {
  return (
    <p className="next-deadline">
      <span className="company-name">NAR1 due {formatDate(deadline.due_date)}</span>
      <span className={deadline.overdue ? 'overdue' : undefined}>
        {formatDaysRemaining(deadline.days_remaining)}
      </span>
      <RiskBadge risk={deadline.risk} />
      {deadline.late_fee_exposure.late && (
        <span className="overdue">
          late fee {formatHkd(deadline.late_fee_exposure.fee_hkd)} if filed today
        </span>
      )}
    </p>
  )
}

function groupGaps(gaps: ServiceGap[]): Map<string, ServiceGap[]> {
  const grouped = new Map<string, ServiceGap[]>()
  for (const gap of gaps) {
    const existing = grouped.get(gap.company_id)
    if (existing === undefined) grouped.set(gap.company_id, [gap])
    else existing.push(gap)
  }
  return grouped
}

function nameOf(cards: CompanyCard[], companyId: string): string | null {
  const card = cards.find((candidate) => candidate.company_id === companyId)
  if (card === undefined) return null
  return card.company_name ?? `BRN ${card.br_number}`
}
