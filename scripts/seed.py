"""Seed the plans and a demo tenant.

Safe to run more than once: the evaluator runs whatever `seed:` points at in
capstone.yaml, and a seed that crashes on a second run is a seed that fails
the moment someone re-runs the setup.
"""

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Plan, Tenant

# Quotas only. Prices are pinned in app/config.py — see DESIGN.md section 2.3.
PLANS = [
    {"code": "free", "api_call_limit": 1_000, "token_limit": 100_000},
    {"code": "pro", "api_call_limit": 50_000, "token_limit": 5_000_000},
]

DEMO_TENANT_NAME = "demo"


def main() -> None:
    with SessionLocal() as session:
        for spec in PLANS:
            plan = session.get(Plan, spec["code"])
            if plan is None:
                session.add(Plan(**spec))
            else:
                plan.api_call_limit = spec["api_call_limit"]
                plan.token_limit = spec["token_limit"]

        tenant = session.scalar(
            select(Tenant).where(Tenant.name == DEMO_TENANT_NAME)
        )
        if tenant is None:
            tenant = Tenant(name=DEMO_TENANT_NAME, plan_code="free")
            session.add(tenant)

        session.commit()

        print(f"plans seeded: {', '.join(s['code'] for s in PLANS)}")
        print(f"demo tenant: {tenant.id} (plan={tenant.plan_code})")


if __name__ == "__main__":
    main()