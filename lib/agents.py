"""
Agent Management and Runtime Enable/Disable Controls for Graviton.

Provides central tracking and state management for autonomous Graviton agents:
- code_reviewer (reviewer)
- code_fixer (fixer)
- issue_triager (triager)
- pr_drafter (drafter)
- codebase_auditor (auditor)

Supports CLI flags, environment variables, state persistence, and REST API controls.
Zero external dependencies (Python standard library only).
"""

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

logger = logging.getLogger("graviton.agents")

# Canonical agent names
AGENT_CODE_REVIEWER = "code_reviewer"
AGENT_CODE_FIXER = "code_fixer"
AGENT_ISSUE_TRIAGER = "issue_triager"
AGENT_PR_DRAFTER = "pr_drafter"
AGENT_CODEBASE_AUDITOR = "codebase_auditor"

CANONICAL_AGENTS: Tuple[str, ...] = (
    AGENT_CODE_REVIEWER,
    AGENT_CODE_FIXER,
    AGENT_ISSUE_TRIAGER,
    AGENT_PR_DRAFTER,
    AGENT_CODEBASE_AUDITOR,
)

# Role alias mappings (role -> canonical agent name)
ROLE_TO_AGENT: Dict[str, str] = {
    "reviewer": AGENT_CODE_REVIEWER,
    "code_reviewer": AGENT_CODE_REVIEWER,
    "code-reviewer": AGENT_CODE_REVIEWER,
    "fixer": AGENT_CODE_FIXER,
    "code_fixer": AGENT_CODE_FIXER,
    "code-fixer": AGENT_CODE_FIXER,
    "triager": AGENT_ISSUE_TRIAGER,
    "issue_triager": AGENT_ISSUE_TRIAGER,
    "issue-triager": AGENT_ISSUE_TRIAGER,
    "drafter": AGENT_PR_DRAFTER,
    "pr_drafter": AGENT_PR_DRAFTER,
    "pr-drafter": AGENT_PR_DRAFTER,
    "auditor": AGENT_CODEBASE_AUDITOR,
    "codebase_auditor": AGENT_CODEBASE_AUDITOR,
    "codebase-auditor": AGENT_CODEBASE_AUDITOR,
}

# Reverse mapping (canonical agent name -> canonical role)
AGENT_TO_ROLE: Dict[str, str] = {
    AGENT_CODE_REVIEWER: "reviewer",
    AGENT_CODE_FIXER: "fixer",
    AGENT_ISSUE_TRIAGER: "triager",
    AGENT_PR_DRAFTER: "drafter",
    AGENT_CODEBASE_AUDITOR: "auditor",
}


def canonicalize_agent_name(name_or_role: Optional[str]) -> Optional[str]:
    """
    Resolve any agent name or role alias to its canonical agent identifier.
    Returns None if input is empty or cannot be matched.

    >>> canonicalize_agent_name("reviewer")
    'code_reviewer'
    >>> canonicalize_agent_name("code-fixer")
    'code_fixer'
    >>> canonicalize_agent_name("unknown_agent")
    'unknown_agent'
    """
    if not name_or_role or not isinstance(name_or_role, str):
        return None
    cleaned = name_or_role.strip().lower().replace("-", "_")
    if not cleaned:
        return None
    if cleaned in ROLE_TO_AGENT:
        return ROLE_TO_AGENT[cleaned]
    # Check with hyphens replaced
    if cleaned.replace("_", "-") in ROLE_TO_AGENT:
        return ROLE_TO_AGENT[cleaned.replace("_", "-")]
    return cleaned


def agent_to_role(agent_name: Optional[str]) -> Optional[str]:
    """Return the short role name corresponding to a canonical agent name."""
    canonical = canonicalize_agent_name(agent_name)
    if not canonical:
        return None
    return AGENT_TO_ROLE.get(canonical, canonical)


def _parse_bool(val: Any) -> Optional[bool]:
    """Parse boolean from string or bool value."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("1", "true", "yes", "on", "enable", "enabled"):
            return True
        if s in ("0", "false", "no", "off", "disable", "disabled"):
            return False
    return None


def is_agent_enabled(agent: str, agent_manager: Optional[Any] = None) -> bool:
    """
    Check if an agent is enabled given an optional AgentManager, dict, or collection.
    If agent_manager is None, defaults to True.
    """
    if agent_manager is None or not agent:
        return True
    if hasattr(agent_manager, "is_enabled") and callable(agent_manager.is_enabled):
        return bool(agent_manager.is_enabled(agent))
    if isinstance(agent_manager, dict):
        if agent in agent_manager:
            return bool(agent_manager[agent])
        canonical = canonicalize_agent_name(agent)
        if canonical and canonical in agent_manager:
            return bool(agent_manager[canonical])
        role = agent_to_role(agent)
        if role and role in agent_manager:
            return bool(agent_manager[role])
        alt = agent.replace("-", "_") if "-" in agent else agent.replace("_", "-")
        if alt in agent_manager:
            return bool(agent_manager[alt])
        return True
    if isinstance(agent_manager, (set, list, tuple)):
        canonical = canonicalize_agent_name(agent)
        role = agent_to_role(agent)
        if canonical and canonical in agent_manager:
            return True
        if role and role in agent_manager:
            return True
        return agent in agent_manager or agent.replace("-", "_") in agent_manager
    return True


class AgentManager:
    """
    Thread-safe manager for tracking and toggling autonomous agent enabled states.

    Features:
    - Default state: all standard agents enabled (True)
    - Environment variable initialization
    - Optional state file persistence (JSON)
    - Role and canonical name resolution
    - Thread-safe updates and queries
    """

    def __init__(
        self,
        initial_states: Optional[Dict[str, bool]] = None,
        state_path: Optional[Union[Path, str]] = None,
        auto_load_env: bool = True,
    ):
        self._lock = threading.RLock()
        self.state_path = Path(state_path) if state_path else None
        self._states: Dict[str, bool] = {agent: True for agent in CANONICAL_AGENTS}

        # Apply initial states if provided
        if initial_states:
            self._apply_dict(initial_states)

        # Load persisted state from disk if available
        if self.state_path and self.state_path.exists():
            self.load_state()

        # Overlay environment variables if requested
        if auto_load_env:
            self._load_from_env()

    def _apply_dict(self, data: Dict[str, Any]) -> None:
        """Apply a dictionary of agent/role states (internal, assumes lock held or in init)."""
        for k, v in data.items():
            canonical = canonicalize_agent_name(k)
            b_val = _parse_bool(v)
            if canonical and canonical in self._states and b_val is not None:
                self._states[canonical] = b_val

    def _load_from_env(self) -> None:
        """Read configuration from environment variables."""
        with self._lock:
            # Check individual enable/disable vars: GRAVITON_ENABLE_<ROLE> or GRAVITON_DISABLE_<ROLE>
            for agent in CANONICAL_AGENTS:
                role = AGENT_TO_ROLE[agent]
                # Check GRAVITON_ENABLE_<ROLE> and GRAVITON_ENABLE_<AGENT>
                for prefix in (f"GRAVITON_ENABLE_{role.upper()}", f"GRAVITON_ENABLE_{agent.upper()}"):
                    val = os.getenv(prefix)
                    if val is not None:
                        b = _parse_bool(val)
                        if b is not None:
                            self._states[agent] = b

                # Check GRAVITON_DISABLE_<ROLE> and GRAVITON_DISABLE_<AGENT>
                for prefix in (f"GRAVITON_DISABLE_{role.upper()}", f"GRAVITON_DISABLE_{agent.upper()}"):
                    val = os.getenv(prefix)
                    if val is not None:
                        b = _parse_bool(val)
                        if b is not None:
                            self._states[agent] = not b

            # Check batch lists: GRAVITON_ENABLED_AGENTS, GRAVITON_DISABLED_AGENTS
            enabled_list = os.getenv("GRAVITON_ENABLED_AGENTS")
            if enabled_list:
                for item in enabled_list.split(","):
                    c = canonicalize_agent_name(item.strip())
                    if c and c in self._states:
                        self._states[c] = True

            disabled_list = os.getenv("GRAVITON_DISABLED_AGENTS")
            if disabled_list:
                for item in disabled_list.split(","):
                    c = canonicalize_agent_name(item.strip())
                    if c and c in self._states:
                        self._states[c] = False

    def is_enabled(self, name_or_role: str) -> bool:
        """
        Check if an agent is currently enabled.
        Unknown agents default to True.
        """
        canonical = canonicalize_agent_name(name_or_role)
        if not canonical:
            return True
        with self._lock:
            return self._states.get(canonical, True)

    def set_enabled(self, name_or_role: str, enabled: Any) -> bool:
        """
        Set an agent's enabled state.
        Returns True if the state was updated, False if agent is unrecognized.
        """
        canonical = canonicalize_agent_name(name_or_role)
        if not canonical or canonical not in self._states:
            return False
        b_val = _parse_bool(enabled)
        bool_val = bool(enabled) if b_val is None else b_val
        with self._lock:
            old_val = self._states.get(canonical)
            self._states[canonical] = bool_val
            changed = old_val != bool_val
        if changed:
            logger.info(f"Agent '{canonical}' enabled state set to: {bool_val}")
            self.save_state()
        return True

    def enable(self, name_or_role: str) -> bool:
        """Convenience method to enable an agent."""
        return self.set_enabled(name_or_role, True)

    def disable(self, name_or_role: str) -> bool:
        """Convenience method to disable an agent."""
        return self.set_enabled(name_or_role, False)

    def toggle(self, name_or_role: str) -> Optional[bool]:
        """
        Toggle an agent's enabled state and return the new state.
        Returns None if agent is unrecognized.
        """
        canonical = canonicalize_agent_name(name_or_role)
        if not canonical or canonical not in self._states:
            return None
        with self._lock:
            current = self._states.get(canonical, True)
            new_val = not current
            self._states[canonical] = new_val
        logger.info(f"Agent '{canonical}' toggled to: {new_val}")
        self.save_state()
        return new_val

    def get_status(self) -> Dict[str, bool]:
        """Return a copy of the canonical agent names to boolean enabled states."""
        with self._lock:
            return dict(self._states)

    def get_roles_status(self) -> Dict[str, bool]:
        """Return a copy of the short role names to boolean enabled states."""
        with self._lock:
            return {
                AGENT_TO_ROLE.get(agent, agent): enabled
                for agent, enabled in self._states.items()
            }

    def get_summary(self) -> Dict[str, Any]:
        """Return a comprehensive dictionary summary of agent statuses."""
        with self._lock:
            agents = dict(self._states)
            roles = {AGENT_TO_ROLE.get(a, a): val for a, val in agents.items()}
            all_enabled = all(agents.values())
            any_disabled = any(not v for v in agents.values())
            disabled_agents = [a for a, v in agents.items() if not v]
            enabled_agents = [a for a, v in agents.items() if v]

        return {
            "agents": agents,
            "roles": roles,
            "all_enabled": all_enabled,
            "any_disabled": any_disabled,
            "enabled_count": len(enabled_agents),
            "disabled_count": len(disabled_agents),
            "enabled_agents": enabled_agents,
            "disabled_agents": disabled_agents,
        }

    def update_from_dict(self, updates: Dict[str, Any]) -> Dict[str, bool]:
        """
        Update multiple agent states simultaneously from a dictionary.
        Keys can be canonical agent names or role aliases.
        """
        changed = False
        with self._lock:
            for k, v in updates.items():
                canonical = canonicalize_agent_name(k)
                if not canonical or canonical not in self._states:
                    continue
                b_val = _parse_bool(v)
                if b_val is not None:
                    if self._states.get(canonical) != b_val:
                        self._states[canonical] = b_val
                        changed = True
                        logger.info(f"Agent '{canonical}' updated to: {b_val}")

        if changed:
            self.save_state()
        return self.get_status()

    def save_state(self) -> None:
        """Persist current agent states to state_path atomically."""
        if not self.state_path:
            return
        with self._lock:
            data = dict(self._states)

        tmp_path = self.state_path.with_suffix(".tmp")
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.state_path)
            logger.debug(f"Saved agent states to {self.state_path}")
        except Exception as e:
            logger.error(f"Failed to persist agent states to {self.state_path}: {e}")
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    def load_state(self) -> bool:
        """Load agent states from state_path."""
        if not self.state_path or not self.state_path.exists():
            return False
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with self._lock:
                    self._apply_dict(data)
                logger.info(f"Loaded agent states from {self.state_path}")
                return True
        except Exception as e:
            logger.error(f"Failed to load agent states from {self.state_path}: {e}")
        return False
