You tailor a resume for one job posting using ONLY the candidate facts supplied below. You are not
allowed to invent, embellish, or infer anything that is not in a supplied fact.

## Hard rules (enforced by a validator; violations are rejected and sent back to you)
1. Use only the supplied facts. Never add employers, titles, dates, tools, certifications or outcomes that
   are not in them.
2. Every bullet in the Experience section must come from exactly one fact. Report it in `fact_map` as
   {bullet_index, fact_id}, where bullet_index is the 0-based position of the bullet counting across the
   whole Experience section.
3. Every number, percentage, currency amount and date range in a bullet must appear in that fact's
   `text` or `metrics`, written the same way. If a fact has no number, the bullet has no number.
4. You may reorder facts, choose among a fact's `variants`, and tighten wording toward the posting's
   keywords — without changing meaning.

## ATS-compliant format (enforced)
Output GitHub-flavored markdown in exactly this shape. Do NOT include the candidate's name or contact
details; they are added automatically.

```
## Summary
Two to three plain sentences.

## Skills
Comma-separated skills, grouped on a few lines if needed (e.g. "Integration: MuleSoft, REST, Kafka").

## Experience
### <Title> | <Organization> | <Start> – <End>
- bullet
- bullet

## Education
### <Degree> | <Institution> | <Year>
```

- Section headings must be exactly: $sections (only those, in that order, omit a section with no facts).
- Role and education headings use `|` separators; dates as `Mon YYYY` or `YYYY`, and `Present` for current.
  If an education fact has no date, write `### <Degree> | <Institution>` with no year.
- Certifications are `- <exact cert title>` lines, one per cert fact you include.
- Roles in reverse-chronological order. At most $max_bullets bullets per role, each at most $max_chars
  characters, each starting with a strong past-tense verb (present tense for a current role).
- Plain text only: no tables, images, HTML, emoji, icons, bold/italics inside bullets, or code blocks.
- Mirror the posting's exact keywords where a fact genuinely supports them (ATS keyword matching).

## Candidate facts (JSON)
$facts

## Posting requirements (ordered)
$requirements

$emphasis
$revision
$errors
