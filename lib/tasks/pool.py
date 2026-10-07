"""
Model and pool resolver for Graviton tasks based on QuotaTracker state.
"""

from typing import Any, Optional, Tuple
from lib.quota import QuotaState, DEFAULT_GEMINI_MODELS, DEFAULT_THIRD_PARTY_MODELS

def resolve_task_pool_and_model(quota_tracker: Optional[Any] = None) -> Tuple[str, str, bool]:
    """
    Evaluates quota tracker pool states, pacing, and remaining percentages to determine
    the target pool ('gemini' or 'claude_gpt'), the active model to execute with,
    and whether all pools are currently exhausted/behind pacing.

    Returns:
        Tuple of (selected_pool, selected_model, all_exhausted)
    """
    selected_pool = "gemini"
    selected_model = DEFAULT_GEMINI_MODELS[0]
    gemini_eligible = True
    claude_eligible = True

    if quota_tracker:
        gemini_behind = (
            quota_tracker.is_pool_behind_pacing("gemini") is True
            if hasattr(quota_tracker, "is_pool_behind_pacing")
            else (quota_tracker.is_behind_pacing() is True)
        )
        gemini_state = (
            quota_tracker.get_pool_state("gemini")
            if hasattr(quota_tracker, "get_pool_state")
            else quota_tracker.state
        )
        if not isinstance(gemini_state, str):
            gemini_state = QuotaState.NORMAL

        gemini_pct = (
            quota_tracker.get_pool_remaining_percentage("gemini")
            if hasattr(quota_tracker, "get_pool_remaining_percentage")
            else quota_tracker.remaining_percentage
        )
        if not isinstance(gemini_pct, (int, float)):
            gemini_pct = 100.0

        claude_behind = (
            quota_tracker.is_pool_behind_pacing("claude_gpt") is True
            if hasattr(quota_tracker, "is_pool_behind_pacing")
            else False
        )
        claude_state = (
            quota_tracker.get_pool_state("claude_gpt")
            if hasattr(quota_tracker, "get_pool_state")
            else QuotaState.EXHAUSTED
        )
        if not isinstance(claude_state, str):
            claude_state = QuotaState.NORMAL

        claude_pct = (
            quota_tracker.get_pool_remaining_percentage("claude_gpt")
            if hasattr(quota_tracker, "get_pool_remaining_percentage")
            else 0.0
        )
        if not isinstance(claude_pct, (int, float)):
            claude_pct = 100.0

        gemini_eligible = (gemini_state != QuotaState.EXHAUSTED) and (not gemini_behind)
        claude_eligible = (claude_state != QuotaState.EXHAUSTED) and (not claude_behind)

        if gemini_eligible and claude_eligible:
            if claude_pct > gemini_pct:
                selected_pool = "claude_gpt"
            elif gemini_pct > claude_pct:
                selected_pool = "gemini"
            else:
                pref = getattr(quota_tracker, "quota_pool", "gemini")
                if isinstance(pref, str) and any(k in pref.lower() for k in ("claude", "gpt", "3p", "third")):
                    selected_pool = "claude_gpt"
                else:
                    selected_pool = "gemini"
        elif claude_eligible:
            selected_pool = "claude_gpt"
        else:
            selected_pool = "gemini"

        if hasattr(quota_tracker, "get_active_model"):
            m_val = quota_tracker.get_active_model(selected_pool)
            selected_model = m_val if isinstance(m_val, str) else (DEFAULT_GEMINI_MODELS[0] if selected_pool == "gemini" else DEFAULT_THIRD_PARTY_MODELS[0])
        else:
            selected_model = (
                getattr(quota_tracker, "active_gemini_model", DEFAULT_GEMINI_MODELS[0])
                if selected_pool == "gemini"
                else getattr(quota_tracker, "active_third_party_model", DEFAULT_THIRD_PARTY_MODELS[0])
            )

    all_exhausted = not gemini_eligible and not claude_eligible
    return (selected_pool, selected_model, all_exhausted)




