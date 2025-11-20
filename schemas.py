"""
Database Schemas for Justifi (MVP)

Each Pydantic model represents a MongoDB collection. The collection name is the lowercase of the class name.

This MVP includes the core entities to submit justifications, route to approvers, take actions, and keep an audit log.
"""

from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from datetime import datetime


class User(BaseModel):
    email: str = Field(..., description="Unique user email")
    name: str = Field(..., description="Display name")
    department: Optional[str] = Field(None, description="Department code or name")
    role: str = Field("user", description="Role: user|approver|admin")


class JustificationType(BaseModel):
    code: str = Field(..., description="Unique code, e.g., PROJECT, ROLE_CHANGE")
    name: str = Field(..., description="Display name")
    dynamic_fields: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="List of field metadata: {key,label,type,required,options?}",
    )


class Justification(BaseModel):
    title: str
    type_code: str
    department: str
    cost_centre: str
    requester_email: str
    urgency: str

    description: str
    business_impact: Optional[str] = None
    alternatives: Optional[str] = None
    cost_estimate: Optional[float] = None
    required_date: Optional[str] = None

    dynamic_values: Dict[str, Any] = Field(default_factory=dict)
    attachments: List[Dict[str, Any]] = Field(default_factory=list)

    status: str = Field(
        "PendingApproval",
        description="PendingApproval|NeedsMoreInfo|Approved|Rejected|Cancelled",
    )


class RoutingRuleCondition(BaseModel):
    field: str
    op: str
    value: Any


class RoutingRule(BaseModel):
    name: str
    # Simple MVP routing: match on department/type_code/spend threshold
    department: Optional[str] = None
    type_code: Optional[str] = None
    spend_threshold: Optional[float] = None
    approver_emails: List[str] = Field(
        default_factory=list, description="Sequential approvers in order"
    )


class ApprovalTask(BaseModel):
    justification_id: str
    approver_email: str
    step_index: int = 0
    status: str = Field(
        "Pending",
        description="Pending|Approved|Rejected|NeedsMoreInfo|Cancelled",
    )
    requested_more_info: Optional[str] = None


class Comment(BaseModel):
    justification_id: str
    author_email: str
    message: str
    is_internal: bool = False


class AuditLog(BaseModel):
    entity: str
    entity_id: str
    action: str
    actor_email: str
    details: Dict[str, Any] = Field(default_factory=dict)
    timestamp: Optional[datetime] = None


class EmailTemplate(BaseModel):
    key: str
    subject: str
    html: str
