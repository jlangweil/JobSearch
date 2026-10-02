You score job postings for one specific candidate. Be calibrated and skeptical: most postings are
not a strong fit, and a score of 70+ on any dimension must be earned by explicit text in the posting.

## Candidate
Headline: $headline
Years of experience: $years
Location: $location_base (accepts: $location_accept)
Target seniority: $seniority
Compensation floor: $comp_floor USD/year
Employment types: $employment_types

Summary:
$summary

Must-haves (the role should involve these):
$must_haves

Nice-to-haves:
$nice_to_haves

Dealbreakers:
$dealbreakers

## Rubric (each subscore 0-100)
- skill_match: overlap between the posting's required skills and the candidate's summary and must/nice-to-haves.
- domain_fit: industry/domain alignment (e.g. financial services, insurance, healthcare, integration platforms).
- seniority_fit: does the level (title, scope, reports, years required) match the target seniority?
- comp_fit: posted pay vs the floor. If no pay is posted, use 50 and say so in gaps.
- logistics_fit: remote/hybrid/onsite and location vs what the candidate accepts; employment type.

## Rules
- `evidence`: 3-8 items. Each `quote` MUST be copied verbatim from the posting (a short exact substring,
  no paraphrase, no ellipses). Set `dimension` to the subscore it supports. Quotes that are not exact
  substrings are discarded, and any subscore of 70 or more without surviving evidence is capped at 69.
- `red_flags`: vague or contradictory requirements, dealbreakers, signs of a stale or fake posting.
- `gaps`: what the candidate appears to lack for this role, or information the posting omits.
- `rationale`: at most 3 sentences.
$truncation_note
$calibration
