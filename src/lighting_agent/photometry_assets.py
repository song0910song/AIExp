"""Durable, project-scoped storage for DIALux photometry downloads."""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from typing import Protocol

from .schemas import LuminaireCandidate, PhotometryAsset, PhotometryExtractedFile, ProjectState


class PhotometryDownloader(Protocol):
    def download_photometry_zip(self, detail_url: str) -> tuple[str, bytes]: ...


class PhotometryAssetStore:
    """Store source ZIPs, extracted photometry files and their manifest per project."""

    manifest_name = "manifest.json"
    _photometry_extensions = {".ies": "ies", ".ldt": "ldt", ".uld": "uld"}
    _max_extracted_file_bytes = 100 * 1024 * 1024
    _max_extracted_total_bytes = 200 * 1024 * 1024
    _max_archive_members = 256
    _max_compression_ratio = 200
    _copy_chunk_bytes = 64 * 1024

    def __init__(self, projects_directory: Path, downloader: PhotometryDownloader) -> None:
        self.projects_directory = projects_directory
        self.downloader = downloader

    def directory(self, project_id: str) -> Path:
        self._validate_project_id(project_id)
        return self.projects_directory / f"{project_id}.photometry"

    def list_assets(self, state: ProjectState) -> list[PhotometryAsset]:
        stored = self._load(state.project_id)
        assets: list[PhotometryAsset] = []
        for luminaire in state.luminaires:
            asset = stored.get(luminaire.luminaire_id)
            if asset is None:
                assets.append(
                    PhotometryAsset(
                        luminaire_id=luminaire.luminaire_id,
                        article_name=luminaire.article_name,
                        status="pending" if luminaire.has_photometry_download else "not_available",
                    )
                )
            else:
                assets.append(asset.model_copy(update={"article_name": luminaire.article_name}))
        return assets

    def download(self, state: ProjectState, luminaire_id: str) -> PhotometryAsset:
        if luminaire_id not in state.selected_luminaire_ids:
            raise ValueError(
                "Only final selected luminaires can download photometry assets"
            )
        luminaire = self._luminaire(state, luminaire_id)
        root = self.directory(state.project_id)
        root.mkdir(parents=True, exist_ok=True)
        assets = self._load(state.project_id)

        if not luminaire.has_photometry_download:
            asset = PhotometryAsset(
                luminaire_id=luminaire.luminaire_id,
                article_name=luminaire.article_name,
                status="not_available",
                error="DIALux does not advertise a photometric ZIP for this luminaire",
            )
            assets[luminaire_id] = asset
            self._save(state.project_id, assets)
            return asset

        try:
            source_url, content = self.downloader.download_photometry_zip(luminaire.detail_url)
            archive_rel, extracted = self._write_download(root, luminaire, content)
            asset = PhotometryAsset(
                luminaire_id=luminaire.luminaire_id,
                article_name=luminaire.article_name,
                status="downloaded",
                source_url=source_url,
                downloaded_at=datetime.now(UTC),
                sha256=hashlib.sha256(content).hexdigest(),
                zip_file=archive_rel,
                zip_size_bytes=len(content),
                extracted_files=extracted,
            )
        except Exception as error:
            asset = PhotometryAsset(
                luminaire_id=luminaire.luminaire_id,
                article_name=luminaire.article_name,
                status="failed",
                error=str(error) or error.__class__.__name__,
            )

        assets[luminaire_id] = asset
        self._save(state.project_id, assets)
        return asset

    def ensure_task_assets(self, state: ProjectState) -> list[PhotometryAsset]:
        """Download every advertised photometry file required by a task package.

        A task handoff must be self-contained.  Earlier exports only embedded
        assets that had been downloaded manually through the luminaire endpoint,
        which silently produced ZIPs without IES/LDT/ULD files.  Reuse complete
        saved assets, but retry missing, failed, or partially deleted ones when
        a new DIALux task package is requested.
        """

        current = {asset.luminaire_id: asset for asset in self.list_assets(state)}
        for luminaire in state.selected_luminaires():
            if not luminaire.has_photometry_download:
                continue
            asset = current[luminaire.luminaire_id]
            if asset.status == "downloaded" and self._asset_files_exist(state.project_id, asset):
                continue
            current[luminaire.luminaire_id] = self.download(state, luminaire.luminaire_id)
        return [current[luminaire.luminaire_id] for luminaire in state.selected_luminaires()]

    def remove(self, project_id: str, luminaire_id: str) -> None:
        root = self.directory(project_id)
        assets = self._load(project_id)
        asset = assets.pop(luminaire_id, None)
        if asset is None:
            return
        for relative in [asset.zip_file, *(item.relative_path for item in asset.extracted_files)]:
            if relative:
                target = self._safe_path(root, relative)
                target.unlink(missing_ok=True)
        stem = self._asset_stem(asset.article_name, luminaire_id)
        extracted_directory = self._safe_path(root, f"extracted/{stem}")
        if extracted_directory.exists():
            shutil.rmtree(extracted_directory)
        self._save(project_id, assets)

    def remove_project(self, project_id: str) -> None:
        root = self.directory(project_id)
        if root.exists():
            shutil.rmtree(root)

    def read_file(self, project_id: str, relative_path: str) -> Path:
        target = self._safe_path(self.directory(project_id), relative_path)
        if not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

    def _asset_files_exist(self, project_id: str, asset: PhotometryAsset) -> bool:
        if not asset.zip_file:
            return False
        try:
            self.read_file(project_id, asset.zip_file)
            for extracted in asset.extracted_files:
                self.read_file(project_id, extracted.relative_path)
        except (FileNotFoundError, ValueError):
            return False
        return True

    def _write_download(
        self, root: Path, luminaire: LuminaireCandidate, content: bytes
    ) -> tuple[str, list[PhotometryExtractedFile]]:
        stem = self._asset_stem(luminaire.article_name, luminaire.luminaire_id)
        archive_rel = f"archives/{stem}.zip"
        archive_path = self._safe_path(root, archive_rel)
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(archive_path, content)

        extracted_root = self._safe_path(root, f"extracted/{stem}")
        if extracted_root.exists():
            shutil.rmtree(extracted_root)
        extracted_root.mkdir(parents=True, exist_ok=True)
        extracted: list[PhotometryExtractedFile] = []
        used_names: set[str] = set()
        try:
            with zipfile.ZipFile(BytesIO(content)) as source:
                members = source.infolist()
                if len(members) > self._max_archive_members:
                    raise ValueError(
                        f"Photometry ZIP has more than {self._max_archive_members} members"
                    )
                declared_size = sum(member.file_size for member in members if not member.is_dir())
                if declared_size > self._max_extracted_total_bytes:
                    raise ValueError("Photometry ZIP exceeds the total 200 MB extraction limit")
                extracted_total = 0
                for member in members:
                    if member.is_dir():
                        continue
                    if member.flag_bits & 0x1:
                        raise ValueError(f"Photometry ZIP member {member.filename!r} is encrypted")
                    if member.file_size and member.compress_size:
                        ratio = member.file_size / member.compress_size
                        if ratio > self._max_compression_ratio:
                            raise ValueError(
                                f"Photometry ZIP member {member.filename!r} exceeds the compression ratio limit"
                            )
                    extension = Path(member.filename).suffix.lower()
                    file_type = self._photometry_extensions.get(extension)
                    if file_type is None:
                        continue
                    if member.file_size > self._max_extracted_file_bytes:
                        raise ValueError(f"Photometry file {member.filename!r} exceeds the 100 MB extraction limit")
                    filename = self._unique_filename(Path(member.filename).name, used_names)
                    target = extracted_root / filename
                    sha256, size = self._extract_member(source, member, target)
                    extracted_total += size
                    if extracted_total > self._max_extracted_total_bytes:
                        raise ValueError("Photometry ZIP exceeds the total 200 MB extraction limit")
                    extracted.append(
                        PhotometryExtractedFile(
                            relative_path=target.relative_to(root).as_posix(),
                            sha256=sha256,
                            size_bytes=size,
                            file_type=file_type,
                        )
                    )
                if not extracted:
                    raise ValueError("Photometry ZIP does not contain an IES, LDT or ULD file")
        except Exception:
            archive_path.unlink(missing_ok=True)
            if extracted_root.exists():
                shutil.rmtree(extracted_root)
            raise
        return archive_rel, extracted

    def _extract_member(self, source: zipfile.ZipFile, member: zipfile.ZipInfo, target: Path) -> tuple[str, int]:
        """Copy one trusted member with actual-byte limits and no in-memory expansion."""

        temporary = target.with_name(f".{target.name}.tmp")
        digest = hashlib.sha256()
        written = 0
        try:
            with source.open(member) as input_file, temporary.open("wb") as output_file:
                while chunk := input_file.read(self._copy_chunk_bytes):
                    written += len(chunk)
                    if written > self._max_extracted_file_bytes:
                        raise ValueError(
                            f"Photometry file {member.filename!r} exceeds the 100 MB extraction limit"
                        )
                    digest.update(chunk)
                    output_file.write(chunk)
            temporary.replace(target)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return digest.hexdigest(), written

    def _load(self, project_id: str) -> dict[str, PhotometryAsset]:
        manifest = self.directory(project_id) / self.manifest_name
        if not manifest.exists():
            return {}
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            records = payload.get("assets", {})
            if not isinstance(records, dict):
                return {}
            return {str(key): PhotometryAsset.model_validate(value) for key, value in records.items()}
        except (OSError, ValueError):
            return {}

    def _save(self, project_id: str, assets: dict[str, PhotometryAsset]) -> None:
        root = self.directory(project_id)
        root.mkdir(parents=True, exist_ok=True)
        payload = {
            "project_id": project_id,
            "updated_at": datetime.now(UTC).isoformat(),
            "assets": {key: value.model_dump(mode="json") for key, value in assets.items()},
        }
        self._atomic_write(root / self.manifest_name, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not project_id.isalnum():
            raise ValueError("project_id must be alphanumeric")

    @staticmethod
    def _luminaire(state: ProjectState, luminaire_id: str) -> LuminaireCandidate:
        for luminaire in state.luminaires:
            if luminaire.luminaire_id == luminaire_id:
                return luminaire
        raise KeyError(f"Luminaire {luminaire_id!r} is not in this project")

    @staticmethod
    def _asset_stem(article_name: str, luminaire_id: str) -> str:
        cleaned = "".join("_" if char in '<>:"/\\|?*' or ord(char) < 32 else char for char in article_name)
        cleaned = " ".join(cleaned.split()).strip(" ._")[:100]
        return f"{cleaned or 'luminaire'}-{luminaire_id[:8]}"

    @staticmethod
    def _unique_filename(name: str, used_names: set[str]) -> str:
        base = Path(name).stem or "photometry"
        suffix = Path(name).suffix.lower()
        candidate = f"{base}{suffix}"
        index = 2
        while candidate.casefold() in used_names:
            candidate = f"{base} ({index}){suffix}"
            index += 1
        used_names.add(candidate.casefold())
        return candidate

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_bytes(content)
        temporary.replace(target)

    @staticmethod
    def _safe_path(root: Path, relative_path: str) -> Path:
        candidate = (root / relative_path).resolve()
        root_resolved = root.resolve()
        if candidate != root_resolved and root_resolved not in candidate.parents:
            raise ValueError("Photometry asset path escapes the project directory")
        return candidate
