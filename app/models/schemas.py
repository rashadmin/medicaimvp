"""Pydantic schemas for request/response validation"""
from pydantic import BaseModel
from typing import Optional


class HealthResponse(BaseModel):
    """Health check response schema"""
    status: str


class ErrorResponse(BaseModel):
    """Error response schema"""
    error: str
    detail: Optional[str] = None
