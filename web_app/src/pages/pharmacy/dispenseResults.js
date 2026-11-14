/**
 * The five dispensing outcomes (Workflow §4 stage 6), one colour each. Shared by
 * the pharmacy and patient screens so both say the same thing about a pack.
 * "Could not verify" is grey and is never drawn like a match.
 */
export const DISPENSE_RESULT = {
  match:             { label: 'Match',                 cls: 'border-care-300 bg-care-50 text-care-900' },
  substitute:        { label: 'Substitute',            cls: 'border-amber-300 bg-amber-50 text-amber-900' },
  strength_mismatch: { label: 'Strength / form mismatch', cls: 'border-orange-400 bg-orange-50 text-orange-900' },
  wrong_medicine:    { label: 'Wrong medicine',        cls: 'border-vital-300 bg-vital-50 text-vital-900' },
  could_not_verify:  { label: 'Could not verify',      cls: 'border-ink-300 bg-ink-50 text-ink-700' },
}
