# Contributing to PDF Statement & Invoice Analyser (`scripts`)

Thank you for your interest in contributing! Bug reports, new PDF provider adapters, and documentation improvements are welcome.

---

## Getting Started

1. **Clone the repository**:
   ```bash
   git clone git@github.com:inodes/scripts.git
   cd scripts
   ```
2. **Install dependencies** (or run `direnv allow`):
   ```bash
   pip install -r requirements.txt
   ```
3. **Run the unit test suite**:
   ```bash
   python3 finance/tests/test_providers.py
   # or from within the finance/ directory:
   cd finance && python3 -m unittest discover -s tests -v
   ```

---

## Adding a New PDF Provider

1. Subclass `BasePDFProvider` in `finance/statements/providers/<provider>.py`.
2. Register the provider in `AVAILABLE_PROVIDERS` in `finance/statements/providers/__init__.py`.
3. Add default provider configuration (`target_dir`, `filename_template`) in `finance/statements/config.py` and `finance/config.example.json`.
4. Add offline unit tests using synthetic (non-PII) statement text in `finance/tests/`.
5. Run `./fix_exec_bits.sh` to ensure all `.py` and `.sh` files have `u+x` permissions.
