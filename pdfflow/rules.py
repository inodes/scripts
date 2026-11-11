"""Default document-type rules for triage. Generic only: no personal names, numbers or suppliers.

Rules are tried in order and the first match wins. Each rule is
  {"name": folder, "text": regex on the first pages' text, "filename": regex on the file name}
(either key may be omitted; a rule matches when ANY given regex matches).
Personal or niche rules belong in config.json:

    "triage": {"types": [{"name": "my_landlord", "text": "Acme Property Management"}]}

Config rules are tried BEFORE these defaults, and a config rule whose name matches a default
replaces it. Set {"name": "<default>", "disabled": true} to switch a default off.
"""
import re
from typing import Dict, List

DEFAULT_TYPES: List[Dict[str, str]] = [
    {"name": "payslips", "text": r"Period of Payment|Pay Period Begin|Payslip|Pay Advice"},
    {"name": "bank_statements",
     "text": r"(Statement (begins|period|number)|STATEMENT PERIOD|Account Summary)[\s\S]{0,2000}(BSB|Opening Balance|Closing Balance)"},
    {"name": "tax",
     "text": r"Australian Taxation Office|Notice of assessment|Individual tax return|Tax return for individuals|PAYG payment summary|Income statement|Division 293"},
    {"name": "shares_ess",
     "text": r"Employee Share Scheme|Shareworks|Solium|Restricted Stock|\bRSU\b|Morgan Stanley at Work"},
    {"name": "energy_utilities",
     "text": r"\bAGL\b|Energy Australia|Origin Energy|\bkWh\b|Ausgrid|Sydney Water|Council Rates|Rates Notice"},
    {"name": "telecom", "text": r"Vodafone|Telstra|Optus|Aussie Broadband|Internode|\bTPG\b|\bBelong\b"},
    {"name": "insurance",
     "text": r"\b(Bupa|Medibank|NRMA|Allianz)\b|Insurance Policy|Certificate of (Insurance|Currency)|Health cover"},
    {"name": "manuals",
     "text": r"AppleCare\+?\s+(for|Terms|Product Disclosure|Plan)|How Consumer Rights Affect this Plan|\bPDS\b"},
    {"name": "medical", "min_hits": 2,
     "text": r"\b(Medical Centre|Pathology|Radiology|Hospital|Prescription|Dental|Patient)\b"},
    {"name": "retail_receipts",
     "text": r"Apple Store|Apple Japan|Order Acknowledg\w+|Bunnings|JB Hi-?Fi|Amazon\.com\.au|Harvey Norman|Officeworks|The Good Guys|Kogan|IKEA|eBay"},
    {"name": "travel",
     "text": r"Qantas|Jetstar|Virgin Australia|e-?ticket|Itinerary|Booking (reference|confirmation)|Airbnb|Marriott"},
    {"name": "vehicle",
     "text": r"Tesla|Mazda|Vehicle Registration|Service NSW|Linkt|Transport for NSW|Supercharg|Roadworthy"},
    {"name": "property",
     "text": r"Contract for (the )?sale|Settlement|Valuation|Strata|Conveyanc|Home Loan|Mortgage|Land Registry|Stamp duty"},
    {"name": "government_id",
     "text": r"Centrelink|Services Australia|Department of Home Affairs|passport|Visa (grant|application)|myGov|Immunisation"},
    {"name": "education", "min_hits": 2, "text": r"\b(University|TAFE|Enrolment|Tuition|Report Card)\b"},
    {"name": "software_subscriptions",
     "text": r"Adobe|Backblaze|Google (One|Storage|Workspace)|Microsoft|JetBrains|GitHub|Dropbox|1Password|OpenAI|Anthropic|Subscription"},
    {"name": "invoices_receipts_other",
     "text": r"Tax Invoice|Invoice (No|Number|#)|Receipt (No|Number|#)|Amount (Paid|Due)"},
    {"name": "manuals",
     "min_hits": 2, "text": r"\b(User (Guide|Manual)|Table of Contents|Installation|Instructions|Warranty|Chapter \d)\b"},
]

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")
RESERVED = {"unsorted", "unknown_type", "needs_review", "duplicates", "archive"}

# handler "reason=" values -> needs_review sub-folder names (overridable in config triage.reasons)
DEFAULT_REASONS: Dict[str, str] = {
    "no_document_date": "no_date",
    "not_reconciled": "unreconciled",
    "domain_matched_but_content_format_changed": "format_changed",
    "empty_or_unreadable_pdf": "unreadable",
}
