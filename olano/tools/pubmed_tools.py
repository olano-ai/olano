"""PubMed toolkit — search biomedical literature via NCBI Entrez.

Auth env vars:
  PUBMED_EMAIL    — required by NCBI for Entrez API usage (any valid address)
  PUBMED_API_KEY  — optional; raises NCBI rate limit from 3 to 10 req/sec

Falls back to LIONCLAW_USER_EMAIL if PUBMED_EMAIL is unset.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any, cast

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


def _entrez_setup() -> tuple[Any, str | None]:
    """Return (Entrez_module, error_string)."""
    bio, err = optional_import("Bio", purpose="PubMed biomedical search")
    if err:
        return None, err
    assert bio is not None
    Entrez = bio.Entrez
    email = get_credential("PUBMED_EMAIL") or os.environ.get("LIONCLAW_USER_EMAIL")
    if not email:
        return None, (
            "[missing-credentials] PUBMED_EMAIL not set. NCBI requires an email "
            "for Entrez API usage. Set PUBMED_EMAIL in the vault."
        )
    Entrez.email = email
    api_key = get_credential("PUBMED_API_KEY")
    if api_key:
        Entrez.api_key = api_key
    return Entrez, None


@tool
def pubmed_search(
    query: Annotated[str, "PubMed query (supports MeSH terms, e.g. 'mRNA vaccines[MeSH]')"],
    limit: Annotated[int, "Max PMIDs to return"] = 20,
) -> str:
    """Search PubMed for biomedical papers and return PMIDs with titles."""
    Entrez, err = _entrez_setup()
    if err:
        return err
    try:
        handle = Entrez.esearch(db="pubmed", term=query, retmax=limit, sort="relevance")
        ids = Entrez.read(handle).get("IdList", [])
        handle.close()
        if not ids:
            return f"No PubMed results for '{query}'."
        summ = Entrez.esummary(db="pubmed", id=",".join(ids))
        docs = Entrez.read(summ)
        summ.close()
        lines = [f"PubMed '{query}' ({len(docs)}):"]
        for d in docs:
            pmid = d.get("Id", "?")
            title = d.get("Title", "?")
            journal = d.get("Source", "?")
            pubdate = d.get("PubDate", "?")
            lines.append(f"- [PMID:{pmid}] {title}\n  {journal} | {pubdate}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] pubmed_search: {exc}"


@tool
def pubmed_get_article(
    pmid: Annotated[str, "PubMed ID (PMID)"],
    include_abstract: Annotated[bool, "Include full abstract"] = True,
) -> str:
    """Fetch metadata + abstract for a single PubMed article by PMID."""
    Entrez, err = _entrez_setup()
    if err:
        return err
    try:
        handle = Entrez.efetch(db="pubmed", id=pmid, rettype="medline", retmode="text")
        text = handle.read()
        handle.close()
        if not text or not text.strip():
            return f"No PubMed article for PMID {pmid}."
        if not include_abstract:
            text = "\n".join(line for line in text.splitlines() if not line.startswith("AB  "))
        return cast(str, text)
    except Exception as exc:
        return f"[tool-error] pubmed_get_article: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
