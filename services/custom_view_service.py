from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from models.custom_view import CustomView
from schemas.custom_view import CustomViewCreate, CustomViewUpdate


class CustomViewService:
    @staticmethod
    def get_user_views(db: Session, user_id: int) -> list[CustomView]:
        """Busca todas as visões de um usuário"""
        return db.query(CustomView).filter(
            CustomView.user_id == user_id
        ).order_by(CustomView.order, CustomView.created_at).all()

    @staticmethod
    def get_view_by_id(db: Session, view_id: int, user_id: int) -> CustomView:
        """Busca uma visão específica"""
        view = db.query(CustomView).filter(
            CustomView.id == view_id,
            CustomView.user_id == user_id
        ).first()
        
        if not view:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Custom view not found"
            )
        
        return view

    @staticmethod
    def create_view(db: Session, view_data: CustomViewCreate) -> CustomView:
        """Cria uma nova visão"""
        # Se não foi especificada uma ordem, coloca no final
        if view_data.order == 0:
            max_order = db.query(CustomView).filter(
                CustomView.user_id == view_data.user_id
            ).count()
            view_data.order = max_order
        
        new_view = CustomView(**view_data.model_dump())
        db.add(new_view)
        db.commit()
        db.refresh(new_view)
        
        return new_view

    @staticmethod
    def update_view(db: Session, view_id: int, user_id: int, view_data: CustomViewUpdate) -> CustomView:
        """Atualiza uma visão"""
        view = CustomViewService.get_view_by_id(db, view_id, user_id)
        
        update_data = view_data.model_dump(exclude_unset=True)
        
        for field, value in update_data.items():
            setattr(view, field, value)
        
        db.commit()
        db.refresh(view)
        
        return view

    @staticmethod
    def delete_view(db: Session, view_id: int, user_id: int) -> None:
        """Deleta uma visão"""
        view = CustomViewService.get_view_by_id(db, view_id, user_id)
        db.delete(view)
        db.commit()

    @staticmethod
    def reorder_views(db: Session, user_id: int, reorder_data: list[dict]) -> list[CustomView]:
        """Reordena as visões do usuário"""
        for item in reorder_data:
            view = CustomViewService.get_view_by_id(db, item["view_id"], user_id)
            view.order = item["new_order"]
        
        db.commit()
        
        return CustomViewService.get_user_views(db, user_id)

    @staticmethod
    def create_default_views(db: Session, user_id: int) -> list[CustomView]:
        """Cria visões padrão para um novo usuário"""
        default_views = [
            {
                "name": "Watching Now",
                "description": "Media I'm currently watching/reading/playing",
                "icon": "▶️",
                "color": "#3b82f6",
                "order": 0,
                "is_pinned": True,
                "filters": {"status": ["in_progress", "following"]},
                "display_settings": {"view_mode": "grid", "sort_by": "updated_at", "sort_order": "desc"}
            },
            {
                "name": "Favorites",
                "description": "My favorite media",
                "icon": "⭐",
                "color": "#f59e0b",
                "order": 1,
                "filters": {"min_rating": 9.0},
                "display_settings": {"view_mode": "grid", "sort_by": "rating", "sort_order": "desc"}
            },
            {
                "name": "To Watch Later",
                "description": "Media in my watchlist",
                "icon": "📌",
                "color": "#8b5cf6",
                "order": 2,
                "filters": {"on_list": True},
                "display_settings": {"view_mode": "list", "sort_by": "created_at", "sort_order": "desc"}
            },
            {
                "name": "Recently Added",
                "description": "Recently added media",
                "icon": "🆕",
                "color": "#10b981",
                "order": 3,
                "filters": {},
                "display_settings": {"view_mode": "grid", "sort_by": "created_at", "sort_order": "desc"}
            }
        ]
        
        created_views = []
        for view_data in default_views:
            view = CustomView(user_id=user_id, **view_data)
            db.add(view)
            created_views.append(view)
        
        db.commit()
        
        for view in created_views:
            db.refresh(view)
        
        return created_views
