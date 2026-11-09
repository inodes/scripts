# Security Policy

## Supported Versions

Security and bug fixes are released on the `main` branch.

| Version | Supported          |
| :------ | :----------------- |
| `main`  | :white_check_mark: |

---

## Zero-PII Policy & Reporting a Vulnerability

This suite parses financial PDF statements and local files.
- **Never commit real PDF statements, CSV exports, SQLite databases, `config.json`, `category_rules.json`, or real account numbers/balances.**
- Real personal configurations, target directories, and custom categorization rules must always reside outside the repository in iCloud at `~/Library/Mobile Documents/com~apple~CloudDocs/Finance/config.json`.
- If you discover a security vulnerability or potential data-safety issue:
  1. **Do not open a public issue.**
  2. Report the issue privately using GitHub's "Report a vulnerability" (private security advisory) option on this repository's Security tab.
