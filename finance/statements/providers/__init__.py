"""Provider Registry and Dispatch."""
from typing import Any, Dict, List, Optional, Tuple
from .base import BasePDFProvider, GenericPDFProvider
from .hsbc import (
    HSBCPremierProvider,
    HSBCCreditCardProvider,
    HSBCSimpleProvider,
    HSBCRouterProvider,
)
from .ing import INGProvider
from .commbank import CommBankProvider
from .workday import WorkdayProvider
from .aapt import AAPTProvider
from .apple import AppleProvider
from .vendors import AussieBBProvider, TelstraProvider, VodafoneProvider, BunningsProvider, GooglePlayProvider

AVAILABLE_PROVIDERS: List[BasePDFProvider] = [
    HSBCCreditCardProvider(),
    HSBCPremierProvider(),
    HSBCSimpleProvider(),
    HSBCRouterProvider(),
    INGProvider(),
    CommBankProvider(),
    WorkdayProvider(),
    AAPTProvider(),
    AppleProvider(),
    AussieBBProvider(),
    TelstraProvider(),
    VodafoneProvider(),
    BunningsProvider(),
    GooglePlayProvider(),
    GenericPDFProvider(),
]


def get_provider(name: Optional[str] = None) -> BasePDFProvider:
    if name:
        n_low = name.lower().strip()
        for p in AVAILABLE_PROVIDERS:
            if p.name.lower() == n_low:
                return p
    return GenericPDFProvider()


def detect_provider(text: str, pdf_path: str, forced_provider: Optional[str] = None) -> BasePDFProvider:
    if forced_provider and forced_provider.lower() != "auto":
        prov = get_provider(forced_provider)
        if prov.name == "hsbc":
            return prov.route(text, pdf_path)
        return prov

    for p in AVAILABLE_PROVIDERS:
        if p.name in ("generic", "hsbc"):
            continue
        if p.detect(text, pdf_path):
            return p

    return GenericPDFProvider()


def detect_and_extract(
    text: str, pdf_path: str, forced_provider: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None
) -> Tuple[BasePDFProvider, Dict[str, Any]]:
    from ..pdf import get_where_froms

    provider = detect_provider(text, pdf_path, forced_provider)
    data = provider.extract(text, pdf_path)
    data["target_filename"] = provider.generate_filename(data, pdf_path, cfg)
    data["target_dir"] = provider.resolve_target_dir(data, pdf_path, cfg)
    urls = get_where_froms(pdf_path)
    if urls:
        data["where_froms"] = urls
        data["source_url"] = urls[0]
    matched_url = provider.matches_source_metadata(pdf_path, cfg=cfg)
    if matched_url:
        data["matched_source_url"] = matched_url
    return provider, data


def detect_provider_by_metadata(
    pdf_path: str,
    provider_filter: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[BasePDFProvider], Optional[str]]:
    """Fast-path check using macOS com.apple.metadata:kMDItemWhereFroms."""
    if provider_filter and provider_filter.lower() != "auto":
        prov = get_provider(provider_filter)
        if prov.name != "generic":
            url = prov.matches_source_metadata(pdf_path, cfg=cfg)
            if url:
                return prov, url
        return None, None

    for p in AVAILABLE_PROVIDERS:
        if p.name in ("generic", "hsbc"):
            continue
        url = p.matches_source_metadata(pdf_path, cfg=cfg)
        if url:
            return p, url
    return None, None


def check_missing_periods_by_provider(
    statements: List[Dict[str, Any]],
    provider_filter: Optional[str] = None,
    cfg: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Run provider-aware missing statement period detection across statements."""
    if provider_filter and provider_filter.lower() != "auto":
        prov = get_provider(provider_filter)
        if prov.name != "generic":
            return prov.check_missing_periods(statements, cfg=cfg)

    gaps: List[Dict[str, Any]] = []
    # Process HSBC as a unified provider family
    hsbc_stmts = [
        s
        for s in statements
        if (s.get("provider") or "").startswith("hsbc")
        or (s.get("doc_type") or "").startswith("hsbc")
    ]
    if hsbc_stmts:
        gaps.extend(get_provider("hsbc").check_missing_periods(hsbc_stmts, cfg=cfg))

    for p in AVAILABLE_PROVIDERS:
        if p.name in ("generic", "hsbc") or p.name.startswith("hsbc_"):
            continue
        sub_stmts = [
            s
            for s in statements
            if (s.get("doc_type") or s.get("provider")) in (p.name, p.doc_type)
        ]
        if sub_stmts:
            gaps.extend(p.check_missing_periods(sub_stmts, cfg=cfg))
    return gaps
