from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models import ModelRegistry
from app.schemas import ModelRegistryCreate, ModelRegistryResponse

router = APIRouter(prefix="/models", tags=["models"])


@router.get("/", response_model=list[ModelRegistryResponse])
def list_models(db: Session = Depends(get_db)):
    return db.query(ModelRegistry).order_by(ModelRegistry.model_name.asc()).all()


@router.post("/register", response_model=ModelRegistryResponse)
def register_model(data: ModelRegistryCreate, db: Session = Depends(get_db)):
    model = db.query(ModelRegistry).filter(ModelRegistry.model_name == data.model_name).first()
    if model:
        model.provider = data.provider
        model.model_type = data.model_type
        model.cost_per_1k_tokens = data.cost_per_1k_tokens
    else:
        model = ModelRegistry(
            model_name=data.model_name,
            provider=data.provider,
            model_type=data.model_type,
            cost_per_1k_tokens=data.cost_per_1k_tokens,
        )
        db.add(model)
    db.commit()
    db.refresh(model)
    return model
