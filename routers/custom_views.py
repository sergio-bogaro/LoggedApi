from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from schemas.custom_view import CustomViewCreate, CustomViewUpdate, CustomViewResponse, CustomViewReorder
from services.custom_view_service import CustomViewService

router = APIRouter(prefix="/custom-views", tags=["Custom Views"])


@router.get("/user/{user_id}", response_model=list[CustomViewResponse])
def get_user_views(user_id: int, db: Session = Depends(get_db)):
    """Busca todas as visões customizadas de um usuário"""
    views = CustomViewService.get_user_views(db, user_id)
    return views


@router.get("/{view_id}", response_model=CustomViewResponse)
def get_view(view_id: int, user_id: int, db: Session = Depends(get_db)):
    """Busca uma visão específica"""
    view = CustomViewService.get_view_by_id(db, view_id, user_id)
    return view


@router.post("/", response_model=CustomViewResponse, status_code=status.HTTP_201_CREATED)
def create_view(view_data: CustomViewCreate, db: Session = Depends(get_db)):
    """Cria uma nova visão customizada"""
    view = CustomViewService.create_view(db, view_data)
    return view


@router.put("/{view_id}", response_model=CustomViewResponse)
def update_view(view_id: int, user_id: int, view_data: CustomViewUpdate, db: Session = Depends(get_db)):
    """Atualiza uma visão customizada"""
    view = CustomViewService.update_view(db, view_id, user_id, view_data)
    return view


@router.delete("/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_view(view_id: int, user_id: int, db: Session = Depends(get_db)):
    """Deleta uma visão customizada"""
    CustomViewService.delete_view(db, view_id, user_id)
    return None


@router.post("/user/{user_id}/reorder", response_model=list[CustomViewResponse])
def reorder_views(user_id: int, reorder_data: list[CustomViewReorder], db: Session = Depends(get_db)):
    """Reordena as visões de um usuário"""
    reorder_list = [item.model_dump() for item in reorder_data]
    views = CustomViewService.reorder_views(db, user_id, reorder_list)
    return views


@router.post("/user/{user_id}/default", response_model=list[CustomViewResponse], status_code=status.HTTP_201_CREATED)
def create_default_views(user_id: int, db: Session = Depends(get_db)):
    """Cria visões padrão para um usuário"""
    views = CustomViewService.create_default_views(db, user_id)
    return views
