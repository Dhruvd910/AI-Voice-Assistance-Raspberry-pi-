"""Source adapters: the only code that talks to the outside world for assets.

Each adapter knows ONE approved source, how to search it (where the source
allows searching), how to read an item's licence, and how to fetch the item.
None of them scrapes arbitrary sites, and none of them invents a URL: when a
source has no search API, the adapter says so instead of guessing one.

    BodyParts3D   anatomy meshes with FMA ids           CC BY 4.0 (whole database)
    NIH3D         entries by 3DPX id                    per entry, chosen by the uploader
    ZAnatomy      Blender atlas built on BodyParts3D    CC BY-SA 4.0, upstream parts vary
    PubChem       computed structures (SDF, SMILES)     NCBI data policy
    RCSB          protein structures (mmCIF/PDB)        CC0 1.0
"""

from __future__ import annotations

import io
import json
import logging
import re
import time
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from models.provenance import KNOWN_LICENSES, LicenseError, ProvenanceRecord, check_license

log = logging.getLogger(__name__)

USER_AGENT = "3d-education-engine/0.1 (educational; contact via project maintainer)"
TIMEOUT = 30


class SourceUnavailable(RuntimeError):
    """The source cannot do what was asked (no search API, network down...)."""


@dataclass
class SourceAsset:
    source: str
    source_id: str
    title: str
    url: str
    license: str | None               # SPDX / LicenseRef id, or None when undetermined
    creator: str = ""
    formats: list[str] = field(default_factory=list)
    notes: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def provenance(self, attribution: str, modification_status: str) -> ProvenanceRecord:
        if self.license is None:
            raise LicenseError(f"{self.source} {self.source_id}: licence cannot be determined")
        lic = check_license(self.license)
        return ProvenanceRecord(
            provider=self.source, url=self.url, source_id=self.source_id,
            creator=self.creator or "unknown", license=lic.spdx, license_url=lic.url,
            attribution=attribution, download_date=date.today().isoformat(),
            modification_status=modification_status)


def http() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def get(session: requests.Session, url: str, **kw) -> requests.Response:
    for attempt in range(3):
        try:
            resp = session.get(url, timeout=TIMEOUT, **kw)
            if resp.status_code in (429, 503) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                continue
            resp.raise_for_status()
            return resp
        except requests.ConnectionError as exc:
            if attempt == 2:
                raise SourceUnavailable(f"cannot reach {url}: {exc}") from exc
            time.sleep(1.0)
    raise SourceUnavailable(f"{url}: gave up")


class SourceAdapter(ABC):
    name: str = ""
    homepage: str = ""

    def __init__(self, cache_dir: Path, session: requests.Session | None = None):
        self.cache_dir = cache_dir / self.name.lower()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = session or http()

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[SourceAsset]: ...

    @abstractmethod
    def metadata(self, source_id: str) -> SourceAsset: ...

    @abstractmethod
    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        """Fetch the ORIGINAL files, unmodified, into dest_dir."""


# =====================================================================
# BodyParts3D
# =====================================================================
class HTTPRangeFile(io.RawIOBase):
    """A remote file that can be seeked, via HTTP Range requests.

    zipfile only needs seek/read, so this lets it read the central directory
    of BodyParts3D's 65 MB archive and pull out the ~90 heart meshes (a few MB)
    without downloading the rest -- which matters on a 32 GB SD card.
    """

    def __init__(self, session: requests.Session, url: str):
        super().__init__()
        self.session, self.url, self.pos = session, url, 0
        head = session.head(url, timeout=TIMEOUT, allow_redirects=True)
        head.raise_for_status()
        if head.headers.get("Accept-Ranges", "").lower() != "bytes":
            raise SourceUnavailable(f"{url} does not support range requests")
        self.size = int(head.headers["Content-Length"])
        self.last_modified = head.headers.get("Last-Modified", "")

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.size}[whence]
        self.pos = max(0, base + offset)
        return self.pos

    def readinto(self, buffer) -> int:
        if self.pos >= self.size:
            return 0
        end = min(self.pos + len(buffer), self.size) - 1
        resp = get(self.session, self.url, headers={"Range": f"bytes={self.pos}-{end}"})
        data = resp.content
        buffer[: len(data)] = data
        self.pos += len(data)
        return len(data)


class BodyParts3DAdapter(SourceAdapter):
    name = "BodyParts3D"
    homepage = "https://dbarchive.biosciencedbc.jp/en/bodyparts3d/download.html"
    base = "https://dbarchive.biosciencedbc.jp/data/bodyparts3d/LATEST"
    archive = "partof_BP3D_4.0_obj_99.zip"
    license_page = "https://dbarchive.biosciencedbc.jp/en/bodyparts3d/lic.html"
    # Verbatim from the licence page (last updated 2025-02-27).
    attribution = ("BodyParts3D, © The Database Center for Life Science "
                   "licensed under CC Attribution 4.0 International")
    license = "CC-BY-4.0"

    def _list(self, filename: str) -> list[list[str]]:
        path = self.cache_dir / filename
        if not path.is_file():
            path.write_bytes(get(self.session, f"{self.base}/{filename}").content)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return [line.split("\t") for line in lines[1:] if line.strip()]

    def concepts(self) -> dict[str, str]:
        """FMA id -> English name, PART-OF tree."""
        return {row[0]: row[2] for row in self._list("partof_parts_list_e.txt") if len(row) >= 3}

    def elements(self) -> dict[str, set[str]]:
        """FMA id -> the FJ element files that make it up."""
        out: dict[str, set[str]] = {}
        for row in self._list("partof_element_parts.txt"):
            if len(row) >= 3:
                out.setdefault(row[0], set()).add(row[2])
        return out

    def search(self, query: str, limit: int = 10) -> list[SourceAsset]:
        words = [w for w in re.findall(r"[a-z]+", query.lower()) if w not in {"human", "the", "a", "of"}]
        elements = self.elements()
        hits = []
        for fma, name in self.concepts().items():
            if all(w in name.lower() for w in words):
                hits.append(self._asset(fma, name, len(elements.get(fma, ()))))
        hits.sort(key=lambda a: (len(a.title), a.title))
        return hits[:limit]

    def _asset(self, fma: str, name: str, n_elements: int) -> SourceAsset:
        return SourceAsset(
            source=self.name, source_id=fma, title=name,
            url=f"{self.base}/{self.archive}", license=self.license,
            creator="The Database Center for Life Science (DBCLS)", formats=["obj"],
            notes=f"{n_elements} element mesh(es); PART-OF tree, 99% polygon reduction",
            extra={"license_page": self.license_page})

    def metadata(self, source_id: str) -> SourceAsset:
        name = self.concepts().get(source_id)
        if name is None:
            raise KeyError(f"BodyParts3D has no concept {source_id}")
        return self._asset(source_id, name, len(self.elements().get(source_id, ())))

    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        return self.download_elements(sorted(self.elements().get(asset.source_id, ())), dest_dir)

    def download_elements(self, element_ids: list[str], dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        wanted = {f"{e}.obj" for e in element_ids}
        missing = [w for w in wanted if not (dest_dir / w).is_file()]
        if missing:
            remote = io.BufferedReader(HTTPRangeFile(self.session, f"{self.base}/{self.archive}"),
                                       buffer_size=256 * 1024)
            with zipfile.ZipFile(remote) as archive:
                members = {Path(n).name: n for n in archive.namelist()}
                for filename in missing:
                    if filename not in members:
                        raise KeyError(f"{filename} is not in {self.archive}")
                    (dest_dir / filename).write_bytes(archive.read(members[filename]))
                    log.info("BodyParts3D: fetched %s", filename)
        return sorted(dest_dir / w for w in wanted)


# =====================================================================
# NIH 3D
# =====================================================================
class NIH3DAdapter(SourceAdapter):
    """NIH 3D entries, looked up by their 3DPX id.

    NIH 3D publishes no public search API (checked 2026-09: the site is a
    Next.js app whose search runs client-side), so `search` explains how to
    find an id rather than guessing an endpoint. Each entry page embeds its
    metadata -- licence, files, uploader -- which is what `metadata` reads.

    Licences on NIH 3D are chosen per entry by the uploader and shown without a
    version ("CC-BY"). A version is never assumed: pass the licence you have
    confirmed on the entry page as `confirmed_license`.
    """

    name = "NIH_3D"
    homepage = "https://3d.nih.gov/"
    UNVERSIONED = {"CC-BY", "CC-BY-SA", "CC-BY-NC", "CC-BY-NC-SA", "CC-BY-ND", "CC-BY-NC-ND"}
    EXACT = {"CC0": "CC0-1.0", "CC0-1.0": "CC0-1.0", "PUBLIC DOMAIN": "LicenseRef-Public-Domain-US-Gov"}
    FORMAT_PREFERENCE = ("glb", "gltf", "stl", "obj", "ply", "x3d", "wrl")

    def search(self, query: str, limit: int = 10) -> list[SourceAsset]:
        raise SourceUnavailable(
            "NIH 3D has no public search API. Search on https://3d.nih.gov/discover, open the "
            "entry, and pass its id (e.g. 3DPX-012345) with --entry.")

    def metadata(self, source_id: str, confirmed_license: str | None = None) -> SourceAsset:
        if not re.fullmatch(r"3DPX-\d{6}", source_id):
            raise ValueError(f"{source_id!r} is not an NIH 3D id (3DPX-######)")
        url = f"https://3d.nih.gov/entries/{source_id}"
        page = get(self.session, url).text.replace('\\"', '"')
        label = _first(page, r'"metadata":\{[^{}]*?"license":"([^"]+)"') or _first(page, r'"license":"([^"]+)"')
        title = _first(page, r'"metadata":\{[^{}]*?"title":"([^"]+)"') or source_id
        creator = _first(page, r'"displayName":"([^"]+)"') or ""
        files = [{"url": m.group(1), "name": m.group(2), "format": Path(m.group(2)).suffix.lstrip(".").lower()}
                 for m in re.finditer(r'"s3Location":"(https://[^"]+)","uploadDate":"[^"]*","name":"([^"]+)"', page)]
        spdx, note = self._license(label, confirmed_license)
        return SourceAsset(self.name, source_id, title, url, spdx, creator,
                           sorted({f["format"] for f in files}), note,
                           extra={"files": files, "license_label": label,
                                  "attribution_instructions": _first(page, r'"attributionInstructions":"([^"]*)"') or ""})

    def _license(self, label: str | None, confirmed: str | None) -> tuple[str | None, str]:
        if not label:
            return None, "no licence found on the entry page"
        key = label.strip().upper()
        if key in self.EXACT:
            return self.EXACT[key], f"licence on entry page: {label}"
        if key in self.UNVERSIONED:
            if confirmed and confirmed.upper().startswith(key + "-") and confirmed in KNOWN_LICENSES:
                return confirmed, f"entry page says {label}; version confirmed by operator as {confirmed}"
            return None, (f"entry page says {label} without a version; confirm it on the page and "
                          f"pass --confirm-license {label}-4.0 (or the version shown)")
        return None, f"unrecognised licence label {label!r}"

    def search_help(self) -> str:
        return self.search.__doc__ or ""

    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        if asset.license is None:
            raise LicenseError(f"{asset.source_id}: {asset.notes}")
        files = asset.extra.get("files", [])
        for fmt in self.FORMAT_PREFERENCE:
            chosen = [f for f in files if f["format"] == fmt]
            if chosen:
                dest_dir.mkdir(parents=True, exist_ok=True)
                path = dest_dir / chosen[0]["name"]
                if not path.is_file():
                    path.write_bytes(get(self.session, chosen[0]["url"]).content)
                return [path]
        raise SourceUnavailable(f"{asset.source_id}: no file in a supported format ({files})")


def _first(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


# =====================================================================
# Z-Anatomy
# =====================================================================
class ZAnatomyAdapter(SourceAdapter):
    """The Z-Anatomy Blender atlas (GitHub: Z-Anatomy/Models-of-human-anatomy).

    Z-Anatomy's own content is CC BY-SA 4.0, but its README lists upstream
    models under OTHER licences (e.g. a kidney under CC BY-NC 4.0, the inner
    ear under CC BY-NC-SA 4.0). So a structure extracted from it gets
    CC BY-SA 4.0 only when its upstream source is known to allow that --
    `upstream_license` must be supplied per structure; the repository licence
    is never applied blindly.

    Extraction needs Blender (the atlas is a .blend inside Z-Anatomy.zip,
    87 MB). See models/converter.py:blender_export_objects.
    """

    name = "Z-Anatomy"
    homepage = "https://github.com/Z-Anatomy/Models-of-human-anatomy"
    api = "https://api.github.com/repos/Z-Anatomy/Models-of-human-anatomy"
    raw = "https://raw.githubusercontent.com/Z-Anatomy/Models-of-human-anatomy/master"
    attribution = "Z-Anatomy - The libre 3D atlas of anatomy - CC-BY-SA 4.0"
    # Upstream sources named in Z-Anatomy's README, with their licences.
    UPSTREAM = {
        "BodyParts3D": "CC-BY-SA-2.1-JP",   # as Z-Anatomy received it; BodyParts3D is now CC BY 4.0
        "Kidney (Lissie Cowley)": "CC-BY-NC-4.0",
        "Inner ear (University of Dundee)": "CC-BY-NC-SA-4.0",
        "Cranial nerves (University of Dundee, CAHID)": "CC-BY-4.0",
    }

    def search(self, query: str, limit: int = 10) -> list[SourceAsset]:
        """Structures named in the atlas's term list (TA2.csv)."""
        path = self.cache_dir / "TA2.csv"
        if not path.is_file():
            path.write_bytes(get(self.session, f"{self.raw}/TA2.csv").content)
        words = query.lower().split()
        hits = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if all(w in line.lower() for w in words):
                name = line.split(",")[0].strip().strip('"') or line[:60]
                hits.append(SourceAsset(self.name, name, name, self.homepage, None,
                                        "Gauthier Kervyn (Z-Anatomy)", ["blend"],
                                        "licence depends on the structure's upstream source; needs Blender"))
            if len(hits) >= limit:
                break
        return hits

    def metadata(self, source_id: str, upstream_license: str | None = None) -> SourceAsset:
        repo = get(self.session, self.api).json()
        return SourceAsset(self.name, source_id, source_id, repo.get("html_url", self.homepage),
                           "CC-BY-SA-4.0" if upstream_license in {"CC-BY-SA-2.1-JP", "CC-BY-4.0", "CC-BY-SA-4.0"} else None,
                           "Gauthier Kervyn, Marcin Zielinski (Z-Anatomy)", ["blend"],
                           "CC BY-SA 4.0 for Z-Anatomy's adaptation; upstream licence: "
                           f"{upstream_license or 'NOT GIVEN -- cannot be determined'}")

    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        if asset.license is None:
            raise LicenseError(f"{asset.source_id}: {asset.notes}")
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / "Z-Anatomy.zip"
        if not path.is_file():
            with get(self.session, f"{self.raw}/Z-Anatomy.zip", stream=True) as resp, open(path, "wb") as f:
                for block in resp.iter_content(1 << 20):
                    f.write(block)
        return [path]


# =====================================================================
# PubChem
# =====================================================================
class PubChemAdapter(SourceAdapter):
    name = "PubChem"
    homepage = "https://pubchem.ncbi.nlm.nih.gov/"
    rest = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
    license = "LicenseRef-NCBI-Data-Policy"
    PROPERTIES = "Title,MolecularFormula,MolecularWeight,SMILES,IUPACName"

    def search(self, query: str, limit: int = 5) -> list[SourceAsset]:
        url = f"{self.rest}/compound/name/{quote(query)}/property/{self.PROPERTIES}/JSON"
        try:
            resp = self.session.get(url, timeout=TIMEOUT)
        except requests.ConnectionError as exc:
            raise SourceUnavailable(f"PubChem unreachable: {exc}") from exc
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        rows = resp.json().get("PropertyTable", {}).get("Properties", [])
        return [self._asset(row) for row in rows[:limit]]

    def metadata(self, source_id: str) -> SourceAsset:
        url = f"{self.rest}/compound/cid/{int(source_id)}/property/{self.PROPERTIES}/JSON"
        row = get(self.session, url).json()["PropertyTable"]["Properties"][0]
        return self._asset(row)

    def _asset(self, row: dict) -> SourceAsset:
        cid = str(row["CID"])
        return SourceAsset(
            source=self.name, source_id=cid, title=row.get("Title") or row.get("IUPACName") or cid,
            url=f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}", license=self.license,
            creator="National Center for Biotechnology Information (NCBI)", formats=["sdf"],
            notes="computed properties and 3D conformer",
            extra={"formula": row.get("MolecularFormula"), "smiles": row.get("SMILES"),
                   "iupac": row.get("IUPACName"), "weight": row.get("MolecularWeight")})

    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        """The PubChem-computed 3D conformer; 2D record when no 3D exists (e.g. ions)."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        cid = int(asset.source_id)
        for record_type in ("3d", "2d"):
            path = dest_dir / f"pubchem_cid{cid}_{record_type}.sdf"
            if path.is_file():
                return [path]
            resp = self.session.get(f"{self.rest}/compound/cid/{cid}/record/SDF",
                                    params={"record_type": record_type}, timeout=TIMEOUT)
            if resp.status_code == 404:
                continue
            resp.raise_for_status()
            path.write_text(resp.text, encoding="utf-8")
            (dest_dir / f"pubchem_cid{cid}_properties.json").write_text(
                json.dumps(asset.extra, indent=2), encoding="utf-8")
            return [path]
        raise SourceUnavailable(f"PubChem CID {cid} has no SDF record")

    def attribution_for(self, asset: SourceAsset) -> str:
        return (f"Structure data from PubChem, National Center for Biotechnology Information: "
                f"{asset.title} (CID {asset.source_id})")


# =====================================================================
# RCSB Protein Data Bank
# =====================================================================
class RCSBAdapter(SourceAdapter):
    name = "RCSB_PDB"
    homepage = "https://www.rcsb.org/"
    search_url = "https://search.rcsb.org/rcsbsearch/v2/query"
    entry_url = "https://data.rcsb.org/rest/v1/core/entry"
    files_url = "https://files.rcsb.org/download"
    license = "CC0-1.0"   # wwPDB: PDB archive data are available under CC0 1.0

    def search(self, query: str, limit: int = 10) -> list[SourceAsset]:
        body = {"query": {"type": "terminal", "service": "full_text", "parameters": {"value": query}},
                "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": limit}}}
        try:
            resp = self.session.post(self.search_url, json=body, timeout=TIMEOUT)
        except requests.ConnectionError as exc:
            raise SourceUnavailable(f"RCSB unreachable: {exc}") from exc
        if resp.status_code == 204:
            return []
        resp.raise_for_status()
        return [self.metadata(hit["identifier"]) for hit in resp.json().get("result_set", [])[:limit]]

    def metadata(self, source_id: str) -> SourceAsset:
        pdb_id = source_id.upper()
        entry = get(self.session, f"{self.entry_url}/{pdb_id}").json()
        citation = entry.get("rcsb_primary_citation", {})
        authors = ", ".join(citation.get("rcsb_authors", [])[:3])
        return SourceAsset(
            source=self.name, source_id=pdb_id, title=entry.get("struct", {}).get("title", pdb_id),
            url=f"https://www.rcsb.org/structure/{pdb_id}", license=self.license,
            creator=authors or "wwPDB depositors", formats=["cif", "pdb"],
            notes=entry.get("exptl", [{}])[0].get("method", ""),
            extra={"citation_title": citation.get("title", ""), "year": citation.get("year"),
                   "doi": citation.get("pdbx_database_id_doi")})

    def download(self, asset: SourceAsset, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{asset.source_id}.cif"
        if not path.is_file():
            path.write_bytes(get(self.session, f"{self.files_url}/{asset.source_id}.cif").content)
        return [path]

    def attribution_for(self, asset: SourceAsset) -> str:
        cite = asset.extra.get("citation_title") or asset.title
        return f"PDB {asset.source_id}: {cite} ({asset.creator}). Data from RCSB PDB (rcsb.org)."


ADAPTERS: dict[str, type[SourceAdapter]] = {
    "BodyParts3D": BodyParts3DAdapter,
    "NIH_3D": NIH3DAdapter,
    "Z-Anatomy": ZAnatomyAdapter,
    "PubChem": PubChemAdapter,
    "RCSB_PDB": RCSBAdapter,
}

# Which sources to consult, in order, for each domain.
SOURCE_PRIORITY = {
    "biology": ["NIH_3D", "Z-Anatomy", "BodyParts3D", "RCSB_PDB"],
    "chemistry": ["PubChem"],
    "physics": [],   # physics is generated, never downloaded
}


def adapter(name: str, cache_dir: Path) -> SourceAdapter:
    if name not in ADAPTERS:
        raise KeyError(f"unknown source {name!r}; approved sources: {sorted(ADAPTERS)}")
    return ADAPTERS[name](cache_dir)
