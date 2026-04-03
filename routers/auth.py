from fastapi import APIRouter, Depends, status
from sqlalchemy.orm import Session

from database import get_db
from schemas.user import UserCreate, UserLogin, UserResponse, LoginResponse, UserUpdate
from services.auth_service import AuthService

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register(user_data: UserCreate, db: Session = Depends(get_db)):
    """Registra um novo usuário"""
    user = AuthService.create_user(db, user_data)
    return user


@router.post("/login", response_model=LoginResponse)
def login(login_data: UserLogin, db: Session = Depends(get_db)):
    """Faz login de um usuário"""
    user = AuthService.authenticate_user(db, login_data)
    return LoginResponse(user=UserResponse.model_validate(user))


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(user_id: int, db: Session = Depends(get_db)):
    """Busca um usuário por ID"""
    user = AuthService.get_user_by_id(db, user_id)
    return user


@router.put("/users/{user_id}", response_model=UserResponse)
def update_user(user_id: int, user_data: UserUpdate, db: Session = Depends(get_db)):
    """Atualiza um usuário"""
    user = AuthService.update_user(db, user_id, user_data)
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, db: Session = Depends(get_db)):
    """Deleta um usuário"""
    AuthService.delete_user(db, user_id)
    return None
