from sqlalchemy.orm import Session


def active_orgs_subq(db: Session):
    """Subquery of org IDs that still exist in the organizations table.

    Apply this whenever org_id is not explicitly filtered so that data
    belonging to deleted organizations is excluded from UI responses.
    The underlying rows stay in the database — only the API visibility
    is restricted.
    """
    from app.models import Organization
    return db.query(Organization.id).subquery()
