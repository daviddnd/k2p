/**
 * The dashboard's client-side filter rule (scope §2.3): search by company
 * name, BRN or CR number, plus status, risk and due-date range.
 *
 * Client-side because the portfolio is small (the Free tier caps it at 100
 * companies) and the loaded dashboard already holds every field the filters
 * read. Its own module so the SSR harness can call it directly — the rendered
 * markup cannot show what typing does.
 */

import type { CompanyCard } from './api/types'

export interface CompanyFilters {
  search: string
  status: string
  risk: string
  dueFrom: string
  dueTo: string
}

export function cardMatches(card: CompanyCard, filters: CompanyFilters): boolean {
  const needle = filters.search.trim().toLowerCase()
  if (needle !== '') {
    const haystack =
      `${card.company_name ?? ''} ${card.br_number} ${card.cr_number ?? ''}`.toLowerCase()
    if (!haystack.includes(needle)) return false
  }
  if (filters.status !== 'all' && (card.status ?? 'none') !== filters.status) return false
  if (filters.risk !== 'all' && (card.next_deadline?.risk ?? 'none') !== filters.risk) return false
  if (filters.dueFrom !== '' || filters.dueTo !== '') {
    const due = card.next_deadline?.due_date
    if (due === undefined) return false
    if (filters.dueFrom !== '' && due < filters.dueFrom) return false
    if (filters.dueTo !== '' && due > filters.dueTo) return false
  }
  return true
}
