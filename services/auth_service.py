from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from models.user import User
from schemas.user import UserCreate, UserLogin, UserUpdate
from services.custom_view_service import CustomViewService


class AuthService:
    @staticmethod
    def create_user(db: Session, user_data: UserCreate) -> User:
        """Cria um novo usuário"""
        # Verifica se o username já existe
        existing_user = db.query(User).filter(User.username == user_data.username).first()
        if existing_user:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Username already registered"
            )
        
        # Cria o usuário
        new_user = User(
            username=user_data.username,
            password=user_data.password
        )
        
        db.add(new_user)
        db.commit()
        db.refresh(new_user)
        
        # Cria visões padrão para o novo usuário
        CustomViewService.create_default_views(db, new_user.id)
        
        return new_user

    @staticmethod
    def authenticate_user(db: Session, login_data: UserLogin) -> User:
        """Autentica um usuário"""
        user = db.query(User).filter(User.username == login_data.username).first()
        
        if not user or user.password != login_data.password:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password"
            )
        
        return user

    @staticmethod
    def get_user_by_id(db: Session, user_id: int) -> User:
        """Busca um usuário por ID"""
        user = db.query(User).filter(User.id == user_id).first()
        
        if not user:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="User not found"
            )
        
        return user

    @staticmethod
    def update_user(db: Session, user_id: int, user_data: UserUpdate) -> User:
        """Atualiza um usuário"""
        user = AuthService.get_user_by_id(db, user_id)
        
        # Atualiza apenas os campos fornecidos
        update_data = user_data.model_dump(exclude_unset=True)
        
        # Verifica se o novo username já existe (se foi fornecido)
        if "username" in update_data and update_data["username"] != user.username:
            existing_user = db.query(User).filter(User.username == update_data["username"]).first()
            if existing_user:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Username already taken"
                )
        
        for field, value in update_data.items():
            setattr(user, field, value)
        
        db.commit()
        db.refresh(user)
        
        return user

    @staticmethod
    def delete_user(db: Session, user_id: int) -> None:
        """Deleta um usuário"""
        user = AuthService.get_user_by_id(db, user_id)
        db.delete(user)
        db.commit()
