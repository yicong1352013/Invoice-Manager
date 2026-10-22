from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field

from app.models.user import UserRole


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=255)


class UserInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    role: UserRole
    enabled: bool
    created_at: datetime
    updated_at: datetime


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserInfo


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=2, max_length=100, pattern=r"^[A-Za-z0-9_\-\u4e00-\u9fa5]+$")
    password: str = Field(min_length=6, max_length=255)
    role: UserRole = UserRole.USER


class UserUpdateRequest(BaseModel):
    role: Optional[UserRole] = None
    enabled: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=255)
