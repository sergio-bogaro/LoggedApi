from sqlalchemy.orm import Session
from fastapi import HTTPException, status

from models.user import User
from models.media import Media
from models.media_list_item import MediaListItem
from models.import_job import ImportJob
from models.library_sync import LibraryConnection, LibraryStatusMapping, LibrarySyncLink
from models.suwayomi import SuwayomiAccount, SuwayomiCategoryMapping, SuwayomiMangaLink
from schemas.user import UserCreate, UserLogin, UserUpdate
from services.image_storage import ImageStorageService
from services.import_jobs.storage import delete_import_path


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
        update_data = user_data.model_dump(exclude_unset=True, exclude_none=True)
        
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
        """Deleta um usuário e os arquivos que pertencem a ele."""
        user = AuthService.get_user_by_id(db, user_id)

        image_paths = [
            row.image_path
            for row in db.query(Media.image_path)
            .filter(Media.user_id == user_id, Media.image_path.is_not(None))
            .all()
            if row.image_path
        ]
        import_files = [
            row.file_path
            for row in db.query(ImportJob.file_path)
            .filter(ImportJob.user_id == user_id, ImportJob.file_path.is_not(None))
            .all()
            if row.file_path
        ]

        db.query(MediaListItem).filter(MediaListItem.user_id == user_id).delete()
        db.query(SuwayomiMangaLink).filter(SuwayomiMangaLink.user_id == user_id).delete()
        db.query(SuwayomiCategoryMapping).filter(
            SuwayomiCategoryMapping.user_id == user_id
        ).delete()
        db.query(SuwayomiAccount).filter(SuwayomiAccount.user_id == user_id).delete()
        db.query(ImportJob).filter(ImportJob.user_id == user_id).delete()
        db.query(LibrarySyncLink).filter(LibrarySyncLink.user_id == user_id).delete()
        db.query(LibraryStatusMapping).filter(
            LibraryStatusMapping.user_id == user_id
        ).delete()
        db.query(LibraryConnection).filter(
            LibraryConnection.user_id == user_id
        ).delete()
        db.delete(user)
        db.commit()

        # Arquivos só são removidos depois do commit: se o banco falhar, eles
        # continuam disponíveis para uma nova tentativa.
        image_service = ImageStorageService()
        for path in image_paths:
            try:
                image_service.delete(path)
            except Exception:  # noqa: BLE001 - caminho inválido não bloqueia a exclusão
                pass
        for path in import_files:
            delete_import_path(path)
