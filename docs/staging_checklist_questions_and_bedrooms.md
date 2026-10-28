# Staging checklist — question words & bedroom ranges

Manual checks on the public chatbot (Ask Tina) after deploying the question-word / bedroom-range fixes.

## Question words are never property names

| # | Say this | Expect |
|---|----------|--------|
| 1 | Do you have anything with 4 or more bedrooms? | Search results for 4+ bedrooms — never `"you" is not in our verified database` |
| 2 | Do you have a pool villa? | Villa + pool search results |
| 3 | Do you have villas with 5 bedrooms under 30M? | Villas, exact/overlapping 5 beds, budget ≤ 30M |
| 4 | Is there anything with 3 bedrooms? | Search for 3 bedrooms — never property_name `"there anything"` |
| 5 | Do you have Pirak Villa? | Property details for Pirak Villa |
| 6 | Do you have Zorblax Towers? | Honest not-found (with suggestions if available) — not a filter dump |

## Bedroom min / max / range

| # | Say this | Expect |
|---|----------|--------|
| 7 | Show largest luxury villas (5-6 beds) | Results whose bedroom range overlaps 5–6; copy says `5-6 bedrooms` |
| 8 | 4 or more bedrooms | Only listings whose largest bedroom count ≥ 4; copy says `4+ bedrooms` |
| 9 | 20 or more bedrooms | Honest no-match; suggest relaxing bedrooms/budget |
| 10 | 4 or more bedrooms → then only under 30M | Keeps min 4 beds and applies 30M budget |
| 11 | show me other (after a 4+ search) | New cards; excludes already shown IDs |

## Languages (spot-check)

- PL: `co najmniej 4 sypialnie`, `4 lub więcej sypialni`, `od 3 do 5 sypialni`
- TH: `4 ห้องนอนขึ้นไป`, `อย่างน้อย 4 ห้องนอน`
