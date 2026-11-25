"""
Configuration Validator
========================
Validates SharePoint and Google Drive configurations for sync operations.
Ensures paths, URLs, and required fields are valid before persistence or sync.
"""

from __future__ import annotations

import os
import re


def _nearest_existing_ancestor(path: str) -> str:
    cur = os.path.abspath(path)
    while cur and not os.path.exists(cur):
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return cur


class ConfigValidator:
    """Validates sync configurations."""

    @staticmethod
    def _validate_local_path(raw_path: str, entry_type: str, errors: list, warnings: list) -> None:
        raw = str(raw_path or "").strip()
        if not raw:
            errors.append("local_path is required")
            return
        local_path = os.path.abspath(os.path.expanduser(raw))
        if entry_type == "folder" and os.path.isfile(local_path):
            errors.append(f"local_path points to an existing file, expected folder: {local_path}")
            return
        parent_dir = os.path.dirname(local_path)
        if not os.path.exists(parent_dir):
            warnings.append(f"Parent directory does not exist yet: {parent_dir}")
            anc = _nearest_existing_ancestor(parent_dir)
            if anc and not os.access(anc, os.W_OK):
                errors.append(f"No write permission for: {anc}")
        elif not os.access(parent_dir, os.W_OK):
            errors.append(f"No write permission for: {parent_dir}")
        elif os.path.isdir(local_path) and not os.access(local_path, os.W_OK):
            errors.append(f"No write permission for: {local_path}")

        if any(ord(c) > 127 for c in local_path) and os.name == "nt":
            warnings.append("Path contains non-ASCII characters — verify Windows long-path support")

    @staticmethod
    def validate_sharepoint_config(config):
        """Validate a single SharePoint configuration."""
        errors = []
        warnings = []

        required_fields = ["name", "rel_url", "local_path"]
        for field in required_fields:
            if field not in config or not str(config[field] or "").strip():
                errors.append(f"Field '{field}' is required")

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings}

        raw_et = config.get("entry_type")
        et = "folder"
        if raw_et is not None and str(raw_et).strip() != "":
            et = str(raw_et).strip().lower()
            if et not in ("folder", "file"):
                errors.append("'entry_type' must be 'folder' or 'file'")

        name = str(config["name"]).strip()
        if len(name) < 3:
            warnings.append("Folder name is very short")

        rel_url = str(config["rel_url"]).strip()
        if not rel_url.startswith("/"):
            errors.append("rel_url must start with '/'")
        elif not any(seg in rel_url.lower() for seg in ("personal", "sites/", "teams/")):
            warnings.append(
                "rel_url does not contain 'personal', 'sites/', or 'teams/' — verify it is a valid SharePoint path"
            )

        ConfigValidator._validate_local_path(config["local_path"], et, errors, warnings)

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    @staticmethod
    def validate_gdrive_config(config):
        """Validate a single Google Drive configuration."""
        errors = []
        warnings = []

        required_fields = ["name", "folder_url", "folder_id", "local_path"]
        for field in required_fields:
            if field not in config or not str(config[field] or "").strip():
                errors.append(f"Field '{field}' is required")

        if errors:
            return {"valid": False, "errors": errors, "warnings": warnings}

        name = str(config["name"]).strip()
        if len(name) < 3:
            warnings.append("Folder name is very short")

        folder_url = str(config["folder_url"]).strip()
        # Accept standard /drive/folders/, /drive/u/N/folders/, /drive/mobile/folders/, /open?id=, /file/d/, or raw ID
        valid_url = bool(
            re.match(
                r"^https?://drive\.google\.com/(?:drive/(?:u/\d+/|mobile/)?folders/[a-zA-Z0-9_-]+|open\?.*id=[a-zA-Z0-9_-]+|file/d/[a-zA-Z0-9_-]+)",
                folder_url,
            )
            or re.match(r"^[a-zA-Z0-9_-]{10,}$", folder_url)
        )
        if not valid_url:
            errors.append("Google Drive URL is invalid")

        folder_id = str(config["folder_id"]).strip()
        if not re.match(r"^[a-zA-Z0-9_-]+$", folder_id):
            errors.append("folder_id is invalid")

        ConfigValidator._validate_local_path(config["local_path"], "folder", errors, warnings)

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    @staticmethod
    def validate_all_configs(sp_configs, gdrive_configs):
        """Validate all configurations."""
        results = {
            "sharepoint": {"configs": [], "total_errors": 0, "total_warnings": 0},
            "gdrive": {"configs": [], "total_errors": 0, "total_warnings": 0},
        }

        for i, config in enumerate(sp_configs):
            validation = ConfigValidator.validate_sharepoint_config(config)
            validation["index"] = i
            validation["config"] = config
            results["sharepoint"]["configs"].append(validation)
            results["sharepoint"]["total_errors"] += len(validation["errors"])
            results["sharepoint"]["total_warnings"] += len(validation["warnings"])

        for i, config in enumerate(gdrive_configs):
            validation = ConfigValidator.validate_gdrive_config(config)
            validation["index"] = i
            validation["config"] = config
            results["gdrive"]["configs"].append(validation)
            results["gdrive"]["total_errors"] += len(validation["errors"])
            results["gdrive"]["total_warnings"] += len(validation["warnings"])

        results["overall_valid"] = (
            results["sharepoint"]["total_errors"] == 0
            and results["gdrive"]["total_errors"] == 0
        )

        return results

    @staticmethod
    def fix_common_issues(config, config_type="sharepoint", *, create_dirs: bool = False):
        """Normalize common configuration fields (does not silently create directories unless requested)."""
        if config_type == "sharepoint":
            raw_et = config.get("entry_type")
            if raw_et is None or str(raw_et).strip() == "":
                config["entry_type"] = "folder"
            else:
                et = str(raw_et).strip().lower()
                config["entry_type"] = et if et in ("folder", "file") else "folder"
            if config.get("entry_type") == "file":
                config.pop("local_flat", None)
            lf = config.get("local_flat")
            if lf is not None and str(lf).strip() != "":
                if isinstance(lf, str):
                    config["local_flat"] = lf.strip().lower() in ("1", "true", "yes")
                else:
                    config["local_flat"] = bool(lf)
            elif "local_flat" in config and (
                config.get("local_flat") is None or str(config.get("local_flat")).strip() == ""
            ):
                config.pop("local_flat", None)
            if "local_path" in config and config["local_path"]:
                config["local_path"] = os.path.abspath(
                    os.path.expanduser(str(config["local_path"]).strip())
                )
            if "rel_url" in config and config["rel_url"]:
                rel_url = str(config["rel_url"]).strip()
                if not rel_url.startswith("/"):
                    rel_url = "/" + rel_url
                config["rel_url"] = rel_url

            if create_dirs and "local_path" in config and config["local_path"]:
                local_path = config["local_path"]
                try:
                    if config.get("entry_type") == "file":
                        parent = os.path.dirname(local_path)
                        if parent:
                            os.makedirs(parent, exist_ok=True)
                    else:
                        os.makedirs(local_path, exist_ok=True)
                except OSError:
                    pass

        elif config_type == "gdrive":
            if "local_path" in config and config["local_path"]:
                config["local_path"] = os.path.abspath(
                    os.path.expanduser(str(config["local_path"]).strip())
                )
            if "folder_url" in config and config["folder_url"] and not config.get("folder_id"):
                from sp_sync.gdrive.service import extract_folder_id

                fid = extract_folder_id(config["folder_url"])
                if fid:
                    config["folder_id"] = fid

            if create_dirs and "local_path" in config and config["local_path"]:
                try:
                    os.makedirs(config["local_path"], exist_ok=True)
                except OSError:
                    pass

        return config


def validate_and_fix_configs(store=None):
    """Validate configurations loaded from the SQLite app store."""
    from sp_sync.db.store import get_store

    store = store or get_store()
    sp_configs = store.get_sharepoint_configs()
    gdrive_configs = store.get_gdrive_configs()
    validation = ConfigValidator.validate_all_configs(sp_configs, gdrive_configs)
    return {
        "sharepoint": {
            "storage": "sqlite",
            "loaded": True,
            "configs": sp_configs,
            "validation": validation["sharepoint"],
        },
        "gdrive": {
            "storage": "sqlite",
            "loaded": True,
            "configs": gdrive_configs,
            "validation": validation["gdrive"],
        },
        "validation": validation,
    }
