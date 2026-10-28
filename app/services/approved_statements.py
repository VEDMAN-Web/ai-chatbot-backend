"""
Client-approved general claims for the public chatbot.

Nothing outside this list may be stated as a general market claim
(e.g. yields, ROI ranges, off-market inventory guarantees).
Add entries only after explicit client approval.
"""

from typing import List

# Empty by default. Each string must be an exact approved phrase.
APPROVED_STATEMENTS: List[str] = []

# Label used when developer marketing / guaranteed-return text is shown
# without endorsing it as a bot fact.
DEVELOPER_MARKETING_CLAIM_LABEL = "developer marketing claim"
