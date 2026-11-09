import { isMessageKey, type MessageKey } from '../../i18n'
import { TIERS, type FindingTier } from '../../api/findings'

/** The tier of a finding when this build knows it; a tier it does not know has no badge. */
export function knownTier(tier: string): FindingTier | null {
  return TIERS.find((t) => t === tier) ?? null
}

/** `health.rule.<id>.<part>` for a rule id, when the catalogue has it. */
export function ruleKey(ruleId: string, part: 'name' | 'text'): MessageKey | null {
  const key = `health.rule.${ruleId}.${part}`
  return isMessageKey(key) ? key : null
}
