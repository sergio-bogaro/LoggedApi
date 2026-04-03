from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, Boolean, func, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database import Base

if TYPE_CHECKING:
    from models.user import User


class CustomView(Base):
    """
    Visões customizadas do usuário para organizar medias
    Exemplos: "Assistindo Agora", "Meus Favoritos", "Para Ver Depois", "Animes de Ação", etc
    """
    __tablename__ = "custom_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    
    # Informações básicas da visão
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    icon: Mapped[str | None] = mapped_column(String(50), nullable=True)  # emoji ou nome do ícone
    color: Mapped[str | None] = mapped_column(String(20), nullable=True)  # cor em hex
    
    # Ordem de exibição
    order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    
    # Visibilidade
    is_visible: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)  # Fixar no topo
    
    # Filtros da visão (armazenado como JSON)
    # Exemplo: {"media_types": ["anime", "manga"], "status": ["in_progress"], "min_rating": 8}
    filters: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    
    # Configurações de exibição
    # Exemplo: {"view_mode": "grid", "sort_by": "rating", "sort_order": "desc"}
    display_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )
    
    # Relacionamento
    user: Mapped["User"] = relationship("User", back_populates="custom_views")

    def __repr__(self) -> str:
        return f"<CustomView(id={self.id}, name={self.name}, user_id={self.user_id})>"
