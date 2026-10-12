"""
Project discovery and workspace trust helpers for Antigravity and Graviton.
"""

import json
import logging
import os
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Optional, Tuple, Union

logger = logging.getLogger("graviton.projects")

DEFAULT_PROJECT_NAME = "Graviton Workers"

__all__ = [
    "DEFAULT_PROJECT_NAME",
    "_extract_uri",
    "ensure_default_project",
    "ensure_workspace_trusted",
    "find_project_for_repo",
]


def _extract_uri(res_item: Any) -> Optional[str]:
    """Safely extract folderUri from a resource item dict or nested gitFolder dict."""
    if not isinstance(res_item, dict):
        return None
    if isinstance(res_item.get("folderUri"), str):
        return res_item["folderUri"]
    gf = res_item.get("gitFolder")
    if isinstance(gf, dict) and isinstance(gf.get("folderUri"), str):
        return gf["folderUri"]
    return None


def ensure_default_project(
    projects_dir: Path,
    repo_path: Optional[Path] = None,
    name: str = DEFAULT_PROJECT_NAME,
) -> Tuple[str, str]:
    """
    Ensures a project definition exists for the given name in projects_dir,
    creating it if not present. Returns (project_id, project_name).
    """
    if not projects_dir.exists():
        try:
            projects_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    for p in projects_dir.glob("*.json"):
        if p.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            p_name = str(data.get("name") or "")
            p_id = str(data.get("id") or p.stem)
            if p_name.strip().lower() == name.lower() or p_id == name:
                proj_res = data.get("projectResources")
                if not isinstance(proj_res, dict):
                    proj_res = {}
                    data["projectResources"] = proj_res
                res_list = proj_res.get("resources")
                if not isinstance(res_list, list):
                    res_list = []
                    proj_res["resources"] = res_list
                updated = False
                if repo_path:
                    target_uri = f"file://{repo_path.resolve()}"
                    has_uri = any(_extract_uri(r) == target_uri for r in res_list)
                    if not has_uri:
                        res_list.append({
                            "gitFolder": {
                                "folderUri": target_uri,
                                "defaultBranch": "main",
                            }
                        })
                        updated = True

                # Always ensure container workspace is registered for sandbox execution
                workspace_uri = "file:///workspace"
                has_workspace = any(_extract_uri(r) == workspace_uri for r in res_list)
                if not has_workspace:
                    res_list.append({
                        "gitFolder": {
                            "folderUri": workspace_uri,
                            "defaultBranch": "main",
                        }
                    })
                    updated = True

                if updated:
                    try:
                        p.write_text(json.dumps(data, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                return (p_id, p_name)
        except Exception:
            continue

    # Deterministic UUID for the project
    proj_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"graviton-{name.lower().replace(' ', '-')}"))
    proj_file = projects_dir / f"{proj_id}.json"
    resources = [
        {
            "gitFolder": {
                "folderUri": "file:///workspace",
                "defaultBranch": "main",
            }
        }
    ]
    if repo_path:
        target_uri = f"file://{repo_path.resolve()}"
        if target_uri != "file:///workspace":
            resources.append({
                "gitFolder": {
                    "folderUri": target_uri,
                    "defaultBranch": "main",
                }
            })
    proj_data = {
        "id": proj_id,
        "name": name,
        "projectResources": {"resources": resources},
        "settings": {},
        "isWorkspaceOnly": False,
    }
    try:
        proj_file.write_text(json.dumps(proj_data, indent=2), encoding="utf-8")
        logger.info(f"Auto-created default project '{name}' ({proj_id}) at {proj_file}")
    except Exception as e:
        logger.debug(f"Could not auto-create project file {proj_file}: {e}")
    return (proj_id, name)


def ensure_workspace_trusted(cli_dir: Optional[Union[str, Path]] = None) -> None:
    """Ensure /workspace is explicitly in trustedWorkspaces in settings.json atomically."""
    base = Path(cli_dir) if cli_dir else (Path.home() / ".gemini" / "antigravity-cli")
    settings_file = base / "settings.json"
    try:
        base.mkdir(parents=True, exist_ok=True)
        if settings_file.is_file():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        else:
            data = {}
        if not isinstance(data, dict):
            data = {}
        tw = data.setdefault("trustedWorkspaces", [])
        if not isinstance(tw, list):
            tw = []
            data["trustedWorkspaces"] = tw
        if "/workspace" not in tw:
            tw.append("/workspace")
            tmp_file = settings_file.with_name(f".{settings_file.name}.tmp_{uuid.uuid4().hex}")
            tmp_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_file.replace(settings_file)
    except Exception as e:
        logger.debug(f"Failed to ensure /workspace in settings.json: {e}")


def find_project_for_repo(
    repo_dir: Union[str, Path],
    config_dir: Optional[Union[str, Path]] = None,
    preferred_name_or_id: Optional[str] = None,
) -> Optional[Tuple[str, str]]:
    """
    Looks in ~/.gemini/config/projects/ to find a project JSON file.
    Prefers:
    1. Explicit preferred_name_or_id if provided.
    2. ANTIGRAVITY_PROJECT or GRAVITON_PROJECT_ID environment variable if set.
    3. Project named DEFAULT_PROJECT_NAME ("Graviton Workers") if it exists, or auto-created.
    4. Project matching repo_dir.
    Returns (project_id, project_name) or None.
    """
    repo_path = Path(repo_dir).resolve()
    projects_dir = Path(config_dir) if config_dir else (Path.home() / ".gemini" / "config" / "projects")
    default_sys_dir = (Path.home() / ".gemini" / "config" / "projects").resolve()
    if not projects_dir.is_dir():
        if projects_dir.resolve() == default_sys_dir:
            return ensure_default_project(projects_dir, repo_path, DEFAULT_PROJECT_NAME)
        return None

    explicit_pref = (
        preferred_name_or_id
        or os.environ.get("ANTIGRAVITY_PROJECT")
        or os.environ.get("GRAVITON_PROJECT_ID")
    )
    if explicit_pref:
        explicit_pref = explicit_pref.strip()

    match_by_repo = None
    match_default_worker = None

    for p in projects_dir.glob("*.json"):
        if p.name in ("outside-of-project.json", "default-cli-project.json"):
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            project_id = str(data.get("id") or p.stem)
            project_name = str(data.get("name") or project_id)

            if explicit_pref and (explicit_pref == project_id or explicit_pref.lower() == project_name.lower()):
                return (project_id, project_name)

            if project_name.strip().lower() == DEFAULT_PROJECT_NAME.lower():
                match_default_worker = (project_id, project_name)

            proj_res = data.get("projectResources")
            resources = proj_res.get("resources", []) if isinstance(proj_res, dict) and isinstance(proj_res.get("resources"), list) else []
            for r in resources:
                folder_uri = _extract_uri(r)
                if folder_uri:
                    parsed_path = urllib.parse.unquote(urllib.parse.urlparse(folder_uri).path)
                    if parsed_path:
                        folder_path = Path(parsed_path).resolve()
                        if folder_path == repo_path or repo_path.is_relative_to(folder_path):
                            if not match_by_repo:
                                match_by_repo = (project_id, project_name)
        except Exception:
            continue

    if match_by_repo:
        return ensure_default_project(projects_dir, repo_path, match_by_repo[1])

    if match_default_worker:
        return ensure_default_project(projects_dir, repo_path, match_default_worker[1])

    # If config_dir is not custom (i.e. default system config dir), auto-ensure DEFAULT_PROJECT_NAME
    default_sys_dir = (Path.home() / ".gemini" / "config" / "projects").resolve()
    if projects_dir.resolve() == default_sys_dir:
        return ensure_default_project(projects_dir, repo_path, DEFAULT_PROJECT_NAME)

    return None
