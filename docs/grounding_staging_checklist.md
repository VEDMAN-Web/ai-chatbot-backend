# Grounding staging checklist (10-turn script)

Run on the public Ask Tina chatbot after deploying hallucination / grounding fixes. Confirm replies stay factual and never invent amenities, yields, ownership, dates, or names.

| # | Say this | Expect |
|---|----------|--------|
| 1 | Show me villas in Phuket | Verified Phuket villa cards only; no invented names |
| 2 | Tell me about the first one | Details for the first card from turn 1 (same id/name) |
| 3 | Does it have a pool? | Yes/no only from verified amenities; never invent spa/gym |
| 4 | Where is it located? | Location from the listing record only |
| 5 | Show me condos in Samui | New Samui condo results; filters switched |
| 6 | Tell me about the second result | Second Samui card from turn 5 |
| 7 | What is the rental yield? | “Not in our verified data” + offer advisory team — no % invented |
| 8 | Is it freehold? | Only if ownership field says so; otherwise honest not-documented |
| 9 | How far is the airport? | Not in verified data (unless distance is on the record) |
| 10 | Do you have Zorblax Towers? | Honest not-found — never invent the project |

## Also watch

- After turn 6+, the bot must not reuse earlier assistant wording as facts.
- Cards and text must agree (same names/prices/beds).
- Logs should show `grounding_metrics` lines when the verifier fires (no personal data).
