"""The dashboard's API surface.

One aggregate router so ``backend.main`` mounts a single object and adding an
endpoint never means touching the app factory.
"""

from fastapi import APIRouter

from backend.api.routers.core import (
    answers_router,
    campaigns_router,
    control_router,
    preferences_router,
    profile_router,
    settings_router,
    templates_router,
)
from backend.api.routers.work import (
    analytics_router,
    applications_router,
    documents_router,
    jobs_router,
    queue_router,
)

__all__ = ["api_router", "router"]

api_router = APIRouter(prefix="/api")

for _router in (
    profile_router,
    campaigns_router,
    control_router,
    preferences_router,
    answers_router,
    templates_router,
    settings_router,
    jobs_router,
    queue_router,
    applications_router,
    analytics_router,
    documents_router,
):
    api_router.include_router(_router)

#: ``backend.main._register_routers`` looks for a module-level ``router``.
#: Exposed under both names so the aggregate reads clearly at its call sites
#: while still satisfying that contract.
router = api_router
