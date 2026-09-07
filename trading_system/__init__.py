"""Trading system foundation (T-001).

Hard-isolated from the retired Command Center: separate repository,
separate Supabase project, separate deployment, separate secrets. Nothing
here imports, references, or connects to that system.
"""
__all__ = ["env", "db", "system_state", "provenance", "health"]
