from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class RegisterRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    phone: Optional[str] = Field(default=None, max_length=20)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(default="CITIZEN")


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    user: "UserOut"


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    name: str
    email: EmailStr
    phone: Optional[str] = None
    role: str
    created_at: datetime


class ComplaintCreate(BaseModel):
    plate: str = Field(min_length=6, max_length=20)
    complaint_type: str = Field(min_length=3, max_length=60)
    description: Optional[str] = Field(default=None, max_length=2000)


class ComplaintOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    plate: str
    complaint_type: str
    description: Optional[str] = None
    status: str
    created_at: datetime
    updated_at: datetime


class HotlistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    plate: str
    complaint_id: Optional[UUID] = None
    status: str
    added_at: datetime
    expiry_at: Optional[datetime] = None
    fir_reference: Optional[str] = None
    fir_verified_at: Optional[datetime] = None
    recovered_at: Optional[datetime] = None
    last_seen_at: Optional[datetime] = None
    last_seen_lat: Optional[float] = None
    last_seen_lng: Optional[float] = None


class HotlistCreate(BaseModel):
    plate: str = Field(min_length=6, max_length=20)
    complaint_id: Optional[UUID] = None
    fir_reference: Optional[str] = None


class HotlistPatch(BaseModel):
    status: Optional[str] = None
    fir_reference: Optional[str] = None


class SightingCreate(BaseModel):
    plate: str = Field(min_length=6, max_length=20)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timestamp: datetime
    confidence: Optional[float] = Field(default=None, ge=0, le=1)
    device_id: UUID


class SightingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    hotlist_id: UUID
    device_id: UUID
    # The plate is included deliberately. A sighting row only ever exists for a
    # plate that is already on the police hot-list (sighting_service returns None
    # otherwise), so this discloses nothing the officer cannot already see - and
    # without it the sightings list is a column of hot-list ids that cannot be
    # matched to anything on screen.
    plate: Optional[str] = None
    latitude: float
    longitude: float
    detected_at: datetime
    confidence: Optional[float] = None
    # Which camera saw it, from the device. Same reason.
    camera: Optional[str] = None


class DeviceRegister(BaseModel):
    device_type: str = Field(default="mobile", max_length=40)
    device_name: Optional[str] = Field(default=None, max_length=120)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    user_id: UUID
    device_type: str
    device_name: Optional[str] = None
    revoked: bool
    last_seen_at: Optional[datetime] = None
    created_at: datetime


class ComplaintAction(BaseModel):
    fir_reference: Optional[str] = None


class HealthOut(BaseModel):
    status: str
    database: str
    redis: str
    demo_mode: bool
